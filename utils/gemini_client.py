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
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-3.1-flash-lite",
]

_current_idx: int = 0  # Round-robin key pointer
_quota_exhausted_map: dict[tuple[int, str], float] = {}  # (key_idx, model) -> timestamp of 429 error
QUOTA_COOLDOWN_SECONDS: float = 1800.0  # 30-minute cooldown for 429'd model/key pairs


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
        or "504" in msg
        or "deadline_exceeded" in msg
        or "deadline expired" in msg
        or "timed out" in msg
        or "timeout" in msg
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
    Call Gemini with strict Upper Model -> Lower Models cascade across keys:
    1. Upper model is prioritized. All configured keys are tried on the upper model.
    2. Only if all keys are exhausted (429) or unavailable (503) for the upper model,
       the system cascades down to the next lower model in the cascade across all keys.
    3. Within each model level, keys rotate round-robin for load-balancing.
    4. If all models across all keys are exhausted, raises RuntimeError so the
       application-level heuristic fallback system seamlessly takes over.
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
    now = time.time()

    for m_idx, current_model in enumerate(models_to_try):
        next_model = models_to_try[m_idx + 1] if m_idx + 1 < len(models_to_try) else None

        # Check if all keys are known to be quota-exhausted for this model within cooldown
        all_keys_in_cooldown = all(
            now - _quota_exhausted_map.get((k_idx, current_model), 0) < QUOTA_COOLDOWN_SECONDS
            for k_idx in range(num_keys)
        )
        if all_keys_in_cooldown:
            continue

        # Try all keys on current_model starting from _current_idx
        for offset in range(num_keys):
            idx = (_current_idx + offset) % num_keys
            key = keys[idx]
            key_label = f"Key {idx + 1}"

            # Check cooldown for this specific key and model
            last_exhausted = _quota_exhausted_map.get((idx, current_model), 0)
            if now - last_exhausted < QUOTA_COOLDOWN_SECONDS:
                continue

            client = genai.Client(api_key=key)
            try:
                kwargs = {"model": current_model, "contents": payload}
                if config is not None:
                    kwargs["config"] = config
                resp = client.models.generate_content(**kwargs)
                _current_idx = (idx + 1) % num_keys
                return resp.text
            except Exception as e:
                err = str(e)
                from utils.network import is_network_error, wait_for_network_recovery
                if is_network_error(e):
                    console.print(
                        f"[yellow]⚠ Internet connection lost during Gemini call ({key_label}). "
                        f"Waiting for Wi-Fi recovery (Checking every 30s, max 10 mins)...[/yellow]"
                    )
                    if wait_for_network_recovery(max_wait_seconds=600, check_interval_seconds=30):
                        try:
                            resp = client.models.generate_content(**kwargs)
                            _current_idx = (idx + 1) % num_keys
                            return resp.text
                        except Exception:
                            pass
                elif _is_quota_exhausted_error(e):
                    _quota_exhausted_map[(idx, current_model)] = time.time()
                    console.print(
                        f"[yellow]⚠ Gemini {key_label} limit exhausted (429/quota) for '{current_model}'. "
                        f"Trying next key...[/yellow]"
                    )
                elif _is_high_demand_error(e):
                    console.print(
                        f"[yellow]⚠ Gemini {key_label} hit high demand/timeout (503/504) for '{current_model}'. "
                        f"Cascading immediately to next model...[/yellow]"
                    )
                    break  # High demand/timeout is a model-level capacity issue on Google's end; cascade immediately!
                elif "404" in err or "not found" in err.lower():
                    console.print(
                        f"[dim]Model '{current_model}' not found on {key_label}. Skipping model...[/dim]"
                    )
                    break  # Skip this model across all keys if not found
                else:
                    console.print(
                        f"[yellow]⚠ Gemini {key_label} failed on '{current_model}': {err[:140]}. Trying next key...[/yellow]"
                    )
                    time.sleep(0.5)

        if next_model:
            console.print(
                f"[yellow]⚠ All keys exhausted/unavailable for '{current_model}'. "
                f"Cascading down to lower model '{next_model}'...[/yellow]"
            )

    raise RuntimeError(
        f"⚠ All Gemini API keys and model fallbacks ({', '.join(models_to_try)}) failed."
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

