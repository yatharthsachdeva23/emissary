from __future__ import annotations
import re
import sys
import json
from pathlib import Path
from datetime import datetime
from utils.sheets import SheetsClient
from rich.console import Console
from rich.table import Table

console = Console()

def normalize_for_match(name: str) -> str:
    """Normalize names to compare them robustly across logs and sheets."""
    if not name:
        return ""
    name = name.lower()
    # Remove common professional suffixes/prefixes
    name = re.sub(r"\b(ph\.?d\.?|mba|m\.?tech|b\.?tech|ms|dr\.?|cpa)\b", "", name)
    # Remove all non-alphanumeric characters
    name = re.sub(r"[^a-z0-9]", "", name)
    return name.strip()

def get_runs_from_log() -> list[dict]:
    """Parse emissary.log and find all runs with their start and end lines."""
    log_path = Path("logs/emissary.log")
    if not log_path.exists():
        console.print(f"[red]Error: Log file not found at {log_path}[/red]")
        return []

    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    runs = []
    # Identify timestamp lines
    # e.g., "Sunday, 14 June 2026 — 10:13"
    for idx, line in enumerate(lines):
        clean = line.strip()
        if "—" in line and any(day in line for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]):
            runs.append({
                "timestamp": clean,
                "start_idx": idx,
                "lines": lines
            })

    # Set end_idx for each run
    for i in range(len(runs)):
        if i < len(runs) - 1:
            runs[i]["end_idx"] = runs[i+1]["start_idx"]
        else:
            runs[i]["end_idx"] = len(lines)

    return runs

def build_leads_database() -> dict:
    """Scan all data/history/*.json and data/leads_today.json to build a name -> lead database."""
    db = {}
    data_dir = Path("data")
    
    # 1. Search in history folder
    history_dir = data_dir / "history"
    json_files = []
    if history_dir.exists():
        json_files.extend(history_dir.glob("*.json"))
    
    # 2. Add leads_today.json
    leads_today = data_dir / "leads_today.json"
    if leads_today.exists():
        json_files.append(leads_today)

    # Sort files so newer ones overwrite older ones in case of duplicate names
    json_files.sort(key=lambda p: p.stat().st_mtime)

    for path in json_files:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                data = json.load(f)
                leads_list = data.get("leads", [])
                for lead in leads_list:
                    name = lead.get("name")
                    if name:
                        norm = normalize_for_match(name)
                        db[norm] = lead
        except Exception as e:
            console.print(f"[yellow]Warning: Could not load lead file {path.name}: {e}[/yellow]")

    return db

