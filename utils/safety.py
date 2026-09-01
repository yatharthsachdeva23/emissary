"""
Safety utilities — Emissary
Rate limiting, human-mimicry helpers, abort detection, and safety guards.
"""

import json
import os
import random
import re
import time
from pathlib import Path
from typing import Tuple
from rich.console import Console

console = Console()

DATA_DIR = Path(__file__).parent.parent / "data"
SEEN_PATH = DATA_DIR / "seen_profiles.json"

# Absolute hard caps — never exceed these
ABSOLUTE_DAILY_MAX = int(os.getenv("MAX_SAFETY_CAP", "50"))
ABSOLUTE_BATCH_MAX = 10


def human_sleep(min_sec: float, max_sec: float, label: str = "") -> None:
    """Sleep for a random human-like duration."""
    duration = random.uniform(min_sec, max_sec)
    if label:
        console.print(f"[dim]  ⏱  {label} ({duration:.1f}s)[/dim]")
    time.sleep(duration)


def batch_sleep(min_min: float = 15.0, max_min: float = 25.0) -> None:
    """Sleep between batches — long, randomised, human-like."""
    duration_min = random.uniform(min_min, max_min)
    duration_sec = duration_min * 60
    console.print(
        f"[yellow]  ⏸  Batch complete. Waiting {duration_min:.1f} minutes before next batch...[/yellow]"
    )
    # Countdown in 60-second chunks
    remaining = duration_sec
    while remaining > 0:
        chunk = min(60, remaining)
        time.sleep(chunk)
        remaining -= chunk
        if remaining > 0:
            console.print(f"[dim]  ... {remaining/60:.1f} min remaining[/dim]")


def is_weekend() -> bool:
    """Check if today is a weekend."""
    from datetime import datetime
    return datetime.now().weekday() >= 5  # Saturday=5, Sunday=6


def get_effective_daily_limit(configured_limit: int) -> int:
    """Return the configured limit capped by the absolute daily max."""
    return min(max(1, configured_limit), ABSOLUTE_DAILY_MAX)


def check_abort_conditions(page) -> Tuple[bool, str]:
    """
    Check if LinkedIn is showing warning signs.
    Returns (should_abort, reason).
    """
    try:
        url = page.url
        
        # CAPTCHA detected via URL
        if "checkpoint" in url or "captcha" in url.lower():
            return True, "CAPTCHA / Checkpoint page detected"

        # Try to get content, but don't abort the whole system if it fails (e.g. mid-navigation)
        try:
            content = page.content().lower()
        except Exception:
            return False, "" # Page is likely mid-navigation, not an abort condition

        # Unusual activity warning
        if "unusual activity" in content or "verify" in url.lower():
            return True, "Unusual activity warning detected"

        # Invitation limit reached - strictly look for the warning modal
        try:
            if page.locator("div[role='dialog'] h2:has-text('weekly invitation limit')").is_visible(timeout=500) or \
               page.locator("div[role='dialog'] h2:has-text('out of invitations')").is_visible(timeout=500):
                return True, "LinkedIn invitation limit reached"
        except Exception:
            pass

        # Account restriction - Use specific phrases to avoid false positives on profiles
        # that mention these words in their job descriptions.
        if "your account is restricted" in content or "your account has been restricted" in content:
            return True, "Account restriction detected"

        return False, ""

    except Exception as e:
        # If we can't even get the URL, something is fundamentally wrong with the browser context
        if "context was destroyed" in str(e).lower() or "target closed" in str(e).lower():
            return True, f"Browser context lost: {e}"
        return False, "" # Ignore other transient errors during safety checks


def get_typing_delay() -> float:
    """Random per-character typing delay in milliseconds."""
    return random.uniform(50, 150)


def random_scroll_params() -> tuple[int, int]:
    """Return random scroll distance and duration for human-like scrolling."""
    distance = random.randint(200, 600)
    duration = random.randint(300, 800)
    return distance, duration


def load_seen_profiles() -> list:
    """Load the list of already seen/contacted LinkedIn profile URLs (ordered)."""
    if SEEN_PATH.exists():
        try:
            with open(SEEN_PATH, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return []
                return list(json.loads(content))
        except Exception as e:
            console.print(f"[red]Error loading seen_profiles.json: {e}. Raising error to prevent overwriting history.[/red]")
            raise e
    return []


def save_seen_profiles(seen: list) -> None:
    """Save the list of already seen/contacted LinkedIn profile URLs."""
    DATA_DIR.mkdir(exist_ok=True)
    try:
        with open(SEEN_PATH, "w", encoding="utf-8") as f:
            json.dump(seen, f, indent=2)
    except Exception as e:
        console.print(f"[red]Error saving seen profiles: {e}[/red]")


def mark_contacted(profile_url: str, status: str = None) -> None:
    """Add a profile URL to the seen list incrementally, rejecting retry or blank statuses."""
    if not profile_url:
        return
    if status:
        s_clean = str(status).strip().lower()
        if s_clean in ("retry", ""):
            return
    seen = load_seen_profiles()
    if profile_url not in seen:
        seen.append(profile_url)
        save_seen_profiles(seen)


def is_same_company_name(target: str, profile: str) -> bool:
    """
    Robust company name comparison that handles:
    1. Legal/Business suffixes ('Services', 'Pvt', 'Ltd', 'India', 'Inc', 'Co', 'Group', 'Technologies', 'Tech', 'Solutions', 'Media', 'Digital', 'Global', 'Enterprises', 'Corp', 'LLP').
    2. Compound brand names ('Utopian Drinks / Nubu Kids' vs 'Utopian Drinks').
    3. Substring inclusion ('Valueleaf' vs 'Valueleaf Services (India) Pvt. Ltd.').
    """
    if not target or not profile:
        return False
        
    t_raw = target.strip().lower()
    p_raw = profile.strip().lower()
    
    if t_raw == p_raw or t_raw in p_raw or p_raw in t_raw:
        return True

    def _normalize(text: str) -> str:
        suffixes = [
            r'\bservices\b', r'\bpvt\b', r'\bltd\b', r'\bprivate\b', r'\blimited\b',
            r'\bindia\b', r'\binc\b', r'\bco\b', r'\bgroup\b', r'\btechnologies\b',
            r'\btech\b', r'\bsolutions\b', r'\bmedia\b', r'\bdigital\b', r'\bglobal\b',
            r'\benterprises\b', r'\bcorp\b', r'\bcorporation\b', r'\bllp\b', r'\bholdings\b',
            r'\bprevious\b', r'\bformer\b'
        ]
        t = text.lower()
        for s in suffixes:
            t = re.sub(s, ' ', t)
        t = re.sub(r'[.,;!|@•/\-\(\)]', ' ', t)
        return " ".join(t.split()).strip()

    t_norm = _normalize(target)
    p_norm = _normalize(profile)

    if not t_norm or not p_norm:
        return False

    if t_norm == p_norm or t_norm in p_norm or p_norm in t_norm:
        return True

    # Word-set overlap check (e.g. "Utopian Drinks" vs "Utopian Drinks / Nubu Kids")
    t_words = set(t_norm.split())
    p_words = set(p_norm.split())
    if t_words and t_words.issubset(p_words):
        return True
    if p_words and p_words.issubset(t_words):
        return True

    return False
