"""
setup_scheduler.py — Emissary
Creates a Windows Task Scheduler task to run main.py every weekday at 10:00 AM.

Usage:
    python setup_scheduler.py           # Create/update the task
    python setup_scheduler.py --remove  # Remove the task
    python setup_scheduler.py --status  # Show task status
"""

import os
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

console = Console()

TASK_NAME = "EmissaryDailyPipeline"
PROJECT_ROOT = Path(__file__).parent.resolve()
PYTHON_EXE = sys.executable  # Current Python interpreter
MAIN_SCRIPT = PROJECT_ROOT / "main.py"
LOG_FILE = PROJECT_ROOT / "logs" / "scheduler.log"


def get_task_xml() -> str:
    """Generate Task Scheduler XML definition."""
    python_path = str(PYTHON_EXE).replace("\\", "\\\\")
    script_path = str(MAIN_SCRIPT).replace("\\", "\\\\")
    working_dir = str(PROJECT_ROOT).replace("\\", "\\\\")
    log_path = str(LOG_FILE).replace("\\", "\\\\")

    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Emissary — Autonomous LinkedIn cold outreach pipeline. Runs daily at 9:00 AM.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T09:00:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <WeeksInterval>1</WeeksInterval>
        <DaysOfWeek>
          <Monday />
          <Tuesday />
          <Wednesday />
          <Thursday />
          <Friday />
          <Saturday />
          <Sunday />
        </DaysOfWeek>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>false</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT4H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <WakeToRun>false</WakeToRun>
    <Priority>7</Priority>
  </Settings>
  <Actions>
    <Exec>
      <Command>{python_path}</Command>
      <Arguments>"{script_path}" >> "{log_path}" 2>&amp;1</Arguments>
      <WorkingDirectory>{working_dir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""


def create_task() -> bool:
    """Register the Task Scheduler task."""
    # Ensure logs dir exists
    (PROJECT_ROOT / "logs").mkdir(exist_ok=True)

    xml_path = PROJECT_ROOT / "logs" / "task_def.xml"
    xml_content = get_task_xml()

    # Write XML with UTF-16 encoding (required by schtasks)
    with open(xml_path, "w", encoding="utf-16") as f:
        f.write(xml_content)

    cmd = [
        "schtasks", "/Create",
        "/TN", TASK_NAME,
        "/XML", str(xml_path),
        "/F",  # Force overwrite if exists
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            console.print(Panel(
                f"[green]✅ Task created successfully![/green]\n\n"
                f"  Task name  : [bold]{TASK_NAME}[/bold]\n"
                f"  Runs at    : [bold]9:00 AM — Every day[/bold]\n"
                f"  Script     : {MAIN_SCRIPT}\n"
                f"  Python     : {PYTHON_EXE}\n"
                f"  Log file   : {LOG_FILE}\n\n"
                f"[dim]Manage with: Task Scheduler → {TASK_NAME}[/dim]",
                title="📅 Task Scheduler",
                border_style="green",
            ))
            return True
        else:
            console.print(f"[red]Task creation failed:\n{result.stderr}[/red]")
            return False
    except FileNotFoundError:
        console.print("[red]schtasks not found. Make sure you're on Windows.[/red]")
        return False


def remove_task() -> bool:
    """Delete the Task Scheduler task."""
    cmd = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            console.print(f"[green]✓ Task '{TASK_NAME}' removed.[/green]")
            return True
        else:
            console.print(f"[red]Remove failed: {result.stderr}[/red]")
            return False
    except FileNotFoundError:
        console.print("[red]schtasks not found.[/red]")
        return False


def show_status() -> None:
    """Show the current task status."""
    cmd = ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            console.print(result.stdout)
        else:
            console.print(f"[yellow]Task '{TASK_NAME}' not found or error:\n{result.stderr}[/yellow]")
    except FileNotFoundError:
        console.print("[red]schtasks not found.[/red]")


def main():
    args = sys.argv[1:]

    if "--remove" in args:
        remove_task()
    elif "--status" in args:
        show_status()
    else:
        console.print(Panel(
            f"[bold cyan]Emissary — Task Scheduler Setup[/bold cyan]\n\n"
            f"This will create a daily Windows Task Scheduler task that runs:\n"
            f"  [bold]{PYTHON_EXE}[/bold]\n"
            f"  [dim]{MAIN_SCRIPT}[/dim]\n\n"
            f"Every day at [bold]9:00 AM[/bold] (when your PC is on and connected to internet).",
            border_style="cyan",
        ))

        from rich.prompt import Confirm
        if Confirm.ask("Proceed?", default=True):
            create_task()
        else:
            console.print("[yellow]Cancelled.[/yellow]")


if __name__ == "__main__":
    main()
