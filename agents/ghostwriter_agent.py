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
import math
from pathlib import Path
from typing import Optional

from google import genai
from dotenv import load_dotenv
from utils.gemini_client import get_client_with_rotation, mark_key_exhausted
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

load_dotenv()
console = Console()

DATA_DIR = Path(__file__).parent.parent / "data"
LEADS_PATH = DATA_DIR / "leads_today.json"
INSTRUCTIONS_PATH = DATA_DIR / "prompt_instructions.json"
MAX_NOTE_LENGTH = 280

# ── Big Tech Bulk Prompt ───────────────────────────────────────────────────────
BIG_TECH_BULK_PROMPT = """You are the personalized messaging drafting engine for "Emissary," built by Yatharth.
Yatharth is a 4th-year student at Delhi Technological University (DTU, 9.3 CGPA) and former AI PM Intern at NoBrokerHood. He specializes in Product Management, B2B sales automation, search algorithm optimization, and product strategy.

ABOUT YATHARTH'S BACKGROUND:
- College: 4th-year student at Delhi Technological University (DTU), Information Technology, 9.3 CGPA.
- Past Experience: AI Product Management Intern at NoBrokerHood.
- Key Outcomes: Built automated B2B sales engines capturing 25+ extra qualified leads/month, optimized search algorithms to deliver 1.5x output coverage within identical credit constraints, and developed automated research intelligence products.
- Hackathon: Ranked 4th in NMG Labs' Agentic AI Hackathon.
- Live Demo: This outreach was researched, targeted, and delivered autonomously by a system built by Yatharth.

YOUR TASK:
For EACH lead, write a personalized, authentic, builder-to-builder direct message (drafted_dm).

CRITICAL TONE:
- DO NOT sound like a salesperson, cold-caller, or recruiter. NO sales pitch, NO corporate fluff, NO "imagine if" or "what if".
- Sound like a smart, curious fellow product builder reaching out directly to another product leader.

STRUCTURE:
Paragraph 1:
- "Hi [First Name],"
- Compliment their specific product work or team focus, mention a specific trade-off or challenge in their area, and ask what they are doing to handle this.
- Describe the potential to streamline or scale this outcome without using words like "imagine" or "what if".

Paragraph 2:
- "I can actually help you guys achieve this." Followed by Yatharth's credentials (DTU 9.3 CGPA, NoBrokerHood 25+ extra leads/mo, 1.5x search optimization, 4th rank Agentic AI Hackathon, and live automation proof).

Paragraph 3:
- "Let's do a quick 12-min call where we can discuss this and see how it matches both of us. You can check my resume and get a quick brief about me here: {resume_link}\\n\\nLet me know a good time for us to do a meet!"

HERE ARE {count} LEADS TO DRAFT FOR:
{leads_payload}

Return ONLY a valid JSON array enclosed in ```json ... ``` tags:
[
  {{
    "name": "Lead Name",
    "drafted_dm": "Hi [First Name],\\n\\n...\\n\\nI can actually help you guys achieve this. I am a 4th-year student at DTU (9.3 CGPA) and former AI PM Intern at NoBrokerHood, where I built automated B2B engines that captured 25+ extra qualified leads a month, and optimized search algorithms to do 1.5x output within the same constraints. I also ranked 4th in NMG Labs' Agentic AI Hackathon. In fact, this message was researched and delivered by an autonomous system I built to test product execution live.\\n\\nLet's do a quick 12-min call where we can discuss this and see how it matches both of us. You can check my resume and get a quick brief about me here: {resume_link}\\n\\nLet me know a good time for us to do a meet!"
  }}
]"""

