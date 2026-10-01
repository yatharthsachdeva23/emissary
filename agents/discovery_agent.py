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
RAW_LEADS_PATH = DATA_DIR / "raw_leads_today.json"
SEEN_PATH = DATA_DIR / "seen_profiles.json"
SERPER_URL = "https://google.serper.dev/search"

# ─── Multi-Segment Keyword Matrices (1: Dubai Proptech, 2: India Non-Tech, 3: US, 4: India Tech) ───
DUBAI_ROLE_SETS = [
    '"Founder" OR "Co-Founder" OR "CEO"',
    '"Head of Sales" OR "VP Sales" OR "Director of Sales"',
    '"Head of Business Development" OR "Commercial Director" OR "Head of Commercial"',
    '"Head of Product" OR "VP Product" OR "Product Lead"',
    '"Managing Director" OR "General Manager" OR "COO"',
    '"Head of Growth" OR "VP Growth" OR "Growth Lead"',
]

DUBAI_THEME_SETS = [
    '"PropTech" OR "Real Estate Tech"',
    '"Property Management" OR "PropTech Platform"',
    '"Real Estate" OR "Residential"',
    '"Property Brokerage" OR "Property Portal"',
    '"Off-plan" OR "Luxury Real Estate" OR "Real Estate Startup"',
    '"Seed" OR "Series A" OR "Funded" "Real Estate"',
]

DUBAI_LOCATIONS = [
    '"Dubai"',
    '"Dubai" OR "UAE"',
    '"United Arab Emirates"',
]

# Priority 2: India Non-Tech Startups (PM, Sales, Founder's Office)
INDIA_NON_TECH_ROLE_SETS = [
    '"Product Manager" OR "APM" OR "Associate Product Manager"',
    '"Head of Product" OR "VP Product" OR "Director of Product"',
    '"Head of Sales" OR "VP Sales" OR "Director of Sales" OR "Sales Lead"',
    '"Head of Business Development" OR "VP Business Development" OR "Commercial Director"',
    '"Founder\'s Office" OR "Chief of Staff"',
    '"Founder" OR "Co-Founder" OR "CEO"',
    '"Head of Growth" OR "VP Growth" OR "Growth Lead"',
]

INDIA_NON_TECH_THEME_SETS = [
    '"D2C" OR "Consumer Brand" OR "Retail"',
    '"Logistics" OR "Supply Chain" OR "Operations"',
    '"Manufacturing" OR "FMCG" OR "Consumer Goods"',
    '"Hospitality" OR "Food & Beverage" OR "Health & Wellness"',
    '"Direct to Consumer" OR "Omnichannel" OR "E-commerce"',
    '"Services" OR "Real Estate" OR "Home Decor" OR "Construction"',
]

INDIA_LOCATIONS = [
    '"Bangalore" OR "Bengaluru"',
    '"Delhi NCR" OR "Gurgaon" OR "Noida"',
    '"Mumbai" OR "Pune"',
    '"India"',
]

# Priority 3: US Tech & Non-Tech Startups
US_ROLE_SETS = [
    '"Product Manager" OR "Founding PM" OR "APM"',
    '"Head of Product" OR "VP Product" OR "CPO"',
    '"Head of Sales" OR "VP Sales" OR "Head of BD"',
    '"Founder\'s Office" OR "Chief of Staff"',
    '"Founder" OR "Co-Founder" OR "CEO"',
    '"Head of Growth" OR "VP Growth" OR "Growth PM"',
]

US_THEME_SETS = [
    '"B2B SaaS" OR "AI Startup" OR "AI Product"',
    '"Fintech" OR "Payments"',
    '"D2C" OR "Consumer" OR "Retail" OR "E-commerce"',
    '"Logistics" OR "Supply Chain" OR "Operations"',
    '"Startup" OR "Seed" OR "Series A"',
]

US_LOCATIONS = [
    '"New York" OR "NYC" OR "Manhattan"',
    '"San Francisco" OR "Austin"',
    '"United States"',
]

# Priority 4: India Tech Startups
INDIA_TECH_ROLE_SETS = [
    '"Product Manager" OR "APM" OR "Associate Product Manager"',
    '"Head of Product" OR "VP Product"',
    '"Founder\'s Office" OR "Chief of Staff"',
    '"Founder" OR "Co-Founder" OR "CEO"',
]

INDIA_TECH_THEME_SETS = [
    '"AI Startup" OR "Generative AI" OR "AI Product"',
    '"B2B SaaS" OR "SaaS"',
    '"Fintech" OR "Payments"',
    '"Developer Tools" OR "DeepTech"',
    '"Startup" "Seed" OR "Series A"',
]


