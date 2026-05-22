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
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

load_dotenv()
console = Console()

DATA_DIR = Path(__file__).parent.parent / "data"
LEADS_PATH = DATA_DIR / "leads_today.json"
SEEN_PATH = DATA_DIR / "seen_profiles.json"
SERPER_URL = "https://google.serper.dev/search"

# ─── Layer 1: Static fresh-signal queries ─────────────────────────────────────
# These are broad, role-targeted profile queries using tbs=qdr:m (past month)
# so results rotate daily as Google indexes newly updated profiles.
STATIC_PROFILE_QUERIES = [
    # Decision-makers at AI/ML startups
    ('site:linkedin.com/in "Co-Founder" OR "CTO" "AI" OR "ML" "startup" "India" -intern -student', "qdr:m"),
    ('site:linkedin.com/in "Engineering Manager" OR "VP Engineering" "AI" OR "ML" "India" -intern', "qdr:m"),
    ('site:linkedin.com/in "Tech Lead" OR "Lead Engineer" "backend" OR "AI" "India" -intern', "qdr:m"),
    ('site:linkedin.com/in "Head of Engineering" OR "Principal Engineer" "India" -intern', "qdr:m"),
    ('site:linkedin.com/in "Founder" OR "CTO" "Fintech" OR "SaaS" OR "DevTools" "India" -intern', "qdr:m"),
    # YC / funded founders
    ('site:linkedin.com/in "founder" "YC" OR "Y Combinator" "India" -intern', None),
    ('site:linkedin.com/in "CTO" OR "Co-Founder" "Series A" OR "Series B" OR "Seed" "India" -intern', None),
    # Big Tech senior engineers (often mentors / refer interns)
    ('site:linkedin.com/in "Senior Engineer" OR "Staff Engineer" "Google" OR "Microsoft" OR "Amazon" "India" -intern', None),
    ('site:linkedin.com/in "Engineering Manager" "Uber" OR "Atlassian" OR "Stripe" OR "Razorpay" "India" -intern', None),
]

# Active hiring signals — post queries use tbs=qdr:w (past week) for maximum freshness
STATIC_POST_QUERIES = [
    ('site:linkedin.com/posts "we are hiring" "software engineer" "India" -intern', "qdr:w"),
    ('site:linkedin.com/posts "hiring" "AI" OR "ML" OR "LLM" "India" -intern', "qdr:w"),
    ('site:linkedin.com/posts "looking for" "backend engineer" OR "developer" "India" -intern', "qdr:w"),
    ('site:linkedin.com/posts "hiring" "software engineer" "Bangalore" OR "Bengaluru" -intern', "qdr:w"),
    ('site:linkedin.com/posts "hiring" "AI" OR "backend" "startup" "India" -intern', "qdr:w"),
]

# ─── Layer 2: LinkedIn Jobs → Leaders ─────────────────────────────────────────
# Job posting queries (past week) to discover which companies are actively hiring
JOB_SOURCING_QUERIES = [
    'site:linkedin.com/jobs/view "software engineer" "India"',
    'site:linkedin.com/jobs/view "AI" OR "machine learning" "India"',
    'site:linkedin.com/jobs/view "backend engineer" OR "SDE" "India"',
    'site:linkedin.com/jobs/view "full stack engineer" OR "developer" "India"',
    'site:linkedin.com/jobs/view "data engineer" OR "engineer" "India"',
]

COMPANY_EXTRACTION_PROMPT = """You are a parsing assistant. Extract unique company names from the following LinkedIn job posting titles and snippets.

Raw postings:
{postings}

Rules:
- Only extract companies that appear to be operating or hiring in India.
- Skip global staffing agencies (e.g., Jobgether, Huptech HR, Converse Placement).
- Skip very large multinational corporations (Google, Amazon, Microsoft, Apple, Meta, Flipkart, Swiggy, Zomato) — we already target their engineers in separate queries.
- Focus on startups, scale-ups, and growth-stage tech companies.
- Return at most 12 company names.

Return ONLY a JSON array of strings:
```json
["company1", "company2"]
```"""

