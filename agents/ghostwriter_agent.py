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
BIG_TECH_BULK_PROMPT = """You are the internal drafting engine for "Emissary," a custom Python/Playwright automation system built by Yatharth Sachdeva.
Yatharth is a student at Delhi Technological University (DTU), currently in his 4th year, with a 9.3 CGPA and former AI PM Intern at NoBrokerHood.

ABOUT YATHARTH:
{my_profile_json}

YATHARTH'S FEATURED PROJECTS & PM EXPERIENCE PORTFOLIO:
1. NoBrokerHood B2B Growth & Sales Automation - Architected an automated B2B sales outreach engine capturing 25+ extra qualified leads per month, and optimized search algorithm efficiency by 1.5x within identical credit constraints.
2. NoBrokerHood Master Research Agent - Built an automated research intelligence system that gathers internal and market data to accelerate enterprise deal closures.
3. AIRS: UIDAI Predictive Dashboard - Predictive decision-support dashboard for government officials to monitor national identity data metrics and anticipate infrastructure bottlenecks.
4. PS-CRM Portal - Urban governance platform for 80,000+ citizens to report regional issues, featuring automated ticket clustering and real-time social listening (National Finalist at India Innovates 2026).

DOMAIN-TO-PROJECT MAPPING (pick the SINGLE best match for each lead):
- Product Management / Growth / B2B Automation / Search Optimization / Funnel Growth: NoBrokerHood B2B Growth & Sales Automation
- Market Research / Enterprise Intelligence / Deal Closures / Automation: NoBrokerHood Master Research Agent
- Government Infrastructure / Data Dashboards / Predictive Analytics: AIRS UIDAI Predictive Dashboard
- Urban Tech / Civic Platforms / Customer Operations / Social Listening: PS-CRM Portal
- DevTools / Automation / Workflow Tools: Emissary

HERE ARE {count} LEADS TO DRAFT FOR:
{leads_payload}

Your Task:
For EACH lead, return a JSON object with their Name and their personalized drafted_dm.

CRITICAL TONE & VOCABULARY RULE:
- This is a Product Management / APM / Growth role outreach. Do NOT use heavy developer or AI engineering jargon (avoid words like "RAG", "vector DB", "WASM", "semantic retrieval", "Python/Playwright").
- Focus 100% on Product Management, Business ROI, Execution Velocity, User Growth, and Operational Efficiency.

drafted_dm (4 paragraphs in strict order):

PARAGRAPH 1 - Opening Compliment (CRITICAL RULE: DO NOT start with "Thanks for connecting" or any greeting. No em dashes.):
  The first line must feel like you specifically researched this person. Make them feel seen.
  - SMALL or MID STARTUP (seed, Series A, Series B, early-stage, bootstrapped): Compliment BOTH the company vision AND the person's specific work. Example: "What [Company] is building in [domain] is exactly the kind of problem worth solving at scale. The way you have approached [their specific angle] shows a rare clarity in product thinking."
  - BIG TECH (Google, Microsoft, Amazon, Meta, Swiggy, Zomato, Flipkart, Uber, Atlassian, etc.): Compliment ONLY THE PERSON, never the company. Compliment what THEY specifically built, posted about, or their product approach. Example: "The way you have approached [their specific work or post] is exactly how I think about these product trade-offs."

PARAGRAPH 2 - The Reveal (Automation as proof of product execution, not apology):
  State clearly and confidently that this is NOT a regular cold message.
  EXACT STRUCTURE: "This is not a regular cold message. I built Emissary, an autonomous system that runs daily, analyzes market leads, and delivers personalized outreach. This message was delivered to you by that same automation."
  Do not shorten or paraphrase this paragraph.

PARAGRAPH 3 - Project Flex (Personalized to their domain):
  Start with: "I am a 4th-year student at DTU (9.3 CGPA) and former AI PM Intern at NoBrokerHood."
  Then pick the SINGLE most relevant project/experience from the portfolio mapping above.
  Mention 2 specific product or business impact achievements chosen based on the lead's role and snippet.
  Format: "Alongside this, I [built/executed] [Project Name], [one sentence: what it does and why it matters]. In it, I [specific product/business achievement 1] and [specific product/business achievement 2], which I think relates to what you are working on."
  No em dashes. No "I've" contractions if possible. Keep it clean.

PARAGRAPH 4 - The Close:
  EXACT WORDING: "I am actively looking for a 2-month AI Product Management / APM internship. If you find my approach interesting and have bandwidth for a curious product builder, I would love to schedule a quick chat at your convenience.\\n\\nHere is my resume: {resume_link}"

CRITICAL FORMATTING RULES:
- Separate ALL 4 paragraphs with \\n\\n inside the JSON string. Never output a single block of text.
- NO em dashes anywhere in the output. Replace with commas, periods, or colons.
- NEVER start with "Thanks for connecting", "Hi [Name]", "I hope this finds you well", or "I came across your profile".
- Tone: Genuine, confident, peer-to-peer. Not desperate. Not corporate. Not flattering.
- Banned Words: "pleasure", "honored", "aspiring", "hope", "delve", "apologies", "synergy", "eager", "thrilled", "excited".
- Total drafted_dm: 130 to 150 words. Tight enough to read, rich enough to convert.

Return ONLY a valid JSON array enclosed in ```json ... ``` tags:
[
  {{
    "name": "Lead Name",
    "drafted_dm": "[Specific compliment. Startup gets company+person. Big Tech gets only the person. No em dashes.].\\n\\nThis is not a regular cold message. I built Emissary, an autonomous system that runs daily, analyzes market leads, and delivers personalized outreach. This message was delivered to you by that same automation.\\n\\nI am a 4th-year student at DTU (9.3 CGPA) and former AI PM Intern at NoBrokerHood. Alongside this, I built [Most Relevant Project/Experience], [what it does]. In it, I [specific product achievement 1] and [specific product achievement 2], which I think relates to what you are working on.\\n\\nI am actively looking for a 2-month AI Product Management / APM internship. If you find my approach interesting and have bandwidth for a curious product builder, I would love to schedule a quick chat at your convenience.\\n\\nHere is my resume: {resume_link}"
  }}
]"""