def generate_random_static_queries() -> list[tuple[str, Optional[str], str]]:
    """
    Generates prioritized, 4-tier Google search dorks on every run:
    - Priority 1: Dubai/UAE Proptech & Property Startups (6 queries: 4 profile, 2 post, gl='ae')
    - Priority 2: India Non-Tech Startups: PM, Sales, Founder's Office (4 queries: 2 profile, 2 post, gl='in')
    - Priority 3: US Tech & Non-Tech Startups (3 queries: 2 profile, 1 post, gl='us')
    - Priority 4: India Tech Startups: PM & Founder's Office (2 queries: 1 profile, 1 post, gl='in')
    Total: 15 queries.
    Returns list of tuples: (query, tbs, gl)
    """
    import random
    queries = []
    used_combos = set()

    # 1. Dubai / UAE Proptech & Property (Priority 1: 4 profile dorks + 2 post dorks)
    attempts = 0
    dubai_profiles = []
    while len(dubai_profiles) < 4 and attempts < 100:
        attempts += 1
        r = random.choice(DUBAI_ROLE_SETS)
        t = random.choice(DUBAI_THEME_SETS)
        l = random.choice(DUBAI_LOCATIONS)
        key = f"dubai_{r}_{t}_{l}"
        if key in used_combos:
            continue
        used_combos.add(key)
        q = f'site:linkedin.com/in {r} {t} {l} -intern -student -stealth'
        dubai_profiles.append((q, None, "ae"))
    queries.extend(dubai_profiles)

    attempts = 0
    dubai_posts = []
    while len(dubai_posts) < 2 and attempts < 100:
        attempts += 1
        sig = random.choice(["we are hiring", "hiring", "open role", "looking for"])
        role = random.choice(['"Sales" OR "Real Estate"', '"Proptech" OR "Property"', '"Product" OR "Growth"'])
        loc = random.choice(['"Dubai"', '"UAE"'])
        key = f"dubai_post_{sig}_{role}_{loc}"
        if key in used_combos:
            continue
        used_combos.add(key)
        q = f'site:linkedin.com/posts "{sig}" {role} {loc} -intern -student -stealth'
        dubai_posts.append((q, "qdr:w", "ae"))
    queries.extend(dubai_posts)

    # 2. India Non-Tech Startups: PM, Sales, Founder's Office (Priority 2: 2 profile dorks + 2 post dorks)
    attempts = 0
    india_nontech_profiles = []
    while len(india_nontech_profiles) < 2 and attempts < 100:
        attempts += 1
        r = random.choice(INDIA_NON_TECH_ROLE_SETS)
        t = random.choice(INDIA_NON_TECH_THEME_SETS)
        l = random.choice(INDIA_LOCATIONS)
        key = f"india_nontech_{r}_{t}_{l}"
        if key in used_combos:
            continue
        used_combos.add(key)
        q = f'site:linkedin.com/in {r} {t} {l} -intern -student -stealth'
        india_nontech_profiles.append((q, None, "in"))
    queries.extend(india_nontech_profiles)

    attempts = 0
    india_nontech_posts = []
    while len(india_nontech_posts) < 2 and attempts < 100:
        attempts += 1
        sig = random.choice(["we are hiring", "hiring", "join our team", "open role"])
        role = random.choice(['"Product Manager" OR "APM"', '"Head of Sales" OR "VP Sales"', '"Founder\'s Office" OR "Growth"'])
        cat = random.choice(['"D2C" OR "Consumer"', '"Retail" OR "Logistics"', '"Operations" OR "FMCG"'])
        loc = random.choice(['"Bangalore" OR "Delhi NCR"', '"Gurgaon" OR "Noida"', '"India"'])
        key = f"india_post_{sig}_{role}_{cat}_{loc}"
        if key in used_combos:
            continue
        used_combos.add(key)
        q = f'site:linkedin.com/posts "{sig}" {role} {cat} {loc} -intern -student -stealth'
        india_nontech_posts.append((q, "qdr:w", "in"))
    queries.extend(india_nontech_posts)

    # 3. US Tech & Non-Tech Startups (Priority 3: 2 profile dorks + 1 post dork)
    attempts = 0
    us_profiles = []
    while len(us_profiles) < 2 and attempts < 100:
        attempts += 1
        r = random.choice(US_ROLE_SETS)
        t = random.choice(US_THEME_SETS)
        l = random.choice(US_LOCATIONS)
        key = f"us_{r}_{t}_{l}"
        if key in used_combos:
            continue
        used_combos.add(key)
        q = f'site:linkedin.com/in {r} {t} {l} -intern -student -stealth'
        us_profiles.append((q, None, "us"))
    queries.extend(us_profiles)

    attempts = 0
    us_posts = []
    while len(us_posts) < 1 and attempts < 100:
        attempts += 1
        sig = random.choice(["we are hiring", "hiring", "open role"])
        role = random.choice(['"Product Manager" OR "APM"', '"Head of Product" OR "Founder\'s Office"'])
        loc = random.choice(['"New York" OR "NYC"', '"United States"'])
        key = f"us_post_{sig}_{role}_{loc}"
        if key in used_combos:
            continue
        used_combos.add(key)
        q = f'site:linkedin.com/posts "{sig}" {role} {loc} -intern -student -stealth'
        us_posts.append((q, "qdr:w", "us"))
    queries.extend(us_posts)

    # 4. India Tech Startups (Priority 4: 1 profile dork + 1 post dork)
    attempts = 0
    india_tech_profiles = []
    while len(india_tech_profiles) < 1 and attempts < 100:
        attempts += 1
        r = random.choice(INDIA_TECH_ROLE_SETS)
        t = random.choice(INDIA_TECH_THEME_SETS)
        l = random.choice(INDIA_LOCATIONS)
        key = f"india_tech_{r}_{t}_{l}"
        if key in used_combos:
            continue
        used_combos.add(key)
        q = f'site:linkedin.com/in {r} {t} {l} -intern -student -stealth'
        india_tech_profiles.append((q, None, "in"))
    queries.extend(india_tech_profiles)

    attempts = 0
    india_tech_posts = []
    while len(india_tech_posts) < 1 and attempts < 100:
        attempts += 1
        sig = random.choice(["we are hiring", "hiring", "looking for"])
        role = random.choice(['"Product Manager" OR "APM"', '"Founder\'s Office" OR "Product Lead"'])
        loc = random.choice(['"Bangalore" OR "Bengaluru"', '"Delhi NCR" OR "Gurgaon"'])
        key = f"india_tech_post_{sig}_{role}_{loc}"
        if key in used_combos:
            continue
        used_combos.add(key)
        q = f'site:linkedin.com/posts "{sig}" {role} {loc} -intern -student -stealth'
        india_tech_posts.append((q, "qdr:w", "in"))
    queries.extend(india_tech_posts)

    return queries


# ─── Layer 2: LinkedIn Jobs → Leaders across Segments ────────────────────────
JOB_SOURCING_QUERIES = [
    ('site:linkedin.com/jobs/view ("Proptech" OR "Real Estate Tech" OR "Property" OR "Residential") ("Startup" OR "Scaleup") "Dubai"', "ae"),
    ('site:linkedin.com/jobs/view ("Sales" OR "Business Development" OR "Commercial") ("Proptech" OR "Real Estate") "Dubai"', "ae"),
    ('site:linkedin.com/jobs/view ("Product Manager" OR "APM" OR "Associate Product Manager" OR "Founder\'s Office") ("D2C" OR "Consumer" OR "Retail" OR "Logistics" OR "Operations") "India"', "in"),
    ('site:linkedin.com/jobs/view ("Head of Sales" OR "VP Sales" OR "Business Development" OR "Growth") ("D2C" OR "Consumer" OR "Retail" OR "Operations") "India"', "in"),
    ('site:linkedin.com/jobs/view ("Product Manager" OR "APM" OR "Founder\'s Office" OR "Sales") ("Seed" OR "Series A" OR "Startup") ("New York" OR "United States")', "us"),
    ('site:linkedin.com/jobs/view ("Product Manager" OR "APM" OR "Founder\'s Office") ("AI" OR "SaaS" OR "Fintech" OR "Startup") "India"', "in"),
]

