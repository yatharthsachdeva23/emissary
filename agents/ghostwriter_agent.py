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

YATHARTH'S FEATURED PROJECTS & AI PM EXPERIENCE PORTFOLIO:
1. NoBrokerHood LinkedIn Automation (Automated B2B Sales Outreach Engine) - Architected a zero-human-touch sales outreach engine capturing 25+ extra qualified leads per month, and engineered a specialized search optimization algorithm delivering 1.5x output coverage within identical API credit constraints.
2. NoBrokerHood Master Research Agent (RAG System & Vector DB Pipeline) - Engineered a RAG-based Master Research Agent utilizing Vector DBs and semantic retrieval pipelines for automated internal and external intelligence gathering to accelerate enterprise deal closures.
3. AIRS: UIDAI Predictive Dashboard - Predictive decision-support ecosystem for government officials to monitor national identity data metrics and anticipate infrastructure loads before impacting citizen services.
4. PS-CRM Portal - Urban governance platform for 80,000+ citizens to report regional issues via voice/text, auto-categorized and routed to government departments. National Finalist at India Innovates 2026. Built AI ticket clustering and real-time social listening engine.

DOMAIN-TO-PROJECT MAPPING (pick the SINGLE best match for each lead):
- AI PM / B2B Outreach / Sales Automation / Search Optimization / Growth / Funnel Optimization: NoBrokerHood LinkedIn Automation
- RAG Systems / Vector DBs / Enterprise Intelligence / Knowledge Graph / Semantic Retrieval: NoBrokerHood Master Research Agent
- Government Data / Identity Systems / Predictive Analytics / Infrastructure Load Dashboards: AIRS UIDAI Predictive Dashboard
- GovTech / Urban Governance / NLP / AI Ticket Clustering / Social Listening / Public Sector: PS-CRM Portal
- DevTools / LinkedIn automation / Outreach tools / Scraping / Workflow automation: Emissary

HERE ARE {count} LEADS TO DRAFT FOR:
{leads_payload}

Your Task:
For EACH lead, return a JSON object with their Name, a 280-character drafted_note, and the final drafted_dm.

PIECE 1 - drafted_note (LinkedIn Connection Hook):
A 280-character hook sent WITH the connection request.
- Sound like a fellow product builder or engineer, NOT a student asking for a job.
- Structure: [Specific observation about their company's tech/product or their work] -> [Yatharth's most relevant project/internship flex] -> [Soft, confident close]
- No URLs, no "Hi [Name]", no resume links. STRICTLY under 280 characters.
- No em dashes. Use commas or periods to separate thoughts.

PIECE 2 - drafted_dm (4 paragraphs in strict order):

PARAGRAPH 1 - Opening Compliment (CRITICAL RULE: DO NOT start with "Thanks for connecting" or any greeting. No em dashes.):
  The first line must feel like you specifically researched this person. Make them feel seen.
  - SMALL or MID STARTUP (seed, Series A, Series B, early-stage, bootstrapped): Compliment BOTH the company vision AND the person's specific work. Example: "What [Company] is building in [domain] is exactly the kind of problem worth solving at scale. The way you have approached [their specific angle] shows a rare clarity in product thinking."
  - BIG TECH (Google, Microsoft, Amazon, Meta, Swiggy, Zomato, Flipkart, Uber, Atlassian, etc.): Compliment ONLY THE PERSON, never the company. Big Tech engineers feel nothing when you praise their employer. Compliment what THEY specifically built, posted about, or their engineering approach. Example: "The way you have approached [their specific work or post] is exactly how I think about these problems."

PARAGRAPH 2 - The Reveal (Automation as proof of work, not apology):
  State clearly and confidently that this is NOT a regular cold message.
  EXACT STRUCTURE: "This is not a regular cold message. I built Emissary, a Python/Playwright system that runs daily, scrapes LinkedIn leads using Google search, scores them with Gemini AI, and autonomously sends connection requests and follow-up DMs. This message was delivered to you by that same automation."
  Do not shorten or paraphrase this paragraph.

PARAGRAPH 3 - Project Flex (Personalized to their domain):
  Start with: "I am a 4th-year student at DTU (9.3 CGPA) and former AI PM Intern at NoBrokerHood."
  Then pick the SINGLE most relevant project/experience from the portfolio mapping above.
  Mention 2 specific technical or product impact achievements chosen based on the lead's role and snippet.
  Format: "Alongside this, I [built/executed] [Project or Experience Name], [one sentence: what it does and why it matters]. In it, I [specific technical/product detail 1] and [specific technical/product detail 2], which I think relates to what you are working on."
  No em dashes. No "I've" contractions if possible. Keep it clean.

