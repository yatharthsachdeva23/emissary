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
        self.batch_size = int(os.getenv("BATCH_SIZE", "5"))
        self.batch_sleep_min = float(os.getenv("BATCH_SLEEP_MIN", "1"))
        self.batch_sleep_max = float(os.getenv("BATCH_SLEEP_MAX", "2"))
        self.sent_count = 0
        self.skipped_count = 0
        self.results = []
        # Detected at runtime: e.g. 'https://in.linkedin.com' for Indian users
        self._linkedin_base = "https://www.linkedin.com"

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

            console.print("\n[cyan]Browser opened. Log in and then press Enter here...[/cyan]")
            input()

            storage_state = context.storage_state()
            has_auth_cookie = any(c.get("name") == "li_at" for c in storage_state.get("cookies", []))
            
            # Check if login was successful
            if has_auth_cookie or "feed" in page.url or "mynetwork" in page.url:
                DATA_DIR.mkdir(exist_ok=True)
                with open(SESSION_PATH, "w") as f:
                    json.dump(storage_state, f)
                console.print("[green]✓ Session saved to linkedin_session.json[/green]")
                browser.close()
                return True
            else:
                console.print(f"[red]Login may not have completed. Current URL: {page.url}[/red]")
                browser.close()
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
        page.evaluate("window.scrollBy(0, 500)")
        human_sleep(1.5, 3.0)
        page.evaluate("window.scrollBy(0, 400)")
        human_sleep(1.5, 2.5)
        page.evaluate("window.scrollTo(0, 0)")
        human_sleep(1, 2)

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
        try:
            url = self._normalize_linkedin_url(url)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            human_sleep(2, 4, "Page load wait")

            # ── Post-navigation URL guard ────────────────────────────────────
            # If LinkedIn redirected us to authwall or login (cookie mismatch or
            # per-profile restriction), abort this lead gracefully.
            current_url = page.url
            if any(x in current_url for x in ("authwall", "/login", "/signup", "checkpoint")):
                console.print(f"  [red]  ✖ Redirected to login/authwall for this profile. Session cookie may have expired or profile is restricted.[/red]")
                console.print(f"  [dim]    URL: {current_url}[/dim]")
                return False

            # Check for abort conditions after each page load
            should_abort, reason = check_abort_conditions(page)
            if should_abort:
                return False

            # Human-like: scroll down the profile slowly
            for _ in range(random.randint(2, 4)):
                dist, dur = random_scroll_params()
                page.evaluate(f"window.scrollBy(0, {dist})")
                human_sleep(0.8, 2.0)

            # Scroll back up
            page.evaluate("window.scrollTo(0, 0)")
            human_sleep(1, 2)

            # ── React Hydration Wait ─────────────────────────────────────────
            # LinkedIn is a React app. domcontentloaded fires when raw HTML is ready,
            # but the JS that actually draws the buttons takes 1-4 more seconds.
            page.wait_for_timeout(4000)

            return True

        except Exception as e:
            console.print(f"[red]  Profile visit error: {e}[/red]")
            return False

    def _type_note(self, page, note: str) -> None:
        """Type a note character by character with random delays."""
        textarea = page.locator('textarea[name="message"]').first
        if not textarea.is_visible():
            # Try alternative selectors
            textarea = page.locator('textarea').first

        textarea.click()
        human_sleep(0.3, 0.8)

        for char in note:
            textarea.type(char, delay=get_typing_delay())
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
            # ── STRATEGY ─────────────────────────────────────────────────────────
            # LinkedIn has TWO types of profiles:
            #
            # TYPE A — Standard (Non-Creator): Connect button is DIRECTLY visible
            #   UI: [Connect] [Message] [···]
            #   DOM: <a href="/preload/custom-invite/?vanityName=...">
            #        OR <button aria-label="Invite ... to connect">
            #
            # TYPE B — Creator: Connect is HIDDEN inside the ··· (More) dropdown
            #   UI: [Message] [Follow] [···]
            #   DOM: <button aria-label="More"> → opens menu →
            #        <a role="menuitem" href="/preload/custom-invite/...">
            #
            # SAFETY: The "More profiles for you" SIDEBAR also has Connect buttons
            # for other people. We prevent accidental sidebar clicks by:
            #   - Scoping PATH 1 & 2 to <main> (sidebar is always in <aside>)
            #   - PATH 3 matches on `custom-invite` href which is profile-specific
            # ─────────────────────────────────────────────────────────────────────
            connect_btn = None
            main_area = page.locator("main")

            # PATH 1: TYPE A — Direct Connect link/button visible on the profile
            # Scope strictly to <main> to avoid sidebar buttons.
            # The href /preload/custom-invite/ or aria-label "Invite X to connect"
            # are set by LinkedIn for the current profile's Connect action.
            direct_btn = main_area.locator(
                "a[href*='/preload/custom-invite/'], "
                "a[href*='custom-invite'], "
                "button[aria-label*='Invite'][aria-label*='connect'], "
                "a[aria-label*='Invite'][aria-label*='connect']"
            ).first
            if direct_btn.is_visible(timeout=1500):
                connect_btn = direct_btn

            # PATH 2: Text-based Connect button (fallback for older DOM patterns)
            # Iterates all visible "Connect" text buttons and uses JS to confirm
            # the element is not inside an <aside> or sidebar recommendation widget.
            if not connect_btn:
                try:
                    all_connect = main_area.locator(
                        "button:has(span:text-is('Connect')), "
                        "a:has(span:text-is('Connect')), "
                        "button:text-is('Connect'), "
                        "a:text-is('Connect')"
                    ).all()
                    for btn in all_connect:
                        try:
                            if not btn.is_visible():
                                continue
                            # Double-check: not inside a sidebar/recommendation widget
                            in_sidebar = btn.evaluate(
                                "el => !!el.closest('aside') || "
                                "!!el.closest('[aria-label*=\"profiles for you\"]') || "
                                "!!el.closest('[data-view-name*=\"pymk\"]')"
                            )
                            if not in_sidebar:
                                connect_btn = btn
                                break
                        except Exception:
                            continue
                except Exception:
                    pass


            # PATH 3: More (···) dropdown — Creator profiles hide Connect inside the three-dots menu.
            # IMPORTANT: There can be multiple More buttons on the page (profile header + posts).
            # The profile header's More button opens a menu containing custom-invite link.
            # We try each visible More button until we find the one with Connect.
            if not connect_btn:
                try:
                    all_more_btns = page.locator("button[aria-label='More']").all()
                    for more_btn in all_more_btns:
                        try:
                            if not more_btn.is_visible():
                                continue
                            more_btn.scroll_into_view_if_needed()
                            page.evaluate("window.scrollBy(0, -100)")
                            page.wait_for_timeout(400)
                            more_btn.click(force=True)
                            page.wait_for_timeout(1500)

                            # LinkedIn's invite menu item has a unique, profile-specific href.
                            # Matching on href is the most robust selector available.
                            dropdown_connect = page.locator(
                                "a[role='menuitem'][href*='custom-invite'], "
                                "a[role='menuitem'][aria-label*='connect'], "
                                "[role='menuitem'][aria-label*='Invite'][aria-label*='connect']"
                            ).first
                            if dropdown_connect.is_visible(timeout=2000):
                                connect_btn = dropdown_connect
                                break  # Found it — stop trying More buttons
                            else:
                                # This More button didn't have Connect — close it and try the next
                                try:
                                    page.keyboard.press("Escape")
                                    page.wait_for_timeout(500)
                                except Exception:
                                    pass
                        except Exception:
                            continue
                except Exception:
                    pass

            # --- 3. EXECUTE THE CLICK ---
            if connect_btn and connect_btn.is_visible():
                console.print(f"  [cyan]  ✓ Found Connect button for {name}. Clicking...[/cyan]")
                
                # Critical Fix: Playwright's scroll puts the button at the very top of the screen,
                # placing it directly under the sticky navigation bar (which contains the Premium button).
                # We scroll it into view, then manually scroll UP by 150px to clear the sticky header.
                connect_btn.scroll_into_view_if_needed()
                page.evaluate("window.scrollBy(0, -150)")
                page.wait_for_timeout(500)
                
                # Now we can safely click without force=True, ensuring we don't accidentally click the overlaying navbar.
                # Note: If connect_btn is an <a href> link (from the More dropdown), clicking it navigates
                # to /preload/custom-invite/ which renders the invitation UI on the profile page itself.
                url_before_click = page.url
                connect_btn.click()
                page.wait_for_timeout(2500)  # Wait for either modal or navigation

                # Check if we navigated to a new page (custom-invite flow)
                if "custom-invite" in page.url or page.url != url_before_click:
                    # Navigation happened — wait for page to fully render
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
                    page.wait_for_timeout(1500)

                # Search for Send button — covers both modal (dialog) and navigated page
                send_blank_btn = None
                send_blank_selectors = [
                    "div[role='dialog'] button[aria-label='Send without a note']",
                    "div[role='dialog'] button:has-text('Send without a note')",
                    "div[role='dialog'] button[aria-label='Send invitation']",
                    "div[role='dialog'] button:has-text('Send invitation')",
                    # Broader fallbacks for navigated invite page (no dialog wrapper)
                    "button[aria-label='Send without a note']",
                    "button:has-text('Send without a note')",
                    "button[aria-label='Send invitation']",
                    "button:has-text('Send invitation')",
                ]
                
                # Give the modal a tiny bit of time to slide into view and become stable
                page.wait_for_timeout(1000)
                
                for sel in send_blank_selectors:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible(timeout=2000):
                            send_blank_btn = btn
                            break
                    except Exception:
                        continue

                # Fallback: any 'Send' button in dialog or on page
                if not send_blank_btn:
                    try:
                        fallback = page.locator("div[role='dialog'] button:has-text('Send'), button:has-text('Send')").first
                        if fallback.is_visible(timeout=1500):
                            send_blank_btn = fallback
                    except Exception:
                        pass
                
                if send_blank_btn:
                    if ghost_run:
                        console.print(f"  [dim]  GHOST RUN: Would have clicked '{send_blank_btn.inner_text().strip()}' for {name}[/dim]")
                        return True, "ghost_sent"
                    # Remove force=True so Playwright validates actionability natively
                    # Wait an extra 1.5 seconds to ensure LinkedIn's React app has fully bound the onClick event listeners
                    page.wait_for_timeout(1500)
                    
                    try:
                        # The most bulletproof way to click in React SPAs: Accessibility Focus + Enter
                        send_blank_btn.focus()
                        page.wait_for_timeout(500)
                        page.keyboard.press("Enter")
                    except Exception:
                        # Fallback to a raw DOM click if focus fails
                        send_blank_btn.evaluate("node => node.click()")
                        
                    human_sleep(2.0, 4.0, "After send")
                    return True, "Blank Sent"
                else:
                    console.print(f"  [yellow]  ⚠ Reached Connect modal, but couldn't find the Send button![/yellow]")
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                    return False, "send_button_not_found"
                    
            else:
                # If all 4 tactics fail, they are either actually restricted or we missed it. Don't assume connected.
                console.print(f"  [red]  ❌ Connect button completely hidden/missing for {name}. Manual review needed.[/red]")
                return False, "connect_button_missing"

        except Exception as e:
            console.print(f"[red]  Connection error for {name}: {e}[/red]")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return False, f"error: {str(e)[:80]}"

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

        daily_limit = get_effective_daily_limit(int(os.getenv("DAILY_SEND_LIMIT", "15")))
        leads = leads[:daily_limit]

        if dry_run:
            console.print(f"[yellow]DRY RUN: Would send {len(leads)} blank connection requests (no browser)[/yellow]")
            for i, lead in enumerate(leads, 1):
                console.print(
                    f"  [{i}] {lead.get('name', '?')} @ {lead.get('company', '?')} → "
                    f"{lead.get('linkedin_url', '?')}"
                )
                lead["status"] = "dry_run"
            return leads

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
                browser.close()
                return leads

            # ── Process leads in batches ─────────────────────────────────────
            for batch_start in range(0, len(leads), self.batch_size):
                batch = leads[batch_start: batch_start + self.batch_size]
                batch_num = (batch_start // self.batch_size) + 1
                console.print(f"\n[bold]Batch {batch_num} — {len(batch)} connections[/bold]")

                for i, lead in enumerate(batch):
                    try:
                        raw_name = lead.get("name", "Unknown")
                        # Sanitize name: remove non-printable/combining characters
                        name = "".join(c for c in raw_name if c.isprintable())
                        name = re.sub(r'[^\x00-\x7F]+', ' ', name).strip()
                        
                        url = lead.get("linkedin_url", "")

                        console.print(f"\n  [{self.sent_count + 1}/{len(leads)}] {name} @ {lead.get('company', '?')}")

                        if not url:
                            console.print(f"  [yellow]  ⚠ No URL for {name} — skipping[/yellow]")
                            lead["status"] = "skipped_no_url"
                            self.skipped_count += 1
                            self.results.append(lead)
                            continue

                        # Visit profile
                        visited = self._visit_profile(page, url)
                        if not visited:
                            # Check if this was an abort condition
                            should_abort, reason = check_abort_conditions(page)
                            if should_abort:
                                console.print(f"[bold red]\n🚨 ABORT: {reason}[/bold red]")
                                notify_abort(reason)
                                # Mark remaining as not_sent
                                for remaining in leads[batch_start + i:]:
                                    remaining["status"] = "aborted"
                                browser.close()
                                return leads

                            console.print(f"  [yellow]  ⚠ Skipped: Visit failed for {name}[/yellow]")
                            lead["status"] = "skipped_visit_failed"
                            self.skipped_count += 1
                            self.results.append(lead)
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
                            console.print(f"  [green]  ✓ Blank request sent![/green]")
                            # Log immediately to sheet so we never lose a send
                            try:
                                from utils.sheets import SheetsClient
                                SheetsClient().update_status(lead.get("linkedin_url", ""), "Blank Sent")
                            except Exception:
                                pass
                        else:
                            self.skipped_count += 1
                            lead["status"] = status
                            console.print(f"  [yellow]  ⚠ Skipped: {status}[/yellow]")

                        self.results.append(lead)

                        # Inter-connection wait (shorter than batch sleep)
                        if i < len(batch) - 1:
                            human_sleep(8, 20, "Between connections")

                    except Exception as e:
                        console.print(f"  [red]  ⚠ CRITICAL ERROR on {lead.get('name', 'Unknown')}: {e}[/red]")
                        lead["status"] = "critical_error"
                        self.skipped_count += 1
                        self.results.append(lead)
                        continue

                # Batch sleep (except after last batch)
                if batch_start + self.batch_size < len(leads):
                    batch_sleep(self.batch_sleep_min, self.batch_sleep_max)

            browser.close()

        console.print(
            Panel(
                f"[green]✅ Done![/green]\n"
                f"Sent: [bold green]{self.sent_count}[/bold green]   "
                f"Skipped: [bold yellow]{self.skipped_count}[/bold yellow]",
                title="Messenger Complete",
                border_style="green"
            )
        )

        if not test_mode:
            notify_done(self.sent_count, self.skipped_count)

        return self.results
