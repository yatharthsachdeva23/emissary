"""
Google Sheets CRM — Emissary
Read/write leads, statuses, and feedback to the central Google Sheet.
"""

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
            spreadsheet = gc.open_by_key(sheet_id)

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

    def update_status(self, profile_url: str, status: str) -> None:
        """Update the status column for a specific lead by profile URL."""
        if not self.available:
            return
        try:
            cell = self._sheet.find(profile_url)
            if cell:
                self._sheet.update_cell(cell.row, COL_STATUS + 1, status)
        except Exception as e:
            console.print(f"[red]Status update error: {e}[/red]")

    def update_status_by_name(self, name: str, status: str) -> bool:
        """
        Update the status for a lead found by fuzzy name match.
        Used by InboxAgent after sending DMs (name is more reliable than URL at that point).
        Returns True if a matching row was found and updated.
        """
        if not self.available:
            return False
        try:
            all_rows = self._sheet.get_all_records()
            for i, row in enumerate(all_rows, start=2):  # +2 for header + 1-indexed
                sheet_name = str(row.get("Name", "")).strip().lower()
                search_name = name.strip().lower()
                if sheet_name and (sheet_name in search_name or search_name in sheet_name):
                    if str(row.get("Status", "")).strip() == "Blank Sent":
                        self._sheet.update_cell(i, COL_STATUS + 1, status)
                        return True
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
