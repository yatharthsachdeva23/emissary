"""
Ghostwriter Agent — Emissary
Bulk-drafts both:
  1. A 280-char connection hook (stored for CRM reference)
  2. A Meta-Flex DM (sent when they accept the connection)
Uses a single Gemini API call to stay within free-tier daily limits.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from google import genai
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

load_dotenv()
console = Console()

DATA_DIR = Path(__file__).parent.parent / "data"
LEADS_PATH = DATA_DIR / "leads_today.json"
INSTRUCTIONS_PATH = DATA_DIR / "prompt_instructions.json"
MAX_NOTE_LENGTH = 280

# ── Bulk Prompt v2: "Meta-Flex" — the bot reveals itself, 3-paragraph DM ──────
BULK_PROMPT = """You are the internal drafting engine for "Emissary," a custom Python/Playwright automation system built by Yatharth Sachdeva.
Yatharth is a B.Tech Information Technology student at Delhi Technological University (DTU) with a 9.29 CGPA. He specializes in high-concurrency backends and AI-agent architectures (having built systems like AetherNet and SentinelMesh).

ABOUT YATHARTH:
{my_profile_json}

HERE ARE {count} LEADS TO DRAFT FOR:
{leads_payload}

Your Task:
I will provide a JSON array of highly qualified leads. For EACH lead, return a JSON object containing their Name, a 280-character drafted_note, and the final drafted_dm.

PIECE 1 - drafted_note (LinkedIn Connection Hook):
A 280-character hook sent WITH the connection request.
- Sound like a fellow engineer, NOT a student asking for a job.
- Structure: [Specific observation about their company's tech] -> [Yatharth's most relevant project flex] -> [Soft, confident close]
- No URLs, no "Hi [Name]", no resume links. STRICTLY under 280 characters.

PIECE 2 - drafted_dm Structure (Strictly 5 sentences, broken into 3 paragraphs):
Paragraph 1: Thank them for connecting and validate their company's tech. Then, proudly state that instead of a standard cold message, Yatharth is giving them a "live demo" or "proof of work" by using a custom Python/Playwright agent he engineered to deliver this message.
Paragraph 2: Frame Yatharth as a builder. State he is an IT student at DTU (9.29 CGPA) who builds complex systems (mention SentinelMesh or AetherNet). State clearly he is looking for a 2-month summer internship to ship backend code.
Paragraph 3: Offer a quick chat to discuss technical alignment, immediately followed by "Here is my resume: {resume_link}".

CRITICAL FORMATTING RULES:
- You MUST separate the 3 paragraphs by inserting \\n\\n into the JSON string. Do not output a single block of text.
- Keep the tone PROUD, transparent, and confident. Frame the automation as a flex of his engineering abilities.
- Banned Words: "pleasure", "honored", "aspiring", "hope", "delve", "apologies", "synergy".
- Keep the total drafted_dm under 100 words. Punchy. Engineers hate walls of text.

