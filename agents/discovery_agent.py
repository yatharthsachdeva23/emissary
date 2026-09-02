"""
Discovery Agent — Emissary
Hybrid lead discovery engine using three layers:
  Layer 1: Static fresh-signal Google dorks with date filters (tbs=qdr:m/w)
  Layer 2: LinkedIn Jobs → Company Extraction → Leader Profile Lookup
  Layer 3: Dynamic Gemini-generated dorks (rotates daily via AI)
Scores and filters results using Gemini, deduplicates against the CRM.
"""

import json
import os
import re
import time
import math
import requests
from datetime import datetime
from pathlib import Path
from typing import Optional

from google import genai
from dotenv import load_dotenv
from utils.gemini_client import get_client_with_rotation, mark_key_exhausted
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

load_dotenv()
console = Console()

DATA_DIR = Path(__file__).parent.parent / "data"
LEADS_PATH = DATA_DIR / "leads_today.json"
SEEN_PATH = DATA_DIR / "seen_profiles.json"
SERPER_URL = "https://google.serper.dev/search"

# ─── Combinatorial Keyword Matrix for Dynamic Query Generation ───────────────
ROLE_SETS = [
    '"Founder" OR "Co-Founder" OR "CEO"',
    '"Head of Product" OR "VP Product" OR "CPO"',
    '"Product Manager" OR "APM" OR "Associate Product Manager"',
    '"Lead Product Manager" OR "Group Product Manager" OR "Staff PM"',
    '"Product Lead" OR "Growth Product Manager" OR "Growth PM"',
    '"Co-Founder" OR "COO" OR "VP Growth"',
    '"Founding Product Manager" OR "Founding PM" OR "Product Manager"',
    '"VP Product" OR "Director of Product" OR "Head of Product"',
    '"Head of Growth" OR "VP Growth" OR "Growth Lead"',
    '"Product Operations Manager" OR "Product Lead" OR "PM"',
]

STARTUP_THEMES = [
    '"Startup" OR "Early Stage"',
    '"Seed" OR "Series A"',
    '"Series B" OR "Scaleup"',
    '"Bootstrapped" OR "Fast Growing"',
    '"B2B SaaS" OR "SaaS"',
    '"Fintech" OR "Payments"',
    '"D2C" OR "E-Commerce"',
    '"AI Product" OR "Generative AI"',
    '"Consumer Tech" OR "Mobile App"',
    '"Healthtech" OR "MedTech"',
    '"EdTech" OR "Learning Tech"',
    '"Logistics Tech" OR "Supply Chain"',
    '"PropTech" OR "Real Estate Tech"',
    '"Product Strategy" OR "PLG"',
    '"Building in Public" OR "Venture Backed"',
    '"User Research" OR "Product Analytics"',
]

LOCATION_SETS = [
    '"India"',
    '"Bangalore"',
    '"Bengaluru"',
    '"Delhi NCR"',
    '"Gurgaon"',
    '"Gurugram"',
    '"Noida"',
    '"Mumbai"',
    '"Pune"',
    '"Hyderabad"',
]

POST_HIRING_SIGNALS = [
    "we are hiring",
    "hiring",
    "looking for",
    "join our product team",
    "seeking APM",
    "open product role",
]


def generate_random_static_queries(count_profiles: int = 10, count_posts: int = 4) -> list[tuple[str, Optional[str]]]:
    """
    Generates a dynamic, randomized set of Google search dorks on every run.
    Randomly samples and combines roles, startup themes, and tech hubs,
    guaranteeing fresh, diverse lead results on every execution without exhausting search credits.
    """
    import random
    queries = []
    used_combos = set()
    attempts = 0

    # 1. Randomized Profile Queries (Full index depth)
    while len(queries) < count_profiles and attempts < 100:
        attempts += 1
        r = random.choice(ROLE_SETS)
        t = random.choice(STARTUP_THEMES)
        l = random.choice(LOCATION_SETS)
        key = f"{r}_{t}_{l}"
        if key in used_combos:
            continue
        used_combos.add(key)
        q = f'site:linkedin.com/in {r} {t} {l} -intern -student'
        queries.append((q, None))

    # 2. Randomized Post Queries (Fresh hiring signals from past 7 days)
    used_post_combos = set()
    post_attempts = 0
    while len(queries) < (count_profiles + count_posts) and post_attempts < 50:
        post_attempts += 1
        sig = random.choice(POST_HIRING_SIGNALS)
        role = random.choice(['"Product Manager" OR "APM"', '"Head of Product" OR "Product Lead"', '"Growth PM" OR "Founding PM"'])
        theme = random.choice(['"Startup" OR "SaaS"', '"Fintech" OR "D2C"', '"AI" OR "Tech"', '"Early Stage"'])
        loc = random.choice(['"India"', '"Bangalore" OR "Delhi NCR"', '"Gurgaon" OR "Noida"', '"Mumbai" OR "Remote"'])
        
        post_key = f"{sig}_{role}_{theme}_{loc}"
        if post_key in used_post_combos:
            continue
        used_post_combos.add(post_key)
        q_post = f'site:linkedin.com/posts "{sig}" {role} {theme} {loc} -intern -student'
        queries.append((q_post, "qdr:w"))

    return queries


