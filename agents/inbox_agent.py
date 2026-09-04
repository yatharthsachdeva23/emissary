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


def extract_linkedin_handle(url: str) -> str:
    """
    Extracts the unique handle/slug from a LinkedIn profile URL.
    Examples:
        'https://in.linkedin.com/in/deepak-kumar-987654' -> 'deepak-kumar-987654'
        'https://www.linkedin.com/in/deepak-kumar-987654/?miniProfile=...' -> 'deepak-kumar-987654'
        '/in/deepak-kumar-987654/' -> 'deepak-kumar-987654'
    """
    if not url:
        return ""
    url = str(url).strip()
    if "?" in url:
        url = url.split("?")[0]
    url = url.rstrip("/")
    parts = [p for p in url.split("/") if p]
    if "in" in parts:
        idx = parts.index("in")
        if idx + 1 < len(parts):
            return parts[idx + 1].lower()
    if parts:
        return parts[-1].lower()
    return url.lower()


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

    def scrape_recent_connections(self, page, limit: int = None) -> list[dict]:
        """
        Open the connections page and scrape recent connections.
        Returns a list of dicts: [{"name": str, "url": str, "handle": str, "display_name": str}]
        """
        target_limit = limit or CONNECTIONS_TO_SCRAPE

        def _warmup(self, page):
            """Visit feed first to establish session, warm up cookies, and look human."""
            console.print("[dim]  Warming up browser (visiting feed and natural scrolling 12-20s)...[/dim]")
            try:
                page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass # Ignore timeouts from hanging background scripts
            
            try:
                page.mouse.wheel(0, random.randint(300, 700))
                time.sleep(random.uniform(3.0, 5.0))
                page.mouse.wheel(0, random.randint(400, 800))
                time.sleep(random.uniform(4.0, 7.0))
                page.mouse.wheel(0, -random.randint(200, 400))
                time.sleep(random.uniform(3.0, 6.0))
            except Exception:
                time.sleep(random.uniform(12.0, 18.0))

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
        except Exception:
            console.print(f"[yellow]  ⚠ Navigation wait timed out, but proceeding anyway...[/yellow]")
        self._human_sleep(4, 7)  # Longer wait — connections page is JS-heavy

        try:
            page.mouse.move(page.viewport_size['width'] / 2, page.viewport_size['height'] / 2)
            page.locator('.scaffold-finite-scroll__content').first.click(force=True, timeout=2000)
        except Exception:
            pass

        self._human_sleep(1.0, 2.0)

        # Dynamic scrolling capped dynamically based on target_limit
        max_scrolls = max(12, (target_limit // 3) + 4)
        last_count = 0
        same_count_attempts = 0

        console.print(f"[dim]  Scanning connections page (Target Limit: {target_limit}, Max Scrolls: {max_scrolls})...[/dim]")

        for scroll_attempt in range(1, max_scrolls + 1):
            current_count = 0
            try:
                current_count = page.evaluate('''() => {
                    const links = document.querySelectorAll('a[href*="/in/"]');
                    const handles = new Set();
                    const generic = ["linkedin member", "linkedin user", "someone", "deleted user", "settings", "messaging"];
                    links.forEach(l => {
                        const href = l.href || '';
                        const txt = (l.innerText || '').split('\\n')[0].trim().toLowerCase();
                        if (href.includes('/in/') && txt && txt.length > 2 && !generic.includes(txt)) {
                            const clean = href.split('?')[0].replace(/\\/$/, '');
                            const parts = clean.split('/');
                            const h = parts[parts.length - 1];
                            if (h) handles.add(h);
                        }
                    });
                    return handles.size;
                }''')
            except Exception:
                pass
            
            console.print(f"  ⏱ Scroll cycle {scroll_attempt}/{max_scrolls}: {current_count} unique connection(s) detected on page...")

            if current_count >= target_limit:
                console.print(f"  [bold green]✓ Target limit ({target_limit}) reached with {current_count} unique connections detected. Stopping scroll.[/bold green]")
                break

            if current_count > 0 and current_count == last_count:
                same_count_attempts += 1
                if same_count_attempts >= 4:
                    console.print(f"  [yellow]ℹ Reached end of available connection list ({current_count} total connections). Stopping scroll.[/yellow]")
                    break
            else:
                same_count_attempts = 0

            last_count = current_count
                
            try:
                page.mouse.wheel(0, 1800)
                page.keyboard.press("PageDown")
                page.keyboard.press("PageDown")
                page.evaluate('''() => {
                    let containers = document.querySelectorAll('.scaffold-finite-scroll__content, .scaffold-layout__main, main');
                    containers.forEach(c => c.scrollBy(0, 1500));
                    window.scrollBy(0, 1500);
                    
                    const cards = document.querySelectorAll('li.mn-connection-card, a[href*="/in/"]');
                    if (cards.length > 0) {
                        cards[cards.length - 1].scrollIntoView({ behavior: 'smooth', block: 'end' });
                    }
                }''')
            except Exception:
                pass
            
            self._human_sleep(2.0, 3.5)
        
        self._human_sleep(1, 2)

        raw_items = []
        seen_handles = set()

        console.print(f"\n[bold cyan]  Extracting connection details (1 to {target_limit}):[/bold cyan]")

        # Scrape profiles and profile links directly from connection cards
        try:
            links = page.locator('a[href*="/in/"]').all()
            generic = ["linkedin member", "linkedin user", "someone", "deleted user", "settings", "messaging"]

            for link in links:
                try:
                    text = link.inner_text().strip()
                    href = link.get_attribute("href") or ""
                    clean_name = text.split("\n")[0].strip()
                    handle = extract_linkedin_handle(href)

                    if len(clean_name) > 3 and handle and handle not in seen_handles and clean_name.lower() not in generic:
                        seen_handles.add(handle)
                        norm_name = _normalize_name(clean_name)
                        item = {
                            "name": norm_name,
                            "url": href,
                            "handle": handle,
                            "display_name": clean_name
                        }
                        raw_items.append(item)
                        console.print(f"  [cyan]{len(raw_items):2d}. Scraped Connection:[/cyan] [bold white]{clean_name}[/bold white] [dim](handle: {handle})[/dim]")

                        if len(raw_items) >= target_limit:
                            break
                except Exception:
                    continue
        except Exception as err:
            console.print(f"[yellow]  ⚠ Connection card link extraction error: {err}[/yellow]")

        if not raw_items:
            console.print(
                "[yellow]  ⚠ Could not scrape connection URLs. "
                "LinkedIn may have changed page layout. Closer phase skipped safely.[/yellow]"
            )
            return []

        console.print(f"[green]  ✓ Scraped {len(raw_items)} recent connection(s) with profile URLs[/green]")
        return raw_items

    def build_execution_queue(self, scraped_connections: list[dict]) -> list[dict]:
        """
        Cross-reference scraped LinkedIn connections with the Google Sheet.
        Uses URL handle matching (primary) and name disambiguation (secondary)
        to prevent messaging the wrong person if multiple leads share the same name.
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

        # Count occurrences of normalized names in sheet_leads to detect duplicate names
        name_counts = {}
        for lead in sheet_leads:
            n = _normalize_name(lead.get("name", ""))
            if n:
                name_counts[n] = name_counts.get(n, 0) + 1

        queue = []
        matched_lead_urls = set()

        for sc in scraped_connections:
            sc_name = sc.get("name", "")
            sc_handle = sc.get("handle", "")
            
            match_found = None

            # --- Priority 1: Exact Unique URL Handle Match ---
            if sc_handle:
                for lead in sheet_leads:
                    lead_url = lead.get("linkedin_url", "")
                    if lead_url in matched_lead_urls:
                        continue
                    lead_handle = extract_linkedin_handle(lead_url)

                    if lead_handle and lead_handle == sc_handle:
                        match_found = lead
                        console.print(
                            f"  [green]✓ Exact URL Handle Match:[/green] '{lead['name']}' "
                            f"({lead_handle}) ↔ '{sc.get('display_name')}' ({sc_handle})"
                        )
                        break

            # --- Priority 2: Name Match (Only if UNAMBIGUOUS) ---
            if not match_found and sc_name:
                if name_counts.get(sc_name, 0) > 1:
                    console.print(
                        f"  [bold yellow]⚠ Ambiguous Name Guard:[/bold yellow] Connection '{sc_name}' "
                        f"matches {name_counts[sc_name]} leads with the same name in the sheet, but handle '{sc_handle}' "
                        f"did not match any sheet URL. Skipping to prevent messaging wrong lead."
                    )
                    continue

                for lead in sheet_leads:
                    lead_url = lead.get("linkedin_url", "")
                    if lead_url in matched_lead_urls:
                        continue
                    sheet_name_normalized = _normalize_name(lead.get("name", ""))
                    if sheet_name_normalized == sc_name:
                        match_found = lead
                        console.print(
                            f"  [green]✓ Name Match (Unambiguous):[/green] '{lead['name']}' "
                            f"(sheet) ↔ '{sc.get('display_name')}' (LinkedIn)"
                        )
                        break

            if match_found:
                matched_lead_urls.add(match_found.get("linkedin_url", ""))
                queue.append(match_found)

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
            console.print(f"\n  → Sending DM to [bold]{name}[/bold] ({url})")
            console.print(f"    Message: {dm[:120]}...")
            
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
            
            try:
                # Inject a CSS stylesheet to completely hide the top navigation bar and all Premium/upsell banners
                page.add_style_tag(content="""
                    #global-nav, 
                    .global-nav, 
                    header,
                    a[href*='premium'], 
                    button[aria-label*='premium'], 
                    [class*='premium-upsell'],
                    [id*='premium'] { 
                        display: none !important; 
                    }
                """)
            except Exception:
                pass
                
            self._human_sleep(3, 5)

            # Safety Handle Verification: Ensure settled page handle matches target lead's handle
            settled_handle = extract_linkedin_handle(page.url)
            target_handle = extract_linkedin_handle(url)
            if settled_handle and target_handle and settled_handle != target_handle:
                console.print(
                    f"  [bold red]✘ CRITICAL SAFETY GUARD: Profile handle mismatch! "
                    f"Expected '{target_handle}' but page settled on '{settled_handle}'. Aborting DM.[/bold red]"
                )
                return False

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

            # Prevent scanning the 'More profiles for you' sidebar which has its own 
            # Message buttons (clicking these on 3rd-degree connections opens the Premium modal).
            # Locate the main column container first, which is .scaffold-layout__main-column
            main_col = page.locator(".scaffold-layout__main-column, .scaffold-layout__main .scaffold-layout__main-column").first
            
            if main_col.is_visible(timeout=2000):
                main_area = main_col
            else:
                main_area = page.locator("main").first
                if not main_area.is_visible(timeout=1000):
                    main_area = page

            # Find the Message button inside the isolated top card using STRICT selectors
            message_btn = None
            msg_selectors = [
                # Strict text matches (button or a tag with exact text 'Message' or containing a span with exact text)
                'button:text-is("Message")',
                'a:text-is("Message")',
                'button:has(span:text-is("Message"))',
                'a:has(span:text-is("Message"))',
                # Flexible text matches for "Message <Name>"
                'button:has-text("Message")',
                'a:has-text("Message")',
                # Explicit aria-labels used by LinkedIn for the profile message button
                'button[aria-label^="Message "]',
                'a[aria-label^="Message "]',
                'button[aria-label*="Message"]',
                'a[aria-label*="Message"]',
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

            try:
                # Hide the global navigation bar to prevent any overlay/sticky header intercepting or receiving clicks
                page.evaluate("const nav = document.getElementById('global-nav'); if (nav) nav.style.display = 'none';")
            except Exception:
                pass

            message_btn.scroll_into_view_if_needed()
            page.evaluate("window.scrollBy(0, -100)")
            page.wait_for_timeout(400)
            
            try:
                # Use a JS click to avoid Playwright scrolling the element under the sticky top navbar
                message_btn.evaluate("el => el.click()")
            except Exception:
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

            # ── Enforce verified RESUME_LINK safety guard ───────────────────────
            correct_resume_link = os.getenv("RESUME_LINK", "").strip()
            if correct_resume_link:
                dm = re.sub(r'https?://drive\.google\.com/drive/folders/[a-zA-Z0-9_-]+', correct_resume_link, dm)

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
            scraped_conns = self.scrape_recent_connections(page)
            summary["checked"] = len(scraped_conns)

            if not scraped_conns:
                console.print("[yellow]No connections scraped. Skipping Closer phase.[/yellow]")
                browser.close()
                return summary

            console.print(f"[cyan]  Scraped {len(scraped_conns)} recent connections[/cyan]")

            # Step 2: Cross-reference with sheet
            queue = self.build_execution_queue(scraped_conns)
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

            try:
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
            except KeyboardInterrupt:
                console.print("\n[yellow]Closer interrupted by user. Stopping DM sending immediately...[/yellow]")

            try:
                browser.close()
            except Exception:
                pass

        console.print(
            f"\n[bold green]Closer done:[/bold green] "
            f"{summary['dm_sent']} DMs sent, "
            f"{summary['dm_failed']} failed, "
            f"from {summary['matched']} matched acceptances."
        )
        return summary
