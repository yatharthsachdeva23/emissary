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
# ── State ──────────────────────────────────────────────────────────────────────
_current_idx: int = 0  # Round-robin key pointer

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


def generate_with_rotation(
    prompt: str,
    model: str = None,
    max_retries_per_key: int = 1, # Not used directly as we switch keys immediately
) -> str:
    """
    Call Gemini with automatic immediate round-robin key rotation on any error.
    Tries all keys in a circular round-robin fashion up to 3 full loops.
    """
    model = model or os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    global _current_idx
    keys = _get_keys()

    if not keys:
        raise RuntimeError(
            "No Gemini API keys configured. Add GEMINI_API_KEY_1 (through _4) "
            "or GEMINI_API_KEY to your .env file."
        )

    num_keys = len(keys)
    max_total_attempts = num_keys * 3  # up to 3 full circle loops

    for attempt in range(max_total_attempts):
        idx = _current_idx % num_keys
        key = keys[idx]
        client = genai.Client(api_key=key)
        key_label = f"Key {idx + 1}"

        try:
            resp = client.models.generate_content(model=model, contents=prompt)
            # Success! Keep _current_idx where it is (or keep it pointing to this successful key index)
            return resp.text
        except Exception as e:
            err = str(e)
            console.print(
                f"[yellow]⚠ Gemini {key_label} failed on attempt {attempt + 1}/{max_total_attempts} with error: {err}. "
                f"Rotating to next key immediately...[/yellow]"
            )
            # Move the pointer to the next key for the next try
            _current_idx = (idx + 1) % num_keys

    raise RuntimeError(
        f"⚠ All Gemini API keys failed after 3 full round-robin loops (total {max_total_attempts} attempts)."
    )


def get_client_with_rotation() -> tuple["genai.Client", str]:
    """
    Return (client, key_label) for the currently active round-robin key.
    """
    keys = _get_keys()
    if not keys:
        raise RuntimeError("No Gemini API keys configured.")
    
    idx = _current_idx % len(keys)
    return genai.Client(api_key=keys[idx]), f"Key {idx + 1}"


def mark_key_exhausted() -> None:
    """
    Moves the round-robin key index pointer to the next key.
    """
    global _current_idx
    keys = _get_keys()
    if keys:
        idx = _current_idx % len(keys)
        _current_idx = (idx + 1) % len(keys)
        console.print(f"[yellow]⚠ Rotated round-robin Gemini Key pointer to Key {_current_idx + 1}.[/yellow]")

