"""
Notifier — Emissary
Desktop push notifications for pipeline events.
"""

from rich.console import Console

console = Console()


def notify(title: str, message: str, app_name: str = "Emissary") -> None:
    """Send a guaranteed desktop popup notification."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        if "ABORTED" in title or "Error" in title:
            messagebox.showwarning(f"{app_name} - {title}", message)
        else:
            messagebox.showinfo(f"{app_name} - {title}", message)
        root.destroy()
    except Exception as e:
        console.print(f"[bold magenta]NOTIFY — {title}:[/bold magenta] {message} ({e})")


def notify_done(sent: int, skipped: int) -> None:
    notify(
        "Emissary Done",
        f"Sent {sent} connections. Skipped {skipped}. Check your Google Sheet."
    )


def notify_abort(reason: str) -> None:
    notify(
        "Emissary ABORTED",
        f"LinkedIn safety trigger: {reason}. Check your account immediately."
    )


def notify_error(error: str) -> None:
    notify(
        "Emissary Error",
        f"Pipeline error: {error[:100]}"
    )


def notify_session_expired() -> None:
    notify(
        "Session Expired",
        "LinkedIn session needs refresh. Run: py -3.12 main.py --setup-session"
    )