# ── Startup/Medium Bulk Prompt ────────────────────────────────────────────────
STARTUP_BULK_PROMPT = """You are the personalized messaging drafting engine for "Emissary," built by Yatharth.
Yatharth is a 4th-year student at Delhi Technological University (DTU, 9.3 CGPA) and former AI PM Intern at NoBrokerHood. He specializes in Product Management, B2B sales automation, search algorithm optimization, and product strategy.

ABOUT YATHARTH'S BACKGROUND & ACHIEVEMENTS:
- College: 4th-year student at Delhi Technological University (DTU), Information Technology, 9.3 CGPA.
- Past Experience: AI Product Management Intern at NoBrokerHood.
- Key Outcomes:
  1. Built automated B2B sales engines capturing 25+ extra qualified leads per month.
  2. Optimized search algorithms to deliver 1.5x output coverage within identical credit constraints.
  3. Developed automated research intelligence products to accelerate enterprise deal closures.
- Hackathon: Ranked 4th in NMG Labs' Agentic AI Hackathon.
- Live Proof: This very message interaction was researched, targeted, and delivered autonomously by a system built by Yatharth.

YOUR TASK:
For EACH lead in the provided JSON array, write a personalized, highly authentic, builder-to-builder direct message (drafted_dm).

CRITICAL TONE & PHILOSOPHY:
- DO NOT sound like a salesperson, a cold-caller, or a recruiter. Absolutely NO aggressive sales pitching, NO canned templates, NO buzzword fluff ("imagine if", "what if", "synergy", "paradigm").
- Sound like a smart, curious fellow product builder reaching out genuinely to another founder or product leader.
- The tone is peer-to-peer, humble yet deeply confident, observant, and conversational.

STRUCTURE OF THE MESSAGE (3 natural paragraphs):

Paragraph 1: Genuine Curiosity & Grounded Vision
- Open with: "Hi [First Name],"
- State that their company has huge potential: "[Company Name] has huge potential, but I am actually curious about [mention a specific, real operational pain point or challenge in their product/domain] and what you guys are doing to handle this."
- Then ground the vision naturally without using words like "imagine" or "what if": "See, [Company Name] has the potential to [paint a concrete, exciting picture of scaling, user growth, or operational efficiency in their domain], and getting this right could really [tangible business/product outcome]."

Paragraph 2: The Solution & Concrete Proof
- Natural transition: "I can actually help you guys achieve this."
- Present Yatharth's credibility naturally to back up the claim: "I am a 4th-year student at DTU (9.3 CGPA) and former AI PM Intern at NoBrokerHood, where I built automated B2B engines that captured 25+ extra qualified leads a month, and optimized search algorithms to do 1.5x output within the same constraints. I also ranked 4th in NMG Labs' Agentic AI Hackathon. In fact, this message was researched and delivered by an autonomous system I built to test product execution live."

Paragraph 3: The 12-Min Chat & Brief Check
- Friendly, low-friction ask: "Let's do a quick 12-min call where we can discuss this and see how it matches both of us."
- Share resume for brief: "You can check my resume and get a quick brief about me here: {resume_link}"
- Close with: "Let me know a good time for us to do a meet!"

CRITICAL RULES:
- Separate the paragraphs with \\n\\n in the JSON string.
- Address the person by their first name: "Hi [First Name],".
- NEVER use words like: "imagine", "what if", "pleasure", "honored", "aspiring", "hope", "delve", "apologize", "sincerely", "opportunity", "passionate".
- Keep length around 120-140 words. Easy to read, authentic, and impactful.

HERE ARE {count} LEADS TO DRAFT FOR:
{leads_payload}

Return ONLY a valid JSON array enclosed in ```json ... ``` tags:
[
  {{
    "name": "Lead Name",
    "drafted_dm": "Hi [First Name],\\n\\n[Company Name] has huge potential, but I am actually curious about [pain point] and what you guys are doing to handle this. See, [Company Name] has the potential to [grounded vision], and getting this right could really [outcome].\\n\\nI can actually help you guys achieve this. I am a 4th-year student at DTU (9.3 CGPA) and former AI PM Intern at NoBrokerHood, where I built automated B2B engines that captured 25+ extra qualified leads a month, and optimized search algorithms to do 1.5x output within the same constraints. I also ranked 4th in NMG Labs' Agentic AI Hackathon. In fact, this message was researched and delivered by an autonomous system I built to test product execution live.\\n\\nLet's do a quick 12-min call where we can discuss this and see how it matches both of us. You can check my resume and get a quick brief about me here: {resume_link}\\n\\nLet me know a good time for us to do a meet!"
  }}
]"""