# ─── Layer 2: LinkedIn Jobs → Leaders (Tightened to 2-3 queries max) ─────────
JOB_SOURCING_QUERIES = [
    'site:linkedin.com/jobs/view ("Product Manager" OR "APM") ("Startup" OR "SaaS" OR "Fintech") "India"',
    'site:linkedin.com/jobs/view ("Growth Manager" OR "Head of Product") ("Startup" OR "Early Stage") "India"',
]

COMPANY_EXTRACTION_PROMPT = """You are a parsing assistant. Extract unique company names from the following LinkedIn job posting titles and snippets.

Raw postings:
{postings}

Rules:
- Only extract companies that appear to be operating or hiring in India.
- Skip global staffing agencies (e.g., Jobgether, Huptech HR, Converse Placement).
- Skip very large multinational corporations (Google, Amazon, Microsoft, Apple, Meta, Flipkart, Swiggy, Zomato, Walmart, Uber).
- Focus strictly on startups, scale-ups, bootstrapped startups, and growth-stage tech companies.
- Return at most 5 unique startup company names.

Return ONLY a JSON array of strings:
```json
["company1", "company2", "company3", "company4", "company5"]
```"""

# ─── Layer 3: Dynamic AI-generated dorks (12 queries) ─────────────────────────
DYNAMIC_DORK_PROMPT = """You are a lead-generation assistant for an internship outreach tool targeting Product Management, Brand, and Growth leadership roles at Startups.

Candidate Profile:
{profile_summary}

Generate exactly 12 unique Google search dorks to find high-value LinkedIn profiles of executive decision-makers at STARTUPS and HIGH-GROWTH COMPANIES in India who could hire this candidate for a Product Management / APM / AI PM / Growth internship.

Rules:
- Mix profile queries (site:linkedin.com/in) and post queries (site:linkedin.com/posts).
- Target key decision-maker roles: "Founder", "Co-Founder", "CEO", "Head of Product", "VP Product", "Product Manager", "APM", "Growth Product Manager", "Product Lead".
- Target Startup & domain keywords: "Startup", "Seed", "Series A", "Bootstrapped", "B2B SaaS", "Fintech", "D2C", "AI Product", "Consumer Tech", "Early Stage".
- Target Indian locations: "India", "Bangalore", "Bengaluru", "Delhi NCR", "Gurgaon", "Noida", "Mumbai", "Pune", "Hyderabad".
- Always exclude interns and students using -intern -student.
- CRITICAL SYNTAX RULE: NEVER add company exclusions like -google, -microsoft, or -linkedin to the query string! Doing so breaks Google search. Focus on positive keywords like "Startup" OR "SaaS" OR "Fintech".
- Each query must be clean, valid Google search syntax.

Return ONLY a JSON array of 12 query strings:
```json
["query1", "query2", ...]
```"""

