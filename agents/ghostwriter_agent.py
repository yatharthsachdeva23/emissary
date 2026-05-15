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

ABOUT YATHARTH:
{my_profile_json}

Key facts to always reference:
- B.Tech Information Technology student at Delhi Technological University (DTU), 9.29 CGPA
- Built AetherNet: a multi-agent security mesh using WASM sandboxes
- Won the UIDAI national hackathon with AIRS (AI-powered identity resolution system)
- Built Emissary: the autonomous Python/Playwright agent that is literally sending this message
- Looking for a 2-month summer SWE/AI/Backend internship

HERE ARE {count} LEADS TO DRAFT FOR:
{leads_payload}

For EACH lead, return TWO pieces of content:

PIECE 1 - drafted_note (LinkedIn Connection Hook):
A 280-character hook sent WITH the connection request.
- Sound like a fellow engineer, NOT a student asking for a job.
- Structure: [Specific observation about their company's tech] -> [Yatharth's most relevant project flex] -> [Soft, confident close]
- No URLs, no "Hi [Name]", no resume links. STRICTLY under 280 characters.

PIECE 2 - drafted_dm (The Meta-Flex Follow-up DM):
A follow-up message sent AFTER they accept. This DM breaks the fourth wall and reveals it was sent by an autonomous bot.

STRICT 3-PARAGRAPH STRUCTURE — you MUST use \\n\\n to separate each paragraph in the JSON string:

PARAGRAPH 1 — The Hook + The Meta-Flex (2 sentences):
  Sentence 1: Thank them for connecting and name one specific, technical thing their company is building.
  Sentence 2: "Full transparency — this message was delivered by an autonomous Python/Playwright agent I built to automate my internship outreach, and it flagged [Company] as a top engineering match."

PARAGRAPH 2 — The Pitch (2 sentences):
  Sentence 3: "I'm an IT student at DTU (9.29 CGPA) who builds real systems" — pick the most relevant project (AetherNet, AIRS, or Emissary itself) and tie it to what their company does.
  Sentence 4: State you are looking for a 2-month summer internship to ship production backend/AI code.

PARAGRAPH 3 — The Close (1 sentence, EXACT wording):
  "If your team has any bandwidth for an intern who can ship, I'd love to chat — here is my resume: {resume_link}"

CRITICAL RULES:
- You MUST insert \\n\\n between each paragraph inside the JSON string.
- Tone: transparent, builder-to-builder, confident. NOT desperate, NOT corporate.
- BANNED WORDS: "pleasure", "honored", "aspiring", "hope", "eager", "delve", "synergy".
- Total drafted_dm MUST be under 100 words. Punchy. Engineers hate walls of text.
- Start with "Thanks for connecting, [FirstName]!" — never "Hi [Name]".

Return ONLY a valid JSON array enclosed in ```json ... ``` tags:
[
  {{
    "name": "Lead Name",
    "drafted_note": "The 280-char connection hook...",
    "drafted_dm": "Thanks for connecting, [First]! [Company-specific observation]. Full transparency — this message was delivered by an autonomous Python/Playwright agent I built, and it flagged [Company] as a top match.\\n\\nI'm an IT student at DTU (9.29 CGPA) who built [most relevant project — tie it to their stack]. I'm looking for a 2-month summer internship to ship production code.\\n\\nIf your team has any bandwidth for an intern who can ship, I'd love to chat — here is my resume: {resume_link}"
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