COMPANY_EXTRACTION_PROMPT = """You are a parsing assistant. Extract unique company names from the following LinkedIn job posting titles and snippets.

Raw postings:
{postings}

Rules:
- Extract companies operating or hiring in Dubai/UAE, India, or United States.
- For India: Focus strictly on startups, scale-ups, and growth-stage companies (0-5 years approx). Skip large Indian legacy IT/corporate conglomerates (TCS, Infosys, Wipro, Cognizant, Reliance).
- For Dubai/UAE and US: Extract startups, mid-level companies, AND prominent industry leaders/enterprises (Proptech platforms, Real Estate developers/brokerages like Emaar, Damac, Aldar, Sobha, Betterhomes, and US tech/enterprises).
- Skip global staffing/recruitment agencies (e.g., Jobgether, Huptech HR, Converse Placement, Michael Page).
- Return at most 6 unique company names.

Return ONLY a JSON array of strings:
```json
["company1", "company2", "company3"]
```"""

# ─── Layer 3: Dynamic AI-generated dorks across Segments (12 queries) ─────────
DYNAMIC_DORK_PROMPT = """You are an expert lead-generation assistant for an outreach engine targeting decision-makers.

Candidate Profile:
{profile_summary}

Generate exactly 12 unique Google search dorks to find high-value LinkedIn profiles of decision-makers:
- For Dubai/UAE & US: Target startups, mid-level firms, AND big giants/enterprises!
- For India: Target startups (0-5 years approx).

PRIORITIZATION & SEGMENTATION RULES:
1. DUBAI / UAE PROPTECH, PROPERTY & REAL ESTATE (1ST PRIORITY - 5 Dorks):
   - Focus MAJORLY on Proptech, Property, Residential, Real Estate companies (from fast startups to established mid-level brokerages and enterprise developers like Emaar, Damac, Sobha, Betterhomes) in Dubai/UAE.
   - Roles: "Founder", "Co-Founder", "CEO", "Head of Sales", "VP Sales", "Commercial Director", "Head of Product", "Managing Director", "Director".
   - Terms: "Dubai" OR "UAE", "Proptech" OR "Real Estate" OR "Property" OR "Residential".
2. INDIA NON-TECH STARTUPS: PM, SALES, FOUNDER'S OFFICE (2ND PRIORITY - 3 Dorks):
   - Focus on Indian Non-Tech startups (D2C, Consumer brands, Retail, Logistics, Supply Chain, Operations, Manufacturing, Hospitality, Health) specifically for:
     a) Product Management (PM / APM / Head of Product) optimizing user experience, checkout, catalog, and operations.
     b) Sales, BD & Growth (Head of Sales, VP Sales, Commercial Director).
     c) Founder's Office (Chief of Staff, Founder & CEO at early startups).
   - Roles: "Product Manager", "APM", "Head of Sales", "VP Sales", "Founder's Office", "Founder".
   - Terms: "Bangalore" OR "Delhi NCR" OR "Gurgaon" OR "Mumbai" OR "India", "D2C" OR "Consumer" OR "Retail" OR "Logistics" OR "Operations".
3. US TECH & NON-TECH (3RD PRIORITY - 2 Dorks):
   - Focus on high-growth Tech and Non-Tech companies in the United States (New York, SF, Austin, etc.) across startups, mid-level scale-ups, and enterprise tech.
   - Roles: "Product Manager", "Founding PM", "Head of Product", "Head of Sales", "Founder's Office", "Founder", "VP Sales".
   - Terms: "New York" OR "NYC" OR "San Francisco" OR "United States", "Startup" OR "B2B SaaS" OR "AI" OR "D2C" OR "Consumer" OR "Enterprise".
4. INDIA TECH STARTUPS (4TH PRIORITY - 2 Dorks):
   - Focus on AI, B2B SaaS, and Fintech tech startups in India (0-5 years).
   - Roles: "Product Manager", "APM", "Head of Product", "Founder's Office", "Founder".
   - Terms: "Bangalore" OR "Delhi NCR" OR "Gurgaon" OR "India", "AI Startup" OR "B2B SaaS" OR "Fintech".

GENERAL SYNTAX RULES:
- Mix profile queries (site:linkedin.com/in) and post queries (site:linkedin.com/posts).
- Always append -intern -student -stealth.
- CRITICAL SYNTAX RULE: NEVER add company exclusions like -google, -microsoft, or -linkedin to the query string. Focus on positive keywords.
- Each query must be clean, valid Google search syntax.

Return ONLY a JSON array of 12 query strings:
```json
["query1", "query2", ...]
```"""