def log_lead_with_date(client: SheetsClient, lead: dict, status: str, date_str: str) -> bool:
    """Log a lead to Google Sheet using a specific historical date string."""
    if not client.available:
        return False

    row = [
        date_str,
        lead.get("name", ""),
        lead.get("company", ""),
        lead.get("role", "") or lead.get("position", ""),
        lead.get("linkedin_url", ""),
        lead.get("connection_note", "") or lead.get("message", ""),
        lead.get("drafted_dm", ""),
        str(round(lead.get("score", 0.0), 2)) if isinstance(lead.get("score"), (int, float)) else str(lead.get("score", "")),
        status,
        "",
        "No"
    ]
    try:
        client._sheet.append_rows([row], value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        console.print(f"[red]Error logging row: {e}[/red]")
        return False

def backfill_missing_logs(target_query: str = None, yes: bool = False):
    # 1. Load all runs
    runs = get_runs_from_log()
    if not runs:
        return

    # Filter runs based on target_query
    matched_runs = []
    if target_query:
        query_norm = target_query.lower().strip()
        matched_runs = [r for r in runs if query_norm in r["timestamp"].lower()]

    # If no unique run or no query, show choices
    selected_run = None
    if not matched_runs:
        if target_query:
            console.print(f"[yellow]No runs matched '{target_query}'.[/yellow]\n")
        
        console.print("[bold]Available runs in log file:[/bold]")
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("#", style="dim", width=4)
        table.add_column("Run Timestamp / Mode", width=50)
        table.add_column("Log Lines Range", width=20)
        
        for idx, r in enumerate(runs):
            table.add_row(str(idx + 1), r["timestamp"], f"Lines {r['start_idx']}-{r['end_idx']}")
        
        console.print(table)
        
        try:
            choice = input("\nSelect a run number to inspect: ").strip()
            if not choice:
                return
            choice_idx = int(choice) - 1
            if 0 <= choice_idx < len(runs):
                selected_run = runs[choice_idx]
            else:
                console.print("[red]Invalid selection.[/red]")
                return
        except ValueError:
            console.print("[red]Please enter a valid number.[/red]")
            return
    elif len(matched_runs) == 1:
        selected_run = matched_runs[0]
        console.print(f"[green]Matched exactly one run: [bold]{selected_run['timestamp']}[/bold][/green]")
    else:
        console.print(f"[yellow]Multiple runs matched your query '{target_query}':[/yellow]")
        for idx, r in enumerate(matched_runs):
            console.print(f" {idx + 1}) {r['timestamp']}")
        try:
            choice = input("Select a run number (1-{}): ".format(len(matched_runs))).strip()
            choice_idx = int(choice) - 1
            if 0 <= choice_idx < len(matched_runs):
                selected_run = matched_runs[choice_idx]
            else:
                console.print("[red]Invalid selection.[/red]")
                return
        except ValueError:
            console.print("[red]Invalid input.[/red]")
            return

    # 2. Extract run metadata
    # Parse human-readable date for Sheets logging, e.g., "Sunday, 14 June 2026 — 10:13" -> "2026-06-14 10:13"
    # Format of timestamp: "Sunday, 14 June 2026 — 10:13 (DRY RUN)" or similar
    timestamp_clean = selected_run["timestamp"].split("(")[0].strip()
    try:
        # e.g., "Sunday, 14 June 2026 — 10:13"
        date_part, time_part = timestamp_clean.split("—")
        date_part = date_part.strip()
        time_part = time_part.strip()
        # Parse e.g., "Sunday, 14 June 2026"
        # We can strip out the day name prefix (e.g. "Sunday, ")
        if "," in date_part:
            date_part = date_part.split(",")[1].strip()
        dt = datetime.strptime(f"{date_part} {time_part}", "%d %B %Y %H:%M")
        sheet_date_str = dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        # Fallback to current time formatted
        sheet_date_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    console.print(f"\nProcessing run: [bold cyan]{selected_run['timestamp']}[/bold cyan] (Date for Sheets: {sheet_date_str})")

    # 3. Scan the run lines for sends
    lines = selected_run["lines"][selected_run["start_idx"]:selected_run["end_idx"]]
    
    lead_start_pat = re.compile(r"^\[\d+/\d+\]\s*(.+?)\s*@\s*(.*)$")
    
    sends = [] # list of dicts: {"name": str, "company": str, "type": "connect" | "dm"}
    active_lead = None

    for line in lines:
        clean = line.strip()
        
        # Match lead header
        # e.g., "[15/50] Abdul Rahman Janoo @ Tericsoft"
        lead_match = lead_start_pat.match(clean)
        if lead_match:
            active_lead = {
                "name": lead_match.group(1).strip(),
                "company": lead_match.group(2).strip(),
            }
            continue

        # Match Blank Request Success
        if "✓ Blank request sent!" in clean and active_lead:
            sends.append({
                "name": active_lead["name"],
                "company": active_lead["company"],
                "type": "connect"
            })
            # Clear active lead to avoid double registering
            active_lead = None
            continue

        # Match DM Success
        # e.g., "✓ DM sent to Manish Bhardwaj" or "✓ DM sent to Manish Bhardwaj (https://...)"
        if "✓ DM sent to" in clean:
            name_part = clean.replace("✓ DM sent to", "").strip()
            # Extract name if URL is appended in parenthesized format
            if "(" in name_part:
                name_part = name_part.split("(")[0].strip()
            sends.append({
                "name": name_part,
                "company": "Unknown",
                "type": "dm"
            })

    if not sends:
        console.print("[yellow]No successfully sent connections or DMs found in this run's logs.[/yellow]")
        return

    console.print(f"[green]Found {len(sends)} sent actions in this run's log:[/green]")
    for s in sends:
        console.print(f"  - [{s['type'].upper()}] {s['name']} ({s['company']})")

    # 4. Load leads database and Google Sheets records
    console.print("\nBuilding historical leads database from JSON files...")
    leads_db = build_leads_database()

    console.print("Connecting to Google Sheets...")
    try:
        client = SheetsClient()
        if not client.available:
            console.print("[red]Could not connect to Google Sheets.[/red]")
            return
        sheet_records = client._sheet.get_all_records()
    except Exception as e:
        console.print(f"[red]Error connecting to Google Sheets: {e}[/red]")
        return

    # Build maps of existing sheet records by profile URL and by normalized name
    sheet_by_url = {}
    sheet_by_name = {}
    for i, row in enumerate(sheet_records, start=2): # 1-indexed, +1 for header
        url = str(row.get("Profile URL", "")).strip().lower()
        if url:
            sheet_by_url[url] = {"row_idx": i, "data": row}
        name_norm = normalize_for_match(str(row.get("Name", "")))
        if name_norm:
            sheet_by_name[name_norm] = {"row_idx": i, "data": row}

    # 5. Process each send
    backfilled_count = 0
    updated_count = 0
    
    for s in sends:
        name_norm = normalize_for_match(s["name"])
        
        # Try to find lead details in database
        lead_details = leads_db.get(name_norm)
        if not lead_details:
            # Fallback if lead_details not found locally, try to get from Google Sheets record if it exists
            sheet_record = sheet_by_name.get(name_norm)
            if sheet_record:
                sr = sheet_record["data"]
                lead_details = {
                    "name": sr.get("Name", s["name"]),
                    "company": sr.get("Company", s["company"]),
                    "role": sr.get("Role", ""),
                    "linkedin_url": sr.get("Profile URL", ""),
                    "connection_note": sr.get("Connection Note", ""),
                    "drafted_dm": sr.get("Drafted_DM", ""),
                    "score": sr.get("Score", 0.0)
                }

        # If still no lead details, construct a basic shell lead
        if not lead_details:
            lead_details = {
                "name": s["name"],
                "company": s["company"],
                "role": "Scraped Lead",
                "linkedin_url": "",
                "connection_note": "Blank connection request" if s["type"] == "connect" else "",
                "drafted_dm": "Direct message hook sent" if s["type"] == "dm" else "",
                "score": 0.0
            }

        # Check if present in Sheets
        is_in_sheet = False
        sheet_row_info = None

        url = lead_details.get("linkedin_url", "").strip().lower()
        if url and url in sheet_by_url:
            is_in_sheet = True
            sheet_row_info = sheet_by_url[url]
        elif name_norm in sheet_by_name:
            is_in_sheet = True
            sheet_row_info = sheet_by_name[name_norm]

        target_status = "Blank Sent" if s["type"] == "connect" else "DM Sent"

        if not is_in_sheet:
            console.print(f"\n[bold red][MISSING][/bold red] {lead_details['name']} ({lead_details.get('linkedin_url', 'NO URL FOUND')}) is not in Google Sheets!")
            
            # Prompt user to backfill
            confirm = "y" if yes else input(f"  Log {lead_details['name']} to Sheets now? (y/n): ").strip().lower()
            if confirm == "y":
                # If no URL, prompt to input it (skip in yes/non-interactive mode)
                if not lead_details.get("linkedin_url") and not yes:
                    url_input = input("  Enter LinkedIn URL for this person (or press enter to leave blank): ").strip()
                    if url_input:
                        lead_details["linkedin_url"] = url_input
                
                success = log_lead_with_date(client, lead_details, target_status, sheet_date_str)
                if success:
                    console.print(f"  [green][SUCCESS] Successfully logged {lead_details['name']} to Sheets with status '{target_status}'.[/green]")
                    backfilled_count += 1
                    # Update cache
                    new_url = lead_details.get("linkedin_url", "").strip().lower()
                    if new_url:
                        sheet_by_url[new_url] = {"data": lead_details}
                    sheet_by_name[name_norm] = {"data": lead_details}
        else:
            current_status = sheet_row_info["data"].get("Status", "").strip()
            row_idx = sheet_row_info["row_idx"]
            
            if current_status != target_status:
                console.print(f"\n[yellow][STATUS MISMATCH][/yellow] {lead_details['name']} is in Sheets at row {row_idx}, but status is '{current_status}' (log indicates '{target_status}').")
                confirm = "y" if yes else input(f"  Update status to '{target_status}'? (y/n): ").strip().lower()
                if confirm == "y":
                    try:
                        # Status is at column I (COL_STATUS index 8 is column 9)
                        client._sheet.update_cell(row_idx, 9, target_status)
                        console.print(f"  [green][SUCCESS] Updated row {row_idx} to status '{target_status}'.[/green]")
                        sheet_row_info["data"]["Status"] = target_status
                        updated_count += 1
                    except Exception as ex:
                        console.print(f"  [red]Failed to update status: {ex}[/red]")
            else:
                console.print(f"  - [OK] {lead_details['name']} is already present with correct status '{target_status}'.")

    console.print(f"\n[bold green]Repair complete![/bold green] Backfilled {backfilled_count} leads, updated status for {updated_count} leads.")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Repair and backfill Google Sheets from emissary logs.")
    parser.add_argument("query", nargs="?", default=None, help="Query string to match run timestamp in logs.")
    parser.add_argument("-y", "--yes", action="store_true", help="Auto-confirm all logging and update actions.")
    args = parser.parse_args()
    backfill_missing_logs(args.query, args.yes)
