"""
Manual Session Saver — Emissary
================================
How to use:
1. Close ALL Chrome windows first (important!)
2. Run THIS script: py -3.12 save_session.py
3. A Chrome window will open. Log into LinkedIn normally.
4. Once you are on the LinkedIn feed, come back here and press Enter.
5. Session saved! You can now run: py -3.12 main.py
"""

import json
import subprocess
import sys
import time
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
SESSION_PATH = DATA_DIR / "linkedin_session.json"

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Users\{}\AppData\Local\Google\Chrome\Application\chrome.exe".format(
        __import__("os").environ.get("USERNAME", "")
    ),
]

def find_chrome():
    for path in CHROME_PATHS:
        if Path(path).exists():
            return path
    return None


def main():
    print("\n" + "="*60)
    print("  Emissary — Manual Session Saver")
    print("="*60)

    chrome_exe = find_chrome()
    if not chrome_exe:
        print("\nERROR: Could not find Chrome. Please install Google Chrome.")
        sys.exit(1)

    print(f"\n[1/3] Found Chrome at: {chrome_exe}")
    print("[2/3] Opening Chrome with debug port... (Close all Chrome windows first!)")

    # Launch Chrome with remote debugging enabled
    subprocess.Popen([
        chrome_exe,
        "--remote-debugging-port=9222",
        "--user-data-dir=C:/tmp/emissary_chrome_profile",
        "https://www.linkedin.com/login",
    ])

    print("\nChrome is opening. Please:")
    print("  -> Log into LinkedIn")
    print("  -> Wait until you see your feed (linkedin.com/feed)")
    print("\nOnce you are on the feed, press Enter here...")
    input()

    print("\n[3/3] Connecting and saving your session...")

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
            context = browser.contexts[0]
            
            # Check we actually got a LinkedIn session cookie
            storage = context.storage_state()
            cookies = storage.get("cookies", [])
            li_at = [c for c in cookies if c.get("name") == "li_at"]

            if not li_at:
                print("\nERROR: LinkedIn session cookie (li_at) not found.")
                print("Make sure you are fully logged in to LinkedIn before pressing Enter.")
                browser.close()
                sys.exit(1)

            DATA_DIR.mkdir(exist_ok=True)
            with open(SESSION_PATH, "w") as f:
                json.dump(storage, f, indent=2)

            print(f"\nSESSION SAVED to {SESSION_PATH}")
            print(f"Found {len(cookies)} cookies including LinkedIn auth token.")
            browser.close()

    except Exception as e:
        print(f"\nError connecting to Chrome: {e}")
        print("Make sure Chrome is open and you are logged into LinkedIn.")
        sys.exit(1)

    print("\n" + "="*60)
    print("  Done! You can now run: py -3.12 main.py")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