Return ONLY a valid JSON array enclosed in ```json ... ``` tags:
[
  {{
    "name": "Lead Name",
    "drafted_note": "The 280-char connection hook...",
    "drafted_dm": "Thanks for connecting, [First]! [Company-specific observation]. Instead of a standard cold message, I wanted to give you a live demo of my engineering—this message was delivered by a custom Python/Playwright agent I engineered.\\n\\nI'm an IT student at DTU (9.29 CGPA) who builds complex systems like SentinelMesh. I am looking for a 2-month summer internship to ship backend code.\\n\\nIf you find this approach interesting and need an engineer who can build autonomously, I’d love to schedule a quick chat. Here is my resume: {resume_link}"
  }}
]"""


class GhostwriterAgent:
    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY") or "dummy_key_for_testing"
        self.client = genai.Client(api_key=api_key)
        self.resume_link = os.getenv("RESUME_LINK", "[ADD_YOUR_RESUME_LINK_HERE]")

    def _load_instructions(self) -> dict:
        if INSTRUCTIONS_PATH.exists():
            with open(INSTRUCTIONS_PATH, encoding="utf-8") as f:
                return json.load(f)
        return {"rules": {"dos": [], "donts": [], "tone": "", "structure": ""}}

    def _extract_json(self, text: str) -> Optional[list]:
        match = re.search(r"```json\s*([\s\S]+?)\s*```", text)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return None
        # Fallback: try to parse entire response as JSON
        try:
            return json.loads(text.strip())
        except Exception:
            return None

    def _truncate(self, note: str, max_len: int = MAX_NOTE_LENGTH) -> str:
        """Truncate a note at word boundary if over limit."""
        if len(note) <= max_len:
            return note
        truncated = note[:max_len - 3]
        last_space = truncated.rfind(" ")
        if last_space > 0:
            truncated = truncated[:last_space]
        return truncated + "..."

    def run(self, leads: list, profile: dict, dry_run: bool = False) -> list:
        console.print("\n[bold cyan]━━━ Phase 2: Ghostwriter (Bulk Processing) ━━━[/bold cyan]")
        instructions = self._load_instructions()
        console.print(f"[cyan]Prompt instructions v{instructions.get('version', 1)}[/cyan]")

        if not leads:
            return []

        leads_payload = json.dumps(leads, indent=2)
        prompt = BULK_PROMPT.format(
            my_profile_json=json.dumps(profile, indent=2),
            count=len(leads),
            leads_payload=leads_payload,
            resume_link=self.resume_link,
        )

        drafted = []
        max_retries = 3

        with Progress(SpinnerColumn(), TextColumn("Gemini bulk drafting notes + DMs..."), console=console) as p:
            p.add_task("", total=None)
            for attempt in range(max_retries):
                try:
                    resp = self.client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
                    drafted = self._extract_json(resp.text) or []
                    break
                except Exception as e:
                    if attempt < max_retries - 1:
                        wait_time = 15 * (attempt + 1)
                        console.print(f"\n[yellow]⚠ Gemini overloaded. Retrying in {wait_time}s...[/yellow]")
                        time.sleep(wait_time)
                    else:
                        console.print(f"\n[red]❌ Gemini API failed after {max_retries} retries: {e}[/red]")
                        return leads  # Return leads without notes rather than crash

        # Build lookup map: name -> {drafted_note, drafted_dm}
        note_map = {}
        for item in drafted:
            if isinstance(item, dict) and item.get("name"):
                note_map[item["name"]] = item

        enriched = []
        for lead in leads:
            name = lead.get("name") or ""
            # Fuzzy match: try exact first, then substring
            matched = note_map.get(name)
            if not matched and name:
                for key, val in note_map.items():
                    if not key:
                        continue
                    if key.lower() in name.lower() or name.lower() in key.lower():
                        matched = val
                        break

            if matched:
                note = self._truncate(matched.get("drafted_note", ""), MAX_NOTE_LENGTH)
                dm = matched.get("drafted_dm", "")

                lead["connection_note"] = note
                lead["note_length"] = len(note)
                lead["drafted_dm"] = dm
                lead["status"] = "queued"
                enriched.append(lead)

                if dry_run:
                    console.print(Panel(
                        f"[bold]{name}[/bold] @ {lead.get('company', '?')}\n\n"
                        f"[bold yellow]Hook ({len(note)} chars):[/bold yellow]\n[cyan]{note}[/cyan]\n\n"
                        f"[bold yellow]DM:[/bold yellow]\n[green]{dm}[/green]",
                        title=f"Draft #{len(enriched)}", border_style="blue",
                    ))
            else:
                console.print(f"[yellow]  ⚠ No draft generated for {name} — skipping[/yellow]")

        console.print(f"[green]✓ Drafted {len(enriched)}/{len(leads)} notes + DMs[/green]")

        # Persist enriched leads
        if LEADS_PATH.exists() or enriched:
            DATA_DIR.mkdir(exist_ok=True)
            existing = {}
            if LEADS_PATH.exists():
                try:
                    with open(LEADS_PATH, encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    pass
            existing["leads"] = enriched
            with open(LEADS_PATH, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)

        return enriched
