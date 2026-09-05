from __future__ import annotations
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from rich.console import Console

load_dotenv()
console = Console()

CREDS_PATH = Path(__file__).parent.parent / "credentials.json"

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


class SheetsClient:
    def __init__(self):
        self._sheet = None
        self._setup()

    def _setup(self):
        """Authenticate and open the Google Sheet."""
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

        except Exception as e:
            console.print(f"[red]Sheets setup error: {e}[/red]")
            self._sheet = None

    @property
    def available(self) -> bool:
        return self._sheet is not None

    def _safe_update_cell(self, row: int, col: int, value: any) -> bool:
        """
        Safely update a single cell with network recovery and retry mechanism.
        If all retries fail, registers the update to be flushed at the end of the run.
        """
        import time
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

    def register_failed_update(self, info: dict) -> None:
        """Record a failed cell update to data/failed_sheet_updates.json for end-of-run reporting."""
        import json
        data_dir = Path(__file__).parent.parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        file_path = data_dir / "failed_sheet_updates.json"
        
        failed_list = []
        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    failed_list = json.load(f)
            except Exception:
                pass
        
        # Avoid duplicate failures for the same cell
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
        import json
        file_path = Path(__file__).parent.parent / "data" / "failed_sheet_updates.json"
        if not file_path.exists():
            return []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                failed_list = json.load(f)
            file_path.unlink(missing_ok=True)
            return failed_list
        except Exception:
            return []

    def log_leads(self, leads: list[dict]) -> int:
        """Append sent leads to the sheet. Returns number logged."""
        if not self.available:
            return 0

        rows = []
        for lead in leads:
            rows.append([
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                lead.get("name", ""),
                lead.get("company", ""),
                lead.get("role", ""),
                lead.get("linkedin_url", ""),
                lead.get("connection_note", ""),
                lead.get("drafted_dm", ""),
                str(round(lead.get("score", 0), 2)),
                "Blank Sent",          # Status after sending blank request
                "",                    # Your Feedback — blank for user to fill
                "No",                  # Feedback Applied
            ])

        try:
            self._sheet.append_rows(rows, value_input_option="USER_ENTERED")
            console.print(f"[green]✓ Logged {len(rows)} leads to Google Sheet[/green]")
            return len(rows)
        except Exception as e:
            console.print(f"[red]Sheet log error: {e}[/red]")
            return 0

    def update_status(self, profile_url: str, status: str) -> bool:
        """Update the status column for a specific lead by profile URL."""
        if not self.available or not profile_url:
            return False
        try:
            cell = self._sheet.find(profile_url)
            if cell:
                return self._safe_update_cell(cell.row, COL_STATUS + 1, status)
        except Exception as e:
            console.print(f"[red]Status update error: {e}[/red]")
        return False

    def update_status_by_name(self, name: str, status: str, url: str = "") -> bool:
        """
        Update the status for a lead found by URL (exact, fastest) or fuzzy name match.
        Used by InboxAgent after sending DMs.
        Returns True if a matching row was found and updated.
        """
        if not self.available:
            return False

        # Fast path: update by exact URL if provided
        if url:
            try:
                cell = self._sheet.find(url)
                if cell:
                    return self._safe_update_cell(cell.row, COL_STATUS + 1, status)
            except Exception:
                pass

        # Fallback path: search records by name
        try:
            all_rows = self._sheet.get_all_records()
            clean_name = name.strip().lower() if name else ""
            clean_url = url.strip().lower() if url else ""
            for i, row in enumerate(all_rows, start=2):  # +2 for header + 1-indexed
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
        except Exception as e:
            console.print(f"[red]Name-based status update error: {e}[/red]")
        return False

    def get_blank_sent_leads(self) -> list[dict]:
        """
        Return all leads where Status == 'Blank Sent'.
        Used by InboxAgent to build the execution queue.
        """
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
        """
        Return rows where 'Your Feedback' is filled but 'Feedback Applied' = 'No'.
        These are the rows the feedback agent will learn from.
        """
        if not self.available:
            return []

        try:
            all_rows = self._sheet.get_all_records()
            pending = []
            for i, row in enumerate(all_rows, start=2):  # +2 for header + 1-indexed
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
                self._sheet.update_cell(row_idx, COL_FEEDBACK_APPLIED + 1, "Yes")
        except Exception as e:
            console.print(f"[red]Mark feedback error: {e}[/red]")

    def get_all_profile_urls(self) -> set:
        """Return all profile URLs already in the sheet (for dedup)."""
        if not self.available:
            return set()
        try:
            col = self._sheet.col_values(COL_URL + 1)
            return set(url.strip() for url in col[1:] if url.strip())  # skip header
        except Exception:
            return set()
