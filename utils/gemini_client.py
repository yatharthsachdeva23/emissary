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
from typing import Optional, Any

from google import genai
from dotenv import load_dotenv
from rich.console import Console
import sys
import io

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

load_dotenv()
console = Console(legacy_windows=False)

MODEL_CASCADE = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
]

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


def _is_high_demand_error(e: Exception) -> bool:
    msg = str(e).lower()
    return (
        "503" in msg
        or "unavailable" in msg
        or "high demand" in msg
        or "spikes in demand" in msg
        or "overloaded" in msg
        or "temporarily unavailable" in msg
        or "capacity" in msg
    )


def _is_quota_exhausted_error(e: Exception) -> bool:
    msg = str(e).lower()
    return (
        "429" in msg
        or "resource_exhausted" in msg
        or "quota" in msg
        or "rate limit" in msg
        or "exhausted" in msg
        or "limit exceeded" in msg
    )


def generate_with_rotation(
    prompt: Optional[str] = None,
    model: Optional[str] = None,
    contents: Any = None,
    config: Any = None,
    max_retries_per_key: int = 1,
) -> str:
    """
    Call Gemini with automatic model cascade on 503 high-demand errors
    and key switching on 429 quota exhausted errors.

    Model Cascade: 3.8-flash -> 3.6-flash -> 3.5-flash -> 3.5-flash-lite
    """
    payload = contents if contents is not None else prompt
    if payload is None:
        raise ValueError("Either prompt or contents must be provided to generate_with_rotation.")

    primary = model or os.getenv("GEMINI_MODEL", MODEL_CASCADE[0])
    models_to_try = [primary] + [m for m in MODEL_CASCADE if m != primary]

    global _current_idx
    keys = _get_keys()
    if not keys:
        raise RuntimeError(
            "No Gemini API keys configured. Add GEMINI_API_KEY_1 (through _4) "
            "or GEMINI_API_KEY to your .env file."
        )

    num_keys = len(keys)
    max_key_attempts = num_keys * 3  # up to 3 full circular passes
    key_attempts = 0

    while key_attempts < max_key_attempts:
        idx = _current_idx % num_keys
        key = keys[idx]
        key_label = f"Key {idx + 1}"
        client = genai.Client(api_key=key)

        model_idx = 0
        while model_idx < len(models_to_try):
            current_model = models_to_try[model_idx]
            try:
                kwargs = {"model": current_model, "contents": payload}
                if config is not None:
                    kwargs["config"] = config
                resp = client.models.generate_content(**kwargs)
                return resp.text
            except Exception as e:
                err = str(e)
                if _is_high_demand_error(e):
                    if model_idx + 1 < len(models_to_try):
                        next_model = models_to_try[model_idx + 1]
                        console.print(
                            f"[yellow]⚠ Model '{current_model}' is experiencing high demand (503). "
                            f"Falling back to '{next_model}' on {key_label}...[/yellow]"
                        )
                        model_idx += 1
                        time.sleep(1.0)
                        continue
                    else:
                        console.print(
                            f"[yellow]⚠ All models ({', '.join(models_to_try)}) hit high demand. "
                            f"Waiting 2s before trying next key...[/yellow]"
                        )
                        time.sleep(2.0)
                        _current_idx = (idx + 1) % num_keys
                        break
                elif _is_quota_exhausted_error(e):
                    console.print(
                        f"[yellow]⚠ Gemini {key_label} limit exhausted (429/quota). "
                        f"Switching to next key...[/yellow]"
                    )
                    _current_idx = (idx + 1) % num_keys
                    time.sleep(0.5)
                    break
                else:
                    console.print(
                        f"[yellow]⚠ Gemini {key_label} failed on '{current_model}': {err[:140]}. "
                        f"Switching to next key...[/yellow]"
                    )
                    _current_idx = (idx + 1) % num_keys
                    time.sleep(1.0)
                    break

        key_attempts += 1

    raise RuntimeError(
        f"⚠ All Gemini API keys and model fallbacks ({', '.join(models_to_try)}) failed "
        f"after {max_key_attempts} attempts."
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

