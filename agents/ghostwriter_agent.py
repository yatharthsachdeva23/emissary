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
from utils.text_cleaner import clean_first_name, clean_company_name
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

load_dotenv()
console = Console()

DATA_DIR = Path(__file__).parent.parent / "data"
LEADS_PATH = DATA_DIR / "leads_today.json"
INSTRUCTIONS_PATH = DATA_DIR / "prompt_instructions.json"
MAX_NOTE_LENGTH = 280

# ── Deep-Dive 1-by-1 Intelligence & DM Drafting Prompt ───────────────────────
DEEP_DIVE_RESEARCH_PROMPT = """You are the personalized messaging drafting engine for "Emissary," built by Yatharth.
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

TARGET LEAD INFORMATION (VERIFIED LIVE FROM LINKEDIN):
- Name: {lead_name}
- Clean First Name: {first_name}
- Current Company (Entity): {lead_company}
- Conversational Company Name: {clean_company}
- Verified Current Role/Title: {lead_role}
- LinkedIn Top Card / Headline:
\"\"\"
{top_card_text}
\"\"\"
- Scraped Live Experience Section:
\"\"\"
{scraped_experience}
\"\"\"

YOUR DEEP-DIVE RESEARCH & DRAFTING INSTRUCTIONS:
Execute this in four rigorous steps:

STEP 1: COMPANY & PRODUCT DECONSTRUCTION
Analyze what {clean_company} actually does. Identify their core platform/offering, target users (B2B, B2C, Enterprise, etc.), and their primary business model.
(Produce a crisp 1-2 sentence breakdown for the company_analysis field).

STEP 2: OPERATIONAL BOTTLENECK AUDIT (STRICT DOMAIN BOUNDARIES)
Identify a concrete, high-friction operational, technical, or product bottleneck at {clean_company} that falls STRICTLY into one of Yatharth's core builder domains:
1. Tech & AI Automation: Agentic workflows, web scrapers, data pipelines, search algorithm optimization, automating manual engineering or operations tasks.
2. Product Management: User activation drop-offs, onboarding friction, feature discovery loops, product-led growth mechanics, sprint execution velocity.
3. B2B Sales & Growth Funnels: Outbound pipeline generation engines, automated lead qualification, reducing SDR prospecting grind, lead enrichment workflows.
4. Growth Marketing: Product-led acquisition loops, conversion funnel leakages, algorithmic targeting.

STRICT EXCLUSIONS - DO NOT PROPOSE OR MENTION:
Financing, fundraising, accounting, legal/compliance, human resources (HR), or cloud infrastructure/DevOps.

STEP 3: GROUNDED OUTCOME FORMULATION
Frame a realistic, tangible operational outcome without using hype words like "imagine", "what if", "synergy", "game-changer", or "paradigm". Focus on concrete efficiency, pipeline scale, or user throughput.

STEP 4: 3-PARAGRAPH DIRECT MESSAGE GENERATION
Write an authentic, builder-to-builder direct message (drafted_dm) structured exactly as follows:

Paragraph 1: Genuine Curiosity & Grounded Vision
- Open with: "Hi {first_name},"
{cohort_p1_instruction}
- Then ground the vision naturally with one of these variations:
  - "See, {clean_company} has the potential to [concrete outcome in their domain], and getting this right could really [tangible product/business benefit]."
  - "If {clean_company} nails [concrete outcome in their domain], it could really [tangible product/business benefit]."
  - "{clean_company} is in a prime spot to [concrete outcome in their domain], which would directly [tangible product/business benefit]."

Paragraph 2: The Solution & Concrete Proof
- Natural transition: "I can actually help you guys achieve this."
- Present Yatharth's credibility naturally to back up the claim: "I am a 4th-year student at DTU (9.3 CGPA) and former AI PM Intern at NoBrokerHood, where I worked cross-functionally across engineering, product, and sales to build automated B2B engines capturing 25+ extra qualified leads a month, and optimized search algorithms to do 1.5x output within the same constraints. I also ranked 4th in NMG Labs' Agentic AI Hackathon. In fact, this message was researched and delivered by an autonomous system I built to test product execution live."

Paragraph 3: The 12-Min Chat & Brief Check
- Friendly, low-friction ask: "Let's do a quick 12-min call where we can discuss this and see how it matches both of us. You can check my resume and get a quick brief about me here: {resume_link}\\n\\nLet me know a good time for us to do a meet!"

CRITICAL RULES:
- Separate the 3 paragraphs with \\n\\n in the JSON string.
- Address the person by their clean first name: "Hi {first_name},". Never address by titles like "Hi Dr," or "Hi Mr,".
- Use the clean, conversational company name "{clean_company}" (NEVER use formal suffixes like "Pvt. Ltd.", "Ltd", "Inc", "LLC").
- STRICTEST RULE - DO NOT MENTION PREVIOUS COMPANIES: If the lead recently changed companies or has older jobs in their experience timeline, you must NEVER mention, reference, or hint at their previous company. Treat them purely as a leader at {clean_company}.
- NEVER use words like: "imagine", "what if", "pleasure", "honored", "aspiring", "hope", "delve", "apologize", "sincerely", "opportunity", "passionate", "revolutionize", "synergy".
- Keep length around 120-140 words. Easy to read, authentic, and impactful.

Return ONLY a valid JSON object wrapped in ```json ... ``` tags:
{{
  "company_analysis": "Crisp 1-2 sentence breakdown of what {clean_company} builds and their market.",
  "identified_pain_point": "The specific bottleneck identified in Tech/AI, PM, Sales Funnels, or Growth Marketing.",
  "grounded_vision": "Concrete picture of scale/efficiency.",
  "drafted_dm": "The complete 3-paragraph direct message formatted with \\n\\n between paragraphs."
}}
"""


