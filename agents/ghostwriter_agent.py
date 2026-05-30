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

# ── Big Tech Bulk Prompt (Original Strategy maintained exactly as-is) ──────────
BIG_TECH_BULK_PROMPT = """You are the internal drafting engine for "Emissary," a custom Python/Playwright automation system built by Yatharth Sachdeva.
Yatharth is a B.Tech Information Technology student at Delhi Technological University (DTU), currently in his 3rd year, with a 9.29 CGPA.

ABOUT YATHARTH:
{my_profile_json}

YATHARTH'S PROJECT PORTFOLIO (use ONLY these projects, never invent others):
1. SentinelMesh - Zero-trust federated hosting platform for multi-agent AI systems using containerized WASM sandboxes. Implemented a "mutual blindness" protocol so users can run sensitive data through third-party AI agents without the developer ever seeing it.
2. PS-CRM Portal - Urban governance platform for 80,000+ citizens to report regional issues via voice or text, auto-categorized and routed to government departments. National Finalist at India Innovates 2026. Built AI ticket clustering and real-time social listening engine.
3. NFPC Behavioral Fraud System - High-performance mule account detection model using LightGBM and Polars, 90%+ accuracy. Engineered Social Graph Entropy and Drain Velocity features to catch sophisticated fraud patterns. Won 1st place at IIT Delhi among 400+ teams.
4. Kinex App - AI-driven fitness assistant (React Native, Supabase) that generates personalized daily workout plans and muscle analysis. Built a localized diet and workout generator that adjusts based on real-time muscle fatigue analysis.
5. AIRS: UIDAI Predictive Dashboard - Predictive decision-support ecosystem for government officials to monitor national identity data metrics and anticipate infrastructure loads before they impact citizen services.
6. Paytm "Digital Udhaar" - Digitizes the traditional informal "khata" credit system. Customers initiate credit via QR scan, merchants get a digital dashboard to manage dues. Bridges traditional credit habits and modern digital payments.
7. SkinAI - Computer vision skincare assistant using CNNs. Users upload a photo to get a science-backed skincare routine (CTTMS). Maps skin analysis results to an ingredient-level database to filter out irritants.
8. NeuroTrack X - Cognitive health diagnostics tool using Azure AI and NLP. Monitors longitudinal speech patterns to detect subtle linguistic shifts for early detection of cognitive health risks years before traditional diagnosis.
9. Grant-Flow - Automated scholarship/grant verification system using multi-agent verification logic to cross-reference application data against institutional databases, reducing manual fraud and processing time.
10. Global Catalogue Registry - Centralized catalog system for the ONDC network. Optimized search complexity from N x N to N + N and reduced API response latency from 200ms to 140ms using Redis caching.
11. Social Media Virality Predictor - ML pipeline analyzing 5000+ posts to forecast engagement levels. Identifies primary factors driving viral engagement using categorical encoding and scikit-learn.
12. Airlines Management System - Enterprise flight booking and passenger record system in Python/SQL for 5 airlines, 100+ users. Complex scheduling and ticket history tracking.
13. HealthSync - Conversational healthcare AI using NLP and speech-to-text to transcribe doctor-patient dialogues into structured medical records and automated prescriptions.
14. Document Analyser - Intelligent document processing engine using OCR and text summarization models to extract key insights from large-scale structured or unstructured documents.
15. National Health Portal - Full-stack digital platform integrating traditional Ayurvedic practices with modern medical science, with a unified database schema cross-referencing remedies with clinical data.
16. Emissary - This LinkedIn automation system itself. It runs daily, scrapes leads from Google using targeted search queries, scores them with Gemini AI, then autonomously navigates LinkedIn to send connection requests and follow-up DMs at scale.

DOMAIN-TO-PROJECT MAPPING (pick the SINGLE best match for each lead):
- Agentic AI / Multi-agent / Zero-trust / Cybersecurity / Security infrastructure: SentinelMesh
- GovTech / Civic tech / Urban infrastructure / NLP / Social platforms / Public sector: PS-CRM Portal
- FinTech / Fraud detection / Banking / Predictive analytics / Risk systems: NFPC Behavioral Fraud System
- Fitness / Consumer health apps / Mobile development / Wellness tech: Kinex App
- Government data / Identity systems / Predictive dashboards / Data engineering: AIRS UIDAI Predictive Dashboard
- Payments / Consumer FinTech / Product management / Digital commerce / UX: Paytm Digital Udhaar
- Healthcare AI / Computer vision / Dermatology / Consumer health / Personalization: SkinAI
- Cognitive health / Speech AI / Neurology / Geriatric care / Azure AI: NeuroTrack X
- Process automation / EdTech / Institutional verification / Document workflows: Grant-Flow
- E-commerce / Backend systems / API optimization / ONDC / Marketplace infra: Global Catalogue Registry
- Social media / Content analytics / Marketing tech / Creator tools / ML: Social Media Virality Predictor
- Enterprise software / ERP / Database systems / B2B SaaS: Airlines Management System
- Healthcare NLP / Conversational AI / Medical records / Clinical tech: HealthSync
- Document AI / OCR / Information extraction / Data engineering: Document Analyser
- Full-stack web / Holistic health / Wellness platforms / Ayurveda / Healthcare: National Health Portal
- DevTools / LinkedIn automation / Outreach tools / Scraping / Workflow automation: Emissary

HERE ARE {count} LEADS TO DRAFT FOR:
{leads_payload}

Your Task:
For EACH lead, return a JSON object with their Name, a 280-character drafted_note, and the final drafted_dm.

PIECE 1 - drafted_note (LinkedIn Connection Hook):
A 280-character hook sent WITH the connection request.
- Sound like a fellow engineer, NOT a student asking for a job.
- Structure: [Specific observation about their company's tech or their work] -> [Yatharth's most relevant project] -> [Soft, confident close]
- No URLs, no "Hi [Name]", no resume links. STRICTLY under 280 characters.
- No em dashes. Use commas or periods to separate thoughts.

PIECE 2 - drafted_dm (4 paragraphs in strict order):

PARAGRAPH 1 - Opening Compliment (CRITICAL RULE: DO NOT start with "Thanks for connecting" or any greeting. No em dashes.):
  The first line must feel like you specifically researched this person. Make them feel seen.
  - SMALL or MID STARTUP (seed, Series A, Series B, early-stage): Compliment BOTH the company vision AND the person's specific work. Example: "What [Company] is building in [domain] is exactly the kind of problem worth solving at the infrastructure level. The way you have approached [their specific angle] shows a rare clarity in product thinking."
  - BIG TECH (Google, Microsoft, Amazon, Meta, Swiggy, Zomato, Flipkart, Uber, Atlassian, etc.): Compliment ONLY THE PERSON, never the company. Big Tech engineers feel nothing when you praise their employer. Compliment what THEY specifically built, posted about, or their engineering approach. Example: "The way you have approached [their specific work or post] is exactly how I think about these problems."

PARAGRAPH 2 - The Reveal (Automation as proof of work, not apology):
  State clearly and confidently that this is NOT a regular cold message.
  EXACT STRUCTURE: "This is not a regular cold message. I built Emissary, a Python/Playwright system that runs daily, scrapes LinkedIn leads using Google search, scores them with Gemini AI, and autonomously sends connection requests and follow-up DMs. This message was delivered to you by that same automation."
  Do not shorten or paraphrase this paragraph.

PARAGRAPH 3 - Project Flex (Personalized to their domain):
  Start with: "I am an IT student at DTU, currently in my 3rd year (9.29 CGPA)."
  Then pick the SINGLE most relevant project from the portfolio mapping above.
  Mention 2 specific technical things built in that project, chosen based on the lead's role and snippet.
  Format: "Alongside this, I built [Project Name], [one sentence: what it does and why it matters]. In it, I [specific technical thing 1] and [specific technical thing 2], which I think relates to what you are working on."
  No em dashes. No "I've" contractions if possible. Keep it clean.

PARAGRAPH 4 - The Close:
  EXACT WORDING: "I am actively looking for a 2-month internship. If you find my approach interesting and have bandwidth for a curious problem solver, I would love to schedule a quick chat at your convenience.\\n\\nHere is my resume: {resume_link}"

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
    "drafted_dm": "[Specific compliment. Startup gets company+person. Big Tech gets only the person. No em dashes.].\\n\\nThis is not a regular cold message. I built Emissary, a Python/Playwright system that runs daily, scrapes LinkedIn leads using Google search, scores them with Gemini AI, and autonomously sends connection requests and follow-up DMs. This message was delivered to you by that same automation.\\n\\nI am an IT student at DTU, currently in my 3rd year (9.29 CGPA). Alongside this, I built [Most Relevant Project from the mapping], [what it does]. In it, I [specific technical thing 1] and [specific technical thing 2], which I think relates to what you are working on.\\n\\nI am actively looking for a 2-month internship. If you find my approach interesting and have bandwidth for a curious problem solver, I would love to schedule a quick chat at your convenience.\\n\\nHere is my resume: {resume_link}"
  }}
]"""

