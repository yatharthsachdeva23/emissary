from __future__ import annotations
import collections
import json
import os
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from rich.console import Console

# Set default global socket timeout so no network operation hangs indefinitely
socket.setdefaulttimeout(15.0)

load_dotenv()
console = Console()

DATA_DIR = Path(__file__).parent.parent / "data"
CREDS_PATH = Path(__file__).parent.parent / "credentials.json"
QUEUE_FILE = DATA_DIR / "sheet_queue.json"

# Sheet column indices (0-based)
COL_DATE = 0
COL_NAME = 1
COL_COMPANY = 2
COL_ROLE = 3
COL_URL = 4
COL_NOTE = 5
COL_DM = 6
COL_SCORE = 7
COL_STATUS = 8
COL_FEEDBACK = 9
COL_FEEDBACK_APPLIED = 10

HEADERS = [
    "Date", "Name", "Company", "Role", "Profile URL",
    "Connection Note", "Drafted_DM", "Score", "Status",
    "Your Feedback", "Feedback Applied"
]

# ── Global FIFO Queue & Worker State ──────────────────────────────────────────
_queue = collections.deque()
_queue_lock = threading.Lock()
_worker_thread: Optional[threading.Thread] = None
_worker_running: bool = False
_url_cache: dict[str, int] = {}
_url_cache_lock = threading.Lock()
_queue_loaded: bool = False


def _load_persisted_queue() -> None:
    global _queue, _queue_loaded
    if _queue_loaded:
        return
    _queue_loaded = True
    if QUEUE_FILE.exists():
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                items = json.load(f)
            with _queue_lock:
                _queue = collections.deque(items)
            if items:
                console.print(f"[cyan]ℹ Loaded {len(items)} pending Google Sheet update(s) from queue file.[/cyan]")
        except Exception as e:
            console.print(f"[dim]Queue load warning: {e}[/dim]")


def _save_persisted_queue() -> None:
    with _queue_lock:
        items = list(_queue)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
    except Exception as e:
        console.print(f"[dim]Queue save warning: {e}[/dim]")


def _enqueue_task(task_dict: dict) -> None:
    _load_persisted_queue()
    with _queue_lock:
        _queue.append(task_dict)
    _save_persisted_queue()
    _start_worker()


def _start_worker() -> None:
    global _worker_thread, _worker_running
    if _worker_running and _worker_thread and _worker_thread.is_alive():
        return
    _worker_running = True
    _worker_thread = threading.Thread(target=_worker_loop, daemon=True, name="SheetQueueWorker")
    _worker_thread.start()


def _worker_loop() -> None:
    global _worker_running
    client: Optional[SheetsClient] = None
    while _worker_running:
        task = None
        with _queue_lock:
            if _queue:
                task = _queue[0]  # Peek FIFO front item
        if not task:
            time.sleep(1.0)
            continue

        # Initialize sheets client lazily inside the worker thread
        if client is None or not client.available:
            try:
                client = SheetsClient(start_worker=False, lazy_setup=False)
            except Exception:
                time.sleep(3.0)
                continue

        if not client.available:
            time.sleep(3.0)
            continue

        try:
            success = client._process_queued_task(task)
            if success:
                with _queue_lock:
                    if _queue and _queue[0] == task:
                        _queue.popleft()
                _save_persisted_queue()
                time.sleep(1.0)  # Gentle spacing between operations (respects 60 req/min limit)
            else:
                attempts = task.get("attempts", 0) + 1
                task["attempts"] = attempts
                if attempts >= 3:
                    with _queue_lock:
                        if _queue and _queue[0] == task:
                            _queue.popleft()
                    console.print(f"[dim]ℹ Sheet update for {task.get('url', task.get('name', 'lead'))} resolved/dropped after {attempts} attempts.[/dim]")
                _save_persisted_queue()
                time.sleep(3.0)  # Wait before retry
        except Exception as e:
            attempts = task.get("attempts", 0) + 1
            task["attempts"] = attempts
            if attempts >= 3:
                with _queue_lock:
                    if _queue and _queue[0] == task:
                        _queue.popleft()
            _save_persisted_queue()
            time.sleep(3.0)


# Initialize persistent queue loading at module import
_load_persisted_queue()