PARAGRAPH 4 - The Close:
  EXACT WORDING: "I am actively looking for a 2-month AI Product Management / APM internship. If you find my approach interesting and have bandwidth for a curious product builder, I would love to schedule a quick chat at your convenience.\\n\\nHere is my resume: {resume_link}"

CRITICAL FORMATTING RULES:
- Separate ALL 4 paragraphs with \\n\\n inside the JSON string. Never output a single block of text.
- NO em dashes anywhere in the output. Replace with commas, periods, or colons.
- NEVER start with "Thanks for connecting", "Hi [Name]", "I hope this finds you well", or "I came across your profile".
- Tone: Genuine, confident, peer-to-peer. Not desperate. Not corporate. Not flattering.
- Banned Words: "pleasure", "honored", "aspiring", "hope", "delve", "apologies", "synergy", "eager", "thrilled", "excited".
- Total drafted_dm: 130 to 160 words. Tight enough to read, rich enough to convert.

Return ONLY a valid JSON array enclosed in ```json ... ``` tags:
[
  {{
    "name": "Lead Name",
    "drafted_note": "The 280-char connection hook (no em dashes, no URLs)...",
    "drafted_dm": "[Specific compliment. Startup gets company+person. Big Tech gets only the person. No em dashes.].\\n\\nThis is not a regular cold message. I built Emissary, a Python/Playwright system that runs daily, scrapes LinkedIn leads using Google search, scores them with Gemini AI, and autonomously sends connection requests and follow-up DMs. This message was delivered to you by that same automation.\\n\\nI am a 4th-year student at DTU (9.3 CGPA) and former AI PM Intern at NoBrokerHood. Alongside this, I built [Most Relevant Project/Experience], [what it does]. In it, I [specific technical detail 1] and [specific technical detail 2], which I think relates to what you are working on.\\n\\nI am actively looking for a 2-month AI Product Management / APM internship. If you find my approach interesting and have bandwidth for a curious product builder, I would love to schedule a quick chat at your convenience.\\n\\nHere is my resume: {resume_link}"
  }}
]"""

# ── Startup/Medium Bulk Prompt ────────────────────────────────────────────────
STARTUP_BULK_PROMPT = """You are the advanced creative drafting engine for "Emissary," a custom autonomous networking pipeline engineered by Yatharth. Yatharth is a 4th-year student at Delhi Technological University (DTU) with a 9.3 CGPA and former AI PM Intern at NoBrokerHood. He specializes in AI Product Management, zero-touch sales automation, search algorithm optimization, and multi-agent systems.

YOUR TASK:
I will provide a JSON array of raw lead profiles scraped from early-stage, bootstrapped, small/medium software companies and startups. For EACH lead, you must analyze their specific role, company domain, and target team framework to return a JSON object containing their 'Name', an internal 'drafted_note', and a hyper-targeted, aggressive, 3-paragraph 'drafted_dm'.

HERE ARE {count} LEADS TO DRAFT FOR:
{leads_payload}

THE 280-CHARACTER LinkedIn Connection Hook (drafted_note):
For EACH lead, generate a concise, professional 280-character connection hook (drafted_note) sent WITH the connection request.
- Sound like a fellow product builder or engineer, NOT a student asking for a job.
- Structure: [Specific observation about their company's tech/product or their work] -> [Yatharth's most relevant AI PM internship/project flex] -> [Soft, confident close]
- No URLs, no "Hi [Name]", no resume links. STRICTLY under 280 characters.
- No em dashes. Use commas or periods to separate thoughts.

THE 3-PARAGRAPH "ROI SALES PITCH" FRAMEWORK (drafted_dm):