# ── Startup/Medium Bulk Prompt (Aggressive ROI/Sales Strategy) ────────────────
STARTUP_BULK_PROMPT = """You are the advanced creative drafting engine for "Emissary," a custom autonomous networking pipeline engineered by Yatharth. Yatharth is a 3rd-year B.Tech Information Technology student at Delhi Technological University (DTU) with a 9.29 CGPA. He is a highly technical systems engineer specializing in high-concurrency backends, zero-trust security infrastructure, and multi-disciplinary systems architecture.

YOUR TASK:
I will provide a JSON array of raw lead profiles scraped from small/medium software companies and early-stage startups. For EACH lead, you must analyze their specific role, company domain, and target team framework to return a JSON object containing their 'Name', an internal 'drafted_note', and a hyper-targeted, aggressive, 3-paragraph 'drafted_dm'.

HERE ARE {count} LEADS TO DRAFT FOR:
{leads_payload}

THE 280-CHARACTER LinkedIn Connection Hook (drafted_note):
For EACH lead, generate a concise, professional 280-character connection hook (drafted_note) sent WITH the connection request.
- Sound like a fellow engineer, NOT a student asking for a job.
- Structure: [Specific observation about their company's tech or their work] -> [Yatharth's most relevant project] -> [Soft, confident close]
- No URLs, no "Hi [Name]", no resume links. STRICTLY under 280 characters.
- No em dashes. Use commas or periods to separate thoughts.

THE 3-PARAGRAPH "ROI SALES PITCH" FRAMEWORK (drafted_dm):

Paragraph 1: The Factual Engineering Hook (Domain-Specific & Real)
- Address the lead by name. Start immediately with a sharp, technically accurate, and highly relevant engineering or product question targeting a structural bottleneck common to their specific domain. Do NOT use greetings (like "Hope you are well") or empty flattery.
- Dynamically tailor this opening question based on the target role type, and expand it with 1-2 lines detailing a real system design trade-off or challenge to demonstrate deep technical understanding:
  * For AI/ML Intern Leads: Focus on multi-agent synchronization, unauthorized data exfiltration risks during third-party integrations, or runtime virtualization latency. Elaborate on the complexity of running untrusted third-party agent code securely.
  * For SDE / Backend Intern Leads: Focus on database query latency, nested O(N*N) looping strains, pipeline backpressure, or caching layer efficiency. Elaborate on the difficulty of maintaining low-latency lookups under high write-concurrency.
  * For Full Stack Intern Leads: Focus on rapid end-to-end prototyping, product shipping bottlenecks, or synchronizing relational data in real time. Elaborate on shipping high-fidelity features cleanly from database schema to UI representation without technical debt.
  * For Product Management Intern Leads: Focus on reducing user onboarding friction, avoiding feature creep, or building predictive decision-support dashboards. Elaborate on balancing immediate user value with backend scale feasibility.
  * For Forward Deployment Engineer Leads: Focus on rapid prototyping under extreme constraints, deployment failures in unrefined client environments, or custom integrations. Elaborate on handling legacy enterprise data pipelines with robust parser design.
  * For Corporate Outreach & Leadership Leads: Focus on scaling developer velocity, reducing infrastructure costs, or managing complex integration timelines.

Paragraph 2: The Authority & Automation Flex (The Live Demo)
- Connect their engineering bottleneck to Yatharth's credentials: "I am a 3rd-year IT student at DTU (9.29 CGPA) who has built 15+ end-to-end systems from scratch, including [Mention a highly relevant project of Yatharth's that solves the Paragraph 1 issue, with a brief sentence on its technical design]."
- Reveal the "magic trick" using this EXACT process and phrasing: "I do not believe in sending generic template text; the message interaction you are reading right now was targeted, analyzed, and delivered entirely by an autonomous multi-agent pipeline I built to demonstrate my system design capabilities live." (Focus on the result and process of live demoing capabilities rather than tool technicalities).

Paragraph 3: The Valuation Trial Close (Position as a Value Proposition, Not Requesting an Internship)
- Lower friction with this EXACT positioning and phrasing: "Instead of a traditional, drawn-out hiring sequence, let's run a risk-free valuation trial. Bring me on as an [AI / SDE / Full-Stack / Product Management] Intern this summer; if my zero-trust architectures and pipeline microservices do not provide immediate technical leverage to your runtime environment, we part ways cleanly. Have a look at my resume, and let me know when you are open for a quick chat this week."
- Dynamic Role Mapping (select the exact match):
  * For AI/ML leads: "AI Intern"
  * For SDE/Backend leads: "SDE Intern"
  * For Full Stack leads: "Full-Stack Intern"
  * For Product Management leads: "Product Management Intern"
  * For Leadership/Forward Deployment/others: "Software Engineering Intern"
- Under no circumstances ask for a "2-month internship" directly or beg/request a favor. Present this as a value deal where you deploy leverage.
- The absolute final line of this paragraph MUST strictly be: "Here is my resume: {resume_link}".

CRITICAL GENERATION CONSTRAINTS:
1. Paragraph Separation: You MUST separate the three distinct paragraphs using double newline string escapes ("\\n\\n") directly inside the JSON string value so it formats perfectly in the LinkedIn message overlay.
2. Tone Policy: Completely transparent, proud, hacker-to-hacker, and entirely focused on what Yatharth can execute *for* them. Avoid any passive or submissive academic phrasing.
3. No Artificial Metrics: Do not invent fake statistical outcomes (e.g., "I will save you exactly 42% on AWS"). Anchor the value entirely in systems engineering methodologies (indexing query paths, data isolation, caching layers, predictive visualization).
4. Word Limit: Aim for a total length of 140 to 150 words. Ensure it is not too short (under 130 words) or too long (over 160 words). This word count provides enough room to build real technical depth and intrigue.
5. Blacklisted Vocabulary: Under no circumstances use any of these words: "pleasure", "honored", "aspiring", "hope", "delve", "apologize", "sincerely", "opportunity", "passionate".

Return ONLY a valid JSON array enclosed in ```json ... ``` tags:
[
  {{
    "name": "Lead Name",
    "drafted_note": "A 280-char connection hook (no em dashes, no URLs)...",
    "drafted_dm": "[Paragraph 1: Hi [Name], sharp technical/engineering question here]\\n\\n[Paragraph 2: I am a 3rd-year IT student at DTU (9.29 CGPA) who has built 15+ end-to-end systems from scratch, including [Project]. I do not believe in sending generic template text; the message interaction you are reading right now was targeted, analyzed, and delivered entirely by an autonomous multi-agent pipeline I built to demonstrate my system design capabilities live.]\\n\\n[Paragraph 3: Instead of a traditional, drawn-out hiring sequence, let's run a risk-free valuation trial. Bring me on as an [AI / SDE / Full-Stack / Product Management] Intern this summer; if my zero-trust architectures and pipeline microservices do not provide immediate technical leverage to your runtime environment, we part ways cleanly. Have a look at my resume, and let me know when you are open for a quick chat this week.\\n\\nHere is my resume: {resume_link}]"
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
        """Calls Gemini API with automatic key rotation on quota errors."""
        drafted = []
        with Progress(SpinnerColumn(), TextColumn(f"Gemini bulk drafting ({cohort_name})..."), console=console) as p:
            p.add_task("", total=None)
            # Try up to 4 keys (one rotation attempt per key)
            for key_attempt in range(4):
                try:
                    client, key_label = get_client_with_rotation()
                    for attempt in range(max_retries_per_key):
                        try:
                            resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
                            drafted = self._extract_json(resp.text) or []
                            return drafted
                        except Exception as e:
                            err_str = str(e)
                            # Quota exceeded → rotate key
                            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                                console.print(f"\n[yellow]⚠ Gemini {key_label} quota exceeded. Rotating key...[/yellow]")
                            # Transient overload → wait and retry same key
                            elif "503" in err_str or "overload" in err_str.lower() or "unavailable" in err_str.lower():
                                m = re.search(r"retry in (\d+(?:\.\d+)?)s", err_str)
                                wait_time = math.ceil(float(m.group(1))) + 2 if m else min(15 * (2 ** attempt), 90)
                                console.print(f"\n[yellow]⚠ Gemini {key_label} overloaded (attempt {attempt+1}/{max_retries_per_key}). Retrying in {wait_time}s...[/yellow]")
                                time.sleep(wait_time)
                                continue
                            else:
                                console.print(f"\n[red]❌ Gemini API failed: {e}[/red]")
                                return []
                            # 429 → mark exhausted and break to try next key
                            mark_key_exhausted()
                            break
                except RuntimeError as e:
                    console.print(f"\n[red]❌ {e}[/red]")
                    return []
        console.print(f"\n[red]❌ All Gemini keys exhausted for cohort '{cohort_name}'.[/red]")
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
