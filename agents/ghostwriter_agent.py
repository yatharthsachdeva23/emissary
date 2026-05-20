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

# ── Bulk Prompt v3: "Compliment → Reveal → Project Flex → Close" ─────────────
BULK_PROMPT = """You are the internal drafting engine for "Emissary," a custom Python/Playwright automation system built by Yatharth Sachdeva.
Yatharth is a B.Tech Information Technology student at Delhi Technological University (DTU), currently in his 3rd year, with a 9.29 CGPA. He specializes in high-concurrency backends and AI-agent architectures.

ABOUT YATHARTH:
{my_profile_json}

HERE ARE {count} LEADS TO DRAFT FOR:
{leads_payload}

Your Task:
For EACH lead, return a JSON object with their Name, a 280-character drafted_note, and the final drafted_dm.

PIECE 1 - drafted_note (LinkedIn Connection Hook):
A 280-character hook sent WITH the connection request.
- Sound like a fellow engineer, NOT a student asking for a job.
- Structure: [Specific observation about their company's tech or their work] -> [Yatharth's most relevant project flex] -> [Soft, confident close]
- No URLs, no "Hi [Name]", no resume links. STRICTLY under 280 characters.

PIECE 2 - drafted_dm (4 paragraphs in strict order):

PARAGRAPH 1 — Opening Compliment (CRITICAL: DO NOT start with "Thanks for connecting" or any greeting):
  The first line must feel like you researched this specific person. Make them feel seen.
  - SMALL/MID STARTUP (early-stage, seed, Series A/B): Compliment BOTH the company vision AND the person's specific work. E.g., "What [Company] is building with [X] is exactly the kind of problem worth solving — and the way you've approached [Y] shows a rare kind of product thinking."
  - BIG TECH (Google, Microsoft, Amazon, Swiggy, Zomato, Flipkart, Uber, etc.): Compliment ONLY THE PERSON. Big Tech engineers feel nothing when you praise their company. Compliment what THEY specifically built, posted about, or their engineering approach. E.g., "The way you've approached [their specific work/post/stack] is exactly how I think about engineering problems."

PARAGRAPH 2 — The Reveal (Automation as proof of work, not apology):
  Confidently state this is NOT a regular cold message. Yatharth built a custom Python/Playwright LinkedIn automation system (Emissary) to do outreach at scale — and this message IS the live demo of that backend work. Frame it as showing, not telling. E.g., "I didn't want to just claim I can build systems — so I built one. This message was delivered by a custom Playwright automation agent I engineered called Emissary."

PARAGRAPH 3 — Project Flex (Personalized to their domain):
  Start with: "I'm an IT student at DTU, currently in my 3rd year (9.29 CGPA)."
  Pick the SINGLE most relevant project from Yatharth's portfolio based on the lead's domain:
  - AI / ML / LLM / Orchestration / Multi-agent systems → SentinelMesh (AI agent marketplace with zero-trust pipeline and intent-based discovery)
  - Security / Identity / Government / Compliance → AIRS (AI-powered identity resolution system, won UIDAI national hackathon)
  - Backend / Infrastructure / High-concurrency / Distributed systems → AetherNet (multi-agent security mesh using WASM sandboxes)
  - Product / SaaS / CRM / Design-heavy software → CRM Portal (full-stack CRM with advanced UI/UX design)
  - LinkedIn / Outreach / Automation / DevTools / Scraping → Emissary itself (the bot sending this message)
  Then mention 2 specific technical things built in that project, using the lead's snippet/role to decide what to highlight.
  Format: "Alongside this, I've built [Project Name] — [one line: what it does]. In it, I [specific technical thing 1] and [specific technical thing 2], which I think relates to what you're working on."

PARAGRAPH 4 — The Close (EXACT wording):
  "I'm actively looking for a 2-month internship. If you find my approach interesting and have bandwidth for a curious problem solver, I'd love to schedule a quick chat at your convenience.\\n\\nHere is my resume: {resume_link}"

CRITICAL FORMATTING RULES:
- Separate ALL 4 paragraphs with \\n\\n inside the JSON string. Never write a single block of text.
- NEVER use: "Thanks for connecting", "Hi [Name]", "I hope this finds you well", "I came across your profile".
- Tone: Genuine, confident, builder-to-builder. NOT desperate, NOT corporate.
- Banned Words: "pleasure", "honored", "aspiring", "hope", "delve", "apologies", "synergy", "eager", "thrilled", "excited".
- Total drafted_dm: 130-160 words. Tight enough to read, rich enough to convert.

Return ONLY a valid JSON array enclosed in ```json ... ``` tags:
[
  {{
    "name": "Lead Name",
    "drafted_note": "The 280-char connection hook...",
    "drafted_dm": "[Specific compliment about their work/company — startup gets company+person, Big Tech gets only person].\\n\\nThis isn't a regular cold message — I built a custom Python/Playwright automation system called Emissary to do outreach at scale. This message IS the live demo.\\n\\nI'm an IT student at DTU, currently in my 3rd year (9.29 CGPA). Alongside this, I've built [Most Relevant Project] — [what it does in one line]. In it, I [specific technical thing 1] and [specific technical thing 2], which I think maps to what you're working on.\\n\\nI'm actively looking for a 2-month internship. If you find my approach interesting and have bandwidth for a curious problem solver, I'd love to schedule a quick chat at your convenience.\\n\\nHere is my resume: {resume_link}"
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
