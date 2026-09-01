# -*- coding: utf-8 -*-
"""
utils/network.py — Network Health & Auto-Reconnection Guard
Monitors internet connectivity. If internet drops mid-execution,
pauses execution and checks every 30 seconds for up to 10 minutes,
then resumes automatically once internet is restored.
"""

from __future__ import annotations
import os
import sys
import time
import socket
import urllib.request
from pathlib import Path
from rich.console import Console
from rich.panel import Panel

console = Console()


def is_internet_available(timeout: float = 4.0) -> bool:
    """Check if internet is reachable by querying reliable DNS servers and HTTPS endpoints."""
    # Test 1: Quick socket connection to Google DNS (8.8.8.8) or Cloudflare DNS (1.1.1.1)
    for host, port in [("8.8.8.8", 53), ("1.1.1.1", 53)]:
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            return True
        except (socket.timeout, OSError):
            pass

    # Test 2: HTTP request fallback
    for url in ["https://www.google.com", "https://www.cloudflare.com"]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout):
                return True
        except Exception:
            pass

    return False


def is_network_error(exc: Exception) -> bool:
    """Check if an exception was caused by lost internet / DNS failure."""
    if exc is None:
        return False
    err_str = str(exc).lower()
    network_keywords = [
        "temporary failure in name resolution",
        "nameresolutionerror",
        "failed to resolve",
        "getaddrinfo failed",
        "nodename nor servname provided",
        "connection reset",
        "connection aborted",
        "connection refused",
        "remotedisconnected",
        "network is unreachable",
        "no route to host",
        "socket.gaierror",
        "max retries exceeded with url",
        "net::err_internet_disconnected",
        "net::err_name_not_resolved",
        "net::err_network_changed",
        "net::err_connection_timed_out",
        "errno -3",
        "errno 101",
        "errno 110",
        "errno 111",
        "errno 113",
        "timed out",
        "timeout",
    ]
    return any(k in err_str for k in network_keywords)


def wait_for_network_recovery(max_wait_seconds: int = 600, check_interval_seconds: int = 30) -> bool:
    """
    Wait for internet connection to be restored.
    Checks every 30 seconds for up to 10 minutes.
    Returns True if reconnected, False if timed out.
    """
    if is_internet_available():
        return True

    console.print(Panel(
        "[bold red]⚠ INTERNET CONNECTION LOST[/bold red]\n\n"
        "The system detected that Wi-Fi / Internet has disconnected mid-run.\n"
        "[bold yellow]Pausing and waiting for reconnection...[/bold yellow]\n"
        f"  • Checking every [bold]{check_interval_seconds} seconds[/bold]\n"
        f"  • Maximum wait time: [bold]{max_wait_seconds // 60} minutes[/bold]\n\n"
        "[dim]The pipeline will automatically resume as soon as Wi-Fi reconnects.[/dim]",
        title="Network Connection Guard",
        border_style="yellow",
    ))

    start_time = time.time()
    attempt = 1

    while time.time() - start_time < max_wait_seconds:
        elapsed = int(time.time() - start_time)
        remaining = max_wait_seconds - elapsed
        console.print(f"[dim]⏳ [Attempt {attempt}] Checking Wi-Fi / Internet connection in {check_interval_seconds}s... (Time remaining: {remaining}s)[/dim]")
        time.sleep(check_interval_seconds)

        if is_internet_available():
            console.print(Panel(
                "[bold green]✓ INTERNET RESTORED![/bold green]\n\n"
                f"Wi-Fi connection recovered after {elapsed + check_interval_seconds} seconds.\n"
                "[bold cyan]Resuming pipeline execution right where it left off...[/bold cyan]",
                title="Connection Restored",
                border_style="green",
            ))
            return True

        attempt += 1

    console.print(Panel(
        f"[bold red]❌ TIMEOUT: Internet connection could not be restored within {max_wait_seconds // 60} minutes.[/bold red]\n\n"
        "The system has safely preserved all progress. You can resume anytime by running python main.py.",
        title="Network Timeout",
        border_style="red",
    ))
    return False