class GhostwriterAgent:
    def __init__(self):
        self.resume_link = os.getenv("RESUME_LINK", "").strip()
        if not self.resume_link or self.resume_link == "[ADD_YOUR_RESUME_LINK_HERE]":
            profile_path = DATA_DIR / "my_profile.json"
            if profile_path.exists():
                try:
                    with open(profile_path, "r", encoding="utf-8") as f:
                        prof = json.load(f)
                        self.resume_link = prof.get("resume_link", "").strip()
                except Exception:
                    pass
        if not self.resume_link:
            self.resume_link = "https://drive.google.com/drive/folders/14NkmTzo2gvSRtooHocBblueGXDqvQWFq"

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

    def draft_single_lead(
        self,
        lead: dict,
        profile: Optional[dict] = None,
        top_card_text: str = "",
        scraped_experience: str = ""
    ) -> dict:
        """
        Dedicated 1-by-1 deep-dive company analysis & DM drafting for a verified lead.
        Runs Gemini with model cascade and rotation.
        Returns a dict containing 'company_analysis', 'identified_pain_point', 'grounded_vision', and 'drafted_dm'.
        """
        name = lead.get("name", "Unknown")
        company = lead.get("company", "Unknown")
        role = lead.get("role", "Unknown")

        # Clean first name and company name
        first_name = clean_first_name(name)
        clean_comp = clean_company_name(company)

        # Cohort-specific opening logic with varied, high-agency hook options
        is_bt = self.is_big_tech(lead)
        if is_bt:
            cohort_p1_inst = (
                f"- For the hook opener, choose naturally between:\n"
                f"  Option 1: \"I've been following {clean_comp}'s work in [mention specific product area or team from their headline/experience], "
                f"but I am actually curious about [mention a specific operational or product trade-off in their area] "
                f"and what you guys are doing to handle this.\"\n"
                f"  Option 2: \"I've been tracking what your team at {clean_comp} is building around [specific product area], "
                f"and I'm really curious about how you balance [specific operational trade-off or challenge] at that scale.\""
            )
        else:
            cohort_p1_inst = (
                f"- For the hook opener, choose naturally among these 3 high-agency opening styles (DO NOT always use the same formula across leads):\n"
                f"  Style A (Potential & Curiosity): \"{clean_comp} has huge potential, but I am actually curious about [mention a specific, real operational pain point or challenge in their product/domain] "
                f"and what you guys are doing to handle this.\"\n"
                f"  Style B (Product Observation & Approach): \"I've been closely tracking what {clean_comp} is building, and I'm really curious about how your team approaches [mention a specific, real operational pain point or challenge in their product/domain] "
                f"and how you guys are tackling that.\"\n"
                f"  Style C (Execution Bottleneck): \"What {clean_comp} is building is super exciting, but one operational hurdle that stands out is [mention a specific, real operational pain point or challenge in their product/domain]—how is your team currently handling this?\""
            )

        prompt = DEEP_DIVE_RESEARCH_PROMPT.format(
            lead_name=name,
            first_name=first_name,
            lead_company=company,
            clean_company=clean_comp,
            lead_role=role,
            top_card_text=top_card_text.strip() if top_card_text else "Not available",
            scraped_experience=scraped_experience.strip() if scraped_experience else "Not available",
            cohort_p1_instruction=cohort_p1_inst,
            resume_link=self.resume_link,
        )

        data = {}
        try:
            from utils.gemini_client import generate_with_rotation
            model_name = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
            resp_text = generate_with_rotation(prompt, model=model_name)

            match = re.search(r"```json\s*([\s\S]+?)\s*```", resp_text)
            if match:
                data = json.loads(match.group(1))
            else:
                data = json.loads(resp_text.strip())
        except Exception as e:
            console.print(f"  [yellow]  ⚠ Gemini drafting error for {name}: {e}. Using grounded fallback.[/yellow]")
            # Deterministic opener rotation based on name hash
            h = abs(hash(name)) % 3
            if h == 0:
                p1_opener = f"{clean_comp} has huge potential, but I am actually curious about automating outbound pipeline and user activation and what you guys are doing to handle this."
            elif h == 1:
                p1_opener = f"I've been closely tracking what {clean_comp} is building, and I'm really curious about how your team approaches scaling outbound pipelines and activation loops."
            else:
                p1_opener = f"What {clean_comp} is building is super exciting, but one operational hurdle that stands out is automating outbound pipeline and user activation—how is your team currently handling this?"

            data = {
                "company_analysis": f"{clean_comp} platform operations.",
                "identified_pain_point": "Scaling automated outbound pipeline and user activation",
                "grounded_vision": "streamline operational efficiency and user growth",
                "drafted_dm": (
                    f"Hi {first_name},\n\n"
                    f"{p1_opener} "
                    f"See, {clean_comp} has the potential to streamline operational efficiency and user growth, and getting this right could really accelerate product adoption.\n\n"
                    f"I can actually help you guys achieve this. I am a 4th-year student at DTU (9.3 CGPA) and former AI PM Intern at NoBrokerHood, "
                    f"where I worked cross-functionally across engineering, product, and sales to build automated B2B engines capturing 25+ extra qualified leads a month, "
                    f"and optimized search algorithms to do 1.5x output within the same constraints. I also ranked 4th in NMG Labs' Agentic AI Hackathon. "
                    f"In fact, this message was researched and delivered by an autonomous system I built to test product execution live.\n\n"
                    f"Let's do a quick 12-min call where we can discuss this and see how it matches both of us. You can check my resume and get a quick brief about me here: {self.resume_link}\n\n"
                    f"Let me know a good time for us to do a meet!"
                )
            }

        # Deterministic enforcement: replace any hallucinated or typo'd Google Drive folder URL with exact verified resume_link
        dm = data.get("drafted_dm", "")
        if self.resume_link and self.resume_link != "[ADD_YOUR_RESUME_LINK_HERE]":
            dm = re.sub(r'https?://drive\.google\.com/drive/folders/[a-zA-Z0-9_-]+', self.resume_link, dm)
            data["drafted_dm"] = dm

        lead["connection_note"] = ""
        lead["note_length"] = 0
        lead["drafted_dm"] = dm
        lead["company_analysis"] = data.get("company_analysis", "")
        lead["identified_pain_point"] = data.get("identified_pain_point", "")
        lead["grounded_vision"] = data.get("grounded_vision", "")

        return data

    def run(self, leads: list, profile: dict, dry_run: bool = False) -> list:
        console.print("\n[bold cyan]━━━ Phase 2: Ghostwriter (1-by-1 Deep Dive) ━━━[/bold cyan]")
        instructions = self._load_instructions()
        console.print(f"[cyan]Prompt instructions v{instructions.get('version', 1)}[/cyan]")

        if not leads:
            return []

        # Check for leads that already have a drafted DM
        already_drafted = [l for l in leads if l.get("drafted_dm")]
        needs_drafting = [l for l in leads if not l.get("drafted_dm")]

        if not needs_drafting:
            console.print(f"[green]✓ All {len(leads)} leads already have drafted DMs. Skipping Gemini drafting.[/green]")
            return leads

        if already_drafted:
            console.print(f"[cyan]ℹ {len(already_drafted)}/{len(leads)} leads already have drafted DMs. Drafting remaining {len(needs_drafting)} leads 1-by-1...[/cyan]")
        else:
            console.print(f"[cyan]Drafting {len(needs_drafting)} leads 1-by-1 with deep-dive company & bottleneck audit...[/cyan]")

        enriched = []
        for idx, lead in enumerate(leads, 1):
            name = lead.get("name") or "Unknown"
            if lead.get("drafted_dm"):
                lead.setdefault("connection_note", "")
                lead.setdefault("note_length", 0)
                lead.setdefault("status", "queued")
                enriched.append(lead)
                continue

            console.print(f"  [{idx}/{len(leads)}] [cyan]▶ Deep-dive research for {name} @ {lead.get('company', '?')}...[/cyan]")
            self.draft_single_lead(lead, profile)
            pain_point = lead.get("identified_pain_point") or "Operational growth"
            console.print(f"  [green]  ✓ DM drafted targeting: {pain_point}[/green]")

            lead["status"] = "queued"
            enriched.append(lead)

            if dry_run:
                console.print(Panel(
                    f"[bold]{name}[/bold] @ {lead.get('company', '?')}\n\n"
                    f"[bold yellow]Identified Bottleneck:[/bold yellow] {lead.get('identified_pain_point')}\n"
                    f"[bold yellow]Company Analysis:[/bold yellow] {lead.get('company_analysis')}\n\n"
                    f"[bold yellow]DM:[/bold yellow]\n[green]{lead.get('drafted_dm')}[/green]",
                    title=f"Draft #{len(enriched)}", border_style="blue",
                ))

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
