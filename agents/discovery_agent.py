"""
Discovery Agent — Emissary
Uses Serper.dev (Google Search API) to find relevant LinkedIn leads every day.
Scores and filters them using Gemini, deduplicates against the CRM.
"""

import json
import os
import re
import requests
from datetime import datetime
from pathlib import Path
from typing import Optional

from google import genai
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

load_dotenv()
console = Console()

DATA_DIR = Path(__file__).parent.parent / "data"
LEADS_PATH = DATA_DIR / "leads_today.json"
SEEN_PATH = DATA_DIR / "seen_profiles.json"
SERPER_URL = "https://google.serper.dev/search"

# Target: full-time employees only — founders, CTOs, EMs, tech leads.
# Deliberately avoid any intern/student results.
SEARCH_QUERIES = [
    # People who are hiring or building — with decision-making power
    'site:linkedin.com/in "Co-Founder" OR "CTO" "AI" OR "ML" "startup" "India" -intern -student',
    'site:linkedin.com/in "Engineering Manager" OR "VP Engineering" "Bangalore" OR "Gurugram" OR "Delhi" -intern',
    'site:linkedin.com/in "Tech Lead" OR "Lead Engineer" "AI" OR "backend" "India" "startup" -intern',
    'site:linkedin.com/in "Founder" OR "CEO" "SaaS" OR "developer tools" "India" "2025" -intern',
    'site:linkedin.com/in "Head of Engineering" OR "Principal Engineer" "India" "2025" -intern',
    # Posts by decision-makers about building/hiring
    'site:linkedin.com/posts "we are hiring" "software engineer" "India" "2025" -intern -internship',
    'site:linkedin.com/posts "building" "multi-agent" OR "LLM" OR "RAG" "2025" "India"',
    'site:linkedin.com/posts "looking for" "engineer" OR "developer" "startup" "India" "2025" -intern',
    # YC and funded startups
    'site:linkedin.com/in "YC" OR "Y Combinator" "India" "founder" OR "engineer" "2025"',
    'site:linkedin.com/in "series A" OR "series B" "CTO" OR "VP" "India" "2025" -intern',
]

SCORING_PROMPT = """You are a lead-scoring assistant for an internship outreach tool.

Student Profile:
{profile_summary}

Score each lead from 0.0 to 1.0.

PRIORITIZE (high scores):
- Founders, Co-Founders, CTOs, VPs, Engineering Managers, Tech Leads, Principal Engineers
- Small/mid-size startups (not FAANG/BigTech)
- Active in AI, backend, security, developer tools, SaaS
- Located in India (Bangalore, Delhi, Gurugram, Hyderabad, Mumbai, remote)
- Recently posting about building, hiring, or shipping product

DISCARD immediately (score=0.0) if:
- Role contains: intern, internship, student, trainee, fresher, apprentice
- Company is: Google, Microsoft, Amazon, Meta, Apple, Infosys, TCS, Wipro, Accenture, Cognizant
- The person IS a student or intern themselves (not a decision-maker)
- The post is just a student sharing their own internship search
- No clear LinkedIn profile URL (e.g., it's a company page or job posting)

Return ONLY a JSON array in ```json ... ``` tags:
[{{"name":null,"company":null,"role":null,"linkedin_url":"url","snippet":"snippet","score":0.0,"discard_reason":null,"source_query":"query"}}]

Raw leads ({count} items):
{leads_json}
"""


