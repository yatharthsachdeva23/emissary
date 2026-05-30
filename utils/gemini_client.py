"""
utils/gemini_client.py — Emissary
Multi-key Gemini client with automatic quota-based failover.

Priority order is defined by the .env variables:
  GEMINI_API_KEY_1  (highest priority — tried first)
  GEMINI_API_KEY_2
  GEMINI_API_KEY_3
  GEMINI_API_KEY_4  (lowest priority — last resort)

Falls back to the legacy GEMINI_API_KEY if the numbered slots are unset.

Rules:
- Each new calendar day, priority resets to Key 1.
- Within a single session, if a key hits a 429 quota error it is
  marked exhausted and the next key in the list is tried automatically.
- All non-quota errors (503, network, etc.) raise normally — only
  ResourceExhausted triggers a key rotation.
"""

import os
import re
import math
import time
from datetime import date
from typing import Optional

from google import genai
from dotenv import load_dotenv
from rich.console import Console

load_dotenv()
console = Console()

# ── State ──────────────────────────────────────────────────────────────────────
_exhausted: set[int] = set()       # indices of keys that returned 429 today
_rotation_date: Optional[date] = None  # last date exhausted list was reset


def _get_keys() -> list[str]:
    """
    Read up to 4 prioritised keys from env. Falls back to the legacy
    GEMINI_API_KEY if the numbered keys are not set.
    """
    numbered = [
        os.getenv("GEMINI_API_KEY_1", ""),
        os.getenv("GEMINI_API_KEY_2", ""),
        os.getenv("GEMINI_API_KEY_3", ""),
        os.getenv("GEMINI_API_KEY_4", ""),
    ]
    # Filter out empty / placeholder values
    keys = [k for k in numbered if k and not k.startswith("your_")]

    # Legacy fallback
    if not keys:
        legacy = os.getenv("GEMINI_API_KEY", "")
        if legacy and not legacy.startswith("your_"):
            keys = [legacy]

    return keys


def has_gemini_keys() -> bool:
    """Return True if at least one valid Gemini API key is configured."""
    return len(_get_keys()) > 0


def _reset_if_new_day() -> None:
    """Reset exhausted set at the start of each new calendar day."""
    global _exhausted, _rotation_date
    today = date.today()
    if _rotation_date != today:
        _exhausted = set()
        _rotation_date = today


def _active_key_index(keys: list[str]) -> Optional[int]:
    """Return index of the first non-exhausted key, or None."""
    for i in range(len(keys)):
        if i not in _exhausted:
            return i
    return None


def generate_with_rotation(
    prompt: str,
    model: str = "gemini-2.5-flash",
    max_retries_per_key: int = 3,
) -> str:
    """
    Call Gemini with automatic key rotation on 429 quota errors.

    Tries each non-exhausted key in priority order.  Within a single key,
    retries up to `max_retries_per_key` times on transient 503/overload errors
    before giving up on that key.

    Raises RuntimeError if all keys are exhausted.
    """
    global _exhausted
    _reset_if_new_day()
    keys = _get_keys()

    if not keys:
        raise RuntimeError(
            "No Gemini API keys configured. Add GEMINI_API_KEY_1 (through _4) "
            "or GEMINI_API_KEY to your .env file."
        )

    while True:
        idx = _active_key_index(keys)
        if idx is None:
            raise RuntimeError(
                "⚠ All Gemini API keys exhausted for today. "
                "Add more keys or wait until midnight for quota reset."
            )

        key = keys[idx]
        client = genai.Client(api_key=key)
        key_label = f"Key {idx + 1}"

        for attempt in range(max_retries_per_key):
            try:
                resp = client.models.generate_content(model=model, contents=prompt)
                return resp.text

            except Exception as e:
                err = str(e)

                # ── Quota exceeded → rotate key ──────────────────────────────
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    console.print(
                        f"[yellow]⚠ Gemini {key_label} quota exhausted "
                        f"(429). Rotating to next key...[/yellow]"
                    )
                    _exhausted.add(idx)
                    break   # break inner retry loop → outer loop picks next key

                # ── Transient overload / 503 → wait and retry same key ───────
                elif "503" in err or "overload" in err.lower() or "unavailable" in err.lower():
                    m = re.search(r"retry in (\d+(?:\.\d+)?)s", err)
                    wait = math.ceil(float(m.group(1))) + 2 if m else min(15 * (2 ** attempt), 90)
                    console.print(
                        f"[yellow]⚠ Gemini {key_label} overloaded "
                        f"(attempt {attempt + 1}/{max_retries_per_key}). "
                        f"Retrying in {wait}s...[/yellow]"
                    )
                    time.sleep(wait)

                # ── Unknown error → re-raise immediately ─────────────────────
                else:
                    raise

        # If we broke out of the retry loop due to 429 → continue outer loop
        # (next iteration will pick the next non-exhausted key)


def get_client_with_rotation() -> tuple["genai.Client", str]:
    """
    Return (client, key_label) for the currently active (non-exhausted) key.
    Useful when agents need to call the Gemini client directly (e.g. for their
    own retry loops), but still want key rotation support.

    Raises RuntimeError if no keys are available.
    """
    _reset_if_new_day()
    keys = _get_keys()

    if not keys:
        raise RuntimeError("No Gemini API keys configured.")

    idx = _active_key_index(keys)
    if idx is None:
        raise RuntimeError(
            "⚠ All Gemini API keys exhausted for today. "
            "Wait until midnight or add more keys."
        )

    return genai.Client(api_key=keys[idx]), f"Key {idx + 1}"


def mark_key_exhausted() -> None:
    """
    Called by agents that manage their own retry loops.
    Marks the current active key as exhausted so the next call
    to get_client_with_rotation() returns the next key.
    """
    global _exhausted
    _reset_if_new_day()
    keys = _get_keys()
    idx = _active_key_index(keys)
    if idx is not None:
        _exhausted.add(idx)
        console.print(f"[yellow]⚠ Gemini Key {idx + 1} marked exhausted. "
                      f"Next call will use Key {idx + 2} if available.[/yellow]")