Paragraph 1: The Factual Engineering/Product Hook (Domain-Specific & Real)
- Address the lead by name. Start immediately with a sharp, technically accurate, and highly relevant product or system design question targeting a structural bottleneck common to their specific domain. Do NOT use greetings (like "Hope you are well") or empty flattery.
- Dynamically tailor this opening question based on the target role type, and expand it with 1-2 lines detailing a real product trade-off:
  * For AI PM / Product Management Leads: Focus on zero-human-touch sales automation, RAG vector retrieval latency, or balancing feature velocity with API credit optimization (e.g. 1.5x efficiency gains).
  * For Product Analyst / Growth Leads: Focus on funnel optimization, severity multipliers for ticket deduplication, or user retention metrics.
  * For Technical PM / Engineering Leads: Focus on multi-agent synchronization, search query latency (reducing from N*N to N+N), or API cost constraints.

Paragraph 2: The Authority & Automation Flex (The Live Demo)
- Connect their bottleneck to Yatharth's credentials: "I am a 4th-year student at DTU (9.3 CGPA) and former AI PM Intern at NoBrokerHood, where I built automated B2B sales engines (25+ extra leads/month), 1.5x credit-optimized search algorithms, and RAG research agents. I also ranked 4th in NMG Labs' Agentic AI Hackathon."
- Reveal the "magic trick" using this EXACT process and phrasing: "I do not believe in sending generic template text; the message interaction you are reading right now was targeted, analyzed, and delivered entirely by an autonomous multi-agent pipeline I built to demonstrate my system design capabilities live."

Paragraph 3: The Valuation Trial Close (Position as a Value Proposition, Not Requesting an Internship)
- Lower friction with this EXACT positioning and phrasing: "Instead of a traditional, drawn-out hiring sequence, let's run a risk-free valuation trial. Bring me on as an [AI PM / APM / Product Management] Intern for 2 months; if my zero-touch architectures, RAG research agents, and optimization pipelines do not provide immediate leverage to your team, we part ways cleanly. Have a look at my resume, and let me know when you are open for a quick chat this week."
- Dynamic Role Mapping (select the exact match):
  * For AI PM / Product Management leads: "AI PM Intern"
  * For Product Analyst / Growth leads: "Product Analyst Intern"
  * For APM / Product Strategy leads: "APM (Associate Product Manager) Intern"
  * For Technical Product Manager leads: "Technical PM Intern"
  * For all other leads: "Product Management Intern"
- Under no circumstances ask for favors or beg. Present this as a value deal where you deploy immediate leverage.
- The absolute final line of this paragraph MUST strictly be: "Here is my resume: {resume_link}".

CRITICAL GENERATION CONSTRAINTS:
1. Paragraph Separation: You MUST separate the three distinct paragraphs using double newline string escapes ("\\n\\n") directly inside the JSON string value so it formats perfectly in the LinkedIn message overlay.
2. Tone Policy: Completely transparent, proud, hacker-to-hacker, and entirely focused on what Yatharth can execute *for* them. Avoid any passive or submissive academic phrasing.
3. No Artificial Metrics: Anchor value in real engineering/product outcomes (25+ extra leads/month via zero-touch automation, 1.5x search efficiency on equal credits, RAG vector agents).
4. Word Limit: Aim for a total length of 140 to 150 words. Ensure it is not too short (under 130 words) or too long (over 160 words).
5. Blacklisted Vocabulary: Under no circumstances use any of these words: "pleasure", "honored", "aspiring", "hope", "delve", "apologize", "sincerely", "opportunity", "passionate".

Return ONLY a valid JSON array enclosed in ```json ... ``` tags:
[
  {{
    "name": "Lead Name",
    "drafted_note": "A 280-char connection hook (no em dashes, no URLs)...",
    "drafted_dm": "[Paragraph 1: Hi [Name], sharp product/engineering question here]\\n\\n[Paragraph 2: I am a 4th-year student at DTU (9.3 CGPA) and former AI PM Intern at NoBrokerHood (built 25+ extra leads/month zero-touch outreach & RAG agents, 4th Rank NMG Labs Agentic AI Hackathon). I do not believe in sending generic template text; the message interaction you are reading right now was targeted, analyzed, and delivered entirely by an autonomous multi-agent pipeline I built to demonstrate my system design capabilities live.]\\n\\n[Paragraph 3: Instead of a traditional, drawn-out hiring sequence, let's run a risk-free valuation trial. Bring me on as an [AI PM / APM / Product Management] Intern for 2 months; if my zero-touch architectures, RAG research agents, and optimization pipelines do not provide immediate leverage to your team, we part ways cleanly. Have a look at my resume, and let me know when you are open for a quick chat this week.\\n\\nHere is my resume: {resume_link}]"
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