class SheetsClient:
    def __init__(self, start_worker: bool = True, lazy_setup: bool = True):
        self._sheet = None
        self._gc = None
        self._spreadsheet = None
        self._setup_attempted = False
        if not lazy_setup:
            self._setup()
        if start_worker:
            _start_worker()

    def _setup(self):
        """Authenticate and open the Google Sheet."""
        self._setup_attempted = True
        sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
        if not sheet_id or sheet_id.startswith("your_"):
            console.print("[yellow]GOOGLE_SHEET_ID not set — Sheet logging disabled[/yellow]")
            return

        if not CREDS_PATH.exists():
            console.print(
                "[yellow]credentials.json not found — Sheet logging disabled.\n"
                "See README.md → 'Google Sheets Setup' for instructions.[/yellow]"
            )
            return

        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            try:
                import gspread
                from google.oauth2.service_account import Credentials

                scopes = [
                    "https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive",
                ]
                creds = Credentials.from_service_account_file(str(CREDS_PATH), scopes=scopes)
                gc = gspread.authorize(creds)
                self._gc = gc
                spreadsheet = gc.open_by_key(sheet_id)
                self._spreadsheet = spreadsheet

                # Use first sheet or create "Emissary CRM" tab
                try:
                    self._sheet = spreadsheet.worksheet("Emissary CRM")
                except gspread.WorksheetNotFound:
                    self._sheet = spreadsheet.add_worksheet("Emissary CRM", rows=1000, cols=12)
                    self._sheet.append_row(HEADERS)
                    console.print("[green]✓ Created 'Emissary CRM' sheet with headers[/green]")
                break
            except Exception as e:
                if attempt < max_attempts:
                    console.print(f"[yellow]⚠ Sheets setup attempt {attempt} failed ({e}). Retrying in 1s...[/yellow]")
                    time.sleep(1.0)
                else:
                    console.print(f"[red]Sheets setup error: {e}[/red]")
                    self._sheet = None

    def _ensure_setup(self) -> bool:
        if self._sheet is None and not self._setup_attempted:
            self._setup()
        return self._sheet is not None

    @property
    def available(self) -> bool:
        return self._ensure_setup()

    def _find_row_by_url(self, profile_url: str) -> Optional[int]:
        """Find row index for a profile URL using fast Column E search + memory cache."""
        if not profile_url or not self.available:
            return None
        norm_url = profile_url.strip().lower()
        with _url_cache_lock:
            if norm_url in _url_cache:
                return _url_cache[norm_url]

        # Populate cache from column E (Profile URL)
        try:
            urls = self._sheet.col_values(COL_URL + 1)
            with _url_cache_lock:
                for idx, u in enumerate(urls, start=1):
                    u_norm = u.strip().lower()
                    if u_norm:
                        _url_cache[u_norm] = idx
                if norm_url not in _url_cache:
                    _url_cache[norm_url] = None
                return _url_cache.get(norm_url)
        except Exception as e:
            console.print(f"[dim]Column URL search note: {e}[/dim]")
            return None

    def _safe_update_row_range(self, row_idx: int, values: list) -> bool:
        """
        Safely update columns B through I in a SINGLE API call instead of 5 individual cell calls.
        values: [name, company, role, profile_url, note, drafted_dm, score, status]
        """
        from utils.network import is_network_error, wait_for_network_recovery
        range_name = f"B{row_idx}:I{row_idx}"
        for attempt in range(1, 4):
            try:
                self._sheet.update(range_name=range_name, values=[values], value_input_option="USER_ENTERED")
                return True
            except Exception as e:
                if is_network_error(e) and attempt < 3:
                    console.print(f"[yellow]⚠ Network lost during Sheet row update. Waiting for Wi-Fi recovery...[/yellow]")
                    if wait_for_network_recovery(max_wait_seconds=600, check_interval_seconds=30):
                        continue
                if attempt < 3:
                    time.sleep(1.5)
        return False

    def _safe_update_cell(self, row: int, col: int, value: any) -> bool:
        """
        Safely update a single cell with network recovery and retry mechanism.
        If all retries fail, registers the update to be flushed at the end of the run.
        """
        from utils.network import is_network_error, wait_for_network_recovery
        for attempt in range(1, 4):
            try:
                self._sheet.update_cell(row, col, value)
                return True
            except Exception as e:
                if is_network_error(e) and attempt < 3:
                    console.print(f"[yellow]⚠ Network lost during Sheet update. Waiting for Wi-Fi recovery (Checking every 30s, max 10 mins)...[/yellow]")
                    if wait_for_network_recovery(max_wait_seconds=600, check_interval_seconds=30):
                        try:
                            sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
                            self._sheet = self._gc.open_by_key(sheet_id).worksheet(self._sheet.title)
                        except Exception:
                            pass
                        continue
                console.print(f"[red]  ⚠ Sheet update attempt {attempt}/3 failed for cell ({row}, {col}): {e}[/red]")
                if attempt < 3:
                    time.sleep(1.5)
        # Register failure for end-of-run reporting
        failed_update_info = {
            "row": row,
            "col": col,
            "value": value,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        self.register_failed_update(failed_update_info)
        return False

    def _safe_append_row(self, row: list) -> bool:
        """
        Safely append a single row to the sheet with network recovery and retry mechanism.
        """
        from utils.network import is_network_error, wait_for_network_recovery
        for attempt in range(1, 4):
            try:
                self._sheet.append_row(row, value_input_option="USER_ENTERED")
                return True
            except Exception as e:
                if is_network_error(e) and attempt < 3:
                    console.print(f"[yellow]⚠ Network lost during Sheet append. Waiting for Wi-Fi recovery (Checking every 30s, max 10 mins)...[/yellow]")
                    if wait_for_network_recovery(max_wait_seconds=600, check_interval_seconds=30):
                        try:
                            sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
                            self._sheet = self._gc.open_by_key(sheet_id).worksheet(self._sheet.title)
                        except Exception:
                            pass
                        continue
                console.print(f"[red]  ⚠ Sheet append attempt {attempt}/3 failed: {e}[/red]")
                if attempt < 3:
                    time.sleep(1.5)
        return False

    def register_failed_update(self, info: dict) -> None:
        """Record a failed cell update to data/failed_sheet_updates.json for end-of-run reporting."""
        file_path = DATA_DIR / "failed_sheet_updates.json"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        failed_list = []
        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    failed_list = json.load(f)
            except Exception:
                pass
        duplicate = False
        for item in failed_list:
            if item.get("row") == info["row"] and item.get("col") == info["col"]:
                item["value"] = info["value"]
                item["timestamp"] = info["timestamp"]
                duplicate = True
                break
        if not duplicate:
            failed_list.append(info)
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(failed_list, f, indent=4)
        except Exception as e:
            console.print(f"[red]Error writing failed updates registry: {e}[/red]")

    @staticmethod
    def get_and_clear_failed_updates() -> list[dict]:
        """Retrieve all registered failed updates and clear the registry file."""
        file_path = DATA_DIR / "failed_sheet_updates.json"
        if not file_path.exists():
            return []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                failed_list = json.load(f)
            file_path.unlink(missing_ok=True)
            return failed_list
        except Exception:
            return []

    # ── Non-blocking Queued API (0ms latency for LinkedIn) ────────────────────

    def log_or_update_lead(self, lead: dict, async_mode: bool = True) -> bool:
        """
        Immediately enqueues lead to background FIFO queue (0ms latency for LinkedIn),
        or performs synchronous update if async_mode is False.
        """
        if not lead:
            return False
        lead["sheet_logged"] = True  # Mark locally logged immediately

        if async_mode:
            _enqueue_task({
                "action": "log_or_update",
                "lead": lead,
                "timestamp": datetime.now().isoformat(),
                "attempts": 0
            })
            return True
        return self._sync_log_or_update_lead(lead)

    def update_status(self, profile_url: str, status: str, async_mode: bool = True) -> bool:
        """Update status column for lead. Non-blocking when async_mode=True."""
        if not profile_url:
            return False
        if async_mode:
            _enqueue_task({
                "action": "update_status",
                "url": profile_url,
                "status": status,
                "timestamp": datetime.now().isoformat(),
                "attempts": 0
            })
            return True
        return self._sync_update_status(profile_url, status)

    def update_status_by_name(self, name: str, status: str, url: str = "", async_mode: bool = True) -> bool:
        """Update status by name/url. Non-blocking when async_mode=True."""
        if async_mode:
            _enqueue_task({
                "action": "update_status_by_name",
                "name": name,
                "status": status,
                "url": url,
                "timestamp": datetime.now().isoformat(),
                "attempts": 0
            })
            return True
        return self._sync_update_status_by_name(name, status, url)

    def update_lead_company_and_dm(
        self,
        profile_url: str,
        new_company: str,
        new_role: str = "",
        new_dm: str = "",
        new_note: str = "",
        async_mode: bool = True
    ) -> bool:
        """Update company/role/DM. Non-blocking when async_mode=True."""
        if not profile_url:
            return False
        if async_mode:
            _enqueue_task({
                "action": "update_company_dm",
                "url": profile_url,
                "company": new_company,
                "role": new_role,
                "dm": new_dm,
                "note": new_note,
                "timestamp": datetime.now().isoformat(),
                "attempts": 0
            })
            return True
        return self._sync_update_lead_company_and_dm(profile_url, new_company, new_role, new_dm, new_note)

    # ── Background Worker Processing Method ───────────────────────────────────

    def _process_queued_task(self, task: dict) -> bool:
        action = task.get("action")
        if action == "log_or_update":
            return self._sync_log_or_update_lead(task.get("lead", {}))
        elif action == "update_status":
            return self._sync_update_status(task.get("url", ""), task.get("status", ""))
        elif action == "update_status_by_name":
            return self._sync_update_status_by_name(task.get("name", ""), task.get("status", ""), task.get("url", ""))
        elif action == "update_company_dm":
            return self._sync_update_lead_company_and_dm(
                task.get("url", ""),
                task.get("company", ""),
                task.get("role", ""),
                task.get("dm", ""),
                task.get("note", "")
            )
        return True

    # ── Synchronous Underlying Execution Implementations ──────────────────────

    def _sync_log_or_update_lead(self, lead: dict) -> bool:
        if not self.available or not lead:
            return False

        profile_url = lead.get("linkedin_url", "").strip()
        name = lead.get("name", "")
        company = lead.get("company", "")
        role = lead.get("role", "")
        drafted_dm = lead.get("drafted_dm", "")
        note = lead.get("connection_note", "")
        score_val = lead.get("score", 0)
        try:
            score = str(round(float(score_val), 2))
        except Exception:
            score = str(score_val)
        status = lead.get("status", "Blank Sent")

        try:
            row_idx = self._find_row_by_url(profile_url)
            if row_idx:
                row_values = [name, company, role, profile_url, note, drafted_dm, score, status]
                ok = self._safe_update_row_range(row_idx, row_values)
                if ok:
                    console.print(f"  [green]✓ Real-time Google Sheet updated for row {row_idx} ({name})[/green]")
                    return True
                return False
            else:
                row = [
                    datetime.now().strftime("%Y-%m-%d %H:%M"),
                    name,
                    company,
                    role,
                    profile_url,
                    note,
                    drafted_dm,
                    score,
                    status,
                    "",
                    "No",
                ]
                ok = self._safe_append_row(row)
                if ok:
                    console.print(f"  [green]✓ Real-time Google Sheet logged new row for {name} @ {company}[/green]")
                    with _url_cache_lock:
                        if profile_url:
                            _url_cache[profile_url.lower()] = len(_url_cache) + 2
                    return True
                return False
        except Exception as e:
            console.print(f"  [dim]Real-time sheet log/update note for {name}: {e}[/dim]")
            return False

    def _sync_update_status(self, profile_url: str, status: str) -> bool:
        if not self.available or not profile_url:
            return False
        try:
            row_idx = self._find_row_by_url(profile_url)
            if row_idx:
                return self._safe_update_cell(row_idx, COL_STATUS + 1, status)
            # Lead not found in sheet: nothing to update, mark resolved so queue is not blocked
            return True
        except Exception as e:
            console.print(f"[dim]Status update note: {e}[/dim]")
        return False

    def _sync_update_lead_company_and_dm(
        self,
        profile_url: str,
        new_company: str,
        new_role: str = "",
        new_dm: str = "",
        new_note: str = ""
    ) -> bool:
        if not self.available or not profile_url:
            return False
        try:
            row_idx = self._find_row_by_url(profile_url)
            if row_idx:
                if new_company:
                    self._safe_update_cell(row_idx, COL_COMPANY + 1, new_company)
                if new_role:
                    self._safe_update_cell(row_idx, COL_ROLE + 1, new_role)
                if new_dm:
                    self._safe_update_cell(row_idx, COL_DM + 1, new_dm)
                if new_note:
                    self._safe_update_cell(row_idx, COL_NOTE + 1, new_note)
                return True
            # Lead not in sheet: mark resolved
            return True
        except Exception as e:
            console.print(f"[dim]Error updating company/role in sheet: {e}[/dim]")
        return False

    def _sync_update_status_by_name(self, name: str, status: str, url: str = "") -> bool:
        if not self.available:
            return False

        if url:
            row_idx = self._find_row_by_url(url)
            if row_idx:
                return self._safe_update_cell(row_idx, COL_STATUS + 1, status)

        try:
            all_rows = self._sheet.get_all_records()
            clean_name = name.strip().lower() if name else ""
            clean_url = url.strip().lower() if url else ""
            for i, row in enumerate(all_rows, start=2):
                sheet_name = str(row.get("Name", "")).strip().lower()
                sheet_url = str(row.get("Profile URL", "")).strip().lower()
                is_match = False
                if clean_url and sheet_url and clean_url in sheet_url:
                    is_match = True
                elif sheet_name and clean_name and (sheet_name in clean_name or clean_name in sheet_name):
                    is_match = True

                if is_match:
                    row_status = str(row.get("Status", "")).strip()
                    if row_status in ("Blank Sent", "Request Sent", ""):
                        return self._safe_update_cell(i, COL_STATUS + 1, status)
            # Not found in sheet: mark resolved
            return True
        except Exception as e:
            console.print(f"[dim]Name-based status update note: {e}[/dim]")
        return False

    def log_leads(self, leads: list[dict]) -> int:
        """Append sent leads to the sheet. Returns number logged."""
        if not self.available:
            return 0
        leads_to_log = [l for l in leads if not l.get("sheet_logged")]
        if not leads_to_log:
            return 0

        rows = []
        for lead in leads_to_log:
            score_val = lead.get("score", 0)
            try:
                score = str(round(float(score_val), 2))
            except Exception:
                score = str(score_val)
            rows.append([
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                lead.get("name", ""),
                lead.get("company", ""),
                lead.get("role", ""),
                lead.get("linkedin_url", ""),
                lead.get("connection_note", ""),
                lead.get("drafted_dm", ""),
                score,
                lead.get("status", "Blank Sent"),
                "",
                "No",
            ])

        try:
            self._sheet.append_rows(rows, value_input_option="USER_ENTERED")
            for lead in leads_to_log:
                lead["sheet_logged"] = True
            return len(rows)
        except Exception as e:
            console.print(f"[red]Sheet log error: {e}[/red]")
            return 0

    def get_blank_sent_leads(self) -> list[dict]:
        """Return all leads where Status == 'Blank Sent'."""
        if not self.available:
            return []
        try:
            all_rows = self._sheet.get_all_records()
            results = []
            for row in all_rows:
                if str(row.get("Status", "")).strip() == "Blank Sent":
                    results.append({
                        "name": row.get("Name", ""),
                        "linkedin_url": row.get("Profile URL", ""),
                        "drafted_dm": row.get("Drafted_DM", ""),
                        "company": row.get("Company", ""),
                    })
            return results
        except Exception as e:
            console.print(f"[red]get_blank_sent_leads error: {e}[/red]")
            return []

    def get_pending_feedback(self) -> list[dict]:
        """Return rows where 'Your Feedback' is filled but 'Feedback Applied' = 'No'."""
        if not self.available:
            return []
        try:
            # Fast check: check column J (Your Feedback) directly. Takes 0.3s instead of minutes!
            col_feedback = self._sheet.col_values(COL_FEEDBACK + 1)
            has_any_feedback = any(f.strip() for f in col_feedback[1:]) if len(col_feedback) > 1 else False
            if not has_any_feedback:
                return []

            all_rows = self._sheet.get_all_records()
            pending = []
            for i, row in enumerate(all_rows, start=2):
                feedback = str(row.get("Your Feedback", "")).strip()
                applied = str(row.get("Feedback Applied", "No")).strip().lower()
                if feedback and applied == "no":
                    pending.append({
                        "row_index": i,
                        "name": row.get("Name", ""),
                        "company": row.get("Company", ""),
                        "role": row.get("Role", ""),
                        "note": row.get("Connection Note", ""),
                        "feedback": feedback,
                        "score": row.get("Score", ""),
                        "status": row.get("Status", ""),
                    })
            return pending
        except Exception as e:
            console.print(f"[red]Feedback read error: {e}[/red]")
            return []

    def mark_feedback_applied(self, row_indices: list[int]) -> None:
        """Mark feedback rows as applied."""
        if not self.available:
            return
        try:
            for row_idx in row_indices:
                self._safe_update_cell(row_idx, COL_FEEDBACK_APPLIED + 1, "Yes")
        except Exception as e:
            console.print(f"[red]Mark feedback error: {e}[/red]")

    def get_all_profile_urls(self) -> set:
        """Return all profile URLs already in the sheet (for dedup)."""
        if not self.available:
            return set()
        try:
            col = self._sheet.col_values(COL_URL + 1)
            return set(url.strip() for url in col[1:] if url.strip())
        except Exception:
            return set()

    @classmethod
    def flush_queue(cls, timeout: float = 12.0) -> None:
        """Wait for background queue to drain up to timeout seconds."""
        start = time.time()
        while time.time() - start < timeout:
            with _queue_lock:
                if not _queue:
                    return
            time.sleep(0.5)