# ─── Layer 3: Dynamic AI-generated dorks ──────────────────────────────────────
DYNAMIC_DORK_PROMPT = """You are a lead-generation assistant for an internship outreach tool.

Candidate Profile:
{profile_summary}

Generate exactly 12 unique Google search dorks to find high-value LinkedIn profiles of decision-makers who could hire this candidate as a software engineering intern.

Rules:
- Mix profile queries (site:linkedin.com/in) and post queries (site:linkedin.com/posts).
- Use varied Indian cities: Bangalore, Hyderabad, Mumbai, Pune, Delhi, Noida, Gurgaon, Chennai, remote.
- Use varied technologies matching the candidate: WASM, Agentic AI, Multi-Agent Systems, LLM, RAG, C++, Python, backend, FinTech, Cybersecurity, GovTech, DevTools.
- Use varied roles: CTO, Co-Founder, Engineering Manager, VP Engineering, Tech Lead, Principal Engineer, Senior SDE.
- NEVER hardcode a specific year. NEVER use "intern" or "student" as required keywords (only as exclusions: -intern -student).
- Each query must be meaningfully different — avoid repeating the same role+tech combo.

Return ONLY a JSON array of 12 query strings (no tbs, no extra fields):
```json
["query1", "query2", ...]
```"""

# ─── Scoring Prompt ───────────────────────────────────────────────────────────
SCORING_PROMPT = """You are a lead-scoring assistant for an internship outreach tool.

Student Profile:
{profile_summary}

Score each lead from 0.0 to 1.0.

PRIORITIZE (high scores):
- Founders, Co-Founders, CTOs, VPs, Engineering Managers, Tech Leads, Principal Engineers, Senior SDEs
- Big Tech / FAANG (Google, Microsoft, Amazon, etc.) located ANYWHERE IN INDIA.
- Startups / Small Companies located SPECIFICALLY IN: Delhi, Bengaluru (Bangalore), Hyderabad, Pune, Mumbai. (Give much higher scores to startups in these cities).
- Profiles explicitly showing "500+ connections" or high follower counts (1k+, etc.) in their snippet.
- Active in AI, backend, security, developer tools, SaaS, FinTech, GovTech
- Recently posting about building, hiring, or shipping product

DISCARD immediately (score=0.0) if:
- Role contains: intern, internship, student, trainee, fresher, apprentice
- The person IS a student or intern themselves (not a decision-maker)
- The post is just a student sharing their own internship search
- No clear LinkedIn profile URL (e.g., it's a company page or job listing page)
- The person is located strictly outside of India (e.g., San Francisco, USA, UK, Europe, etc.). ALL leads must be from India.

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

    # ─── Core Search ──────────────────────────────────────────────────────────

    def _serper_search(self, query: str, num: int = 10, tbs: Optional[str] = None) -> list:
        if not self.serper_key or self.serper_key.startswith("your_"):
            return []
        headers = {"X-API-KEY": self.serper_key, "Content-Type": "application/json"}
        payload = {"q": query, "num": num, "gl": "in", "hl": "en"}
        if tbs:
            payload["tbs"] = tbs
        try:
            resp = requests.post(SERPER_URL, headers=headers, json=payload, timeout=15)
            resp.raise_for_status()
            return [{"url": i.get("link", ""), "title": i.get("title", ""),
                     "snippet": i.get("snippet", ""), "date": i.get("date", ""),
                     "source_query": query}
                    for i in resp.json().get("organic", [])]
        except Exception as e:
            console.print(f"[red]Serper error: {e}[/red]")
            return []

    def _gemini_call(self, prompt: str, label: str = "Gemini") -> Optional[str]:
        """Single Gemini call with retry on 503/overload/429."""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = self.client.models.generate_content(
                    model="gemini-2.5-flash", contents=prompt
                )
                return resp.text
            except Exception as e:
                err_str = str(e)
                if attempt < max_retries - 1:
                    match = re.search(r"retry in (\d+(?:\.\d+)?)s", err_str)
                    if "429" in err_str and match:
                        wait = math.ceil(float(match.group(1))) + 2
                        console.print(f"[yellow]⚠ {label} quota exceeded. Waiting {wait}s...[/yellow]")
                    elif "503" in err_str or "overload" in err_str.lower() or "unavailable" in err_str.lower() or "429" in err_str:
                        wait = 15 * (attempt + 1)
                        console.print(f"[yellow]⚠ {label} API busy. Retrying in {wait}s...[/yellow]")
                    else:
                        console.print(f"[red]❌ {label} failed: {e}[/red]")
                        return None
                    time.sleep(wait)
                else:
                    console.print(f"[red]❌ {label} failed after {max_retries} retries: {e}[/red]")
                    return None
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

    # ─── Layer 1: Static Queries ───────────────────────────────────────────────

    def _gather_static_leads(self, progress, task) -> list:
        results = []
        total = len(STATIC_PROFILE_QUERIES) + len(STATIC_POST_QUERIES)
        progress.update(task, total=total, description="Layer 1: Static queries...")

        for query, tbs in STATIC_PROFILE_QUERIES:
            results.extend(self._serper_search(query, tbs=tbs))
            progress.advance(task)

        for query, tbs in STATIC_POST_QUERIES:
            results.extend(self._serper_search(query, tbs=tbs))
            progress.advance(task)

        console.print(f"[dim]  Layer 1: {len(results)} raw results[/dim]")
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
            q = (f'site:linkedin.com/in "CTO" OR "Co-Founder" OR "Founder" OR '
                 f'"Engineering Manager" OR "Tech Lead" "{company}" "India" -intern')
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
            console.print("[dim]  Layer 3: Could not generate dorks. Skipping.[/dim]")
            return []

        # Sanitise: must be strings, cap at 12
        dorks = [d for d in dorks if isinstance(d, str)][:12]
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
        """Parse follower count from LinkedIn snippet text.

        Handles: '319K followers', '2,345 followers', '1.2M followers',
                 '12k followers', '500+ connections' (treated as 500).
        Returns -1 if nothing found (do not penalise; treat as unknown).
        """
        # Explicit follower count e.g. "319K followers" / "2.5M followers"
        m = re.search(r'([\d,]+\.?\d*)\s*([kKmM])?\s*followers', text)
        if m:
            num = float(m.group(1).replace(',', ''))
            suffix = (m.group(2) or '').lower()
            if suffix == 'k':
                return int(num * 1_000)
            elif suffix == 'm':
                return int(num * 1_000_000)
            return int(num)
        # "500+ connections" ⟹ assume exactly 500 followers as base
        if '500+ connections' in text:
            return 500
        return -1  # unknown — don't penalise

    def _follower_bonus(self, followers: int) -> float:
        """Log-normal bell-curve bonus peaking at 10 K followers.

        Curve shape (log10 space, sigma=1.0):
          ~500  → +0.06     ← base signal, decent reach
          ~1 K  → +0.09     ← solid presence
          ~5 K  → +0.14     ← strong presence
          ~10 K → +0.15     ← PEAK (sweet spot: active & reachable)
          ~20 K → +0.14
          ~100K → +0.09     ← influential but harder to get a reply
          ~1 M  → +0.02     ← celebrity tier; reply rate very low

        Always positive — large followings are still a plus, just less so.
        """
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

        HIGH_ROLE = ['cto', 'co-founder', 'cofounder', 'founder', 'vp engineering',
                     'head of engineering', 'engineering manager', 'principal engineer',
                     'tech lead', 'staff engineer', 'vp of engineering']
        MED_ROLE  = ['senior engineer', 'senior sde', 'lead engineer', 'senior developer',
                     'engineering lead', 'sde ii', 'sde 2']
        GOOD_TECH = ['ai', 'ml', 'llm', 'backend', 'python', 'rag', 'fintech', 'saas',
                     'agentic', 'wasm', 'rust', 'golang', 'devtools']
        HIRING_SIG = ['hiring', 'we are hiring', 'looking for', 'join us', 'open role']
        DISCARD    = ['intern', 'student', 'fresher', 'trainee', 'apprentice', 'undergraduate']
        NON_INDIA  = ['usa', 'san francisco', 'new york', 'london', 'uk', 'canada',
                      'europe', 'australia', 'germany', 'singapore', 'dubai']
        PREFERRED_CITIES = ['delhi', 'bengaluru', 'bangalore', 'hyderabad', 'pune', 'mumbai']
        BIG_TECH   = ['google', 'microsoft', 'amazon', 'apple', 'meta', 'uber',
                      'stripe', 'netflix', 'adobe', 'salesforce']

        if any(kw in text for kw in DISCARD):
            return 0.0
        if any(kw in text for kw in NON_INDIA):
            return 0.0

        score = 0.35  # base — gets promoted if signals match
        if any(kw in text for kw in HIGH_ROLE):
            score += 0.40
        elif any(kw in text for kw in MED_ROLE):
            score += 0.20
        if any(kw in text for kw in GOOD_TECH):
            score += 0.15
        if any(kw in text for kw in HIRING_SIG):
            score += 0.10

        # ── Follower bell-curve bonus ─────────────────────────────────────
        # If count is explicitly mentioned, apply curve (peaks at 10K).
        # If only '500+ connections' is mentioned, treat as 500.
        # If nothing is mentioned at all, skip — we do NOT penalise.
        follower_count = self._parse_follower_count(text)
        if follower_count > 0:
            score += self._follower_bonus(follower_count)

        # ── City / company-tier preference ───────────────────────────────
        is_big_tech  = any(kw in text for kw in BIG_TECH)
        in_pref_city = any(kw in text for kw in PREFERRED_CITIES)

        if is_big_tech:
            score += 0.15          # Big Tech anywhere in India is fine
        elif in_pref_city:
            score += 0.15          # Startup in preferred city
        else:
            score -= 0.10          # Startup outside preferred city

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

        # ── Heuristic pre-filter: rank all fresh leads and keep top (daily_limit × 2) ──
        # This avoids sending 200-300 leads to Gemini when we only need 40.
        # We score every lead with the fast keyword heuristic, sort descending,
        # and hand only the best candidates to the (slower, smarter) Gemini scorer.
        GEMINI_POOL = self.daily_limit * 2  # e.g. 40 × 2 = 80 candidates
        if len(fresh) > GEMINI_POOL:
            prescored = sorted(fresh, key=lambda l: self._heuristic_score(l), reverse=True)
            candidates = prescored[:GEMINI_POOL]
            console.print(
                f"[dim]  Pre-filter: kept top {len(candidates)} from {len(fresh)} "
                f"fresh leads for Gemini scoring[/dim]"
            )
        else:
            candidates = fresh

        profile_summary = (
            f"{profile.get('name')}, {profile.get('year')} @ {profile.get('college')}, "
            f"{profile.get('branch')}\n"
            f"Skills: {', '.join(profile.get('skills', []))}\n"
            f"Targets: {', '.join(profile.get('target_roles', []))} | "
            f"{', '.join(profile.get('target_industries', []))} | "
            f"{', '.join(profile.get('geography', []))}"
        )

        # ── Chunked Gemini scoring (40 leads per call to stay under token limits) ──
        # Uses `candidates` (pre-filtered top 80), NOT the full `fresh` list.
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
                        # Log first 400 chars of response for debugging
                        preview = (raw_text or "")[:400].replace("\n", " ")
                        console.print(f"[yellow]  Batch {idx}: JSON parse failed. "
                                      f"Response preview: {preview}[/yellow]")
                        # Fall back to heuristic for this chunk
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

        # Hard post-filter: drop anyone who IS an intern/student
        INTERN_KEYWORDS = [
            "intern", "internship", "student", "trainee", "fresher",
            "apprentice", "undergraduate", "postgraduate",
        ]
        def _is_intern(lead: dict) -> bool:
            role = (lead.get("role") or "").lower()
            name = (lead.get("name") or "").lower()
            return any(kw in role or kw in name for kw in INTERN_KEYWORDS)

        pre_filter = [l for l in scored if l.get("score", 0) >= 0.4]
        valid = sorted([l for l in pre_filter if not _is_intern(l)],
                       key=lambda x: x.get("score", 0), reverse=True)
        intern_dropped = len(pre_filter) - len(valid)
        if intern_dropped:
            console.print(f"[dim]  Dropped {intern_dropped} intern/student profiles[/dim]")

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

        console.print(f"[green]✓ {len(scored)} scored → {len(valid)} qualified → {len(top)} selected[/green]")

        table = Table(title="Today's Leads", header_style="bold cyan")
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
        with open(LEADS_PATH, "w", encoding="utf-8") as f:
            json.dump({"date": datetime.now().isoformat(), "count": len(leads), "leads": leads},
                      f, indent=2, ensure_ascii=False)
        console.print(f"[green]✓ Saved {len(leads)} leads[/green]")

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