# ─── Scoring Prompt ───────────────────────────────────────────────────────────
SCORING_PROMPT = """You are a lead-scoring assistant for a Product Management internship outreach tool.

Student Profile:
{profile_summary}

Score each lead from 0.0 to 1.0 based on how valuable they are as a potential manager/founder for a Product Management (PM / APM / AI PM / Growth) internship.

TARGET AUDIENCE & PRIORITIES (High Scores: 0.85 - 1.0):
- Founders, Co-Founders, CEOs, COOs, VPs of Product, VPs of Growth, VPs of Marketing, Heads of Product, Heads of Brand, Heads of Growth, Product Managers, APMs, Product Leads, Brand Managers, Growth Managers.
- STARTUPS & GROWTH COMPANIES ONLY (Seed, Series A/B, Bootstrapped, D2C, B2B SaaS, Fintech, E-Commerce, Consumer Tech, EdTech, GovTech, Retail) located in India (Delhi NCR, Gurgaon, Noida, Bangalore, Mumbai, Hyderabad, Pune, Remote).
- Profiles showing active hiring or building in product, brand, or business growth.

CRITICAL DISCARD RULES (score = 0.0):
1. BIG TECH & GIANT CORPORATES (DISCARD IMMEDIATELY): Discard anyone working at Google, Microsoft, Amazon, Meta, Apple, Netflix, Uber, Walmart, Salesforce, Swiggy, Zomato, Flipkart, Adobe, LinkedIn, Facebook, Atlassian, TCS, Infosys, Wipro, Cognizant, Accenture, Sabre, Cvent, Coupa.
2. MISSING / UNKNOWN COMPANY OR ROLE (DISCARD IMMEDIATELY): If the person's company or role is unknown, missing, null, or empty string, DISCARD IT (score = 0.0). EVERY output lead MUST have an explicit, named company and title extracted from the title/snippet.
3. INTERNS & STUDENTS (DISCARD IMMEDIATELY): Discard anyone whose role or title contains: intern, internship, student, trainee, fresher, apprentice, undergraduate.
4. PURE TECH / NON-PM ROLES: Discard pure software engineers, SDEs, QA testers, or devops engineers who have no product, brand, growth, or executive management responsibilities.
5. NON-INDIA: Discard anyone located outside of India.

Return ONLY a JSON array of objects wrapped in ```json ... ``` tags:
[
  {{
    "name": "Full Name extracted from title",
    "company": "Exact Startup Company Name extracted from snippet/title",
    "role": "Exact Role/Title (e.g. Founder & CEO, Head of Product, APM)",
    "linkedin_url": "url",
    "snippet": "snippet",
    "score": 0.95,
    "discard_reason": null,
    "source_query": "query"
  }}
]

Raw leads ({count} items):
{leads_json}
"""


