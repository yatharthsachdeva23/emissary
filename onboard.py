"""
onboard.py — Emissary
Run this ONCE to set up your persona profile.
Re-run anytime to update it.

Usage:
    python onboard.py
    python onboard.py --update   (re-runs the interview, overwrites profile)
"""

import sys
import os
from pathlib import Path
from utils.gemini_client import has_gemini_keys

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from agents.persona_agent import PersonaAgent
from rich.console import Console
from rich.prompt import Confirm
from dotenv import load_dotenv

load_dotenv()
console = Console()

PROFILE_PATH = Path("data/my_profile.json")


def check_env():
    """Verify required environment variables are set."""
    missing = []
    if not has_gemini_keys():
        missing.append("GEMINI_API_KEY_1 (or GEMINI_API_KEY)")
    for key in ["SERPER_API_KEY", "GOOGLE_SHEET_ID"]:
        val = os.getenv(key, "")
        if not val or val.startswith("your_"):
            missing.append(key)
    
    if missing:
        console.print("\n[bold red]⚠ Missing environment variables:[/bold red]")
        for key in missing:
            console.print(f"  • {key}")
        console.print(
            "\n[yellow]Copy .env.example to .env and fill in your API keys.[/yellow]\n"
            "See README.md for instructions on getting each key.\n"
        )
        if "GEMINI_API_KEY_1 (or GEMINI_API_KEY)" in missing:
            sys.exit(1)
        else:
            console.print("[yellow]Continuing with Gemini only (other keys needed for full pipeline).[/yellow]\n")


def main():
    check_env()
    
    update_mode = "--update" in sys.argv
    
    # If profile exists and not in update mode, ask
    if PROFILE_PATH.exists() and not update_mode:
        import json
        with open(PROFILE_PATH) as f:
            existing = json.load(f)
        
        name = existing.get("name", "Unknown")
        console.print(f"\n[green]✓ Profile already exists for: [bold]{name}[/bold][/green]")
        
        if not Confirm.ask("Do you want to update your profile?", default=False):
            console.print("[cyan]No changes made. Run [bold]python main.py[/bold] to start the daily pipeline.[/cyan]")
            return
    
    # Run the interview
    agent = PersonaAgent()
    profile = agent.run_interview()
    
    if profile:
        agent.save_profile(profile)
        console.print(
            "\n[bold cyan]✅ Setup complete![/bold cyan]\n"
            "Next steps:\n"
            "  1. Make sure your .env file is fully configured\n"
            "  2. Set up Google Sheets credentials (see README.md)\n"
            "  3. Run [bold]python main.py --setup-session[/bold] to capture your LinkedIn session\n"
            "  4. Run [bold]python main.py --dry-run[/bold] to test the full pipeline\n"
            "  5. Set up the Task Scheduler: [bold]python setup_scheduler.py[/bold]\n"
        )


if __name__ == "__main__":
    main()
