"""
Ghostwriter Agent — Emissary
Bulk-drafts both:
  1. A 280-char connection hook (stored for CRM reference)
  2. A 5-part DM (sent when they accept the connection)
Uses a single Gemini API call to stay within free-tier daily limits.
"""

import json
import os
import re
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

# ── Bulk Prompt — returns both connection hook AND follow-up DM in one call ──
BULK_PROMPT = """You are an expert SDR writing on behalf of Yatharth Sachdeva, an IT student at DTU (9.29 CGPA).

His Profile:
{my_profile_json}

Here are {count} LinkedIn leads:
{leads_payload}

For EACH lead, generate TWO pieces of content:

1. drafted_note: A 280-character LinkedIn connection hook. Sound like a fellow engineer/builder, NOT a student begging for a job. Structure: Observation (their specific tech) → Flex (Yatharth's project/achievement) → Soft close. No URLs, no "Hi [Name]", no resume links. Under 280 chars strictly.

2. drafted_dm: A 5-sentence follow-up DM to send AFTER they accept the request. Structure:
   - Sentence 1: Thank them for connecting + mention their company's specific tech.
   - Sentence 2: "I'm an IT student at DTU (9.29 CGPA)" + drop a heavy technical flex — mention AetherNet (multi-agent security mesh using WASM sandboxes), winning the UIDAI hackathon with AIRS architecture, or scaling Polars-based ML pipelines. Pick the most relevant one for this lead.
   - Sentence 3: State clearly you are looking for a 2-month summer SWE/AI internship.
   - Sentence 4: Low-friction CTA — "If you're open to it, I'd love to set up a quick chat to discuss how my architecture experience might align with your roadmap."
   - Sentence 5 (MANDATORY, EXACT): "Here is my resume: {resume_link}"
   RULES: Do NOT use 'pleasure', 'honored', 'aspiring', or 'hope'. Confident peer-to-peer tone. Under 90 words total.

Return ONLY a valid JSON array enclosed in ```json ... ``` tags:
[
  {{
    "name": "Lead Name",
    "drafted_note": "The 280-char connection hook...",
    "drafted_dm": "The 5-sentence follow-up DM..."
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
        import time
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

        # Build lookup map: name → {drafted_note, drafted_dm}
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
