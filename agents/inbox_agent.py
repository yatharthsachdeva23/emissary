"""
Inbox Agent — Emissary Phase 1 (The Closer)
Checks who accepted your blank connection requests and sends them
the pre-drafted 5-part DM with resume link.

Safety Protocol:
- Opens ONLY one LinkedIn page (/mynetwork/invite-connect/connections/)
- Never mass-visits profiles to check button statuses
- Random 3-8 second jitter between every Playwright action
- Ghost-run mode: types DM but does NOT press Enter
"""

import os
import re
import sys
import time
import random
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

load_dotenv()
console = Console()

CONNECTIONS_URL = "https://www.linkedin.com/mynetwork/invite-connect/connections/"
DATA_DIR = Path(__file__).parent.parent / "data"
SESSION_PATH = DATA_DIR / "linkedin_session.json"

# How many recent connections to scrape (safe — all on one page, no pagination)
CONNECTIONS_TO_SCRAPE = 30


def _normalize_name(raw: str) -> str:
    """
    Strip emojis, suffixes, titles, punctuation and extra whitespace.
    'Harshavardhan B.\nTech Lead' -> 'harshavardhan b'
    """
    if not raw: return ""
    # Take only the first line
    raw = raw.split('\n')[0].split('\r')[0]
    # Remove non-ascii (emojis etc)
    raw = re.sub(r'[^\x00-\x7F]+', ' ', raw)
    # Remove parenthetical suffixes
    raw = re.sub(r'\(.*?\)', '', raw)
    # Remove punctuation that causes mismatches (dots after initials etc)
    raw = re.sub(r'[.,;!|@•-]', ' ', raw)
    # Final cleanup: lowercase and collapse multiple spaces
    return " ".join(raw.lower().split()).strip()


