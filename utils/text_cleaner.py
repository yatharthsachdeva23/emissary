"""
Text Cleaner Utility — Emissary
Normalizes lead names and company names for natural, conversational cold outreach:
1. Strips professional titles / honorifics (Dr., Prof., CA, etc.) from first names.
2. Strips legal entity designations (Pvt. Ltd., Inc., LLC, etc.) from company names.
"""

import re
from typing import Set

HONORIFICS: Set[str] = {
    "dr", "doctor",
    "prof", "professor",
    "ca", "c.a",
    "cs", "c.s",
    "adv", "advocate",
    "er",
    "mr", "mister",
    "mrs",
    "ms",
    "miss",
    "shri", "smt",
    "col", "colonel",
    "capt", "captain",
    "major",
    "lt", "lieutenant",
}

LEGAL_PATTERNS = [
    r',?\s*\b(pvt\.?\s*ltd\.?|private\s+limited)\b',
    r',?\s*\b(pty\.?\s*ltd\.?)\b',
    r',?\s*\b(ltd\.?|limited)\b',
    r',?\s*\b(inc\.?|incorporated)\b',
    r',?\s*\b(llc|l\.l\.c\.)\b',
    r',?\s*\b(corp\.?|corporation)\b',
    r',?\s*\b(gmbh)\b',
    r',?\s*\b(co\.?|company)\b$',
]


def clean_first_name(name: str) -> str:
    """
    Extract a clean, natural first name from a LinkedIn profile name string.
    - Strips bracketed tokens like (He/Him), [Hiring], etc.
    - Strips honorifics / titles (Dr., Prof., CA, Mr., etc.)
    - Handles initials (e.g., 'Dr. A. B. Roy' -> 'Roy')
    - Returns clean alpha name or 'there' fallback.
    """
    if not name:
        return "there"

    # Remove parentheticals, brackets, and emojis
    name_clean = re.sub(r'[\(\[\{].*?[\)\]\}]', '', name).strip()
    tokens = name_clean.split()
    if not tokens:
        return "there"

    idx = 0
    # Skip leading honorifics / titles
    while idx < len(tokens):
        norm = tokens[idx].lower().strip(".,/-_:")
        if norm in HONORIFICS:
            idx += 1
        else:
            break

    remaining_tokens = tokens[idx:]
    if not remaining_tokens:
        return "there"

    # Look for candidate name:
    # If initial tokens are single-letter initials (e.g. 'A.', 'B.' in 'Dr. A. B. Roy'),
    # scan forward to find the first multi-letter name token
    multi_tokens = [
        "".join(c for c in t if c.isalpha())
        for t in remaining_tokens
    ]
    valid_multi = [c for c in multi_tokens if len(c) > 1 and c.lower() not in HONORIFICS]
    if valid_multi:
        return valid_multi[0]

    # Fallback if only single-letter tokens exist (e.g., 'J.')
    for t in remaining_tokens:
        clean = "".join(c for c in t if c.isalpha())
        if clean and clean.lower() not in HONORIFICS:
            return clean

    return "there"


def clean_company_name(company: str) -> str:
    """
    Cleans formal corporate entity suffixes and clutter from company names for natural conversation.
    E.g.
      "OsteoForge MedTech Pvt. Ltd." -> "OsteoForge MedTech"
      "Hetero Healthcare Ltd"        -> "Hetero Healthcare"
      "UnifyApps, Inc."              -> "UnifyApps"
      "Gangs Of Hackpur (startup)"   -> "Gangs Of Hackpur"
    """
    if not company or company.strip().lower() in ("unknown", "none", ""):
        return company or ""

    clean = company.strip()

    # 1. Remove parentheticals (e.g. "Gangs Of Hackpur (startup)" -> "Gangs Of Hackpur")
    clean = re.sub(r'[\(\[\{].*?[\)\]\}]', '', clean).strip()

    # 2. Remove trailing legal entity suffixes
    for pat in LEGAL_PATTERNS:
        clean = re.sub(pat, '', clean, flags=re.IGNORECASE).strip()

    # 3. Clean trailing punctuation/dashes
    clean = re.sub(r'[\s,\.\-]+$', '', clean).strip()

    return clean if clean else company
