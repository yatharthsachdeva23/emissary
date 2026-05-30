# -*- coding: utf-8 -*-
"""
main.py — Emissary Daily Pipeline Orchestrator
This is the file Task Scheduler calls every morning at 10:00 AM.

Usage:
    python main.py                  # Full daily run
    python main.py --dry-run        # Simulate without sending
    python main.py --test-mode      # Open browser, visit profiles, don't send
    python main.py --setup-session  # Capture LinkedIn session cookies (run once)
    python main.py --skip-feedback  # Skip feedback loop (faster for testing)
    python main.py --skip-send      # Discovery + ghostwrite only, no Playwright
"""

import json
import io
import os
import sys
from datetime import datetime
from pathlib import Path
from utils.gemini_client import has_gemini_keys

# ── Make sure project root is on the path ────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

LOG_FILE_PATH = Path("logs") / "emissary.log"
Path("logs").mkdir(exist_ok=True)

class DualLogger:
    def __init__(self, original_stream, log_file):
        self.original_stream = original_stream
        self.log_file = log_file
        self.encoding = "utf-8"

    def write(self, message):
        self.original_stream.write(message)
        self.log_file.write(message)
        self.log_file.flush()

    def flush(self):
        self.original_stream.flush()
        self.log_file.flush()

    def isatty(self):
        return False

# Setup dual logging to both console and file
_log_file = open(LOG_FILE_PATH, "a", encoding="utf-8", errors="replace")

# Force UTF-8 on Windows before importing Rich (legacy console fix)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.stdout = DualLogger(sys.stdout, _log_file)
sys.stderr = DualLogger(sys.stderr, _log_file)

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

load_dotenv()
console = Console()

DATA_DIR = Path("data")
LEADS_PATH = DATA_DIR / "leads_today.json"
LOG_PATH = Path("logs")


def parse_args() -> dict:
    args = sys.argv[1:]
    return {
        "dry_run":        "--dry-run" in args,
        "test_mode":      "--test-mode" in args,
        "setup_session":  "--setup-session" in args,
        "skip_feedback":  "--skip-feedback" in args,
        "skip_send":      "--skip-send" in args,
        "ghost_run":      "--ghost-run" in args,
    }


def check_env(dry_run: bool = False) -> bool:
    """Verify critical environment variables."""
    # 1. Verification
    if not has_gemini_keys():
        if not dry_run:
            console.print("[bold red]No GEMINI_API_KEY_1 (or GEMINI_API_KEY) set in .env — see README.md[/bold red]")
            sys.exit(1)
        else:
            console.print("[yellow]No Gemini keys set — dry-run will use mock scoring[/yellow]")
            return True  # allow dry-run without a real key
        console.print("[bold red]GEMINI_API_KEY not set in .env — see README.md[/bold red]")
        return False
    return True


def write_run_log(summary: dict) -> None:
    """Append a JSON summary of this run to logs/runs.jsonl"""
    LOG_PATH.mkdir(exist_ok=True)
    log_file = LOG_PATH / "runs.jsonl"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(summary) + "\n")


