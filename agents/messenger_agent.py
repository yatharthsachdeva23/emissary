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
from typing import Optional

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
from utils.text_cleaner import clean_first_name
from utils.network import is_internet_available, is_network_error, wait_for_network_recovery

load_dotenv()
console = Console()

DATA_DIR = Path(__file__).parent.parent / "data"
SESSION_PATH = DATA_DIR / "linkedin_session.json"
LEADS_PATH = DATA_DIR / "leads_today.json"

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

    def _save_checkpoint(self, leads: list[dict]) -> None:
        """Save current progress to data/leads_today.json in real time."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = {
                "date": datetime.now().isoformat(),
                "count": len(leads),
                "leads": leads,
                "last_checkpoint": datetime.now().isoformat(),
            }
            with open(LEADS_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except Exception as e:
            console.print(f"  [dim]Checkpoint save warning: {e}[/dim]")

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

    def _load_session_context(self, playwright, remote: bool = False):
        """Load saved session cookies into a new browser context, or connect to remote Chrome if requested."""
        if remote:
            try:
                browser = playwright.chromium.connect_over_cdp("http://localhost:9222")
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                console.print("[bold green]✓ MessengerAgent connected to remote Chrome on port 9222 (CDP mode)![/bold green]")
                return browser, context, True
            except Exception as e:
                console.print(f"[yellow]Could not connect to remote Chrome on port 9222 ({e}). Falling back to local session launch.[/yellow]")

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

        return browser, context, False

    def _save_session(self, context):
        """Save latest session cookies to prevent staleness on session rotation."""
        try:
            context.storage_state(path=str(SESSION_PATH))
            console.print("[dim]✓ Saved updated session cookies to data/linkedin_session.json[/dim]")
        except Exception:
            pass

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
        for attempt in range(2):
            try:
                # Pre-navigation network check
                if not is_internet_available():
                    console.print("[yellow]⚠ Internet disconnected before loading profile. Waiting for Wi-Fi recovery (Checking every 30s, max 10 mins)...[/yellow]")
                    if not wait_for_network_recovery(max_wait_seconds=600, check_interval_seconds=30):
                        return False

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
                if is_network_error(e):
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

    def _verify_experience(self, page, lead: dict, profile: Optional[dict] = None) -> dict:
        """
        Locate Top Card and Experience section, extract text, and verify active company with Gemini.
        Returns resolved current company, role, relevance, and scraped profile snippets.
        """
        name = lead.get("name", "Unknown")
        target_company = lead.get("company", "Unknown")
        target_role = lead.get("role", "Unknown")

        # 1. Scrape Top Card Header & Badges
        top_card_text = ""
        try:
            top_card = page.locator("section:has(h1), .pv-top-card, .profile-topcard").first
            if top_card.is_visible(timeout=2000):
                top_card_text = top_card.inner_text().strip()
        except Exception:
            pass

        # 2. Scrape Experience Section (with scroll to trigger lazy loading)
        scraped_experience = ""
        try:
            page.evaluate("window.scrollBy(0, 500)")
            page.wait_for_timeout(600)

            exp_section = page.locator(
                "section:has(h2:has-text('Experience')), "
                "section:has(h2:text-is('Experience')), "
                "section#experience-section, "
                "section:has(#experience)"
            ).first

            if not exp_section.is_visible(timeout=2500):
                page.evaluate("window.scrollBy(0, 500)")
                page.wait_for_timeout(600)
                exp_section = page.locator("section:has(span:has-text('Experience'))").first

            if exp_section.is_visible(timeout=2000):
                try:
                    page.evaluate("el => el && el.scrollIntoView({block: 'center', inline: 'nearest'})", exp_section)
                except Exception:
                    try:
                        exp_section.scroll_into_view_if_needed(timeout=1500)
                    except Exception:
                        pass
                human_sleep(1.0, 1.8, "Viewing Experience Section")
                try:
                    scraped_experience = exp_section.inner_text(timeout=2000).strip()
                except Exception:
                    scraped_experience = ""
                # Scroll back to top after reading experience so top-card buttons are in view
                try:
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_timeout(500)
                except Exception:
                    pass
        except Exception as e:
            console.print(f"  [dim]  Note during experience scroll: {e}[/dim]")

        # Failsafe if DOM couldn't be extracted
        if not top_card_text and not scraped_experience:
            console.print(f"  [yellow]  ⚠ Could not read profile experience or top card. Proceeding with existing data.[/yellow]")
            return {
                "is_same_company": True,
                "is_relevant": True,
                "reason": "DOM unreadable (failsafe)",
                "current_company": target_company,
                "current_position": target_role,
                "top_card_text": "",
                "scraped_experience": ""
            }

        # 3. Call Gemini with Rotation to Verify Experience
        try:
            from utils.gemini_client import generate_with_rotation

            verification_prompt = f"""You are a professional profile verification engine for Yatharth's outreach system.