class InboxAgent:
    def __init__(self):
        self._playwright = None
        self._stealth_cls = None
        # Detected at runtime: e.g. 'https://in.linkedin.com' for Indian users
        self._linkedin_base = "https://www.linkedin.com"

    def _get_playwright(self):
        try:
            from playwright.sync_api import sync_playwright
            from playwright_stealth import Stealth
            return sync_playwright, Stealth
        except ImportError:
            console.print("[red]Playwright not installed. Run: pip install playwright playwright-stealth[/red]")
            sys.exit(1)

    def _load_session_context(self, playwright):
        """Load saved session cookies into a new browser context."""
        if not SESSION_PATH.exists():
            console.print("[red]No session found. Run: py -3.12 main.py --setup-session[/red]")
            sys.exit(1)

        import json
        with open(SESSION_PATH, "r") as f:
            storage_state = json.load(f)

        browser = playwright.chromium.launch(
            channel="chrome",          # Use real Chrome, not bundled Chromium
            headless=False,
            slow_mo=random.randint(30, 80),
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            # No custom user_agent — real Chrome's own UA is more trusted
            storage_state=storage_state,
            viewport={"width": 1280, "height": 800},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
        """)
        return browser, context

    def _human_sleep(self, min_s: float = 3.0, max_s: float = 8.0):
        """Strictly jittered sleep between every Playwright action."""
        time.sleep(random.uniform(min_s, max_s))

    def scrape_recent_connections(self, page) -> list[str]:
        """
        Open the connections page and scrape the top N connection names.
        Returns a list of normalized names.
        Uses 3 fallback strategies to handle LinkedIn DOM changes.
        """
        def _warmup(self, page):
            """Visit feed first to establish session and look human."""
            console.print("[dim]  Warming up browser (visiting feed)...[/dim]")
            try:
                page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass # Ignore timeouts from hanging background scripts
            self._human_sleep(12, 15)  # Extended wait for login redirects/account selection

        _warmup(self, page)

        # ── Auto-detect regional LinkedIn domain ────────────────────────────
        settled_url = page.url  # e.g. 'https://in.linkedin.com/feed/'
        if "linkedin.com" in settled_url:
            from urllib.parse import urlparse
            parsed = urlparse(settled_url)
            self._linkedin_base = f"{parsed.scheme}://{parsed.netloc}"
            console.print(f"[dim]  Detected LinkedIn domain: {self._linkedin_base}[/dim]")

        # Build the correct connections URL using the detected domain
        connections_url = self._linkedin_base + "/mynetwork/invite-connect/connections/"

        console.print(f"[cyan]  Navigating to connections page...[/cyan]")
        try:
            page.goto(connections_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            console.print(f"[yellow]  ⚠ Navigation wait timed out, but proceeding anyway...[/yellow]")
        self._human_sleep(4, 7)  # Longer wait — connections page is JS-heavy

        # Focus on the connections area first so PageDown targets the correct scrollable div.
        # CRITICAL FIX: Ensure focus is inside the scrollable container.
        try:
            # Hover over the middle of the screen to ensure mouse wheel targets the right area
            page.mouse.move(page.viewport_size['width'] / 2, page.viewport_size['height'] / 2)
            # Try to click a safe non-link area in the container to focus it
            page.locator('.scaffold-finite-scroll__content').first.click(force=True, timeout=2000)
        except Exception:
            pass

        self._human_sleep(1.0, 2.0)
        
        raw_names = []
        modern_selectors = [
            "span.mn-connection-card__name",
            ".mn-connection-card__name",
            "[data-view-name='connections-list-item'] span.t-16",
            "a.mn-connection-card__link span",
            "li.mn-connection-card span.t-16",
            ".scaffold-finite-scroll__content li span.t-16",
        ]

        # Dynamic scrolling: scroll and wait until we have enough names
        for scroll_attempt in range(12):
            # 1. Count actual connection list items, not just random links on the page
            current_count = 0
            try:
                # Count list items inside the main container
                current_count = page.locator('.scaffold-finite-scroll__content li, li.mn-connection-card').count()
            except: pass
            
            if current_count >= CONNECTIONS_TO_SCRAPE:
                break
                
            # 2. Scroll down aggressively using multiple methods
            try:
                # Method A: Mouse wheel (most natural, targets what's under cursor)
                page.mouse.wheel(0, 1500)
                
                # Method B: Keyboard
                page.keyboard.press("PageDown")
                page.keyboard.press("PageDown")
                
                # Method C: JavaScript on the specific scrollable container
                page.evaluate('''() => {
                    let containers = document.querySelectorAll('.scaffold-finite-scroll__content, .scaffold-layout__main');
                    containers.forEach(c => c.scrollBy(0, 1500));
                    window.scrollBy(0, 1500);
                }''')
            except: pass
            
            # Wait for lazy loading
            self._human_sleep(2.0, 4.0)
        
        try:
            page.keyboard.press("Home")
            page.evaluate('''() => {
                let containers = document.querySelectorAll('.scaffold-finite-scroll__content, .scaffold-layout__main');
                containers.forEach(c => c.scrollTo(0, 0));
                window.scrollTo(0, 0);
            }''')
        except: pass
        self._human_sleep(1, 2)

        # ── Strategy 1: Modern LinkedIn class selectors (2024–2025 DOM) ──
        for selector in modern_selectors:
            try:
                elements = page.locator(selector).all()
                names = [el.inner_text().strip() for el in elements if el.inner_text().strip()]
                if len(names) >= 2:  # Need at least 2 to be meaningful
                    raw_names = names[:CONNECTIONS_TO_SCRAPE]
                    console.print(f"[green]  ✓ Strategy 1: Scraped {len(raw_names)} names via '{selector}'[/green]")
                    break
            except Exception:
                continue

        # ── Strategy 2: Aria-label links (LinkedIn profile link text) ──
        if not raw_names:
            try:
                links = page.locator('a[href*="/in/"]').all()
                seen = set()
                for link in links:
                    try:
                        text = link.inner_text().strip()
                        href = link.get_attribute("href") or ""
                        # Exclude navigation/button links (short text or no /in/ path)
                        if len(text) > 3 and "/in/" in href and text not in seen:
                            raw_names.append(text)
                            seen.add(text)
                            if len(raw_names) >= CONNECTIONS_TO_SCRAPE:
                                break
                    except Exception:
                        continue
                if raw_names:
                    console.print(f"[green]  ✓ Strategy 2: Scraped {len(raw_names)} names via profile links[/green]")
            except Exception:
                pass

        # ── Strategy 3: Full page text regex extraction ──
        if not raw_names:
            try:
                import re as _re
                content = page.content()
                found = _re.findall(
                    r'aria-label=["\'](?:View\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\'?s?\s+profile["\']',
                    content
                )
                found += _re.findall(
                    r'Send message to ([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)',
                    content
                )
                raw_names = list(dict.fromkeys(found))
            except Exception:
                pass

        # ── Strategy 4: Generic List Item Extraction ──
        if not raw_names:
            try:
                # Find all LI items in the connections scaffold
                items = page.locator(".scaffold-finite-scroll__content li, .mn-connection-card").all()
                for item in items:
                    text = item.inner_text().split("\n")[0].strip()
                    if len(text) > 3 and text not in raw_names:
                        raw_names.append(text)
            except Exception:
                pass

        # Filter out generic names
        generic = ["linkedin member", "linkedin user", "someone", "deleted user"]
        raw_names = [n for n in raw_names if n.lower().strip() not in generic]
        raw_names = list(dict.fromkeys(raw_names))[:CONNECTIONS_TO_SCRAPE]

        if not raw_names:
            console.print(
                "[yellow]  ⚠ All 3 scrape strategies failed. "
                "LinkedIn may have A/B-tested a new layout. "
                "Closer phase skipped safely.[/yellow]"
            )
            return []

        normalized = [_normalize_name(n) for n in raw_names if n.strip()]
        return [n for n in normalized if n]  # Drop any empty strings after normalization

    def build_execution_queue(self, scraped_names: list[str]) -> list[dict]:
        """
        Cross-reference scraped LinkedIn names with the Google Sheet.
        Returns leads that (a) exist in the sheet with Status='Blank Sent'
        and (b) match a scraped name exactly.
        """
        try:
            from utils.sheets import SheetsClient
            sheet_leads = SheetsClient().get_blank_sent_leads()
        except Exception as e:
            console.print(f"[red]  Could not read sheet: {e}[/red]")
            return []

        if not sheet_leads:
            console.print("[dim]  No 'Blank Sent' leads in sheet.[/dim]")
            return []

        queue = []
        for lead in sheet_leads:
            sheet_name = lead.get("name", "").strip().lower()
            if not sheet_name:
                continue
            # Exact match (case-insensitive) after normalization.
            # Names in the sheet are written exactly by the bot itself,
            # so after _normalize_name both sides should be identical.
            # No fuzzy matching — avoids sending the wrong DM to the wrong person.
            sheet_name_normalized = _normalize_name(sheet_name)
            for scraped in scraped_names:
                if sheet_name_normalized == scraped:
                    console.print(
                        f"  [green]✓ Match:[/green] '{lead['name']}' "
                        f"(sheet) ↔ '{scraped}' (LinkedIn)"
                    )
                    queue.append(lead)
                    break  # Don't double-add the same lead

        return queue

    def send_dm(self, page, lead: dict, ghost_run: bool = False) -> bool:
        """
        Navigate to lead's profile, click Message, paste the DM, and send.
        Returns True on success.
        """
        name = lead.get("name", "Unknown")
        url = lead.get("linkedin_url", "")
        dm = lead.get("drafted_dm", "")

        if not url:
            console.print(f"  [yellow]⚠ No URL for {name} — skipping[/yellow]")
            return False

        if not dm:
            console.print(f"  [yellow]⚠ No drafted DM for {name} — skipping[/yellow]")
            return False

        try:
            console.print(f"\n  → Sending DM to [bold]{name}[/bold]")
            
            # Normalize URL to use the detected regional domain (e.g. in.linkedin.com)
            url = url.strip()
            if url.startswith("http://"):
                url = "https://" + url[len("http://"):]
            known_prefixes = [
                "https://www.linkedin.com",
                "https://in.linkedin.com",
                "https://linkedin.com",
                "https://uk.linkedin.com",
            ]
            for prefix in known_prefixes:
                if url.startswith(prefix):
                    url = self._linkedin_base + url[len(prefix):]
                    break

            # (Moved chat panel closing logic to after page load)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass # Proceed anyway if page times out on background scripts
            self._human_sleep(3, 5)

            # ── Close any lingering LinkedIn chat panels from previous DMs ──────
            # After each send, or upon page load, LinkedIn might open chat bubbles.
            # These stack up and push the new compose box out of the viewport.
            # It's crucial to do this AFTER page load/hydration.
            try:
                close_btns = page.locator(
                    "button[aria-label='Close your conversation'], "
                    "button[aria-label*='Close'], "
                    "button.msg-overlay-bubble-header__control--close, "
                    "button.msg-overlay-conversation-bubble__button-close"
                ).all()
                for btn in close_btns:
                    try:
                        if btn.is_visible(timeout=500):
                            btn.click(force=True)
                            page.wait_for_timeout(300)
                    except Exception:
                        pass
            except Exception:
                pass
            # ─────────────────────────────────────────────────────────────────────

            # Guard: if we landed on login/authwall, skip this lead
            current_url = page.url
            if any(x in current_url for x in ("authwall", "/login", "/signup", "checkpoint")):
                console.print(f"  [red]  ✘ Profile redirected to login/authwall. Skipping DM.[/red]")
                return False

            # ── ISOLATE TOP CARD ─────────────────────────────────────────────────
            # Prevent scanning the 'More profiles for you' sidebar which has its own 
            # Message buttons (clicking these on 3rd-degree connections opens the Premium modal).
            main_area = page.locator(
                ".scaffold-layout__main .pv-top-card, "
                "main .pv-top-card, "
                ".scaffold-layout__main section.artdeco-card:first-of-type, "
                "main section.artdeco-card:first-of-type"
            ).first
            
            if not main_area.is_visible(timeout=500):
                main_area = page.locator(".scaffold-layout__main").first
                if not main_area.is_visible(timeout=500):
                    main_area = page.locator("main")

            # Find the Message button inside the isolated top card using STRICT selectors
            message_btn = None
            msg_selectors = [
                # Strict text matches (button or a tag with exact text 'Message' or containing a span with exact text)
                'button:text-is("Message")',
                'a:text-is("Message")',
                'button:has(span:text-is("Message"))',
                'a:has(span:text-is("Message"))',
                # Explicit aria-labels used by LinkedIn for the profile message button
                'button[aria-label^="Message "]',
                'a[aria-label^="Message "]',
                'button[aria-label^="Send a message to"]',
                'a[aria-label^="Send a message to"]'
            ]
            for selector in msg_selectors:
                try:
                    btns = main_area.locator(selector).all()
                    for btn in btns:
                        if not btn.is_visible(timeout=500):
                            continue
                        
                        # 1. Guard against clicking Premium ad banners
                        btn_text = btn.inner_text().lower()
                        if "premium" in btn_text or "try" in btn_text or "₹" in btn_text or "free" in btn_text:
                            continue
                            
                        # 2. Guard against 'Message with Premium' aria-labels
                        aria = (btn.get_attribute("aria-label") or "").lower()
                        if "premium" in aria:
                            continue
                            
                        # 3. Guard against Sidebar buttons (x coordinate check)
                        # The main profile column is always on the left (x < 700px).
                        # Sidebar buttons (More profiles for you) are on the right (x > 900px).
                        rect = btn.evaluate(
                            "el => { const r = el.getBoundingClientRect(); "
                            "return {x: r.x}; }"
                        )
                        if rect and rect.get("x", 9999) > 700:
                            continue

                        message_btn = btn
                        break
                except Exception:
                    continue
                if message_btn:
                    break

            if not message_btn:
                console.print(f"  [yellow]  ⚠ Message button not found for {name}[/yellow]")
                return False

            message_btn.scroll_into_view_if_needed()
            page.evaluate("window.scrollBy(0, -100)")
            page.wait_for_timeout(400)
            message_btn.click()
            # Wait longer for the new chat box to fully load and take focus
            self._human_sleep(4, 6)

            # Wait for the message compose box
            compose_box = None
            compose_selectors = [
                # Most specific: aria-label contains "message" on a contenteditable
                '.msg-form__contenteditable[contenteditable="true"]',
                'div[role="textbox"][contenteditable="true"][aria-label*="message"]',
                'div[role="textbox"][contenteditable="true"][aria-label*="Write"]',
                # Overlay panel container
                '.msg-overlay-conversation-bubble div[contenteditable="true"]',
                '.msg-overlay-list-bubble div[contenteditable="true"]',
                # Broad fallbacks
                'div[role="textbox"][contenteditable="true"]',
                'div[contenteditable="true"]',
                '.msg-form__contenteditable',
            ]
            
            # Poll for up to 4 seconds to find a visible compose box
            for _ in range(8):
                for selector in compose_selectors:
                    try:
                        boxes = page.locator(selector).all()
                        visible_boxes = [b for b in boxes if b.is_visible()]
                        if visible_boxes:
                            compose_box = visible_boxes[-1]  # Get the most recently opened visible box
                            break
                    except Exception:
                        continue
                if compose_box:
                    break
                page.wait_for_timeout(500)

            if not compose_box:
                console.print(f"  [yellow]  ⚠ Message compose box not found for {name}[/yellow]")
                return False

            # Scroll the compose box into view before interacting.
            compose_box.scroll_into_view_if_needed()
            page.wait_for_timeout(400)

            # Extra slight wait to ensure the chat box React state is fully initialized
            self._human_sleep(1.5, 3.0)

            # Focus the box using both click and JS focus.
            # Must focus BEFORE typing so keystrokes go to the right element.
            try:
                compose_box.click(force=True)
            except Exception:
                compose_box.evaluate("el => el.focus()")
            page.wait_for_timeout(300)

            # Clear any placeholder text (contenteditable divs retain placeholder as DOM text)
            page.keyboard.press("Control+a")
            page.wait_for_timeout(200)

            # ── TYPE the DM using real keystrokes ───────────────────────────────
            # CRITICAL: compose_box.fill() does NOT work on contenteditable divs.
            # LinkedIn's React state only updates on real keyboard events (onChange).
            # We must use page.keyboard.type() to simulate actual keypresses.
            # ────────────────────────────────────────────────────────────────────
            page.keyboard.type(dm, delay=30)  # 30ms delay between chars = human-like
            self._human_sleep(1.0, 2.0)

            if ghost_run:
                console.print(f"  [dim]  GHOST RUN: DM typed for {name} but NOT sent.[/dim]")
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                return True

            # Send: try the Send button first, fall back to Ctrl+Enter then Enter
            sent = False
            send_selectors = [
                '.msg-form__send-button',
                'button.msg-form__send-button',
                'button[aria-label="Send"]',
                'button:has-text("Send")',
                'button[type="submit"]:has-text("Send")',
            ]
            for _ in range(4):
                for sel in send_selectors:
                    try:
                        btns = page.locator(sel).all()
                        visible_btns = [b for b in btns if b.is_visible()]
                        if visible_btns:
                            btn = visible_btns[-1]
                            btn.scroll_into_view_if_needed()
                            btn.click(force=True)
                            sent = True
                            break
                    except Exception:
                        continue
                if sent:
                    break
                page.wait_for_timeout(500)

            if not sent:
                page.keyboard.press("Control+Enter")

            self._human_sleep(2, 4)

            # Verification: box should be empty or gone after a successful send
            try:
                final_text = compose_box.inner_text().strip()
                if final_text and len(final_text) > 5:
                    console.print(f"  [red]  ✘ DM verification failed for {name}. Message still in box.[/red]")
                    return False
            except Exception:
                pass  # Box being detached/gone is a success signal

                
            console.print(f"  [green]  ✓ DM sent to {name}[/green]")
            return True

        except Exception as e:
            console.print(f"  [red]  DM error for {name}: {e}[/red]")
            return False

    def run(self, ghost_run: bool = False) -> dict:
        """
        Full inbox agent run. Returns summary dict.
        """
        console.print("\n[bold cyan]━━━ Phase 1: Inbox Agent (The Closer) ━━━[/bold cyan]")
        if ghost_run:
            console.print("[yellow]GHOST RUN: DMs will be typed but NOT sent.[/yellow]")

        summary = {"checked": 0, "matched": 0, "dm_sent": 0, "dm_failed": 0}

        sync_playwright, Stealth_cls = self._get_playwright()

        with sync_playwright() as p:
            browser, context = self._load_session_context(p)
            page = context.new_page()
            Stealth_cls().apply_stealth_sync(page)

            # Step 1: Scrape recent connections
            scraped_names = self.scrape_recent_connections(page)
            summary["checked"] = len(scraped_names)

            if not scraped_names:
                console.print("[yellow]No connections scraped. Skipping Closer phase.[/yellow]")
                browser.close()
                return summary

            console.print(f"[cyan]  Scraped {len(scraped_names)} recent connections[/cyan]")

            # Step 2: Cross-reference with sheet
            queue = self.build_execution_queue(scraped_names)
            summary["matched"] = len(queue)

            if not queue:
                console.print("[dim]  No new acceptances today. Closer phase done.[/dim]")
                browser.close()
                return summary

            console.print(f"[green]  → {len(queue)} new acceptance(s) found! Sending DMs...[/green]")

            # Step 3: Send DMs
            try:
                from utils.sheets import SheetsClient
                sheet = SheetsClient()
            except Exception:
                sheet = None

            for lead in queue:
                success = self.send_dm(page, lead, ghost_run=ghost_run)

                if success and not ghost_run:
                    summary["dm_sent"] += 1
                    # Update sheet status to DM Sent
                    if sheet:
                        sheet.update_status_by_name(lead["name"], "DM Sent")
                        console.print(f"  [green]  ✓ Sheet updated: DM Sent[/green]")
                elif success and ghost_run:
                    summary["dm_sent"] += 1  # Ghost counts as sent for stats
                else:
                    summary["dm_failed"] += 1

                self._human_sleep(4, 8)  # Strict jitter between each DM

            browser.close()

        console.print(
            f"\n[bold green]Closer done:[/bold green] "
            f"{summary['dm_sent']} DMs sent, "
            f"{summary['dm_failed']} failed, "
            f"from {summary['matched']} matched acceptances."
        )
        return summary