def main():
    flags = parse_args()

    console.print(Rule("[bold cyan]EMISSARY PIPELINE[/bold cyan]"))
    console.print(
        f"[dim]{datetime.now().strftime('%A, %d %B %Y — %H:%M')}[/dim]"
        + (" [yellow](DRY RUN)[/yellow]" if flags["dry_run"] else "")
        + (" [yellow](TEST MODE)[/yellow]" if flags["test_mode"] else "")
        + (" [magenta](GHOST RUN)[/magenta]" if flags["ghost_run"] else "")
    )

    # ── Time Check Safeguard ───────────────────────────────────────────────
    if not (flags["setup_session"] or flags["dry_run"] or flags["test_mode"] or flags["ghost_run"]):
        current_hour = datetime.now().hour
        if current_hour < 8 or current_hour >= 19:
            console.print(f"\n[yellow]⚠ Current time: {datetime.now().strftime('%H:%M')}[/yellow]")
            console.print("[yellow]Outside working hours (8 AM - 7 PM). Emissary is sleeping.[/yellow]")
            return

    # ── Handle --setup-session ─────────────────────────────────────────────
    if flags["setup_session"]:
        from agents.messenger_agent import MessengerAgent
        agent = MessengerAgent()
        success = agent.setup_session()
        if success:
            console.print("\n[bold green]Session captured. You can now run: python main.py[/bold green]")
        else:
            console.print("\n[bold red]Session capture failed. Please try again.[/bold red]")
        return

    # ── Validate env ───────────────────────────────────────────────────────
    if not check_env(dry_run=flags["dry_run"]):
        sys.exit(1)

    # ── Load profile ───────────────────────────────────────────────────────
    from agents.persona_agent import PersonaAgent
    try:
        profile = PersonaAgent.load_profile()
        console.print(f"[green]Profile loaded: [bold]{profile.get('name')}[/bold][/green]")
    except FileNotFoundError:
        console.print("[bold red]No profile found. Run: python onboard.py[/bold red]")
        sys.exit(1)

    run_summary = {
        "date": datetime.now().isoformat(),
        "dry_run": flags["dry_run"],
        "test_mode": flags["test_mode"],
        "ghost_run": flags["ghost_run"],
        "dms_sent": 0,
        "leads_discovered": 0,
        "notes_drafted": 0,
        "connections_sent": 0,
        "connections_skipped": 0,
        "feedback_processed": False,
        "error": None,
    }

    try:
        # ── Step 1: Feedback Loop ──────────────────────────────────────
        if not flags["skip_feedback"] and not flags["dry_run"]:
            from agents.feedback_agent import FeedbackAgent
            fb_agent = FeedbackAgent()
            run_summary["feedback_processed"] = fb_agent.run()
        else:
            console.print("[dim]Skipping feedback loop[/dim]")

        # ── Step 2: Inbox Agent (The Closer) ────────────────────────
        if not flags["dry_run"] and not flags["skip_send"]:
            from agents.inbox_agent import InboxAgent
            inbox = InboxAgent()
            inbox_summary = inbox.run(ghost_run=flags["ghost_run"])
            run_summary["dms_sent"] = inbox_summary.get("dm_sent", 0)
        else:
            console.print("[dim]Skipping Inbox Agent (dry-run or skip-send)[/dim]")

        # ── Step 3: Discovery ──────────────────────────────────────────
        from agents.discovery_agent import DiscoveryAgent
        discovery = DiscoveryAgent()
        leads = discovery.run(profile, dry_run=flags["dry_run"])
        run_summary["leads_discovered"] = len(leads)

        if not leads:
            console.print("[yellow]No leads discovered today. Exiting.[/yellow]")
            write_run_log(run_summary)
            return

        # ── Step 4: Ghostwriter ────────────────────────────────────────
        from agents.ghostwriter_agent import GhostwriterAgent
        writer = GhostwriterAgent()
        leads = writer.run(leads, profile, dry_run=flags["dry_run"])
        run_summary["notes_drafted"] = len(leads)

        # ── Step 5: Messenger (Blank Requests) ─────────────────────
        if flags["skip_send"]:
            console.print("[yellow]--skip-send: Skipping Playwright messenger.[/yellow]")
        else:
            from agents.messenger_agent import MessengerAgent
            messenger = MessengerAgent()
            results = messenger.run(
                leads,
                dry_run=flags["dry_run"],
                test_mode=flags["test_mode"],
                ghost_run=flags["ghost_run"],
            )

            sent = [r for r in results if r.get("status") in ("Blank Sent", "ghost_sent")]
            skipped = [r for r in results if r.get("status") not in ("Blank Sent", "ghost_sent", "dry_run", "test_visited")]
            run_summary["connections_sent"] = len(sent)
            run_summary["connections_skipped"] = len(skipped)

            # ── Step 6: Log to Google Sheet and Mark Seen ────────────────────
            if not flags["dry_run"] and not flags["test_mode"] and not flags["ghost_run"]:
                # Log successful sends to Google Sheets
                if sent:
                    try:
                        from utils.sheets import SheetsClient
                        client = SheetsClient()
                        client.log_leads(sent)
                    except Exception as e:
                        console.print(f"[yellow]Sheet logging error: {e}[/yellow]")

                # Mark BOTH sent and skipped leads as locally seen so we never try them again
                try:
                    from agents.discovery_agent import DiscoveryAgent as _DA
                    d_agent = _DA()
                    for lead in sent + skipped:
                        if lead.get("linkedin_url"):
                            d_agent.mark_contacted(lead["linkedin_url"])
                except Exception as e:
                    console.print(f"[yellow]Failed to mark seen profiles: {e}[/yellow]")


    except KeyboardInterrupt:
        console.print("\n[yellow]Pipeline interrupted by user.[/yellow]")
        run_summary["error"] = "KeyboardInterrupt"

    except Exception as e:
        console.print(f"\n[bold red]Pipeline error: {e}[/bold red]")
        run_summary["error"] = str(e)
        from utils.notifier import notify_error
        notify_error(str(e))
        import traceback
        traceback.print_exc()

    finally:
        write_run_log(run_summary)

    # ── Final summary ──────────────────────────────────────────────────────
    console.print(Rule())
    console.print(Panel(
        f"[bold]Run Summary[/bold]\n\n"
        f"  DMs sent         : [bold green]{run_summary['dms_sent']}[/bold green]  (from yesterday's acceptances)\n"
        f"  Leads discovered : {run_summary['leads_discovered']}\n"
        f"  Notes drafted    : {run_summary['notes_drafted']}\n"
        f"  Blank requests   : [green]{run_summary['connections_sent']}[/green]\n"
        f"  Skipped          : [yellow]{run_summary['connections_skipped']}[/yellow]\n"
        f"  Feedback applied : {'[green]Yes[/green]' if run_summary['feedback_processed'] else '[dim]No[/dim]'}\n\n"
        f"[dim]Log: logs/runs.jsonl[/dim]",
        title="[green]DONE[/green]",
        border_style="cyan",
    ))


if __name__ == "__main__":
    main()