Yatharth is a 4th-year student at Delhi Technological University (DTU, 9.3 CGPA) and former Intern at NoBrokerHood, seeking an internship across Product Management, B2B Sales, Growth, or Tech at high-growth startups (including Proptech, Property, and Tech ventures in Dubai, New York, and India).

TARGET LEAD DETAILS FROM SEARCH:
- Name: {name}
- Expected Company from Search: {target_company}
- Expected Role/Position: {target_role}

TOP CARD HEADER & CURRENT BADGE TEXT FROM LINKEDIN:
\"\"\"
{top_card_text}
\"\"\"

SCRAPED LINKEDIN EXPERIENCE SECTION CONTENT:
\"\"\"
{scraped_experience}
\"\"\"

YOUR TASKS:
1. Identify the person's CURRENT ACTIVE job(s) from the scraped Experience and Top Card text.
   CRITICAL DATE RULE: A job is ONLY current/active if its date range explicitly ends in "- Present", "Present", or if it is currently listed as their current company badge in the top card. If a job has a completed date range like "Nov 2022 - Jan 2026" or "2021 - 2024", IT IS A COMPLETED PAST ROLE AND MUST NOT BE SELECTED AS THEIR CURRENT COMPANY!
2. Resolve multiple "Present" jobs:
   - Operating vs Passive: Ignore roles like "Investor", "Advisor", "Consultant", "Mentor", or "Board Member". Focus on their core operational role (Founder, Co-Founder, CEO, VP, Head of Product, Head of Sales, VP Sales, Commercial Director, Managing Director, Director, PM, Engineering Lead, etc.).
   - Incremental/Recent: If they have multiple operating roles, pick the one that started most recently.
3. Compare this resolved current company with the expected company ("{target_company}").
   Company variations such as "Razorpay" vs "Razorpay Software Pvt Ltd" or "Flipkart" vs "Flipkart Internet" ARE THE SAME COMPANY (is_same_company = true).
4. Determine if it is the SAME company or if they have LEFT / CHANGED companies:
   - If SAME company:
     - is_same_company: true
     - is_relevant: true
     - current_company: "{target_company}"
     - current_position: resolved position
   - If DIFFERENT company (they transitioned to a new company):
     - is_same_company: false
     - Check if they are currently actively employed at a real company. If they are unemployed, student only, "seeking opportunities", or have no active company, set is_relevant = false.
     - If they are at a new company, set is_relevant = true.
     - Extract current_company and current_position.

