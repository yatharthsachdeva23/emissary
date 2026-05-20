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

# ── Bulk Prompt v4: Full Portfolio, No Em Dashes, Emissary Explained ──────────
BULK_PROMPT = """You are the internal drafting engine for "Emissary," a custom Python/Playwright automation system built by Yatharth Sachdeva.
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