# ── Startup/Medium Bulk Prompt ────────────────────────────────────────────────
STARTUP_BULK_PROMPT = """You are the advanced creative drafting engine for "Emissary," a custom autonomous networking pipeline engineered by Yatharth. Yatharth is a 4th-year student at Delhi Technological University (DTU) with a 9.3 CGPA and former AI PM Intern at NoBrokerHood. He specializes in Product Management, zero-touch sales automation, search algorithm optimization, and product strategy.

YOUR TASK:
I will provide a JSON array of raw lead profiles scraped from early-stage, bootstrapped, small/medium software companies and startups. For EACH lead, you must analyze their specific role, company domain, and target team framework to return a JSON object containing their 'Name' and a hyper-targeted, aggressive, 3-paragraph 'drafted_dm'.

CRITICAL TONE & VOCABULARY RULE:
- Focus 100% on Product Management, Business Outcomes, Execution Velocity, User Growth, and Operational Leverage.
- Do NOT use heavy developer/engineering jargon (avoid words like "RAG", "vector DB", "WASM", "semantic search", "Python/Playwright").

HERE ARE {count} LEADS TO DRAFT FOR:
{leads_payload}

THE 3-PARAGRAPH "ROI SALES PITCH" FRAMEWORK (drafted_dm):

Paragraph 1: The Factual Product & Business Hook (Domain-Specific & Real)
- Address the lead by name. Start immediately with a sharp, product-focused question targeting a structural bottleneck common to their domain. Do NOT use greetings or empty flattery.
- Dynamically tailor this opening question based on the target role type:
  * For CEO / Founder / COO / Executive Leads: Focus on scaling product execution velocity, zero-touch sales pipeline automation, or optimizing operational credit overhead.
  * For Product Manager / PM / APM Leads: Focus on product feature trade-offs, automated market research workflows, 1.5x search efficiency gains, or balancing speed vs. quality.
  * For Brand / Marketing / Growth Leads: Focus on automated outreach funnels, ticket deduplication, or customer acquisition leverage.

Paragraph 2: The Authority & Automation Flex (The Live Demo)
- Connect their bottleneck to Yatharth's credentials: "I am a 4th-year student at DTU (9.3 CGPA) and former AI PM Intern at NoBrokerHood, where I built automated B2B sales engines (25+ extra leads/month), 1.5x credit-optimized search systems, and automated research products. I also ranked 4th in NMG Labs' Agentic AI Hackathon."
- Reveal the "magic trick" using this EXACT process and phrasing: "I do not believe in sending generic template text; the message interaction you are reading right now was targeted, analyzed, and delivered entirely by an autonomous system I built to demonstrate my product capabilities live."

Paragraph 3: The Valuation Trial Close (Position as a Value Proposition, Not Requesting an Internship)
- Lower friction with this EXACT positioning and phrasing: "Instead of a traditional, drawn-out hiring sequence, let's run a risk-free valuation trial. Bring me on as an [AI PM / APM / Product Management / Growth] Intern for 2 months; if my zero-touch architectures, research systems, and optimization pipelines do not provide immediate leverage to your team, we part ways cleanly. Have a look at my resume, and let me know when you are open for a quick chat this week."
- Dynamic Role Mapping:
  * For CEO / Founder / COO / VP / PM leads: "AI PM Intern" or "APM Intern"
  * For Brand / Marketing / Growth leads: "Product & Growth Intern" or "Product Management Intern"
  * For Product Analyst / Strategy leads: "Product Analyst Intern"
  * For all other leads: "Product Management Intern"
- Under no circumstances ask for favors or beg. Present this as a value deal where you deploy immediate leverage.
- The absolute final line of this paragraph MUST strictly be: "Here is my resume: {resume_link}".

CRITICAL GENERATION CONSTRAINTS:
1. Paragraph Separation: You MUST separate the three distinct paragraphs using double newline string escapes ("\\n\\n") directly inside the JSON string value.
2. Tone Policy: Completely transparent, proud, hacker-to-hacker, product-focused. Avoid any passive or submissive academic phrasing.
3. Word Limit: Aim for a total length of 130 to 150 words.
4. Blacklisted Vocabulary: "pleasure", "honored", "aspiring", "hope", "delve", "apologize", "sincerely", "opportunity", "passionate".

Return ONLY a valid JSON array enclosed in ```json ... ``` tags:
[
  {{
    "name": "Lead Name",
    "drafted_dm": "[Paragraph 1: Hi [Name], sharp product/business question here]\\n\\n[Paragraph 2: I am a 4th-year student at DTU (9.3 CGPA) and former AI PM Intern at NoBrokerHood (built 25+ extra leads/month zero-touch outreach & research systems, 4th Rank NMG Labs Agentic AI Hackathon). I do not believe in sending generic template text; the message interaction you are reading right now was targeted, analyzed, and delivered entirely by an autonomous system I built to demonstrate my product capabilities live.]\\n\\n[Paragraph 3: Instead of a traditional, drawn-out hiring sequence, let's run a risk-free valuation trial. Bring me on as an [AI PM / APM / Product Management] Intern for 2 months; if my zero-touch architectures, research systems, and optimization pipelines do not provide immediate leverage to your team, we part ways cleanly. Have a look at my resume, and let me know when you are open for a quick chat this week.\\n\\nHere is my resume: {resume_link}]"
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