# ─── Scoring Prompt ───────────────────────────────────────────────────────────
SCORING_PROMPT = """You are a lead-scoring assistant for an outreach engine.

Student Profile:
{profile_summary}

Candidate Strengths & Versatility:
- Yatharth is a 4th-year student at DTU (Information Technology, 9.3 CGPA) and former Intern at NoBrokerHood (India's premier PropTech unicorn).
- High-agency multi-domain builder: Strong across Product Management (PM/APM), B2B Sales, Founder's Office, Growth Marketing, and Tech/AI.
- 15+ End-to-End Real-World Projects: Built 15+ complete projects from scratch with obsessive focus on exceptional User Experience (UX) and solving real-life problems (never building if it doesn't solve a real issue).
- Sales Proof: Automated B2B sales outreach at NoBrokerHood (capturing 25+ extra qualified leads/mo) AND personal college society fest corporate sponsorship work (closed ₹3–10 Lakh corporate deals each year via cold outbound - FOR INDIAN SALES ONLY, NOT DUBAI).
- Proptech Track (Dubai 1st Priority): Pitches combined B2B Sales + Tech, highlighting NoBrokerHood internship (automated sales outreach delivering 25+ extra leads/mo, 1.5x search efficiency).
- Property Track (Dubai 1st Priority): Pitches high-velocity B2B Sales, outbound/inbound pipeline conversion, deal acceleration, client acquisition.
- Non-Tech PM Track (India 2nd Priority): Pitches PM / APM with 15+ real-world UX projects and NoBrokerHood search/discovery optimization (1.5x efficiency).
- Non-Tech Sales Track (India 2nd Priority): Pitches B2B Sales, corporate partnerships (NoBrokerHood 25+ leads/mo + ₹3–10L fest corporate deals).
- US Tech & Non-Tech Track (US 3rd Priority): Pitches PM, Sales, or Founder's Office for US companies.
- Tech PM Track (India 4th Priority): Pitches PM/APM/AI PM with 15+ real-world UX projects and NoBrokerHood AI PM.
- Founder's Office Track: Pitches high-agency generalist operator for startups wearing multiple hats across product UX, B2B sales pipelines, and operations.

SEGMENTATION & PRIORITIES (Score 0.85 - 1.0 for sweet-spot leads):
1. GEOGRAPHIC & TIER PRIORITIES:
   - 1ST PRIORITY: Dubai / UAE PropTech, Real Estate, Residential, Property Management, and Brokerages (Startups, Mid-level, AND Big Giants like Emaar, Damac, Sobha, Nakheel, Betterhomes).
   - 2ND PRIORITY: India Non-Tech Startups (D2C, Consumer Brands, Retail, Logistics, Supply Chain, Operations, Manufacturing, Hospitality, Health) for PM, Sales, or Founder's Office.
   - 3RD PRIORITY: US Tech & Non-Tech (New York, SF, US) across Startups, Mid-level, and Enterprise.
   - 4TH PRIORITY: India Tech Startups (AI, B2B SaaS, Fintech).
2. COMPANY TYPE:
   - Proptech: STRICTLY companies whose core proprietary product is property technology, real estate software, portals, smart building platforms, or tenant management (e.g. Property Finder, PRYPCO, Coraly.ai, Smart Bricks).
   - Property: Traditional residential & commercial brokerages, real estate agencies, property developers, leasing firms (e.g. haus & haus, McCone Properties, Emaar, Damac).
   - Non-tech: D2C brands, consumer products, retail, logistics, manufacturing, operations-heavy businesses.
   - Tech: Software, AI, B2B SaaS, Fintech, Consumer Tech, Enterprise Platforms, design/product studios.
   - CRITICAL DISAMBIGUATION RULE: Do NOT classify design studios, UI/UX agencies, or dev shops as "proptech" just because they designed or built a real estate portal for a client (e.g., Layerat, Prex Studio)! If the company is an agency or studio, classify as "tech" (or discard if micro/freelance).
3. MATURITY / STAGE CONSTRAINT & TIERS:
   - FOR INDIA LEADS: STRICTLY STARTUPS (0 to ~5 years operating, ~5 to 100 people). Discard Indian Big Tech and Indian legacy corporate monoliths (TCS, Infosys, Wipro, Cognizant, Swiggy, Zomato, Flipkart).
   - FOR NON-INDIAN LEADS (Dubai/UAE, US/New York): TARGET STARTUPS, MID-LEVEL COMPANIES, AND BIG GIANTS / ENTERPRISE (e.g., Emaar, Damac, Aldar, Sobha, Betterhomes, US tech enterprises). All tiers are welcome for non-Indian leads!
4. COMPANY TIER CLASSIFICATION:
   - "enterprise": Big giants, major developers, multinational corporations, global leaders.
   - "mid_level": Established mid-market companies, scale-ups, prominent brokerages/agencies.
   - "startup": Agile early-stage startups (0-5 years).

DISCARD RULES (score = 0.0):
1. SOLO FOUNDERS / MICRO-TEAMS / FREELANCERS / STEALTH (< 3-5 people): Discard 'stealth mode', solo freelancers, boutique 1-person design studios or solo consulting gigs, dormant side projects.
2. INDIAN BIG CORPORATE MONOLITHS: For India ONLY, discard TCS, Infosys, Wipro, Cognizant, Accenture, Swiggy, Zomato, Flipkart, Reliance, Tata. (Non-Indian giants like Emaar, Damac, Sobha, Google, Microsoft, Salesforce in Dubai/US are ALLOWED and should be scored!).
3. STAFFING & RECRUITMENT AGENCIES: Discard staffing, placement, and recruiting agencies (Michael Page, Adecco, Randstad, etc.).
4. MISSING / UNKNOWN COMPANY OR ROLE: If person's company or role is missing/null, discard.
5. INTERNS & STUDENTS: Discard anyone whose role is intern, internship, student, trainee, fresher, apprentice.
6. PURE LOW-LEVEL CODING WITH NO BUSINESS/GROWTH: Discard pure junior backend coders or QA testers with zero product, sales, or management scope.
(NOTE: Dubai, UAE, New York, US, and India leads are ALL WELCOME and encouraged! DO NOT discard based on location).

Return ONLY a JSON array of objects wrapped in ```json ... ``` tags:
[
  {{
    "id": 0,
    "name": "Full Name extracted from title",
    "company": "Exact Company Name extracted from snippet/title",
    "role": "Exact Role/Title (e.g. Founder & CEO, Head of Sales, VP Product, Founder's Office, Managing Director)",
    "score": 0.95,
    "geo_segment": "dubai | new_york | uae | us | india | other",
    "company_type": "proptech | property | tech | non_tech",
    "company_tier": "startup | mid_level | enterprise",
    "pitch_track": "proptech_sales_tech | property_sales | non_tech_pm | non_tech_growth_sales | tech_pm | founders_office",
    "discard_reason": null
  }}
]

Raw leads ({count} items):
{leads_json}
"""