class GhostwriterAgent:
    def __init__(self):
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

    def is_big_tech(self, lead: dict) -> bool:
        """Classify if a lead belongs to a Big Tech/enterprise company or not."""
        company = (lead.get("company") or "").lower()
        title = (lead.get("title") or "").lower()
        role = (lead.get("role") or "").lower()
        
        # Word boundary match using regex is safer to avoid false positives (e.g. metadata -> meta)
        big_tech_pattern = r'\b(google|microsoft|amazon|apple|meta|uber|stripe|netflix|adobe|salesforce|flipkart|swiggy|zomato|atlassian)\b'
        
        if re.search(big_tech_pattern, company):
            return True
        if not company and (re.search(big_tech_pattern, title) or re.search(big_tech_pattern, role)):
            return True
        return False

    def _call_gemini_for_cohort(self, prompt: str, cohort_name: str, max_retries_per_key: int = 3) -> list:
        """Calls Gemini API with automatic round-robin rotation on any errors."""
        drafted = []
        with Progress(SpinnerColumn(), TextColumn(f"Gemini bulk drafting ({cohort_name})..."), console=console) as p:
            p.add_task("", total=None)
            try:
                from utils.gemini_client import generate_with_rotation
                resp_text = generate_with_rotation(prompt, model="gemini-2.5-flash")
                drafted = self._extract_json(resp_text) or []
                return drafted
            except Exception as e:
                console.print(f"\n[red]❌ Gemini API failed for cohort '{cohort_name}': {e}[/red]")
                return []
        return drafted

    def run(self, leads: list, profile: dict, dry_run: bool = False) -> list:
        console.print("\n[bold cyan]━━━ Phase 2: Ghostwriter (Bulk Processing) ━━━[/bold cyan]")
        instructions = self._load_instructions()
        console.print(f"[cyan]Prompt instructions v{instructions.get('version', 1)}[/cyan]")

        if not leads:
            return []

        # Split leads into Big Tech vs. Startup/Medium companies
        big_tech_leads = [l for l in leads if self.is_big_tech(l)]
        startup_leads = [l for l in leads if not self.is_big_tech(l)]

        drafted = []

        # Process Big Tech Cohort
        if big_tech_leads:
            console.print(f"[cyan]Processing {len(big_tech_leads)} Big Tech leads...[/cyan]")
            leads_payload_bt = json.dumps(big_tech_leads, indent=2)
            prompt_bt = BIG_TECH_BULK_PROMPT.format(
                my_profile_json=json.dumps(profile, indent=2),
                count=len(big_tech_leads),
                leads_payload=leads_payload_bt,
                resume_link=self.resume_link,
            )
            drafted_bt = self._call_gemini_for_cohort(prompt_bt, "Big Tech")
            drafted.extend(drafted_bt)

        # Process Startup Cohort
        if startup_leads:
            console.print(f"[cyan]Processing {len(startup_leads)} Startup / Medium leads...[/cyan]")
            leads_payload_su = json.dumps(startup_leads, indent=2)
            prompt_su = STARTUP_BULK_PROMPT.format(
                count=len(startup_leads),
                leads_payload=leads_payload_su,
                resume_link=self.resume_link,
            )
            drafted_su = self._call_gemini_for_cohort(prompt_su, "Startup/Medium")
            drafted.extend(drafted_su)


        # Build lookup map: name -> {drafted_dm}
        dm_map = {}
        for item in drafted:
            if isinstance(item, dict) and item.get("name"):
                dm_map[item["name"]] = item

        enriched = []
        for lead in leads:
            name = lead.get("name") or ""
            # Fuzzy match: try exact first, then substring
            matched = dm_map.get(name)
            if not matched and name:
                for key, val in dm_map.items():
                    if not key:
                        continue
                    if key.lower() in name.lower() or name.lower() in key.lower():
                        matched = val
                        break

            if matched:
                dm = matched.get("drafted_dm", "")
                # Deterministic enforcement: replace any hallucinated or typo'd Google Drive folder URL with exact verified resume_link
                if self.resume_link and self.resume_link != "[ADD_YOUR_RESUME_LINK_HERE]":
                    dm = re.sub(r'https?://drive\.google\.com/drive/folders/[a-zA-Z0-9_-]+', self.resume_link, dm)

                lead["connection_note"] = ""
                lead["note_length"] = 0
                lead["drafted_dm"] = dm
                lead["status"] = "queued"
                enriched.append(lead)

                if dry_run:
                    console.print(Panel(
                        f"[bold]{name}[/bold] @ {lead.get('company', '?')}\n\n"
                        f"[bold yellow]DM:[/bold yellow]\n[green]{dm}[/green]",
                        title=f"Draft #{len(enriched)}", border_style="blue",
                    ))
            else:
                console.print(f"[yellow]  ⚠ No draft generated for {name} — skipping[/yellow]")

        console.print(f"[green]✓ Drafted {len(enriched)}/{len(leads)} DMs[/green]")

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