class DiscoveryAgent:
    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY") or "dummy_key_for_testing"
        self.client = genai.Client(api_key=api_key)
        self.serper_key = os.getenv("SERPER_API_KEY", "")
        self.daily_limit = int(os.getenv("DAILY_SEND_LIMIT", "20"))

    def _serper_search(self, query: str, num: int = 5) -> list:
        if not self.serper_key or self.serper_key.startswith("your_"):
            return []
        headers = {"X-API-KEY": self.serper_key, "Content-Type": "application/json"}
        try:
            resp = requests.post(SERPER_URL, headers=headers,
                                 json={"q": query, "num": num, "gl": "in", "hl": "en"}, timeout=15)
            resp.raise_for_status()
            return [{"url": i.get("link",""), "title": i.get("title",""),
                     "snippet": i.get("snippet",""), "date": i.get("date",""),
                     "source_query": query}
                    for i in resp.json().get("organic", [])]
        except Exception as e:
            console.print(f"[red]Serper error: {e}[/red]")
            return []

    def _extract_json(self, text: str) -> Optional[list]:
        match = re.search(r"```json\s*([\s\S]+?)\s*```", text)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return None
        return None

    def _load_seen_profiles(self) -> set:
        if SEEN_PATH.exists():
            with open(SEEN_PATH) as f:
                return set(json.load(f))
        return set()

    def _save_seen_profiles(self, seen: set) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        with open(SEEN_PATH, "w") as f:
            json.dump(list(seen), f, indent=2)

    def _load_sheet_contacted(self) -> set:
        try:
            from utils.sheets import SheetsClient
            return SheetsClient().get_all_profile_urls()
        except Exception:
            return set()

    def gather_raw_leads(self, dry_run: bool = False) -> list:
        if dry_run:
            console.print("[yellow]DRY RUN: Using mock leads[/yellow]")
            return self._mock_leads()
        all_results = []
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as p:
            task = p.add_task("Searching via Google Dorks...", total=len(SEARCH_QUERIES))
            for query in SEARCH_QUERIES:
                all_results.extend(self._serper_search(query))
                p.advance(task)
        filtered = [r for r in all_results if "linkedin.com" in r.get("url","") or "internshala.com" in r.get("url","")]
        console.print(f"[green]✓ {len(filtered)} raw results[/green]")
        return filtered

    def score_and_filter(self, raw_leads: list, profile: dict) -> list:
        if not raw_leads:
            return []
        seen_urls, unique = set(), []
        for lead in raw_leads:
            if lead.get("url","") not in seen_urls:
                seen_urls.add(lead["url"])
                unique.append(lead)
        already_seen = self._load_seen_profiles() | self._load_sheet_contacted()
        fresh = [l for l in unique if l.get("url","") not in already_seen]
        console.print(f"[cyan]{len(fresh)} fresh leads to score[/cyan]")
        if not fresh:
            console.print("[yellow]No new leads today.[/yellow]")
            return []

        profile_summary = (
            f"{profile.get('name')}, {profile.get('year')} @ {profile.get('college')}, {profile.get('branch')}\n"
            f"Skills: {', '.join(profile.get('skills', []))}\n"
            f"Targets: {', '.join(profile.get('target_roles', []))} | "
            f"{', '.join(profile.get('target_industries', []))} | "
            f"{', '.join(profile.get('geography', []))}"
        )
        prompt = SCORING_PROMPT.format(
            profile_summary=profile_summary, count=len(fresh),
            leads_json=json.dumps(fresh, indent=2)
        )
        import time
        max_retries = 3
        scored = []
        with Progress(SpinnerColumn(), TextColumn("Gemini bulk scoring..."), console=console) as p:
            p.add_task("", total=None)
            for attempt in range(max_retries):
                try:
                    resp = self.client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
                    scored = self._extract_json(resp.text) or []
                    break
                except Exception as e:
                    if attempt < max_retries - 1:
                        wait_time = 10 * (attempt + 1)
                        console.print(f"\n[yellow]⚠ Gemini API overloaded. Retrying in {wait_time}s...[/yellow]")
                        time.sleep(wait_time)
                    else:
                        console.print(f"\n[red]❌ Gemini API failed: {e}[/red]")

        if not scored:
            console.print("[red]Could not parse any Gemini scoring[/red]")
            return []

        # Hard post-filter: drop any lead who IS an intern/student themselves
        INTERN_ROLE_KEYWORDS = [
            "intern", "internship", "student", "trainee", "fresher",
            "apprentice", "undergraduate", "postgraduate",
        ]
        def _is_intern(lead: dict) -> bool:
            role = (lead.get("role") or "").lower()
            name = (lead.get("name") or "").lower()
            return any(kw in role or kw in name for kw in INTERN_ROLE_KEYWORDS)

        pre_filter = [l for l in scored if l.get("score", 0) >= 0.4]
        valid = sorted([l for l in pre_filter if not _is_intern(l)],
                       key=lambda x: x.get("score", 0), reverse=True)
        intern_dropped = len(pre_filter) - len(valid)
        if intern_dropped:
            console.print(f"[dim]  Dropped {intern_dropped} intern/student profiles[/dim]")
        top = valid[:self.daily_limit]

        # Normalize all URLs to https://www.linkedin.com (some come as bare linkedin.com from Google)
        for lead in top:
            raw_url = lead.get("url", "")
            if raw_url.startswith("https://linkedin.com"):
                lead["url"] = "https://www." + raw_url[len("https://"):]
            elif raw_url.startswith("http://linkedin.com"):
                lead["url"] = "https://www." + raw_url[len("http://"):]
            elif raw_url.startswith("http://"):
                lead["url"] = "https://" + raw_url[len("http://"):]
            # Alias for messenger agent (it reads 'linkedin_url' key)
            lead.setdefault("linkedin_url", lead.get("url", ""))

        console.print(f"[green]✓ {len(scored)} scored → {len(valid)} qualified → {len(top)} selected[/green]")


        table = Table(title="Today's Leads", header_style="bold cyan")
        table.add_column("#", width=3); table.add_column("Name", width=22)
        table.add_column("Role", width=22); table.add_column("Company", width=18); table.add_column("Score", width=7)
        for i, l in enumerate(top, 1):
            s = l.get("score", 0)
            c = "green" if s >= 0.7 else "yellow" if s >= 0.5 else "white"
            table.add_row(str(i), l.get("name") or "?", l.get("role") or "—",
                          l.get("company") or "—", f"[{c}]{s:.2f}[/{c}]")
        console.print(table)
        return top

    def save_leads(self, leads: list) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        with open(LEADS_PATH, "w", encoding="utf-8") as f:
            json.dump({"date": datetime.now().isoformat(), "count": len(leads), "leads": leads},
                      f, indent=2, ensure_ascii=False)
        console.print(f"[green]✓ Saved {len(leads)} leads[/green]")

    def mark_contacted(self, profile_url: str) -> None:
        seen = self._load_seen_profiles()
        seen.add(profile_url)
        self._save_seen_profiles(seen)

    def run(self, profile: dict, dry_run: bool = False) -> list:
        console.print("\n[bold cyan]━━━ Phase 1: Discovery Engine ━━━[/bold cyan]")
        raw = self.gather_raw_leads(dry_run=dry_run)
        leads = self.score_and_filter(raw, profile)
        if leads:
            self.save_leads(leads)
        return leads

    def _mock_leads(self) -> list:
        return [
            {"url": "https://www.linkedin.com/in/rahul-mock-cto/",
             "title": "Rahul Verma — Co-Founder & CTO at NeuralStack AI",
             "snippet": "Just shipped multi-agent orchestration layer. Looking for smart interns. DM open.",
             "date": "3 days ago", "source_query": "mock"},
            {"url": "https://www.linkedin.com/in/priya-tech-lead/",
             "title": "Priya Sharma — Engineering Lead at Sprinklr",
             "snippet": "Building LLM-powered customer intelligence. Team growing.",
             "date": "1 week ago", "source_query": "mock"},
            {"url": "https://www.linkedin.com/in/arjun-fintech-founder/",
             "title": "Arjun Singh — Founder at PayZen (Series A)",
             "snippet": "Scaling fraud detection with ML. 50-person team Bangalore.",
             "date": "5 days ago", "source_query": "mock"},
        ]
