"""
Feedback Agent — Emissary
Reads 'Your Feedback' from Google Sheet → updates prompt_instructions.json via Gemini.
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

from google import genai
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

load_dotenv()
console = Console()

DATA_DIR = Path(__file__).parent.parent / "data"
INSTRUCTIONS_PATH = DATA_DIR / "prompt_instructions.json"

FEEDBACK_PROMPT = """You are improving a LinkedIn cold outreach message drafting system.

Current rules:
```json
{current_instructions}
```

User feedback on recent messages:
{feedback_items}

Update the rules based on this feedback. Add specific DO/DON'T rules, update tone if needed.
Keep working rules, remove contradicted ones. Add each feedback to feedback_history with timestamp.

Return ONLY a valid JSON object in ```json ... ``` tags with the EXACT same schema."""


class FeedbackAgent:
    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY") or "dummy_key_for_testing"
        self.client = genai.Client(api_key=api_key)

    def _load_instructions(self) -> dict:
        if INSTRUCTIONS_PATH.exists():
            with open(INSTRUCTIONS_PATH, encoding="utf-8") as f:
                return json.load(f)
        return {"version": 1, "rules": {"dos": [], "donts": [], "tone": "", "structure": ""},
                "feedback_history": []}

    def _save_instructions(self, instructions: dict) -> None:
        instructions["version"] = instructions.get("version", 1) + 1
        instructions["last_updated"] = datetime.now().isoformat()
        with open(INSTRUCTIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(instructions, f, indent=2, ensure_ascii=False)
        console.print(f"[green]✓ Instructions updated to v{instructions['version']}[/green]")

    def _extract_json(self, text: str):
        match = re.search(r"```json\s*([\s\S]+?)\s*```", text)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return None
        return None

    def run(self) -> bool:
        console.print("\n[bold cyan]━━━ Feedback Loop ━━━[/bold cyan]")
        try:
            from utils.sheets import SheetsClient
            client = SheetsClient()
            if not client.available:
                console.print("[yellow]Sheets not configured — skipping feedback[/yellow]")
                return False
            pending = client.get_pending_feedback()
        except Exception as e:
            console.print(f"[yellow]Feedback read error: {e}[/yellow]")
            return False

        if not pending:
            console.print("[dim]No pending feedback.[/dim]")
            return False

        console.print(f"[cyan]{len(pending)} feedback item(s):[/cyan]")
        for item in pending:
            console.print(f'  • [bold]{item["name"]}[/bold]: "[italic]{item["feedback"]}[/italic]"')

        feedback_text = ""
        for i, item in enumerate(pending, 1):
            feedback_text += (
                f"\n--- Feedback {i} ---\n"
                f"Person: {item['name']} @ {item['company']} ({item['role']})\n"
                f"Message sent: {item['note']}\n"
                f"Feedback: {item['feedback']}\n"
            )

        current = self._load_instructions()
        prompt = FEEDBACK_PROMPT.format(
            current_instructions=json.dumps(current, indent=2),
            feedback_items=feedback_text,
        )

        try:
            resp = self.client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
            updated = self._extract_json(resp.text)
            if not updated:
                console.print("[red]Failed to parse updated instructions[/red]")
                return False

            self._save_instructions(updated)
            client.mark_feedback_applied([item["row_index"] for item in pending])

            console.print(Panel(
                f"[green]Processed {len(pending)} feedback item(s)[/green]\n"
                f"Instructions → v{updated.get('version','?')}",
                title="✅ Feedback Applied", border_style="green",
            ))
            return True
        except Exception as e:
            console.print(f"[red]Feedback error: {e}[/red]")
            return False