class DiscoveryAgent:
    def __init__(self):
        self.serper_key = os.getenv("SERPER_API_KEY", "")
        self.daily_limit = int(os.getenv("DAILY_SEND_LIMIT", "50"))

    # ─── Core Search ──────────────────────────────────────────────────────────

    def _serper_search(self, query: str, num: int = 10, tbs: Optional[str] = None) -> list:
        if not self.serper_key or self.serper_key.startswith("your_"):
            return []
        headers = {"X-API-KEY": self.serper_key, "Content-Type": "application/json"}
        payload = {"q": query, "num": num, "gl": "in", "hl": "en"}
        if tbs:
            payload["tbs"] = tbs
        # Try request with up to 3 retries on transient connection timeouts
        for attempt in range(3):
            try:
                resp = requests.post(SERPER_URL, headers=headers, json=payload, timeout=30)
                if resp.status_code == 400:
                    try:
                        err_json = resp.json()
                        err_msg = err_json.get("message", "")
                        if "credit" in err_msg.lower():
                            console.print("[bold red]🚨 Serper API Error: Out of credits! Please refill your Serper account or replace the SERPER_API_KEY in your .env file.[/bold red]")
                            raise Exception("Serper API: Out of credits. Please update your Serper key.")
                    except ValueError:
                        pass
                resp.raise_for_status()
                return [{"url": i.get("link", ""), "title": i.get("title", ""),
                         "snippet": i.get("snippet", ""), "date": i.get("date", ""),
                         "source_query": query}
                        for i in resp.json().get("organic", [])]
            except Exception as e:
                # If we raised the custom out of credits exception, propagate it up
                if "Out of credits" in str(e):
                    raise e
                if attempt < 2:
                    console.print(f"[yellow]⚠ Serper query failed on attempt {attempt + 1}/3: {e}. Retrying in 3s...[/yellow]")
                    time.sleep(3)
                else:
                    console.print(f"[red]Serper error: {e}[/red]")
                    return []

    def _gemini_call(self, prompt: str, label: str = "Gemini") -> Optional[str]:
        """Single Gemini call with automatic round-robin rotation on any errors."""
        try:
            from utils.gemini_client import generate_with_rotation
            return generate_with_rotation(prompt, model="gemini-2.5-flash")
        except Exception as e:
            console.print(f"[red]❌ {label} failed: {e}[/red]")
            return None

    def _extract_json(self, text: str) -> Optional[list]:
        if not text:
            return None
        match = re.search(r"```json\s*([\s\S]+?)\s*```", text)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return None
        return None

    # ─── Data Helpers ─────────────────────────────────────────────────────────

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

    def _normalize_linkedin_url(self, url: str) -> str:
        """Normalise any LinkedIn URL to a canonical /in/ profile URL."""
        url = url.split("?")[0]
        # Job listing → not a profile; skip
        if "/jobs/view/" in url:
            return ""
        # Post URL → extract the author's /in/ username
        if "/posts/" in url:
            try:
                parts = url.split("/posts/")[1]
                username = parts.split("_")[0]
                if username:
                    return f"https://www.linkedin.com/in/{username}/"
            except Exception:
                pass
        # Normalise subdomain  (in.linkedin.com, ca.linkedin.com → www.linkedin.com)
        url = re.sub(r"https?://[a-z]{2,3}\.linkedin\.com", "https://www.linkedin.com", url)
        if not url.startswith("https://"):
            url = "https://" + url.lstrip("http://")
        return url

    # ─── Layer 1: Combinatorial Random Queries ─────────────────────────────────

    def _gather_static_leads(self, progress, task) -> list:
        results = []
        # Generate fresh randomized queries for this run
        dynamic_queries = generate_random_static_queries(count_profiles=10, count_posts=4)
        progress.update(task, total=len(dynamic_queries), description="Layer 1: Running randomized startup dorks...")

        for query, tbs in dynamic_queries:
            results.extend(self._serper_search(query, tbs=tbs))
            progress.advance(task)

        console.print(f"[dim]  Layer 1: {len(results)} raw results from {len(dynamic_queries)} dynamic keyword combinations[/dim]")
        return results

    # ─── Layer 2: Jobs → Companies → Leaders ──────────────────────────────────

    def _gather_job_based_leads(self, progress, task) -> list:
        # Step 2a: Fetch job postings from the past week
        job_results = []
        progress.update(task, description="Layer 2: Fetching job postings...")
        for query in JOB_SOURCING_QUERIES:
            job_results.extend(self._serper_search(query, tbs="qdr:w"))
            progress.advance(task)

        if not job_results:
            console.print("[dim]  Layer 2: No job postings found. Skipping.[/dim]")
            return []

        # Step 2b: Extract company names using Gemini
        postings_summary = json.dumps(
            [{"title": j.get("title", ""), "snippet": j.get("snippet", "")} for j in job_results],
            indent=2
        )
        progress.update(task, description="Layer 2: Extracting companies via Gemini...")
        raw_text = self._gemini_call(
            COMPANY_EXTRACTION_PROMPT.format(postings=postings_summary),
            label="Company Extraction"
        )
        companies = self._extract_json(raw_text or "") if raw_text else None

        if not companies or not isinstance(companies, list):
            # Regex fallback to extract company names from quotes if JSON markdown wrapper failed
            matches = re.findall(r'"([^"]+)"', raw_text or "")
            if matches:
                companies = [m.strip() for m in matches if len(m.strip()) > 2 and m.lower() not in ("json", "company1", "company2", "company3", "company4", "company5")]

        if not companies or not isinstance(companies, list):
            console.print("[dim]  Layer 2: Could not extract companies. Skipping.[/dim]")
            return []

        # Deduplicate and cap to 12 companies
        companies = list(dict.fromkeys(c for c in companies if isinstance(c, str)))[:12]
        console.print(f"[dim]  Layer 2: Targeting {len(companies)} companies: {', '.join(companies)}[/dim]")

        # Step 2c: Search for leaders at each company
        leader_results = []
        progress.update(task, total=progress._tasks[task].total + len(companies),
                        description="Layer 2: Searching for leaders...")
        for company in companies:
            q = (f'site:linkedin.com/in ("Founder" OR "Co-Founder" OR "CEO" OR "Head of Product" OR "VP Product" OR "Product Manager") "{company}" "India" -intern')
            leader_results.extend(self._serper_search(q))
            progress.advance(task)

        console.print(f"[dim]  Layer 2: {len(leader_results)} leader profiles found[/dim]")
        return leader_results

    # ─── Layer 3: Dynamic AI-Generated Dorks ──────────────────────────────────

    def _gather_dynamic_leads(self, profile: dict, progress, task) -> list:
        profile_summary = (
            f"{profile.get('name')}, {profile.get('year')} @ {profile.get('college')}, "
            f"{profile.get('branch')}\n"
            f"Skills: {', '.join(profile.get('skills', []))}\n"
            f"Targets: {', '.join(profile.get('target_roles', []))} | "
            f"{', '.join(profile.get('target_industries', []))} | "
            f"{', '.join(profile.get('geography', []))}"
        )

        progress.update(task, description="Layer 3: Generating dynamic dorks via Gemini...")
        raw_text = self._gemini_call(
            DYNAMIC_DORK_PROMPT.format(profile_summary=profile_summary),
            label="Dynamic Dork Generation"
        )
        dorks = self._extract_json(raw_text or "") if raw_text else None

        if not dorks or not isinstance(dorks, list):
            # Regex fallback
            matches = re.findall(r'"(site:linkedin\.com/[^"]+)"', raw_text or "")
            if matches:
                dorks = matches

        if not dorks or not isinstance(dorks, list):
            console.print("[dim]  Layer 3: Could not generate dorks. Skipping.[/dim]")
            return []

        # Sanitise: must be strings, cap at 12, remove negative exclusions that break Google search
        clean_dorks = []
        for d in dorks:
            if isinstance(d, str) and "site:linkedin.com" in d:
                # Strip accidental -linkedin or -google negative operator chains
                d_clean = re.sub(r'-(?:linkedin|google|microsoft|amazon|meta|apple|netflix|uber|walmart|salesforce|swiggy|zomato|flipkart|adobe)\b', '', d, flags=re.IGNORECASE)
                d_clean = " ".join(d_clean.split()).strip()
                clean_dorks.append(d_clean)
        dorks = clean_dorks[:12]
        console.print(f"[dim]  Layer 3: Running {len(dorks)} dynamic queries[/dim]")

        results = []
        progress.update(task, total=progress._tasks[task].total + len(dorks),
                        description="Layer 3: Running dynamic queries...")
        for dork in dorks:
            # Profile dorks get past-month freshness; post dorks get past-week
            tbs = "qdr:w" if "linkedin.com/posts" in dork else "qdr:m"
            results.extend(self._serper_search(dork, tbs=tbs))
            progress.advance(task)

        console.print(f"[dim]  Layer 3: {len(results)} raw results[/dim]")
        return results

    # ─── Gather + Filter ──────────────────────────────────────────────────────

    def gather_raw_leads(self, profile: Optional[dict] = None, dry_run: bool = False) -> list:
        if dry_run:
            console.print("[yellow]DRY RUN: Using mock leads[/yellow]")
            return self._mock_leads()

        all_results = []
        # Estimate total progress steps: static + job queries + dynamic (adjusts dynamically)
        estimated_total = (
            len(STATIC_PROFILE_QUERIES) + len(STATIC_POST_QUERIES) +
            len(JOB_SOURCING_QUERIES)
        )
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as prog:
            task = prog.add_task("Discovery Engine starting...", total=estimated_total)

            # Layer 1
            all_results.extend(self._gather_static_leads(prog, task))

            # Layer 2 (jobs → companies → leaders)
            all_results.extend(self._gather_job_based_leads(prog, task))

            # Layer 3 (dynamic dorks) — only if profile provided
            if profile:
                all_results.extend(self._gather_dynamic_leads(profile, prog, task))

        # Filter to only LinkedIn profile URLs; skip job listing pages
        filtered = []
        for r in all_results:
            url = r.get("url", "")
            if "linkedin.com" not in url:
                continue
            if "/jobs/view/" in url or "/company/" in url:
                continue
            normalized = self._normalize_linkedin_url(url)
            if normalized:
                r["url"] = normalized
                filtered.append(r)

        console.print(f"[green]✓ {len(filtered)} raw LinkedIn leads collected (all 3 layers)[/green]")
        return filtered

    # ─── Score & Filter ───────────────────────────────────────────────────────

    def _parse_follower_count(self, text: str) -> int:
        """Parse follower/connection count from snippet text."""
        match = re.search(r'([\d\.,]+)\s*([kkMm])?\s*(?:followers|connections)', text, re.IGNORECASE)
        if match:
            raw_num = match.group(1).replace(',', '')
            suffix  = (match.group(2) or '').lower()
            try:
                num = float(raw_num)
            except ValueError:
                return -1
            if suffix == 'k':
                return int(num * 1_000)
            elif suffix == 'm':
                return int(num * 1_000_000)
            return int(num)
        if '500+ connections' in text:
            return 500
        return -1

    def _follower_bonus(self, followers: int) -> float:
        """Log-normal bell-curve bonus peaking at 10 K followers."""
        if followers <= 0:
            return 0.0
        PEAK_LOG  = math.log10(10_000)   # 4.0
        SIGMA     = 1.0                  # 1 order of magnitude width
        MAX_BONUS = 0.15
        x_log = math.log10(max(followers, 1))
        bonus = MAX_BONUS * math.exp(-((x_log - PEAK_LOG) ** 2) / (2 * SIGMA ** 2))
        return round(bonus, 3)

    def _heuristic_score(self, lead: dict) -> float:
        """Keyword-based fallback scorer used when Gemini is unavailable."""
        title   = (lead.get('title')   or '').lower()
        snippet = (lead.get('snippet') or '').lower()
        text    = title + ' ' + snippet

        HIGH_ROLE = ['ceo', 'founder', 'cofounder', 'co-founder', 'coo', 'vp product',
                     'vp growth', 'vp marketing', 'head of product', 'head of growth',
                     'head of brand', 'chief product officer', 'director of product',
                     'director of marketing', 'director of growth', 'cpo']
        MED_ROLE  = ['product manager', 'lead product manager', 'group product manager',
                     'product lead', 'brand manager', 'marketing manager', 'growth manager',
                     'product analyst', 'growth pm', 'product operations manager', 'apm',
                     'associate product manager', 'product strategy']
        GOOD_TECH = ['product', 'brand', 'building', 'business', 'growth', 'funnel',
                     'ai product management', 'b2b', 'saas', 'user research',
                     'product strategy', 'roadmap', 'growth marketing']
        HIRING_SIG = ['hiring', 'we are hiring', 'looking for', 'join us', 'open role']
        DISCARD    = ['intern', 'student', 'fresher', 'trainee', 'apprentice', 'undergraduate']
        NON_INDIA  = ['usa', 'san francisco', 'new york', 'london', 'uk', 'canada',
                      'europe', 'australia', 'germany', 'singapore', 'dubai']
        PREFERRED_CITIES = ['delhi', 'bengaluru', 'bangalore', 'hyderabad', 'pune', 'mumbai', 'gurgaon', 'noida']
        BIG_TECH   = ['google', 'microsoft', 'amazon', 'apple', 'meta', 'uber',
                      'stripe', 'netflix', 'adobe', 'salesforce', 'swiggy', 'zomato',
                      'flipkart', 'cvent', 'sabre', 'coupa', 'linkedin', 'facebook',
                      'walmart', 'atlassian', 'tcs', 'infosys', 'wipro', 'cognizant', 'accenture']

        # Discard Big Tech, non-India, or intern/student leads immediately
        if any(kw in text for kw in BIG_TECH):
            return 0.0
        if any(kw in text for kw in DISCARD):
            return 0.0
        if any(kw in text for kw in NON_INDIA):
            return 0.0

        score = 0.35  # base
        if any(kw in text for kw in HIGH_ROLE):
            score += 0.40
        elif any(kw in text for kw in MED_ROLE):
            score += 0.20
        if any(kw in text for kw in GOOD_TECH):
            score += 0.15
        if any(kw in text for kw in HIRING_SIG):
            score += 0.10

        follower_count = self._parse_follower_count(text)
        if follower_count > 0:
            score += self._follower_bonus(follower_count)

        if any(kw in text for kw in PREFERRED_CITIES):
            score += 0.15

        return round(min(max(score, 0.0), 1.0), 2)

    def score_and_filter(self, raw_leads: list, profile: dict) -> list:
        if not raw_leads:
            return []

        # Deduplicate by URL within this batch
        seen_urls, unique = set(), []
        for lead in raw_leads:
            url = lead.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique.append(lead)

        # Remove profiles already seen or contacted
        already_seen = self._load_seen_profiles() | self._load_sheet_contacted()
        fresh = [l for l in unique if l.get("url", "") not in already_seen]
        console.print(f"[cyan]{len(fresh)} fresh leads to score "
                      f"(from {len(unique)} unique, {len(already_seen)} already seen)[/cyan]")

        if not fresh:
            console.print("[yellow]No new leads today — all sources exhausted.[/yellow]")
            return []

        # Pass ALL fresh leads directly to Gemini AI (chunked into 40-lead batches)
        candidates = fresh
        console.print(
            f"[cyan]  Passing ALL {len(candidates)} fresh leads directly to Gemini AI for full evaluation...[/cyan]"
        )

        profile_summary = (
            f"{profile.get('name')}, {profile.get('year')} @ {profile.get('college')}, "
            f"{profile.get('branch')}\n"
            f"Skills: {', '.join(profile.get('skills', []))}\n"
            f"Targets: {', '.join(profile.get('target_roles', []))} | "
            f"{', '.join(profile.get('target_industries', []))} | "
            f"{', '.join(profile.get('geography', []))}"
        )

        # ── Chunked Gemini scoring (40 leads per call to stay under token limits) ──
        CHUNK_SIZE = 40
        chunks = [candidates[i:i + CHUNK_SIZE] for i in range(0, len(candidates), CHUNK_SIZE)]
        scored = []
        gemini_ok = False

        with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as prog:
            score_task = prog.add_task(
                f"Gemini scoring ({len(chunks)} batch{'es' if len(chunks) > 1 else ''})...",
                total=len(chunks)
            )
            for idx, chunk in enumerate(chunks, 1):
                prog.update(score_task,
                            description=f"Gemini scoring batch {idx}/{len(chunks)}...")
                prompt = SCORING_PROMPT.format(
                    profile_summary=profile_summary,
                    count=len(chunk),
                    leads_json=json.dumps(chunk, indent=2)
                )
                raw_text = self._gemini_call(prompt, label=f"Scoring batch {idx}")
                if raw_text:
                    batch_scored = self._extract_json(raw_text)
                    if batch_scored and isinstance(batch_scored, list):
                        scored.extend(batch_scored)
                        gemini_ok = True
                    else:
                        preview = (raw_text or "")[:400].replace("\n", " ")
                        console.print(f"[yellow]  Batch {idx}: JSON parse failed. "
                                      f"Response preview: {preview}[/yellow]")
                        for lead in chunk:
                            title_parts = lead.get("title", "").split(" - ")
                            lead["score"] = self._heuristic_score(lead)
                            lead["name"] = lead.get("name") or (title_parts[0] if title_parts else "?")
                            lead["role"] = lead.get("role") or (title_parts[1] if len(title_parts) > 1 else "—")
                            lead["company"] = lead.get("company") or "—"
                            lead["discard_reason"] = "heuristic_fallback"
                            lead["source_query"] = lead.get("source_query", "")
                            lead["linkedin_url"] = lead.get("url", "")
                            scored.append(lead)
                else:
                    console.print(f"[yellow]  Batch {idx}: Gemini call failed — using heuristic.[/yellow]")
                    for lead in chunk:
                        title_parts = lead.get("title", "").split(" - ")
                        lead["score"] = self._heuristic_score(lead)
                        lead["name"] = lead.get("name") or (title_parts[0] if title_parts else "?")
                        lead["role"] = lead.get("role") or (title_parts[1] if len(title_parts) > 1 else "—")
                        lead["company"] = lead.get("company") or "—"
                        lead["discard_reason"] = "heuristic_fallback"
                        lead["source_query"] = lead.get("source_query", "")
                        lead["linkedin_url"] = lead.get("url", "")
                        scored.append(lead)
                prog.advance(score_task)

        if not scored:
            console.print("[red]Could not score any leads (Gemini + heuristic both failed)[/red]")
            return []

        scored_by = "Gemini AI" if gemini_ok else "heuristic fallback"
        console.print(f"[dim]  Scored {len(scored)} leads via {scored_by}[/dim]")

        # ── Hard Post-Filters ─────────────────────────────────────────────
        # 1. Drop interns & students
        # 2. Drop Big Tech & giant multinationals
        # 3. Drop leads with missing / empty company names or roles
        INTERN_KEYWORDS = [
            "intern", "internship", "student", "trainee", "fresher",
            "apprentice", "undergraduate", "postgraduate",
        ]
        BIG_TECH_KEYWORDS = [
            "google", "microsoft", "amazon", "apple", "meta", "uber",
            "stripe", "netflix", "adobe", "salesforce", "swiggy", "zomato",
            "flipkart", "cvent", "sabre", "coupa", "linkedin", "facebook",
            "walmart", "atlassian", "tcs", "infosys", "wipro", "cognizant", "accenture"
        ]

        def _is_valid_startup_lead(lead: dict) -> bool:
            score = lead.get("score", 0)
            if score < 0.4:
                return False

            role = (lead.get("role") or "").lower()
            name = (lead.get("name") or "").lower()
            company = (lead.get("company") or "").lower().strip()

            # Must have a valid, non-empty company name
            if not company or company in ("—", "null", "none", "unknown", "undefined"):
                return False
            # Must not be an intern or student
            if any(kw in role or kw in name for kw in INTERN_KEYWORDS):
                return False
            # Must NOT currently work at Big Tech or giant multinational
            if any(kw in company for kw in BIG_TECH_KEYWORDS):
                return False
            if any(f"at {kw}" in role or f"@{kw}" in role for kw in BIG_TECH_KEYWORDS):
                return False
            return True

        valid = sorted([l for l in scored if _is_valid_startup_lead(l)],
                       key=lambda x: x.get("score", 0), reverse=True)

        top = valid[:self.daily_limit]

        # Normalise URL scheme; add linkedin_url alias for MessengerAgent
        for lead in top:
            raw_url = lead.get("url", "") or lead.get("linkedin_url", "")
            if raw_url.startswith("https://linkedin.com"):
                raw_url = "https://www." + raw_url[len("https://"):]
            elif raw_url.startswith("http://linkedin.com"):
                raw_url = "https://www." + raw_url[len("http://"):]
            elif raw_url.startswith("http://"):
                raw_url = "https://" + raw_url[len("http://"):]
            lead["url"] = raw_url
            lead.setdefault("linkedin_url", raw_url)

        console.print(f"[green]✓ {len(scored)} scored → {len(valid)} qualified startup leads → {len(top)} selected[/green]")

        table = Table(title="Today's Startup Leads", header_style="bold cyan")
        table.add_column("#", width=3)
        table.add_column("Name", width=22)
        table.add_column("Role", width=22)
        table.add_column("Company", width=18)
        table.add_column("Score", width=7)
        for i, l in enumerate(top, 1):
            s = l.get("score", 0)
            c = "green" if s >= 0.7 else "yellow" if s >= 0.5 else "white"
            table.add_row(str(i), l.get("name") or "?", l.get("role") or "—",
                          l.get("company") or "—", f"[{c}]{s:.2f}[/{c}]")
        console.print(table)
        return top

    # ─── Save / Mark ──────────────────────────────────────────────────────────

    def save_leads(self, leads: list) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        # Write leads_today.json
        payload = {"date": datetime.now().isoformat(), "count": len(leads), "leads": leads}
        with open(LEADS_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        
        # Write historical copy
        history_dir = DATA_DIR / "history"
        history_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        history_path = history_dir / f"leads_{timestamp}.json"
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            
        console.print(f"[green]✓ Saved {len(leads)} leads (and historical backup: data/history/{history_path.name})[/green]")

    def mark_contacted(self, profile_url: str) -> None:
        seen = self._load_seen_profiles()
        seen.add(profile_url)
        self._save_seen_profiles(seen)

    # ─── Main Entry Point ─────────────────────────────────────────────────────

    def run(self, profile: dict, dry_run: bool = False) -> list:
        console.print("\n[bold cyan]━━━ Phase 1: Discovery Engine (Hybrid) ━━━[/bold cyan]")
        raw = self.gather_raw_leads(profile=profile, dry_run=dry_run)
        leads = self.score_and_filter(raw, profile)
        if leads:
            self.save_leads(leads)
        return leads

    # ─── Mock Data (dry-run only) ──────────────────────────────────────────────

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
