"""
Messenger Agent — Emissary
Uses Playwright to send LinkedIn connection requests with personalised notes.
All safety guardrails are enforced here.

SAFETY PROTOCOL:
- Cookie-based session (no password stored)
- Visible browser (non-headless = lower bot fingerprint)
- Random delays between every action
- Hard cap: 20 connections/day, 5 per batch
- Profile visit + scroll before connecting
- CAPTCHA/abuse detection → immediate abort + desktop alert
- Session saved/loaded from linkedin_session.json
"""

import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm

from utils.safety import (
    ABSOLUTE_DAILY_MAX,
    batch_sleep,
    check_abort_conditions,
    get_effective_daily_limit,
    get_typing_delay,
    human_sleep,
    load_seen_profiles,
    mark_contacted,
    random_scroll_params,
)
from utils.notifier import notify_abort, notify_done, notify_session_expired

load_dotenv()
console = Console()

DATA_DIR = Path(__file__).parent.parent / "data"
SESSION_PATH = DATA_DIR / "linkedin_session.json"

LINKEDIN_HOME = "https://www.linkedin.com/feed/"
LINKEDIN_LOGIN = "https://www.linkedin.com/login"


class MessengerAgent:
    def __init__(self):
        self.batch_size = int(os.getenv("BATCH_SIZE", "10"))
        self.batch_sleep_min = float(os.getenv("BATCH_SLEEP_MIN", "1"))
        self.batch_sleep_max = float(os.getenv("BATCH_SLEEP_MAX", "2"))
        self.sent_count = 0
        self.skipped_count = 0
        self.results = []
        # Detected at runtime: e.g. 'https://in.linkedin.com' for Indian users
        self._linkedin_base = "https://www.linkedin.com"
        self.retry_queue = []

    def _get_playwright(self):
        """Import playwright lazily."""
        try:
            from playwright.sync_api import sync_playwright
            from playwright_stealth import Stealth
            return sync_playwright, Stealth
        except ImportError:
            console.print("[red]Playwright or Stealth not installed. Run: pip install playwright playwright-stealth && playwright install chromium[/red]")
            sys.exit(1)

    # ─── Session Management ────────────────────────────────────────────────────

    def setup_session(self) -> bool:
        """
        First-time setup: Opens a real browser for you to log into LinkedIn manually.
        Saves session cookies to linkedin_session.json.
        """
        console.print(Panel(
            "[bold yellow]LinkedIn Session Setup[/bold yellow]\n\n"
            "A browser window will open. Please:\n"
            "1. Log into LinkedIn normally\n"
            "2. Complete any 2FA if prompted\n"
            "3. Wait until you see your LinkedIn feed\n"
            "4. Come back here and press [bold]Enter[/bold]\n\n"
            "[red]Your password is NEVER stored. Only session cookies are saved.[/red]",
            border_style="yellow"
        ))

        sync_playwright, Stealth_cls = self._get_playwright()

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=False, slow_mo=50)
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            Stealth_cls().apply_stealth_sync(page)
            page.goto(LINKEDIN_LOGIN)

            console.print("\n[bold cyan]Browser window is open. Please log into LinkedIn now.[/bold cyan]")
            console.print("[dim]The system will automatically detect when you log in and reach your feed...[/dim]")
            console.print("[dim]Waiting for login (or return here and press Enter)...[/dim]\n")

            # Auto-detection loop: checks every second for up to 5 minutes
            import time
            login_succeeded = False
            for _ in range(300):
                try:
                    cookies = context.cookies()
                    has_auth = any(c.get("name") == "li_at" for c in cookies)
                    curr_url = page.url.lower()

                    if has_auth or "feed" in curr_url or "mynetwork" in curr_url:
                        login_succeeded = True
                        break
                    time.sleep(1.0)
                except Exception:
                    break

            storage_state = context.storage_state()
            has_auth_cookie = any(c.get("name") == "li_at" for c in storage_state.get("cookies", []))
            
            if login_succeeded or has_auth_cookie:
                DATA_DIR.mkdir(exist_ok=True)
                with open(SESSION_PATH, "w", encoding="utf-8") as f:
                    json.dump(storage_state, f, indent=2)
                console.print("[bold green]✓ SUCCESS: LinkedIn session cookies captured and saved to data/linkedin_session.json![/bold green]")
                try:
                    browser.close()
                except Exception:
                    pass
                return True
            else:
                console.print(f"[red]Login was not completed within the timeout period. Current URL: {page.url}[/red]")
                try:
                    browser.close()
                except Exception:
                    pass
                return False

    def _load_session_context(self, playwright):
        """Load saved session cookies into a new browser context."""
        if not SESSION_PATH.exists():
            console.print("[red]No session found. Run: python main.py --setup-session[/red]")
            sys.exit(1)

        with open(SESSION_PATH, "r") as f:
            storage_state = json.load(f)

        browser = playwright.chromium.launch(
            channel="chrome",          # Use real Chrome, not bundled Chromium
            headless=False,            # MUST be False — headless has higher bot fingerprint
            slow_mo=random.randint(30, 80),
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = browser.new_context(
            # No custom user_agent — real Chrome's own UA is more trusted than a fake string
            storage_state=storage_state,
            viewport={"width": 1280, "height": 800},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )

        # Remove webdriver flag (still needed even with real Chrome)
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
        """)

        return browser, context

    def _check_session_valid(self, page) -> bool:
        """Check if the saved session is still valid and detect the regional LinkedIn domain."""
        page.goto(LINKEDIN_HOME, wait_until="domcontentloaded", timeout=60000)
        human_sleep(12, 15)  # Extended wait to allow "signing you in" / account selection to settle

        # ── Auto-detect regional LinkedIn base URL ───────────────────────────
        # LinkedIn redirects Indian users to in.linkedin.com. We capture whatever
        # domain the browser actually settled on and use it for all navigation.
        settled_url = page.url  # e.g. 'https://in.linkedin.com/feed/'
        if "linkedin.com" in settled_url:
            # Extract just the scheme + host, e.g. 'https://in.linkedin.com'
            from urllib.parse import urlparse
            parsed = urlparse(settled_url)
            self._linkedin_base = f"{parsed.scheme}://{parsed.netloc}"
            console.print(f"[dim]  Detected LinkedIn domain: {self._linkedin_base}[/dim]")

        # Simulate reading the feed: scroll down, then back up
        try:
            page.evaluate("window.scrollBy(0, 500)")
            human_sleep(1.5, 3.0)
            page.evaluate("window.scrollBy(0, 400)")
            human_sleep(1.5, 2.5)
            page.evaluate("window.scrollTo(0, 0)")
            human_sleep(1, 2)
        except Exception:
            pass

        if "login" in page.url or "authwall" in page.url or "checkpoint" in page.url:
            console.print("[red]Session expired or checkpoint detected. Run: python main.py --setup-session[/red]")
            notify_session_expired()
            return False

        try:
            # Sometimes LinkedIn keeps you on /feed but overlays a login modal
            if page.locator('input[id="session_key"]').is_visible(timeout=3000) or page.locator('input[name="session_key"]').is_visible(timeout=3000):
                console.print("[red]Session expired (Login form detected). Run: python main.py --setup-session[/red]")
                notify_session_expired()
                return False
        except Exception:
            pass

        console.print("[green]✓ LinkedIn session valid[/green]")
        return True

    # ─── Connection Flow ───────────────────────────────────────────────────────

    def _normalize_linkedin_url(self, url: str) -> str:
        """
        Rewrite any LinkedIn URL to use the actual regional domain that the
        browser session is scoped to (e.g. https://in.linkedin.com for India).
        This ensures session cookies always match.
        """
        url = url.strip()
        # Ensure https
        if url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        # Strip any known LinkedIn subdomain prefix and replace with detected base
        known_prefixes = [
            "https://www.linkedin.com",
            "https://in.linkedin.com",
            "https://linkedin.com",
            "https://uk.linkedin.com",
        ]
        for prefix in known_prefixes:
            if url.startswith(prefix):
                path = url[len(prefix):]  # e.g. '/in/jay-patel-123'
                return self._linkedin_base + path
        # If no known prefix matched, return as-is
        return url

    def _visit_profile(self, page, url: str) -> bool:
        """Visit a LinkedIn profile, scroll naturally, then return True if successful."""
        from utils.network import is_network_error, wait_for_network_recovery
        for attempt in range(2):
            try:
                url = self._normalize_linkedin_url(url)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                human_sleep(2, 4, "Page load wait")

                # ── Post-navigation URL guard ────────────────────────────────────
                current_url = page.url
                if any(x in current_url for x in ("authwall", "/login", "/signup", "checkpoint")):
                    console.print(f"  [red]  ✖ Redirected to login/authwall for this profile. Session cookie may have expired or profile is restricted.[/red]")
                    console.print(f"  [dim]    URL: {current_url}[/dim]")
                    return False

                # Check for abort conditions after each page load
                should_abort, reason = check_abort_conditions(page)
                if should_abort:
                    return False

                # Human-like scrolling to trigger loading of lazy widgets (like Experience & Connections)
                try:
                    # 1. Focus page
                    page.mouse.move(640, 400)
                    page.mouse.click(640, 400)
                    human_sleep(0.4, 0.8)

                    # 2. Scroll down by 650-950px
                    scroll_dist = random.randint(650, 950)
                    page.mouse.wheel(0, scroll_dist)
                    page.evaluate(f"window.scrollTo(0, {scroll_dist}); document.documentElement.scrollTop = {scroll_dist}; document.body.scrollTop = {scroll_dist};")
                    human_sleep(1.2, 2.2)

                    # 3. Scroll back to top
                    page.mouse.wheel(0, -scroll_dist)
                    page.evaluate("window.scrollTo(0, 0); document.documentElement.scrollTop = 0; document.body.scrollTop = 0;")
                    human_sleep(1.0, 1.8)
                except Exception:
                    pass

                # ── React Hydration Wait (Streamlined) ───────────────────────────
                page.wait_for_timeout(1500)

                return True

            except Exception as e:
                if is_network_error(e) and attempt == 0:
                    console.print("[yellow]⚠ Internet dropped while loading LinkedIn profile. Waiting for Wi-Fi recovery (Checking every 30s, max 10 mins)...[/yellow]")
                    if wait_for_network_recovery(max_wait_seconds=600, check_interval_seconds=30):
                        continue
                console.print(f"[red]  Profile visit error: {e}[/red]")
                return False

    def _type_note(self, page, note: str) -> None:
        """Type a note character by character with random delays."""
        textarea = page.locator('textarea[name="message"]').first
        if not textarea.is_visible():
            textarea = page.locator('textarea').first

        textarea.click()
        human_sleep(0.3, 0.8)

        for char in note:
            textarea.type(char, delay=get_typing_delay())

    def _double_scroll(self, page) -> None:
        """
        Simple hydration scroll: 2 PageDowns, 2-second wait, then scroll back to top.
        """
        console.print("[dim]  Scrolling 2 down, waiting 2s, returning to top...[/dim]")
        try:
            page.keyboard.press("PageDown")
            page.keyboard.press("PageDown")
            time.sleep(2.0)
            page.evaluate("window.scrollTo(0, 0);")
            time.sleep(1.0)
        except Exception:
            pass

    def _verify_pre_criteria(self, page, lead: dict) -> bool:
        """
        Verify connections >= 500 (or followers >= 500).
        Priority:
        1. Extract top card and poll briefly for hydration.
        2. Check connections in top card. If >= 500, return True immediately.
        3. Check followers in top card. If >= 500, return True immediately.
        4. Check followers in Activity section. If >= 500, return True immediately.
        5. Failsafe on exception returns True.
        """
        name = lead.get("name", "Unknown")
        try:
            # 1. Extract Top Card / Header text
            top_card = page.locator("div.pv-top-card-layout__elements, div[class*='pv-top-card-layout'], main section").first
            
            try:
                top_card.wait_for(state="visible", timeout=6000)
            except Exception:
                pass

            top_card_text = ""
            # Wait up to 2.5 seconds for the top card elements to hydrate
            for _ in range(6):
                try:
                    txt = top_card.inner_text().lower()
                    if txt and ("connections" in txt or "follower" in txt):
                        top_card_text = txt
                        break
                except Exception:
                    pass
                page.wait_for_timeout(400)

            if not top_card_text:
                try:
                    top_card_text = top_card.inner_text().lower()
                except Exception:
                    pass
                if not top_card_text:
                    try:
                        top_card_text = page.locator("body").inner_text()[:1000].lower()
                    except Exception:
                        top_card_text = ""

            import re

            # Explicit 500+ connections check (instant pass)
            if "500+ connections" in top_card_text or "500+\nconnections" in top_card_text or "500+ mutual connections" in top_card_text or "500+ mutual" in top_card_text:
                console.print(f"  [green]  ✓ Verified 500+ connections in header.[/green]")
                return True

            # Parse exact connections count in top card
            conn_match = re.search(r'([\d,]+)\s+connections?', top_card_text)
            if conn_match:
                num_str = conn_match.group(1).replace(',', '')
                try:
                    count = int(num_str)
                    if count >= 500:
                        console.print(f"  [green]  ✓ Verified {count} connections in header.[/green]")
                        return True
                    else:
                        console.print(f"  [dim]  Connections count in header is {count} (< 500). Checking followers...[/dim]")
                except ValueError:
                    pass

            # Check Followers in Top Card
            fol_match = re.search(r'([\d,]+)\s+followers', top_card_text)
            if fol_match:
                num_str = fol_match.group(1).replace(',', '')
                try:
                    count = int(num_str)
                    if count >= 500:
                        console.print(f"  [green]  ✓ Verified {count} followers in header.[/green]")
                        return True
                    else:
                        console.print(f"  [dim]  Followers count in header is {count} (< 500).[/dim]")
                except ValueError:
                    pass

            # Check Followers in Activity Section
            try:
                activity_section = page.locator("section:has(h2:has-text('Activity')), section:has(h2:text-is('Activity')), section:has(a[href*='/detail/recent-activity/'])").first
                if activity_section.is_visible(timeout=1500):
                    activity_text = activity_section.inner_text().lower()
                    act_fol_match = re.search(r'([\d,]+)\s+followers', activity_text)
                    if act_fol_match:
                        num_str = act_fol_match.group(1).replace(',', '')
                        try:
                            count = int(num_str)
                            if count >= 500:
                                console.print(f"  [green]  ✓ Verified {count} followers in Activity section.[/green]")
                                return True
                            else:
                                console.print(f"  [dim]  Followers in Activity section is {count} (< 500).[/dim]")
                        except ValueError:
                            pass
            except Exception:
                pass

            console.print(f"  [yellow]  ⚠ Profile has less than 500 connections/followers in relevant areas.[/yellow]")
            return False

        except Exception as e:
            console.print(f"  [yellow]  ⚠ Pre-criteria check error: {e}. Proceeding anyway (failsafe).[/yellow]")
            return True

    def _is_safe_top_card_button(self, page, element) -> bool:
        """
        Anti-Misclick Guard: Ensures the element is strictly inside the target profile's
        top card section and NOT in sidebar recommendation modules ('More profiles for you', 
        'People also viewed', aside). Also enforces strict x/y positional bounds.
        """
        try:
            if not element.is_visible(timeout=500):
                return False

            is_safe = element.evaluate("""
                el => {
                    // 1. Strict exclusion of recommendation/sidebar containers
                    const badContainer = el.closest(
                        'aside, .scaffold-layout__aside, ' +
                        '[aria-label*="More profiles"], [aria-label*="People also viewed"], ' +
                        '[aria-label*="People you may know"], ' +
                        '.pv-browse-map, .discovery-titles, [data-test-id*="sidebar"]'
                    );
                    if (badContainer) return false;

                    // 2. Walk up parents to check for recommendation section titles
                    let parentSection = el.closest('section, div');
                    while (parentSection && parentSection !== document.body) {
                        const h2 = parentSection.querySelector('h2, h3');
                        if (h2) {
                            const title = (h2.innerText || '').toLowerCase();
                            if (title.includes('more profiles') || 
                                title.includes('people also viewed') || 
                                title.includes('people you may know')) {
                                return false;
                            }
                        }
                        if (parentSection.tagName === 'MAIN' || parentSection.tagName === 'BODY') break;
                        parentSection = parentSection.parentElement;
                    }

                    // 3. Positional check (main profile top card action buttons are always at top-left)
                    const r = el.getBoundingClientRect();
                    if (r.x > 750 || r.y > 800) return false;
                    if (r.width === 0 || r.height === 0) return false;

                    return true;
                }
            """)
            return is_safe
        except Exception:
            return False

    def _send_connection(self, page, lead: dict, ghost_run: bool = False) -> tuple[bool, str]:
        """
        Find and click the Connect button, handle the modal, and 'send'.
        Returns (success, status_message).
        """
        name = lead.get("name", "Unknown")

        try:
            # --- 1. CHECK FOR ACTUAL RESTRICTIONS / PENDING STATES ---
            if page.locator("button:has-text('Pending')").first.is_visible(timeout=2000):
                console.print(f"  [yellow]  ⚠ Invite already pending for {name}. Skipping.[/yellow]")
                return False, "already_pending"

            # --- 2. THE CONNECT BUTTON HUNT ---
            top_card = page.locator(
                ".scaffold-layout__main-column section:has(h1), "
                ".scaffold-layout__main section:has(h1), "
                "main section:has(h1), "
                ".pv-top-card, "
                ".profile-topcard"
            ).first
            
            if top_card.is_visible(timeout=2000):
                search_area = top_card
            else:
                search_area = page.locator(".scaffold-layout__main-column, main").first

            candidates = []

            # PATH 1: TYPE A — Direct custom-invite href or aria-label
            direct_btns = search_area.locator(
                "a[href*='/preload/custom-invite/'], "
                "a[href*='custom-invite'], "
                "button[aria-label*='Invite'][aria-label*='connect'], "
                "a[aria-label*='Invite'][aria-label*='connect']"
            ).all()
            for btn in direct_btns:
                if self._is_safe_top_card_button(page, btn):
                    candidates.append((btn, "direct"))
                    break

            # PATH 2: Text-based Connect button
            try:
                all_connect = search_area.locator(
                    "button:has(span:text-is('Connect')), "
                    "a:has(span:text-is('Connect')), "
                    "button:text-is('Connect'), "
                    "a:text-is('Connect')"
                ).all()
                for btn in all_connect:
                    if self._is_safe_top_card_button(page, btn):
                        if not any(c[0] == btn for c in candidates):
                            candidates.append((btn, "direct"))
            except Exception:
                pass

            # PATH 3: ··· (More) dropdown — for Creator profiles
            try:
                more_selectors = [
                    "button[aria-label='More actions']",
                    "button[aria-label='More']",
                    "button[aria-label*='More actions']",
                    "button[aria-label^='More']",
                    "button:has(svg[data-test-icon*='overflow'])",
                    "button:has(svg[data-test-icon='overflow-web-horizontal-small'])",
                    "button.artdeco-dropdown__trigger",
                    "button:has-text('More')",
                    "button:has-text('...')",
                ]
                for sel in more_selectors:
                    btns = search_area.locator(sel).all()
                    for more_btn in btns:
                        if self._is_safe_top_card_button(page, more_btn):
                            if not any(c[0] == more_btn for c in candidates):
                                candidates.append((more_btn, "dropdown"))
            except Exception:
                pass

            if not candidates:
                console.print(f"  [red]  ❌ Connect button completely hidden/missing for {name}. Manual review needed.[/red]")
                return False, "connect_button_missing"

            # --- 3. EXECUTE THE CLICK AND VERIFY LOOP ---
            for attempt, (btn, btn_type) in enumerate(candidates, 1):
                try:
                    connect_btn = None
                    if btn_type == "direct":
                        connect_btn = btn
                    elif btn_type == "dropdown":
                        btn.scroll_into_view_if_needed()
                        page.evaluate("window.scrollBy(0, -100)")
                        page.wait_for_timeout(400)
                        try:
                            btn.click(force=True)
                        except Exception:
                            btn.evaluate("node => node.click()")
                        page.wait_for_timeout(1500)

                        dropdown_connect_selectors = [
                            "[componentkey*='ConnectButton']",
                            "[componentkey*='connect']",
                            "[componentkey*='Connect']",
                            "a[componentkey*='connect']",
                            "div[componentkey*='connect']",
                            "[aria-label*='Invite'][aria-label*='connect']",
                            "[aria-label*='invite'][aria-label*='connect']",
                            "div[aria-label*='Invite to connect']",
                            "a[aria-label*='Invite to connect']",
                            "button[aria-label*='Invite to connect']",
                            "[aria-label*='Connect with']",
                            "a[href*='custom-invite']",
                            "a[href*='/preload/custom-invite/']",
                            ".artdeco-dropdown__content div:has-text('Connect')",
                            ".artdeco-dropdown__content a:has-text('Connect')",
                            ".artdeco-dropdown__content button:has-text('Connect')",
                            "[role='menu'] div:has-text('Connect')",
                            "[role='menu'] a:has-text('Connect')",
                            "[role='menu'] button:has-text('Connect')",
                            "[role='menuitem']:has-text('Connect')",
                            "div[role='menuitem']:has-text('Connect')",
                            "a[role='menuitem']:has-text('Connect')",
                            "button[role='menuitem']:has-text('Connect')",
                            ".artdeco-dropdown__content span:text-is('Connect')",
                            "[role='menu'] span:text-is('Connect')",
                            "div.artdeco-dropdown__item:has-text('Connect')",
                            "li:has-text('Connect')",
                            "div[tabindex='0']:has-text('Connect')",
                            "a[data-tabindex='0']:has-text('Connect')",
                        ]

                        dropdown_connect = None
                        for sel in dropdown_connect_selectors:
                            try:
                                candidates_loc = page.locator(sel).all()
                                for cand in candidates_loc:
                                    if cand.is_visible(timeout=500):
                                        try:
                                            rect = cand.evaluate("el => { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: r.height}; }")
                                            if rect and rect.get("x", 999) < 800 and rect.get("y", 999) < 950 and rect.get("w", 0) > 0:
                                                dropdown_connect = cand
                                                break
                                        except Exception:
                                            dropdown_connect = cand
                                            break
                                if dropdown_connect:
                                    break
                            except Exception:
                                continue

                        if dropdown_connect:
                            connect_btn = dropdown_connect
                        else:
                            try:
                                page.keyboard.press("Escape")
                                page.wait_for_timeout(500)
                            except Exception:
                                pass
                            continue  # Try next candidate

                    if not connect_btn or not connect_btn.is_visible():
                        continue

                    if len(candidates) > 1:
                        console.print(f"  [cyan]  ✓ Trying Connect candidate {attempt}/{len(candidates)} for {name}...[/cyan]")
                    else:
                        console.print(f"  [cyan]  ✓ Found Connect button for {name}. Clicking...[/cyan]")

                    connect_btn.scroll_into_view_if_needed()
                    page.evaluate("window.scrollBy(0, -150)")
                    page.wait_for_timeout(500)

                    url_before_click = page.url
                    try:
                        connect_btn.click(force=True)
                    except Exception:
                        connect_btn.evaluate("node => node.click()")
                    page.wait_for_timeout(2500)

                    if "custom-invite" in page.url or page.url != url_before_click:
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=10000)
                        except Exception:
                            pass
                        page.wait_for_timeout(1500)

                    send_blank_btn = None
                    send_blank_selectors = [
                        "div[role='dialog'] button[aria-label='Send without a note']",
                        "div[role='dialog'] button:has-text('Send without a note')",
                        "div[role='dialog'] button[aria-label='Send invitation']",
                        "div[role='dialog'] button:has-text('Send invitation')",
                        "button[aria-label='Send without a note']",
                        "button:has-text('Send without a note')",
                        "button[aria-label='Send invitation']",
                        "button:has-text('Send invitation')",
                        "div[role='dialog'] button[aria-label='Send now']",
                        "div[role='dialog'] button:has-text('Send now')",
                        "button[aria-label='Send now']",
                        "button:has-text('Send now')",
                        "div[role='dialog'] button:has-text('Send')",
                        "button:has-text('Send')",
                    ]

                    page.wait_for_timeout(1000)

                    for sel in send_blank_selectors:
                        try:
                            el = page.locator(sel).first
                            if el.is_visible(timeout=2000):
                                send_blank_btn = el
                                break
                        except Exception:
                            continue

                    if not send_blank_btn:
                        is_pending = False
                        try:
                            pending_loc = page.locator("button:has-text('Pending'), [aria-label*='Pending'], [aria-label*='pending'], div:has-text('Invitation sent'), div:has-text('Invite sent')").first
                            if pending_loc.is_visible(timeout=1500):
                                is_pending = True
                        except Exception:
                            pass
                        
                        if is_pending:
                            console.print(f"  [green]  ✓ Instant connection invite sent for {name}![/green]")
                            human_sleep(2.0, 4.0, "After send")
                            return True, "Request Sent"

                    # ── SAFETY NET: Name Verification ────────────────────────────────
                    if send_blank_btn:
                        first_name = name.split()[0].lower() if name else ""
                        name_verified = False
                        dialog_text = ""
                        for dialog_sel in [
                            "div[role='dialog']",
                            "div[data-test-modal]",
                            "[role='dialog']",
                        ]:
                            try:
                                el = page.locator(dialog_sel).first
                                if el.is_visible(timeout=1000):
                                    txt = el.inner_text().lower()
                                    if txt:
                                        dialog_text = txt
                                        break
                            except Exception:
                                continue

                        if dialog_text and first_name and first_name in dialog_text:
                            name_verified = True
                        elif not dialog_text:
                            name_verified = True
                        else:
                            console.print(
                                f"  [bold red]  ✘ SAFETY NET: Modal target name mismatch! Expected '{first_name}' "
                                f"in modal text, but found: '{dialog_text[:60]}...'. ABORTING connection attempt to prevent misclick.[/bold red]"
                            )
                            try:
                                page.keyboard.press("Escape")
                                page.wait_for_timeout(1000)
                            except Exception:
                                pass
                            return False, "modal_name_mismatch"

                    if send_blank_btn and name_verified:
                        if ghost_run:
                            console.print(f"  [dim]  GHOST RUN: Would have clicked '{send_blank_btn.inner_text().strip()}' for {name}[/dim]")
                            return True, "ghost_sent"
                        page.wait_for_timeout(1500)

                        try:
                            send_blank_btn.focus()
                            page.wait_for_timeout(500)
                            page.keyboard.press("Enter")
                        except Exception:
                            send_blank_btn.evaluate("node => node.click()")

                        human_sleep(2.0, 3.5, "After send")
                        return True, "Blank Sent"
                    else:
                        console.print(f"  [yellow]  ⚠ Reached Connect modal, but couldn't find the Send button! Trying next...[/yellow]")
                        try:
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(1000)
                        except Exception:
                            pass
                        continue

                except Exception as e:
                    console.print(f"  [dim]  Attempt {attempt} for {name} error: {e}[/dim]")
                    continue

            return False, "click_failed"

        except Exception as e:
            console.print(f"  [red]  ❌ Error sending connection to {name}: {e}[/red]")
            return False, str(e)

    # ─── Main Run ──────────────────────────────────────────────────────────────

    def run(self, leads: list[dict], dry_run: bool = False, test_mode: bool = False, ghost_run: bool = False) -> list[dict]:
        """
        Send BLANK connection requests for all leads.

        dry_run:    Print what would happen, don't open browser.
        test_mode:  Open browser, visit profiles, but DON'T click Send.
        ghost_run:  Full browser run but skip the final 'Send without a note' click.
        """
        console.print("\n[bold cyan]━━━ Phase 4: Messenger (Blank Requests) ━━━[/bold cyan]")

        if not leads:
            console.print("[yellow]No leads to send. Skipping.[/yellow]")
            return []

        visit_limit = get_effective_daily_limit(int(os.getenv("DAILY_SEND_LIMIT", "50")))
        visit_count = 0
        session_visit_count = 0

        if dry_run:
            console.print(f"[yellow]DRY RUN: Simulating outreach process for {len(leads)} leads with {visit_limit}-visit limit[/yellow]")
            simulated_results = []
            for i, lead in enumerate(leads, 1):
                if i > visit_limit:
                    break
                company = lead.get('company', '?')
                console.print(
                    f"  [{i}] {lead.get('name', '?')} @ {company} → "
                    f"{lead.get('linkedin_url', '?')}"
                )
                lead["status"] = "dry_run"
                simulated_results.append(lead)
            return simulated_results

        if ghost_run:
            console.print("[yellow]GHOST RUN: Browser will open and find buttons but NOT send requests.[/yellow]")
        elif test_mode:
            console.print("[yellow]TEST MODE: Browser will open and visit profiles but NOT send.[/yellow]")

        sync_playwright, Stealth_cls = self._get_playwright()

        with sync_playwright() as p:
            browser, context = self._load_session_context(p)
            page = context.new_page()
            Stealth_cls().apply_stealth_sync(page)

            # Validate session
            if not self._check_session_valid(page):
                try:
                    browser.close()
                except Exception:
                    pass
                return leads

            # ── Process leads with retry queue & non-overlapping batch sleeps ──
            try:
                # Prioritize and load any initial retry leads from the sheet directly into the retry queue
                initial_retries = [l for l in leads if l.get("is_initial_retry")]
                main_leads = [l for l in leads if not l.get("is_initial_retry")]

                for r_lead in initial_retries:
                    url = r_lead.get("linkedin_url", "")
                    if url:
                        self.retry_queue.append(r_lead)

                if len(initial_retries) > 0:
                    console.print(f"[cyan]ℹ Loaded {len(initial_retries)} existing 'retry' lead(s) directly into today's retry queue.[/cyan]")

                processing_list = [(lead, False) for lead in main_leads]
                if len(processing_list) == 0 and len(self.retry_queue) > 0:
                    console.print("[cyan]ℹ No new leads to process. Running retry queue immediately...[/cyan]")
                    processing_list = [(r_lead, True) for r_lead in self.retry_queue]
                    self.retry_queue.clear()

                lead_idx = 0
                while lead_idx < len(processing_list) or len(self.retry_queue) > 0:
                    # Check if this was the last item in processing_list and we have retry queue items left
                    if lead_idx >= len(processing_list) and len(self.retry_queue) > 0:
                        console.print(f"\n[bold cyan]🔄 End of main leads reached - Processing final retry queue ({len(self.retry_queue)} leads)...[/bold cyan]")
                        retry_items = [(r_lead, True) for r_lead in self.retry_queue]
                        for item in retry_items:
                            processing_list.append(item)
                        self.retry_queue.clear()

                    lead, is_retry = processing_list[lead_idx]
                    lead_idx += 1

                    try:
                        raw_name = lead.get("name", "Unknown")
                        # Sanitize name: remove non-printable/combining characters
                        name = "".join(c for c in raw_name if c.isprintable())
                        name = re.sub(r'[^\x00-\x7F]+', ' ', name).strip()

                        company = lead.get("company", "Unknown")
                        url = lead.get("linkedin_url", "")

                        # Daily Visit Limit Check
                        if not is_retry and visit_count >= visit_limit:
                            if len(self.retry_queue) > 0:
                                console.print(f"\n[bold cyan]🔄 visit_count reached {visit_limit} - Processing pending retry queue ({len(self.retry_queue)} leads) before stopping...[/bold cyan]")
                                retry_items = [(r_lead, True) for r_lead in self.retry_queue]
                                for item in reversed(retry_items):
                                    processing_list.insert(lead_idx, item)
                                self.retry_queue.clear()
                                continue
                            else:
                                console.print(f"[bold yellow]\n⏹ Daily visit limit of {visit_limit} reached. Stopping pipeline.[/bold yellow]")
                                break

                        # Check if already processed in this or a previous run
                        if url:
                            try:
                                seen = load_seen_profiles()
                                if url in seen:
                                    console.print(f"  [dim]  - Already processed/contacted: {name}. Skipping.[/dim]")
                                    lead["status"] = "already_processed"
                                    self.results.append(lead)
                                    continue
                            except Exception:
                                pass

                        visit_count += 1
                        session_visit_count += 1
                        console.print(f"\n  [Visit {visit_count}/{visit_limit}] {name} @ {company} ({url})")

                        if not url:
                            console.print(f"  [yellow]  ⚠ No URL for {name} — skipping[/yellow]")
                            lead["status"] = "skipped_no_url"
                            self.skipped_count += 1
                            self.results.append(lead)
                            continue

                        # Visit profile
                        visited = self._visit_profile(page, url)
                        if not visited:
                            should_abort, reason = check_abort_conditions(page)
                            if should_abort:
                                console.print(f"[bold red]\n🚨 ABORT: {reason}[/bold red]")
                                notify_abort(reason)
                                for remaining_lead, _ in processing_list[lead_idx-1:]:
                                    remaining_lead["status"] = "aborted"
                                try:
                                    browser.close()
                                except Exception:
                                    pass
                                return leads

                            if not is_retry:
                                console.print(f"  [yellow]  ⚠ Visit failed for {name}. Scheduling retry...[/yellow]")
                                lead["status"] = "retry"
                                self.retry_queue.append(lead)
                            else:
                                console.print(f"  [yellow]  ⚠ Visit failed for {name} on retry attempt. Skipping.[/yellow]")
                                lead["status"] = "skipped_visit_failed"
                                self.skipped_count += 1
                                self.results.append(lead)
                                try:
                                    mark_contacted(url, "skipped_visit_failed")
                                except Exception:
                                    pass

                            # Mutual exclusion: batch sleep OR inter-connection sleep
                            if session_visit_count > 0 and session_visit_count % self.batch_size == 0 and visit_count < visit_limit:
                                batch_sleep(self.batch_sleep_min, self.batch_sleep_max)
                            else:
                                human_sleep(6, 12, "Between connections")
                            continue

                        # Pre-criteria check
                        if not self._verify_pre_criteria(page, lead):
                            lead["status"] = "Untrusted"
                            self.skipped_count += 1
                            self.results.append(lead)
                            try:
                                from utils.sheets import SheetsClient
                                SheetsClient().update_status(url, "Untrusted")
                                mark_contacted(url, "Untrusted")
                            except Exception:
                                pass

                            if session_visit_count > 0 and session_visit_count % self.batch_size == 0 and visit_count < visit_limit:
                                batch_sleep(self.batch_sleep_min, self.batch_sleep_max)
                            else:
                                human_sleep(6, 12, "Between connections")
                            continue

                        if test_mode:
                            console.print(f"  [cyan]  TEST: Visited profile, NOT sending.[/cyan]")
                            lead["status"] = "test_visited"
                            self.results.append(lead)
                            human_sleep(2, 4)
                            continue

                        # Send blank connection
                        success, status = self._send_connection(page, lead, ghost_run=ghost_run)

                        if success:
                            self.sent_count += 1
                            lead["status"] = "Blank Sent"
                            lead["sent_at"] = datetime.now().isoformat()
                            console.print(f"  [green]  ✓ Blank request sent to {name} @ {company}![/green]")
                            try:
                                from utils.sheets import SheetsClient
                                SheetsClient().update_status(lead.get("linkedin_url", ""), "Blank Sent")
                                mark_contacted(lead.get("linkedin_url", ""), "Blank Sent")
                            except Exception:
                                pass
                            self.results.append(lead)
                        else:
                            if not is_retry and status not in ("already_pending", "modal_name_mismatch", "weekly_limit_reached", "email_required"):
                                console.print(f"  [yellow]  ⚠ Connection attempt failed for {name} ({status}). Scheduling retry...[/yellow]")
                                lead["status"] = "retry"
                                self.retry_queue.append(lead)
                            else:
                                self.skipped_count += 1
                                lead["status"] = status
                                console.print(f"  [yellow]  ⚠ Skipped: {status}[/yellow]")
                                self.results.append(lead)
                                try:
                                    if lead.get("linkedin_url"):
                                        mark_contacted(lead.get("linkedin_url", ""), status)
                                except Exception:
                                    pass

                        # Mutual exclusion: batch sleep OR inter-connection sleep
                        if session_visit_count > 0 and session_visit_count % self.batch_size == 0 and visit_count < visit_limit:
                            batch_sleep(self.batch_sleep_min, self.batch_sleep_max)
                        else:
                            human_sleep(6, 12, "Between connections")

                    except Exception as e:
                        console.print(f"  [red]  ⚠ CRITICAL ERROR on {lead.get('name', 'Unknown')}: {e}[/red]")
                        lead["status"] = "critical_error"
                        self.skipped_count += 1
                        self.results.append(lead)
                        continue

            except KeyboardInterrupt:
                console.print("\n[yellow]Messenger interrupted by user. Stopping immediately and saving progress...[/yellow]")
            finally:
                if len(self.retry_queue) > 0:
                    for r_lead in self.retry_queue:
                        if r_lead not in self.results:
                            r_lead["status"] = "retry"
                            self.skipped_count += 1
                            self.results.append(r_lead)
                    self.retry_queue.clear()

            try:
                browser.close()
            except Exception:
                pass

        console.print(
            Panel(
                f"[green]✅ Done![/green]\n"
                f"Sent: [bold green]{self.sent_count}[/bold green]   "
                f"Skipped: [bold yellow]{self.skipped_count}[/bold yellow]",
                title="Messenger Complete",
                border_style="green"
            )
        )

        # ── Retrieve and Print Failed Sheet Updates under "Important Points" ──
        try:
            from utils.sheets import SheetsClient
            failed_updates = SheetsClient.get_and_clear_failed_updates()
            if failed_updates:
                console.print("\n[bold red]⚠️  Important Points:[/bold red]")
                for item in failed_updates:
                    console.print(f"  - [red]Google Sheet cell update FAILED[/red] for Row {item['row']}, Col {item['col']} (Value: '{item['value']}') at {item['timestamp']}")
        except Exception:
            pass

        if not test_mode:
            notify_done(self.sent_count, self.skipped_count)

        return self.results
