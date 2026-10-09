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
    """Add a profile URL to the seen list incrementally, rejecting retry, transient errors, or blank statuses."""
    if not profile_url:
        return
    if status:
        s_clean = str(status).strip().lower()
        if s_clean in ("retry", "", "connect_button_missing", "click_failed"):
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


def close_active_message_boxes(page) -> bool:
    """
    Detects and closes any active or lingering LinkedIn message boxes/overlays:
    - Open conversation bubbles (.msg-overlay-conversation-bubble, .msg-convo-wrapper)
    - Open chat headers or overlays (aside.msg-overlay-container)
    - Expanded messaging tray (.msg-overlay-list-bubble)
    - InMail or direct message compose modals
    Ensures profile buttons (Connect, More, Dropdowns) are completely visible,
    unobstructed, and interactive.
    Returns True if an active box was detected and closed.
    """
    if not page:
        return False

    closed_any = False
    try:
        # 1. Comprehensive check: Is any conversation bubble, chat header, or expanded messaging tray present?
        has_active_overlay = page.evaluate("""
            () => {
                // Check for conversation bubbles or open chat windows
                const bubbles = Array.from(document.querySelectorAll(
                    '.msg-overlay-conversation-bubble, .msg-convo-wrapper, [data-view-name="message-overlay"], ' +
                    'aside.msg-overlay-container .msg-overlay-conversation-bubble, div[data-control-name="overlay.conversation"], ' +
                    '.msg-thread, .msg-overlay-bubble-header, aside.msg-overlay-container div[role="dialog"]'
                ));
                const isAnyBubbleVisible = bubbles.some(b => {
                    const r = b.getBoundingClientRect();
                    return r.width > 20 && r.height > 20 && r.top < window.innerHeight;
                });

                // Check for expanded messaging list tray (not minimized)
                const listBubble = document.querySelector('.msg-overlay-list-bubble');
                const isListExpanded = listBubble && (
                    listBubble.classList.contains('msg-overlay-list-bubble--is-expanded') ||
                    (!listBubble.classList.contains('msg-overlay-list-bubble--is-minimized') && listBubble.getBoundingClientRect().height > 80)
                );

                // Check for any modal dialogs containing message form
                const msgModals = Array.from(document.querySelectorAll(
                    '.msg-compose-modal, div[role="dialog"]:has(textarea[name="message"]), div[role="dialog"]:has(.msg-form), ' +
                    '.artdeco-modal:has(textarea[name="message"])'
                ));
                const isModalVisible = msgModals.some(m => {
                    const r = m.getBoundingClientRect();
                    return r.width > 50 && r.height > 50;
                });

                return isAnyBubbleVisible || isListExpanded || isModalVisible;
            }
        """)

        if not has_active_overlay:
            return False

        console.print("  [cyan]  🧹 Active LinkedIn message box detected — closing it first...[/cyan]")

        # 2. Try closing conversation bubbles via Playwright native clicks
        close_selectors = [
            "button[data-control-name='overlay.close_conversation_window']",
            "button.msg-overlay-bubble-header__control--close",
            "button.msg-overlay-conversation-bubble__button-close",
            "button[aria-label*='Close your conversation' i]",
            "button[aria-label*='Close conversation' i]",
            "button[aria-label*='Close chat' i]",
            "button[aria-label*='Dismiss' i]",
            "button[data-view-name='chat-close']",
            ".msg-overlay-conversation-bubble header button:has(svg[data-test-icon*='close'])",
            ".msg-overlay-conversation-bubble header button:has(li-icon[type*='cancel'])",
            ".msg-overlay-conversation-bubble header button:has(li-icon[type*='close'])",
            ".msg-overlay-bubble-header__control--close",
            "button.artdeco-modal__dismiss",
        ]

        for sel in close_selectors:
            try:
                close_btns = page.locator(sel).all()
                for btn in close_btns:
                    try:
                        if btn.is_visible(timeout=300):
                            btn.click(force=True, timeout=600)
                            closed_any = True
                            page.wait_for_timeout(200)
                    except Exception:
                        pass
            except Exception:
                pass

        # 3. Comprehensive JavaScript dismissal & event dispatch
        js_closed = page.evaluate("""
            () => {
                let count = 0;
                // Target all close / dismiss buttons in conversation bubbles and overlays
                const buttons = document.querySelectorAll(
                    '.msg-overlay-conversation-bubble button[data-control-name="overlay.close_conversation_window"], ' +
                    '.msg-overlay-conversation-bubble button.msg-overlay-bubble-header__control--close, ' +
                    '.msg-overlay-conversation-bubble button.msg-overlay-conversation-bubble__button-close, ' +
                    '.msg-overlay-conversation-bubble button[aria-label*="Close" i], ' +
                    '.msg-overlay-conversation-bubble button[aria-label*="Dismiss" i], ' +
                    '.msg-overlay-conversation-bubble button[data-view-name="chat-close"], ' +
                    '.msg-overlay-bubble-header__control--close, ' +
                    '.msg-overlay-conversation-bubble header button:last-child, ' +
                    'aside.msg-overlay-container button[aria-label*="Close" i]'
                );
                buttons.forEach(b => {
                    try {
                        b.click();
                        ['mousedown', 'mouseup', 'click'].forEach(evt => {
                            b.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                        });
                        count++;
                    } catch(e) {}
                });

                // Check for expanded list bubble and collapse/minimize it
                const listBubble = document.querySelector('.msg-overlay-list-bubble');
                if (listBubble && !listBubble.classList.contains('msg-overlay-list-bubble--is-minimized')) {
                    const collapseBtn = listBubble.querySelector(
                        'button[data-control-name="overlay.collapse_list_window"], ' +
                        'button[aria-label*="Collapse" i], button[aria-label*="Minimize" i], ' +
                        '.msg-overlay-bubble-header'
                    );
                    if (collapseBtn) {
                        try { collapseBtn.click(); } catch(e) {}
                        count++;
                    }
                }

                return count;
            }
        """)
        if js_closed > 0:
            closed_any = True
            page.wait_for_timeout(300)

        # 4. Press Escape to dismiss any popups/modals
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
        except Exception:
            pass

        # 5. Fail-safe: Hide any remaining conversation bubbles
        still_visible = page.evaluate("""
            () => {
                const remaining = Array.from(document.querySelectorAll(
                    '.msg-overlay-conversation-bubble, .msg-convo-wrapper, [data-view-name="message-overlay"], aside.msg-overlay-container .msg-overlay-conversation-bubble'
                ));
                let count = 0;
                remaining.forEach(b => {
                    const r = b.getBoundingClientRect();
                    if (r.width > 20 && r.height > 20) {
                        b.style.display = 'none';
                        b.setAttribute('aria-hidden', 'true');
                        b.setAttribute('data-antigravity-suppressed', 'true');
                        count++;
                    }
                });
                return count;
            }
        """)
        if still_visible > 0:
            closed_any = True

        if closed_any:
            console.print("  [green]  ✓ Closed active LinkedIn message box.[/green]")

        return closed_any
    except Exception as e:
        console.print(f"  [dim]  Note on closing message box: {e}[/dim]")
        return False