class DiscoveryAgent:
    def __init__(self):
        self.serper_key = os.getenv("SERPER_API_KEY", "")
        self.daily_limit = int(os.getenv("DAILY_SEND_LIMIT", "50"))

    # ─── Inference Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _infer_geo_segment(lead: dict) -> str:
        text = f"{lead.get('company', '')} {lead.get('role', '')} {lead.get('snippet', '')} {lead.get('source_query', '')}".lower()
        if any(w in text for w in ["dubai", "uae", "emirates", "abu dhabi", "sharjah"]):
            return "dubai"
        if any(w in text for w in ["new york", "nyc", "manhattan", "brooklyn"]):
            return "new_york"
        if any(w in text for w in ["united states", "usa", "san francisco", "austin", "seattle", "california"]):
            return "us"
        if any(w in text for w in ["bangalore", "bengaluru", "delhi", "gurgaon", "gurugram", "noida", "mumbai", "pune", "hyderabad", "india"]):
            return "india"
        return "other"

    @staticmethod
    def _infer_company_type(lead: dict) -> str:
        # Evaluate ONLY company name, role, and profile snippet.
        # DO NOT include source_query here — query keywords (e.g. "PropTech") cause severe false-positive contamination!
        comp = (lead.get("company") or "").lower()
        role = (lead.get("role") or "").lower()
        snippet = (lead.get("snippet") or "").lower()
        text = f"{comp} {role} {snippet}"

        # 1. Agency / Studio / Design / Services check (e.g. Layerat, Prex Studio)
        # Studios & agencies doing client work should NEVER be classified as PropTech
        agency_keywords = [
            "design studio", "creative studio", "ui/ux studio", "digital agency",
            "design agency", "software consultancy", "branding agency", "design firm"
        ]
        if any(w in comp for w in ["studio", "agency", "creative", "designers", "consultancy"]) or any(w in text for w in agency_keywords):
            return "tech"

        # 2. Genuine PropTech: software, AI, or SaaS specifically dedicated to real estate / property
        proptech_strong = [
            "proptech", "real estate tech", "property tech", "property portal",
            "real estate portal", "smart building", "tenant management software"
        ]
        if any(w in text for w in proptech_strong):
            return "proptech"

        # Check for combination of property keywords AND software/tech keywords
        has_real_estate = any(w in text for w in ["real estate", "property", "residential", "housing", "mortgage"])
        has_tech = any(re.search(pat, text) for pat in [r'\bsoftware\b', r'\bsaas\b', r'\bai\b', r'\bapp\b', r'\bplatform\b'])
        if has_real_estate and has_tech:
            # If company name clearly indicates a traditional brokerage/agency, keep as property
            if any(w in comp for w in ["properties", "realty", "real estate", "brokerage", "homes", "developments"]):
                return "property"
            return "proptech"

        # 3. Traditional Property / Brokerages / Developers
        if any(w in text for w in ["real estate", "property", "residential", "brokerage", "realtor", "housing", "mortgage", "developer", "realty", "properties"]):
            return "property"

        # 4. General Tech / Software / SaaS
        tech_patterns = [
            r'\bsoftware\b', r'\bsaas\b', r'\bai\b', r'\bgenai\b', r'\bplatform\b',
            r'\bcloud\b', r'\btech\b', r'\bfintech\b', r'\bhealthtech\b', r'\bedtech\b',
            r'\bdeveloper\b', r'\bdeeptech\b'
        ]
        if any(re.search(pat, text) for pat in tech_patterns):
            return "tech"
        return "non_tech"

    @staticmethod
    def _infer_pitch_track(lead: dict) -> str:
        comp_type = lead.get("company_type") or DiscoveryAgent._infer_company_type(lead)
        role = (lead.get("role") or "").lower()
        snippet = (lead.get("snippet") or "").lower()
        combined = f"{role} {snippet}"

        # 1. Founder's Office / Chief of Staff role check
        fo_patterns = [r"\bfounder'?s?\s+office\b", r"\bchief\s+of\s+staff\b"]
        if any(re.search(pat, combined) for pat in fo_patterns):
            return "founders_office"

        if comp_type == "proptech":
            return "proptech_sales_tech"
        if comp_type == "property":
            return "property_sales"
        if comp_type == "tech":
            # If tech startup has a sales / BD role -> non_tech_growth_sales
            sales_patterns = [r'\bsales\b', r'\bbusiness development\b', r'\bbd\b', r'\bcommercial\b']
            if any(re.search(pat, role) for pat in sales_patterns):
                return "non_tech_growth_sales"
            return "tech_pm"

        # Non-tech startup (India, US, or other)
        pm_patterns = [r'\bproduct\b', r'\bpm\b', r'\bapm\b', r'\bcpo\b', r'\buser experience\b', r'\bux\b']
        if any(re.search(pat, role) for pat in pm_patterns):
            return "non_tech_pm"
        return "non_tech_growth_sales"

    @staticmethod
    def _infer_company_tier(lead: dict) -> str:
        """
        Infer company tier: 'enterprise' (big giants), 'mid_level', or 'startup'.
        For Indian companies, strictly 'startup'.
        For Non-Indian (Dubai, US, etc.), can be startup, mid_level, or enterprise.
        """
        geo = (lead.get("geo_segment") or DiscoveryAgent._infer_geo_segment(lead)).lower()
        if geo in ("india", "in"):
            return "startup"

        company = (lead.get("company") or "").lower().strip()
        role = (lead.get("role") or "").lower()
        snippet = (lead.get("snippet") or "").lower()
        combined = f"{company} {role} {snippet}"

        # Big Giants / Enterprise
        ENTERPRISE_KEYWORDS = [
            "emaar", "damac", "aldar", "sobha", "nakheel", "meraas", "omniyat",
            "danube", "deyaar", "binghatti", "mag lifestyle", "azizi", "cbre",
            "jll", "colliers", "savills", "knight frank", "cushman",
            "google", "microsoft", "amazon", "apple", "meta", "salesforce",
            "oracle", "stripe", "uber", "airbnb", "netflix", "adobe",
            "compass", "zillow", "redfin", "costar", "opendoor",
            "enterprise", "multinational", "conglomerate", "fortune 500", "publicly traded",
            "global real estate", "nasdaq", "nyse"
        ]
        if any(kw in company for kw in ENTERPRISE_KEYWORDS):
            return "enterprise"

        # Mid-Level / Established Medium Companies
        MID_LEVEL_KEYWORDS = [
            "betterhomes", "allsopp", "haus & haus", "cavendish", "propsearch",
            "bayut", "propertyfinder", "fäm properties", "fam properties",
            "dacha", "driven properties", "provident estate",
            "series b", "series c", "series d", "scale-up", "scaleup",
            "200+ employees", "500+ employees", "mid-market", "established",
            "medium"
        ]
        if any(kw in combined for kw in MID_LEVEL_KEYWORDS):
            return "mid_level"

        return "startup"

    @staticmethod
    def _parse_title_heuristics(title: str) -> tuple[str, str, str]:
        """Extract (name, role, company) from standard LinkedIn search title."""
        clean_title = re.sub(r'\s*[-—–|]\s*LinkedIn.*$', '', title, flags=re.IGNORECASE).strip()
        parts = [p.strip() for p in re.split(r'\s+[-—–|]\s+', clean_title) if p.strip()]
        name = parts[0] if parts else "?"
        role = "—"
        company = "—"

        if len(parts) >= 2:
            middle = parts[1]
            if " at " in middle.lower():
                r, c = re.split(r'\s+at\s+', middle, maxsplit=1, flags=re.IGNORECASE)
                role, company = r.strip(), c.strip()
            elif " @ " in middle:
                r, c = middle.split(" @ ", 1)
                role, company = r.strip(), c.strip()
            elif len(parts) >= 3:
                role = parts[1]
                company = parts[2]
            else:
                role = middle
        return name, role, company

    # ─── Core Search ──────────────────────────────────────────────────────────

    def _serper_search(self, query: str, num: int = 10, tbs: Optional[str] = None, gl: str = "in") -> list:
        if not self.serper_key or self.serper_key.startswith("your_"):
            return []
        headers = {"X-API-KEY": self.serper_key, "Content-Type": "application/json"}
        payload = {"q": query, "num": num, "gl": gl, "hl": "en"}
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
                         "source_query": query, "gl": gl}
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
            model_name = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
            return generate_with_rotation(prompt, model=model_name)
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
        # Generate fresh randomized queries for this run across Dubai, NY, and India
        dynamic_queries = generate_random_static_queries()
        progress.update(task, total=len(dynamic_queries), description="Layer 1: Running segmented startup dorks (Dubai/NY/India)...")

        for query, tbs, gl in dynamic_queries:
            results.extend(self._serper_search(query, tbs=tbs, gl=gl))
            progress.advance(task)

        console.print(f"[dim]  Layer 1: {len(results)} raw results from {len(dynamic_queries)} segmented dorks[/dim]")
        return results

    # ─── Layer 2: Jobs → Companies → Leaders ──────────────────────────────────

    def _gather_job_based_leads(self, progress, task) -> list:
        # Step 2a: Fetch job postings from the past week across Dubai, NY, India
        job_results = []
        progress.update(task, description="Layer 2: Fetching job postings across Dubai, NY, India...")
        for query, gl in JOB_SOURCING_QUERIES:
            job_results.extend(self._serper_search(query, tbs="qdr:w", gl=gl))
            progress.advance(task)

        if not job_results:
            console.print("[dim]  Layer 2: No job postings found. Skipping.[/dim]")
            return []

        # Step 2b: Extract company names using Gemini
        postings_summary = json.dumps(
            [{"title": j.get("title", ""), "snippet": j.get("snippet", ""), "gl": j.get("gl", "in")} for j in job_results],
            indent=2
        )
        progress.update(task, description="Layer 2: Extracting companies via Gemini...")
        raw_text = self._gemini_call(
            COMPANY_EXTRACTION_PROMPT.format(postings=postings_summary),
            label="Company Extraction"
        )
        companies = self._extract_json(raw_text or "") if raw_text else None

        if not companies or not isinstance(companies, list):
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
                        description="Layer 2: Searching for leaders at extracted startups...")
        for company in companies:
            q = (f'site:linkedin.com/in ("Founder" OR "Co-Founder" OR "CEO" OR "Head of Sales" OR "VP Sales" OR "Head of Product" OR "VP Product" OR "Product Manager") "{company}" -intern -student')
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
                d_clean = re.sub(r'-(?:linkedin|google|microsoft|amazon|meta|apple|netflix|uber|walmart|salesforce|swiggy|zomato|flipkart|adobe)\b', '', d, flags=re.IGNORECASE)
                d_clean = " ".join(d_clean.split()).strip()
                clean_dorks.append(d_clean)
        dorks = clean_dorks[:12]
        console.print(f"[dim]  Layer 3: Running {len(dorks)} dynamic queries[/dim]")

        results = []
        progress.update(task, total=progress._tasks[task].total + len(dorks),
                        description="Layer 3: Running dynamic queries...")
        for dork in dorks:
            tbs = "qdr:w" if "linkedin.com/posts" in dork else "qdr:m"
            dork_lower = dork.lower()
            if any(k in dork_lower for k in ["dubai", "uae", "emirates", "abu dhabi"]):
                gl = "ae"
            elif any(k in dork_lower for k in ["new york", "nyc", "manhattan", "united states", "usa"]):
                gl = "us"
            else:
                gl = "in"
            results.extend(self._serper_search(dork, tbs=tbs, gl=gl))
            progress.advance(task)

        console.print(f"[dim]  Layer 3: {len(results)} raw results[/dim]")
        return results

    # ─── Gather + Filter ──────────────────────────────────────────────────────

    def gather_raw_leads(self, profile: Optional[dict] = None, dry_run: bool = False) -> list:
        if dry_run:
            console.print("[yellow]DRY RUN: Using mock leads[/yellow]")
            return self._mock_leads()

        all_results = []
        # Estimate total progress steps: 14 static/post queries + job queries + 12 dynamic dorks
        estimated_total = 14 + len(JOB_SOURCING_QUERIES) + 12
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
        if filtered and not dry_run:
            self.save_raw_leads(filtered)
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
        # Strip trailing '| linkedin' so Google search page titles don't trigger false positives
        clean_title = re.sub(r'[-—–|]\s*linkedin.*$', '', title, flags=re.IGNORECASE).strip()
        text    = clean_title + ' ' + snippet

        HIGH_ROLE = ['ceo', 'founder', 'cofounder', 'co-founder', 'coo', 'vp product',
                     'vp growth', 'vp sales', 'head of sales', 'head of product', 'head of growth',
                     'commercial director', 'managing director', 'director of sales',
                     'head of brand', 'chief product officer', 'director of product',
                     'director of growth', 'cpo']
        MED_ROLE  = ['product manager', 'lead product manager', 'group product manager',
                     'product lead', 'brand manager', 'marketing manager', 'growth manager',
                     'sales manager', 'business development', 'growth pm', 'apm',
                     'associate product manager', 'product strategy']
        GOOD_KEYWORDS = ['product', 'brand', 'building', 'business', 'growth', 'funnel',
                         'sales', 'b2b', 'saas', 'proptech', 'real estate', 'residential',
                         'property', 'user research', 'lead generation']
        HIRING_SIG = ['hiring', 'we are hiring', 'looking for', 'join us', 'open role']
        DISCARD    = ['intern', 'student', 'fresher', 'trainee', 'apprentice', 'undergraduate']
        
        STEALTH_OR_SOLO = [
            'stealth', 'in stealth', 'stealth mode', 'stealth startup',
            'solo founder', 'seeking co-founder', 'looking for co-founder',
            'looking for a co-founder', 'co-founder wanted', 'technical co-founder',
            'working on an idea', 'ideation stage', 'side project', 'dorm room',
            'pre-incorporation'
        ]
        BIG_TECH   = ['google', 'microsoft', 'amazon', 'apple', 'meta', 'uber',
                      'stripe', 'netflix', 'adobe', 'salesforce', 'swiggy', 'zomato',
                      'flipkart', 'cvent', 'sabre', 'coupa', 'facebook',
                      'walmart', 'atlassian', 'tcs', 'infosys', 'wipro', 'cognizant', 'accenture',
                      'emaar', 'damac', 'aldar', 'sobha', 'nakheel', 'meraas', 'omniyat']

        # Discard stealth, solo founders seeking co-founders, Big Tech, or intern/student leads immediately
        if any(kw in text for kw in STEALTH_OR_SOLO):
            return 0.0
        if any(kw in text for kw in BIG_TECH):
            return 0.0
        if any(kw in text for kw in DISCARD):
            return 0.0

        score = 0.35  # base
        if any(kw in text for kw in HIGH_ROLE):
            score += 0.35
        elif any(kw in text for kw in MED_ROLE):
            score += 0.20
        if any(kw in text for kw in GOOD_KEYWORDS):
            score += 0.15
        if any(kw in text for kw in HIRING_SIG):
            score += 0.10

        follower_count = self._parse_follower_count(text)
        if follower_count > 0:
            score += self._follower_bonus(follower_count)

        # Geographic and segment priority weighting (4 tiers):
        # Priority 1: Dubai / UAE Proptech & Property (Highest Priority)
        if any(kw in text for kw in ['dubai', 'uae', 'united arab emirates']):
            if any(kw in text for kw in ['proptech', 'real estate', 'property', 'residential']):
                score += 0.30
            else:
                score += 0.15
        # Priority 2: India Non-Tech (PM, Sales, Founder's Office)
        elif any(kw in text for kw in ['delhi', 'bengaluru', 'bangalore', 'hyderabad', 'pune', 'mumbai', 'gurgaon', 'noida', 'india']) and any(kw in text for kw in ['d2c', 'consumer', 'retail', 'logistics', 'operations', 'fmcg', 'supply chain', 'hospitality', 'services', 'fashion', 'brand']):
            score += 0.25
        # Priority 3: US Tech & Non-Tech Startups
        elif any(kw in text for kw in ['new york', 'nyc', 'manhattan', 'san francisco', 'austin', 'united states', 'usa']):
            score += 0.18
        # Priority 4: India Tech Startups
        elif any(kw in text for kw in ['delhi', 'bengaluru', 'bangalore', 'hyderabad', 'pune', 'mumbai', 'gurgaon', 'noida', 'india']):
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
            f"[cyan]  Passing ALL {len(candidates)} fresh leads directly to Gemini AI for multi-segment scoring...[/cyan]"
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
                # Pass ONLY what Gemini needs for evaluation (id, title, snippet) — no URLs or queries
                clean_chunk = [
                    {
                        "id": c_idx,
                        "title": c.get("title", ""),
                        "snippet": c.get("snippet", ""),
                    }
                    for c_idx, c in enumerate(chunk)
                ]
                prompt = SCORING_PROMPT.format(
                    profile_summary=profile_summary,
                    count=len(clean_chunk),
                    leads_json=json.dumps(clean_chunk, indent=2)
                )
                raw_text = self._gemini_call(prompt, label=f"Scoring batch {idx}")
                if raw_text:
                    batch_scored = self._extract_json(raw_text)
                    if batch_scored and isinstance(batch_scored, list):
                        for item in batch_scored:
                            if not isinstance(item, dict):
                                continue
                            item_id = item.get("id")
                            orig = None
                            if item_id is not None and isinstance(item_id, int) and 0 <= item_id < len(chunk):
                                orig = chunk[item_id]
                            else:
                                item_name = (item.get("name") or "").lower().strip()
                                for c in chunk:
                                    if item_name and item_name in (c.get("title") or "").lower():
                                        orig = c
                                        break
                            if not orig:
                                continue

                            # Take all original search fields (url, query, snippet) directly from Serper result
                            lead_scored = dict(orig)
                            p_name, p_role, p_company = self._parse_title_heuristics(orig.get("title", ""))
                            lead_scored["name"] = item.get("name") or p_name
                            lead_scored["company"] = item.get("company") or p_company
                            lead_scored["role"] = item.get("role") or p_role
                            lead_scored["score"] = float(item.get("score") or 0.0)
                            lead_scored["geo_segment"] = item.get("geo_segment") or self._infer_geo_segment(orig)
                            lead_scored["company_type"] = item.get("company_type") or self._infer_company_type(orig)
                            lead_scored["company_tier"] = item.get("company_tier") or self._infer_company_tier(orig)
                            lead_scored["pitch_track"] = item.get("pitch_track") or self._infer_pitch_track(orig)
                            lead_scored["discard_reason"] = item.get("discard_reason")
                            scored.append(lead_scored)
                        gemini_ok = True
                    else:
                        preview = (raw_text or "")[:400].replace("\n", " ")
                        console.print(f"[yellow]  Batch {idx}: JSON parse failed. "
                                      f"Response preview: {preview}[/yellow]")
                        for lead in chunk:
                            p_name, p_role, p_company = self._parse_title_heuristics(lead.get("title", ""))
                            lead["score"] = self._heuristic_score(lead)
                            lead["name"] = lead.get("name") or p_name
                            lead["role"] = lead.get("role") or p_role
                            lead["company"] = lead.get("company") or p_company
                            lead["geo_segment"] = self._infer_geo_segment(lead)
                            lead["company_type"] = self._infer_company_type(lead)
                            lead["pitch_track"] = self._infer_pitch_track(lead)
                            lead["discard_reason"] = "heuristic_fallback"
                            lead["source_query"] = lead.get("source_query", "")
                            lead["linkedin_url"] = lead.get("url", "")
                            scored.append(lead)
                else:
                    console.print(f"[yellow]  Batch {idx}: Gemini call failed — using heuristic.[/yellow]")
                    for lead in chunk:
                        p_name, p_role, p_company = self._parse_title_heuristics(lead.get("title", ""))
                        lead["score"] = self._heuristic_score(lead)
                        lead["name"] = lead.get("name") or p_name
                        lead["role"] = lead.get("role") or p_role
                        lead["company"] = lead.get("company") or p_company
                        lead["geo_segment"] = self._infer_geo_segment(lead)
                        lead["company_type"] = self._infer_company_type(lead)
                        lead["pitch_track"] = self._infer_pitch_track(lead)
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
        INTERN_KEYWORDS = [
            "intern", "internship", "student", "trainee", "fresher",
            "apprentice", "undergraduate", "postgraduate",
        ]
        INDIAN_BIG_CORP = [
            "google", "microsoft", "amazon", "apple", "meta", "uber",
            "stripe", "netflix", "adobe", "salesforce", "swiggy", "zomato",
            "flipkart", "cvent", "sabre", "coupa", "walmart", "atlassian",
            "tcs", "infosys", "wipro", "cognizant", "accenture", "reliance",
            "tata", "paytm", "ola", "byju", "cred", "meesho", "delhivery",
            "zepto", "blinkit", "phonepe", "razorpay", "groww"
        ]
        STEALTH_OR_SOLO_KEYWORDS = [
            "stealth", "in stealth", "stealth mode", "stealth startup",
            "solo founder", "seeking co-founder", "looking for co-founder",
            "looking for a co-founder", "co-founder wanted", "technical co-founder",
            "working on an idea", "ideation stage", "side project", "dorm room",
            "pre-incorporation"
        ]

        def _is_valid_startup_lead(lead: dict) -> bool:
            score = lead.get("score", 0)
            if score < 0.4:
                return False

            role = (lead.get("role") or "").lower()
            name = (lead.get("name") or "").lower()
            company = (lead.get("company") or "").lower().strip()
            snippet = (lead.get("snippet") or "").lower()
            combined = f"{role} {company} {snippet}"

            # Must have a valid, non-empty company name
            if not company or company in ("—", "null", "none", "unknown", "undefined"):
                return False
            # Must have a valid LinkedIn profile URL
            u = lead.get("url") or lead.get("linkedin_url")
            if not u or not isinstance(u, str) or not u.strip():
                return False
            # Must not be an intern or student
            if any(kw in role or kw in name for kw in INTERN_KEYWORDS):
                return False
            # Must not be stealth or solo-founder seeking co-founder
            if any(kw in combined for kw in STEALTH_OR_SOLO_KEYWORDS):
                return False

            # Ensure classification tags are set
            if not lead.get("pitch_track"):
                lead["pitch_track"] = self._infer_pitch_track(lead)
            if not lead.get("geo_segment"):
                lead["geo_segment"] = self._infer_geo_segment(lead)
            if not lead.get("company_type"):
                lead["company_type"] = self._infer_company_type(lead)
            if not lead.get("company_tier"):
                lead["company_tier"] = self._infer_company_tier(lead)

            # For Indian companies ONLY, strictly enforce startup filter (no big corporate monoliths)
            geo = lead["geo_segment"].lower()
            if geo in ("india", "in"):
                if any(kw in company for kw in INDIAN_BIG_CORP):
                    return False
                if any(f"at {kw}" in role or f"@{kw}" in role for kw in INDIAN_BIG_CORP):
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

        console.print(f"[green]✓ {len(scored)} scored → {len(valid)} qualified leads → {len(top)} selected[/green]")

        table = Table(title="Today's Segmented Leads", header_style="bold cyan")
        table.add_column("#", width=3)
        table.add_column("Name", width=18)
        table.add_column("Role", width=20)
        table.add_column("Company", width=16)
        table.add_column("Segment", width=10)
        table.add_column("Tier", width=11)
        table.add_column("Track", width=22)
        table.add_column("Score", width=6)
        for i, l in enumerate(top, 1):
            s = l.get("score", 0)
            c = "green" if s >= 0.7 else "yellow" if s >= 0.5 else "white"
            seg = (l.get("geo_segment") or "—").upper()
            tier = (l.get("company_tier") or "startup").upper()
            track = l.get("pitch_track") or "—"
            table.add_row(str(i), l.get("name") or "?", l.get("role") or "—",
                          l.get("company") or "—", seg, tier, track, f"[{c}]{s:.2f}[/{c}]")
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

    def save_raw_leads(self, raw_leads: list) -> None:
        """Cache raw search results immediately to disk before Gemini scoring."""
        DATA_DIR.mkdir(exist_ok=True)
        payload = {
            "date": datetime.now().isoformat(),
            "count": len(raw_leads),
            "raw_leads": raw_leads
        }
        try:
            with open(RAW_LEADS_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            console.print(f"[dim]  Cached {len(raw_leads)} raw search results to data/{RAW_LEADS_PATH.name}[/dim]")
        except Exception as e:
            console.print(f"[yellow]Failed to cache raw leads: {e}[/yellow]")

    def load_raw_leads(self) -> list:
        """Load cached raw search results from data/raw_leads_today.json if available."""
        if RAW_LEADS_PATH.exists():
            try:
                with open(RAW_LEADS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("raw_leads", [])
            except Exception as e:
                console.print(f"[yellow]Failed to load cached raw leads: {e}[/yellow]")
        return []

    def mark_contacted(self, profile_url: str) -> None:
        seen = self._load_seen_profiles()
        seen.add(profile_url)
        self._save_seen_profiles(seen)

    # ─── Main Entry Point ─────────────────────────────────────────────────────

    def run(self, profile: dict, dry_run: bool = False, raw_leads: Optional[list] = None) -> list:
        console.print("\n[bold cyan]━━━ Phase 1: Discovery Engine (Hybrid) ━━━[/bold cyan]")
        if raw_leads:
            console.print(f"[bold green]▶ Using {len(raw_leads)} cached raw search leads (skipping Serper Google search)...[/bold green]")
            raw = raw_leads
        else:
            raw = self.gather_raw_leads(profile=profile, dry_run=dry_run)
        leads = self.score_and_filter(raw, profile)
        if leads:
            self.save_leads(leads)
        return leads

    # ─── Mock Data (dry-run only) ──────────────────────────────────────────────

    def _mock_leads(self) -> list:
        return [
            {"url": "https://www.linkedin.com/in/jad-halaoui-mock/",
             "title": "Jad Halaoui - Co-Founder & COO at Huspy | LinkedIn",
             "snippet": "Huspy is a proptech platform transforming real estate transactions in Dubai and UAE. 60-person team.",
             "date": "2 days ago", "source_query": "mock"},
            {"url": "https://www.linkedin.com/in/sarah-jenkins-mock/",
             "title": "Sarah Jenkins - Head of Sales at Premier Luxury Real Estate | LinkedIn",
             "snippet": "Leading luxury residential property sales and client acquisition across Dubai.",
             "date": "3 days ago", "source_query": "mock"},
            {"url": "https://www.linkedin.com/in/vikram-sethi-mock/",
             "title": "Vikram Sethi - Product Manager at Mokobara | LinkedIn",
             "snippet": "Leading direct-to-consumer D2C e-commerce checkout and discovery in Bangalore.",
             "date": "5 days ago", "source_query": "mock"},
            {"url": "https://www.linkedin.com/in/rohan-deshmukh-mock/",
             "title": "Rohan Deshmukh - Head of Business Development at KwikLogistics | LinkedIn",
             "snippet": "Scaling commercial enterprise fleet operations and supply chain in Gurgaon Delhi NCR.",
             "date": "1 week ago", "source_query": "mock"},
        ]
