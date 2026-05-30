"""
Safety utilities — Emissary
Rate limiting, human-mimicry helpers, abort detection.
"""

import random
import time
from pathlib import Path
from typing import Tuple
from rich.console import Console

console = Console()

# Absolute hard caps — never exceed these
ABSOLUTE_DAILY_MAX = 50
ABSOLUTE_BATCH_MAX = 5


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
    """Return the configured limit capped by the absolute daily max, ignoring weekends."""
    return min(configured_limit, ABSOLUTE_DAILY_MAX)


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