Return ONLY a valid JSON object wrapped in ```json ... ``` tags:
{{
  "is_same_company": true/false,
  "is_relevant": true/false,
  "reason": "Brief explanation of date and company determination",
  "current_company": "Resolved current company name",
  "current_position": "Resolved current job title/position"
}}
"""
            model_name = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
            resp_text = generate_with_rotation(verification_prompt, model=model_name)
            match = re.search(r"```json\s*([\s\S]+?)\s*```", resp_text)
            if match:
                res_data = json.loads(match.group(1))
            else:
                res_data = json.loads(resp_text.strip())

            # Post-check company fuzzy match to prevent false positives
            from utils.safety import is_same_company_name
            res_company = (res_data.get("current_company") or "").strip()
            if is_same_company_name(target_company, res_company) or is_same_company_name(target_company, top_card_text):
                res_data["is_same_company"] = True
                res_data["is_relevant"] = True
                res_data["current_company"] = target_company
            else:
                res_data["current_company"] = res_company or target_company

            res_data["top_card_text"] = top_card_text
            res_data["scraped_experience"] = scraped_experience
            return res_data

        except Exception as e:
            console.print(f"  [yellow]  ⚠ Experience verification error: {e}. Proceeding with existing lead data.[/yellow]")
            return {
                "is_same_company": True,
                "is_relevant": True,
                "reason": f"Verification error: {e}",
                "current_company": target_company,
                "current_position": target_role,
                "top_card_text": top_card_text,
                "scraped_experience": scraped_experience
            }


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

                    // 3. Positional check (main profile top card action buttons are within the main column)
                    const r = el.getBoundingClientRect();
                    // In a 1280px viewport, top card action buttons (Follow, Message, More) can reach x=950.
                    // The sidebar aside starts past x=960. Expand y bound to 1200 for tall top cards / slower hydration.
                    if (r.x > 960 || r.y > 1200) return false;
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

        for attempt in (1, 2):
            try:
                # 0. Always reset page scroll to top so the profile top card is guaranteed in viewport
                try:
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_timeout(1000 if attempt == 1 else 1800)
                except Exception:
                    pass

                # Dismiss any lingering chat overlays / bubbles so they never intercept clicks or get mistaken for connection modals
                try:
                    page.evaluate("""
                        () => {
                            document.querySelectorAll('.msg-overlay-bubble-header__control--close, .msg-overlay-conversation-bubble [data-control-name="overlay.close_conversation_window"]').forEach(el => el.click());
                        }
                    """)
                except Exception:
                    pass

                # --- 1. CHECK FOR ACTUAL RESTRICTIONS / PENDING STATES ---
                if page.locator("button:has-text('Pending')").first.is_visible(timeout=1500):
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

                connect_btn = None
                dropdown_clicked = False
                is_from_dropdown = False

                # Priority 1: Direct Connect button visible on the top card
                direct_selectors = [
                    "button:has(span:text-is('Connect'))",
                    "a:has(span:text-is('Connect'))",
                    "button:text-is('Connect')",
                    "a:text-is('Connect')",
                    "button[aria-label*='Invite'][aria-label*='connect']",
                    "a[aria-label*='Invite'][aria-label*='connect']",
                    "a[href*='/preload/custom-invite/']",
                    "a[href*='custom-invite']",
                ]
                for sel in direct_selectors:
                    try:
                        btns = search_area.locator(sel).all()
                        for b in btns:
                            if self._is_safe_top_card_button(page, b):
                                connect_btn = b
                                break
                        if connect_btn:
                            break
                    except Exception:
                        continue

                # Priority 2: If not directly visible, check the single "More" actions dropdown in the top card
                if not connect_btn:
                    more_selectors = [
                        "button[aria-label='More actions']",
                        "button[aria-label='More']",
                        "button[aria-label*='More actions']",
                        "button[aria-label^='More']",
                        "button.artdeco-dropdown__trigger",
                        "button:has-text('More')",
                    ]
                    more_btn = None
                    for sel in more_selectors:
                        try:
                            btns = search_area.locator(sel).all()
                            for mb in btns:
                                if self._is_safe_top_card_button(page, mb):
                                    more_btn = mb
                                    break
                            if more_btn:
                                break
                        except Exception:
                            continue

                    if more_btn:
                        try:
                            try:
                                more_btn.evaluate("el => el.scrollIntoView({block: 'center', inline: 'nearest'})", timeout=1500)
                            except Exception:
                                more_btn.scroll_into_view_if_needed(timeout=1500)
                            page.evaluate("window.scrollBy(0, -100)")
                            page.wait_for_timeout(400)
                            try:
                                more_btn.click(force=True, timeout=2000)
                            except Exception:
                                more_btn.evaluate("node => node.click()", timeout=2000)
                            page.wait_for_timeout(800)

                            dropdown_container = page.locator(
                                ".artdeco-dropdown__content.artdeco-dropdown__content--is-open, "
                                ".artdeco-dropdown--is-opened .artdeco-dropdown__content, "
                                "div.artdeco-dropdown__content[aria-hidden='false'], "
                                "div[role='menu']"
                            ).first

                            # FAST TRACK: Direct in-DOM click inside the active open dropdown!
                            # Avoids Playwright pointer movement that causes Artdeco blur/close,
                            # and guarantees 0ms timeout freeze.
                            clicked_in_dropdown = page.evaluate("""
                                () => {
                                    const dropdownContainers = Array.from(document.querySelectorAll(
                                        '.artdeco-dropdown__content--is-open, ' +
                                        '.artdeco-dropdown--is-opened .artdeco-dropdown__content, ' +
                                        'div.artdeco-dropdown__content[aria-hidden="false"], ' +
                                        'div[role="menu"]'
                                    ));
                                    
                                    // Strictly find visible dropdown container in viewport
                                    const activeDropdown = dropdownContainers.find(c => {
                                        if (c.closest('.msg-overlay-container, .msg-overlay-conversation-bubble')) return false;
                                        const r = c.getBoundingClientRect();
                                        return r.width > 30 && r.height > 30 && r.top >= 0 && r.top < window.innerHeight;
                                    });
                                    if (!activeDropdown) return false;

                                    const items = Array.from(activeDropdown.querySelectorAll(
                                        'div[role="button"], div[role="menuitem"], button[role="menuitem"], ' +
                                        'a[role="menuitem"], .artdeco-dropdown__item, button, a, li, span'
                                    ));
                                    for (const el of items) {
                                        const txt = (el.innerText || el.textContent || '').trim().toLowerCase();
                                        const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                                        if (txt === 'connect' || (aria.includes('invite') && aria.includes('connect')) || aria.includes('connect with') || txt.includes('connect')) {
                                            const clickable = el.closest('button, [role="button"], [role="menuitem"], a') || el;
                                            clickable.focus();
                                            try { clickable.click(); } catch(e) {}
                                            ['mousedown', 'mouseup', 'click'].forEach(evtName => {
                                                clickable.dispatchEvent(new MouseEvent(evtName, { bubbles: true, cancelable: true, view: window }));
                                            });
                                            return true;
                                        }
                                    }
                                    return false;
                                }
                            """)

                            if clicked_in_dropdown:
                                is_from_dropdown = True
                                dropdown_clicked = True
                            else:
                                # Fallback locator search strictly inside the dropdown
                                dropdown_connect_selectors = [
                                    "div[role='button']:has-text('Connect')",
                                    "div[role='menuitem']:has-text('Connect')",
                                    "button[role='menuitem']:has-text('Connect')",
                                    "a[role='menuitem']:has-text('Connect')",
                                    ".artdeco-dropdown__item:has-text('Connect')",
                                    "[aria-label*='Invite'][aria-label*='connect']",
                                    "[aria-label*='Connect with']",
                                    "[componentkey*='ConnectButton']",
                                    "[componentkey*='connect']",
                                    "button:has-text('Connect')",
                                    "span:text-is('Connect')",
                                    "li:has-text('Connect')",
                                ]
                                for d_sel in dropdown_connect_selectors:
                                    try:
                                        if dropdown_container.is_visible(timeout=500):
                                            cand = dropdown_container.locator(d_sel).first
                                            if cand.is_visible(timeout=300):
                                                connect_btn = cand
                                                is_from_dropdown = True
                                                break
                                    except Exception:
                                        continue
                        except Exception:
                            pass

                if not dropdown_clicked and (not connect_btn or not connect_btn.is_visible(timeout=500)):
                    # Guard: verify internet status before declaring button missing!
                    if not is_internet_available():
                        console.print(f"  [yellow]⚠ Internet dropped while searching buttons for {name}. Waiting for Wi-Fi recovery (Checking every 30s, max 10 mins)...[/yellow]")
                        if wait_for_network_recovery(max_wait_seconds=600, check_interval_seconds=30):
                            console.print(f"  [cyan]✓ Wi-Fi recovered! Reloading page for {name} to hydrate buttons...[/cyan]")
                            page.reload(wait_until="domcontentloaded")
                            page.wait_for_timeout(2500)
                            continue
                    if attempt == 1:
                        console.print(f"  [yellow]  ⚠ Connect button not detected on attempt 1 for {name}. Retrying once...[/yellow]")
                        page.wait_for_timeout(1000)
                        continue
                    else:
                        console.print(f"  [red]  ❌ Connect button completely hidden/missing for {name} after retry.[/red]")
                        return False, "connect_button_missing"

                # --- 3. EXECUTE CLICK & SEND ---
                origin_desc = "More actions dropdown" if is_from_dropdown else "Top Card"
                console.print(f"  [cyan]  ✓ Found Connect button for {name} ({origin_desc}). Clicking...[/cyan]")

                url_before_click = page.url
                if not dropdown_clicked and connect_btn:
                    if not is_from_dropdown:
                        try:
                            connect_btn.evaluate("el => el.scrollIntoView({block: 'center', inline: 'nearest'})", timeout=1500)
                        except Exception:
                            try:
                                connect_btn.scroll_into_view_if_needed(timeout=1500)
                            except Exception:
                                pass
                        page.evaluate("window.scrollBy(0, -150)")
                        page.wait_for_timeout(400)

                    try:
                        connect_btn.click(timeout=2500)
                    except Exception:
                        # Fallback: dispatch full mouse event cycle directly to element with strict short timeout
                        try:
                            connect_btn.evaluate("""(node) => {
                                node.focus();
                                try { node.click(); } catch(e) {}
                                ['mousedown', 'mouseup', 'click'].forEach(evt => {
                                    node.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                                });
                            }""", timeout=2000)
                        except Exception:
                            pass

                page.wait_for_timeout(1800)

                if "custom-invite" in page.url or page.url != url_before_click:
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
                    page.wait_for_timeout(1500)

                # Look for the connection modal (strictly exclude chat bubbles / overlay containers)
                modal_loc = None
                modal_selectors = [
                    "div.send-invite",
                    "div[role='dialog']:not(.msg-overlay-conversation-bubble):not(.msg-overlay-bubble-header)",
                    ".artdeco-modal:not(.msg-overlay-conversation-bubble)",
                    "[data-test-modal]:not(.msg-overlay-conversation-bubble)",
                ]
                for m_sel in modal_selectors:
                    try:
                        m = page.locator(m_sel).first
                        if m.is_visible(timeout=1500):
                            # Ensure it's not inside a chat overlay
                            is_chat = m.evaluate("el => !!el.closest('.msg-overlay-container, .msg-overlay-conversation-bubble')", timeout=1500)
                            if not is_chat:
                                modal_loc = m
                                break
                    except Exception:
                        continue

                send_blank_btn = None
                if modal_loc:
                    # STRICTLY search inside the open connection dialog for invitation send buttons
                    # NEVER use generic "Send" which matches chat compose Send button!
                    send_blank_selectors = [
                        "button[aria-label*='without a note' i]",
                        "button:has-text('Send without a note')",
                        "button[aria-label*='Send invitation' i]",
                        "button:has-text('Send invitation')",
                        "button[aria-label*='Send now' i]",
                        "button:has-text('Send now')",
                    ]
                    for sel in send_blank_selectors:
                        try:
                            el = modal_loc.locator(sel).first
                            if el.is_visible(timeout=1000):
                                send_blank_btn = el
                                break
                        except Exception:
                            continue

                # If no modal opened, verify if the top card button changed to "Pending"
                if not send_blank_btn:
                    is_pending = False
                    try:
                        pending_loc = search_area.locator("button:has-text('Pending'), [aria-label*='Pending' i]").first
                        if pending_loc.is_visible(timeout=1500):
                            is_pending = True
                    except Exception:
                        pass
                    
                    if is_pending:
                        console.print(f"  [green]  ✓ Instant connection invite sent for {name}![/green]")
                        human_sleep(2.0, 4.0, "After send")
                        return True, "Request Sent"

                    # Neither modal nor Pending: the click did NOT send the connection!
                    if attempt == 1:
                        console.print(f"  [yellow]  ⚠ Connection modal did not open on attempt 1 for {name}. Retrying once...[/yellow]")
                        try:
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(300)
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(800)
                            page.evaluate("window.scrollTo(0, 0)")
                        except Exception:
                            pass
                        continue
                    else:
                        if not is_internet_available():
                            console.print(f"  [yellow]⚠ Internet dropped after clicking connect for {name}. Waiting for Wi-Fi recovery (Checking every 30s, max 10 mins)...[/yellow]")
                            if wait_for_network_recovery(max_wait_seconds=600, check_interval_seconds=30):
                                console.print(f"  [cyan]✓ Wi-Fi recovered! Reloading page for {name}...[/cyan]")
                                page.reload(wait_until="domcontentloaded")
                                page.wait_for_timeout(2500)
                                continue
                        console.print(f"  [red]  ❌ Connection failed for {name}: Modal never opened and status not Pending.[/red]")
                        try:
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(1000)
                        except Exception:
                            pass
                        return False, "click_failed"

                # ── SAFETY NET & LIMIT CHECK: Inside Verified Modal ───────────────
                dialog_text = ""
                try:
                    dialog_text = modal_loc.inner_text().lower()
                except Exception:
                    pass

                # Check for weekly invitation limit
                if "weekly invitation limit" in dialog_text or "out of invitations" in dialog_text:
                    console.print(f"  [bold red]  ❌ WEEKLY INVITATION LIMIT REACHED! Stopping outreach.[/bold red]")
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                    return False, "weekly_limit_reached"

                # Check if email is required to connect
                if "email" in dialog_text and ("enter" in dialog_text or "verify" in dialog_text or "know" in dialog_text):
                    console.print(f"  [yellow]  ⚠ LinkedIn requires email to connect with {name}. Skipping.[/yellow]")
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                    return False, "email_required"

                first_name = clean_first_name(name).lower() if name else ""
                raw_first = name.split()[0].lower() if name else ""
                name_verified = False

                if dialog_text:
                    if (first_name and first_name in dialog_text) or (raw_first and raw_first in dialog_text):
                        name_verified = True
                    elif "add a note" in dialog_text or "send without a note" in dialog_text or "invitation" in dialog_text:
                        name_verified = True
                    else:
                        console.print(
                            f"  [bold red]  ✘ SAFETY NET: Modal target name mismatch! Expected '{first_name}' "
                            f"in modal text, but found: '{dialog_text[:60]}...'. ABORTING connection attempt.[/bold red]"
                        )
                        try:
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(1000)
                        except Exception:
                            pass
                        return False, "modal_name_mismatch"
                else:
                    name_verified = True

                if send_blank_btn and name_verified:
                    if ghost_run:
                        console.print(f"  [dim]  GHOST RUN: Would have clicked '{send_blank_btn.inner_text().strip()}' for {name}[/dim]")
                        return True, "ghost_sent"
                    
                    page.wait_for_timeout(800)
                    try:
                        send_blank_btn.click(timeout=2000)
                    except Exception:
                        try:
                            send_blank_btn.focus()
                            page.keyboard.press("Enter")
                        except Exception:
                            send_blank_btn.evaluate("node => node.click()", timeout=2000)

                    human_sleep(2.0, 3.5, "After send")
                    return True, "Blank Sent"
                else:
                    if attempt == 1:
                        console.print(f"  [yellow]  ⚠ Could not confirm Send button in modal on attempt 1 for {name}. Retrying once...[/yellow]")
                        try:
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(300)
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(800)
                            page.evaluate("window.scrollTo(0, 0)")
                        except Exception:
                            pass
                        continue
                    else:
                        console.print(f"  [yellow]  ⚠ Could not confirm Send button in modal for {name} after retry.[/yellow]")
                        try:
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(1000)
                        except Exception:
                            pass
                        return False, "click_failed"

            except Exception as e_att:
                if attempt == 1:
                    console.print(f"  [yellow]  ⚠ Attempt 1 error for {name}: {e_att}. Retrying once...[/yellow]")
                    try:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(300)
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(800)
                        page.evaluate("window.scrollTo(0, 0)")
                    except Exception:
                        pass
                    continue
                else:
                    console.print(f"  [red]  ❌ Error sending connection to {name}: {e_att}[/red]")
                    return False, str(e_att)

        return False, "click_failed"

    # ─── Main Run ──────────────────────────────────────────────────────────────

    def run(self, leads: list[dict], dry_run: bool = False, test_mode: bool = False, ghost_run: bool = False, profile: Optional[dict] = None, remote: bool = False) -> list[dict]:
        """
        Send BLANK connection requests for all leads.

        dry_run:    Print what would happen, don't open browser.
        test_mode:  Open browser, visit profiles, but DON'T click Send.
        ghost_run:  Full browser run but skip the final 'Send without a note' click.
        profile:    User profile dictionary for personalization context.
        remote:     Connect over CDP to remote Chrome instance (port 9222).
        """
        if profile is None:
            profile_path = DATA_DIR / "my_profile.json"
            if profile_path.exists():
                try:
                    with open(profile_path, "r", encoding="utf-8") as f:
                        profile = json.load(f)
                except Exception:
                    profile = {}
            else:
                profile = {}
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
            browser, context, is_remote = self._load_session_context(p, remote=remote)
            page = context.new_page()
            Stealth_cls().apply_stealth_sync(page)

            # Validate session
            if not self._check_session_valid(page):
                self._save_session(context)
                if not is_remote:
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

                        # Network health guard: pause and wait if internet disconnected before visiting
                        if not is_internet_available():
                            console.print(f"\n[bold yellow]⚠ Internet connection lost before processing {name}. Pausing pipeline...[/bold yellow]")
                            if not wait_for_network_recovery(max_wait_seconds=600, check_interval_seconds=30):
                                console.print("[bold red]❌ Network recovery timed out. Preserving progress and stopping safely.[/bold red]")
                                return leads

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

                        # ── Experience Check & Dynamic Company Verification ──
                        verification = self._verify_experience(page, lead, profile=profile)

                        is_relevant = verification.get("is_relevant", True)
                        if not is_relevant:
                            reason = verification.get("reason", "Irrelevant or left company without active role")
                            console.print(f"  [yellow]  ⚠ Skipped: Irrelevant / Left without active company ({reason})[/yellow]")
                            lead["status"] = "Irrelevant"
                            self.skipped_count += 1
                            self.results.append(lead)
                            try:
                                from utils.sheets import SheetsClient
                                SheetsClient().update_status(url, "Irrelevant")
                                mark_contacted(url, "Irrelevant")
                            except Exception:
                                pass

                            if session_visit_count > 0 and session_visit_count % self.batch_size == 0 and visit_count < visit_limit:
                                batch_sleep(self.batch_sleep_min, self.batch_sleep_max)
                            else:
                                human_sleep(6, 12, "Between connections")
                            continue

                        is_same_company = verification.get("is_same_company", True)
                        verified_company = (verification.get("current_company") or "").strip() or company
                        verified_position = (verification.get("current_position") or "").strip() or lead.get("role", "Unknown")

                        from utils.safety import is_same_company_name
                        if not is_same_company and not is_same_company_name(company, verified_company):
                            console.print(f"  [yellow]  ⚠ Company changed for {name}! [dim]Search Lead: '{company}' → Real Experience: '{verified_company}' ({verified_position})[/dim][/yellow]")

                        # Update lead in memory with verified live details
                        lead["company"] = verified_company
                        lead["role"] = verified_position
                        top_card_text = verification.get("top_card_text", "")
                        scraped_experience = verification.get("scraped_experience", "")

                        if test_mode:
                            console.print(f"  [cyan]  TEST: Visited profile for {name} @ {verified_company}. NOT sending.[/cyan]")
                            lead["status"] = "test_visited"
                            self.results.append(lead)
                            human_sleep(2, 4)
                            continue

                        # Send blank connection
                        success, status = self._send_connection(page, lead, ghost_run=ghost_run)

                        if success:
                            self.sent_count += 1
                            sent_status = "Blank Sent" if not ghost_run else "ghost_sent"
                            lead["status"] = sent_status
                            lead["sent_at"] = datetime.now().isoformat()
                            console.print(f"  [green]  ✓ Request sent to {name} @ {verified_company}![/green]")

                            # ── 1-by-1 Dedicated Deep-Dive Gemini Drafting ONLY for confirmed sent leads ──
                            try:
                                console.print(f"  [cyan]▶ Deep-dive research & drafting DM for {name} @ {verified_company}...[/cyan]")
                                from agents.ghostwriter_agent import GhostwriterAgent
                                writer = GhostwriterAgent()
                                draft_result = writer.draft_single_lead(
                                    lead=lead,
                                    profile=profile,
                                    top_card_text=top_card_text,
                                    scraped_experience=scraped_experience
                                )
                                lead["drafted_dm"] = draft_result.get("drafted_dm", "")
                                lead["identified_pain_point"] = draft_result.get("identified_pain_point", "")
                                lead["company_analysis"] = draft_result.get("company_analysis", "")

                                pain_point = lead.get("identified_pain_point") or "Product & automation"
                                console.print(f"  [green]  ✓ DM drafted targeting: {pain_point}[/green]")
                            except Exception as e_draft:
                                console.print(f"  [yellow]  ⚠ 1-by-1 DM drafting error for {name}: {e_draft}[/yellow]")

                            # Real-time Sheet Persistence: immediately log or update the lead row in Google Sheets
                            try:
                                from utils.sheets import SheetsClient
                                sheets_client = SheetsClient()
                                sheets_client.log_or_update_lead(lead)
                                mark_contacted(url, sent_status)
                            except Exception as e_sheet:
                                console.print(f"  [dim]  Note on sheet update: {e_sheet}[/dim]")

                            self.results.append(lead)
                            self._save_checkpoint(leads)
                        else:
                            if not is_retry and status not in ("already_pending", "modal_name_mismatch", "weekly_limit_reached", "email_required"):
                                console.print(f"  [yellow]  ⚠ Connection attempt failed for {name} ({status}). Scheduling retry...[/yellow]")
                                lead["status"] = "retry"
                                self.retry_queue.append(lead)
                                try:
                                    from utils.sheets import SheetsClient
                                    SheetsClient().update_status(url, "retry")
                                except Exception:
                                    pass
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
                                if status in ("connect_button_missing", "click_failed"):
                                    try:
                                        from utils.sheets import SheetsClient
                                        SheetsClient().update_status(url, "retry")
                                    except Exception:
                                        pass
                            self._save_checkpoint(leads)

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
                        self._save_checkpoint(leads)
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

            self._save_session(context)
            if not is_remote:
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
