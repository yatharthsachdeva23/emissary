"""
Persona Agent — Emissary
Analyses resume + conducts an interactive interview to build my_profile.json
"""

import json
import os
import re
from pathlib import Path
from typing import Optional

from google import genai
from utils.gemini_client import get_client_with_rotation
from google.genai import types
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

load_dotenv()
console = Console()

DATA_DIR = Path(__file__).parent.parent / "data"
PROFILE_PATH = DATA_DIR / "my_profile.json"

SYSTEM_PROMPT = """You are an expert career coach and talent strategist helping a student craft the perfect 
internship outreach strategy. Your job is to deeply understand who they are — not just what's on their resume, 
but what makes them interesting, what they've built, what they care about, and what kind of work excites them.

You will conduct a structured but conversational interview. Ask one question at a time. Be warm, curious, 
and encouraging. After gathering enough information, synthesise everything into a structured JSON profile.

The JSON profile must follow this exact schema:
{
  "name": "string",
  "college": "string",
  "year": "string (e.g. 2nd Year, 3rd Year)",
  "branch": "string",
  "skills": ["list of technical skills"],
  "projects": [
    {
      "name": "project name",
      "description": "2-3 sentence description",
      "tech_stack": ["technologies used"],
      "what_makes_it_interesting": "the unique angle or insight"
    }
  ],
  "interests": ["list of genuine interests — technical and non-technical"],
  "target_roles": ["e.g. SDE Intern, ML Intern, Backend Intern"],
  "target_industries": ["e.g. AI/ML, Fintech, DevTools, Security"],
  "target_company_stages": ["e.g. Seed, Series A, Series B — avoid FAANG"],
  "geography": ["e.g. Bangalore, Remote, Pan-India"],
  "internship_duration": "e.g. 2 months, 6 months, flexible",
  "what_makes_me_different": "1-2 sentences — the genuine differentiator",
  "hook": "The ONE sentence that captures who you are and why someone should talk to you",
  "resume_text": "full resume text as provided"
}

When you are ready to output the final profile (after all questions are answered), output ONLY a valid JSON 
block wrapped in ```json ... ``` tags. Nothing else."""


class PersonaAgent:
    def __init__(self):
        pass  # Setup happens per-call in extract_profile_info
        self.history = []
        self.resume_text = ""
        self.resume_link = ""

    def _chat(self, user_message: str) -> str:
        """Send a message in a multi-turn conversation."""
        self.history.append(
            types.Content(role="user", parts=[types.Part(text=user_message)])
        )
        
        import time
        max_retries = 5
        base_delay = 5
        for attempt in range(max_retries):
            try:
                client, key_label = get_client_with_rotation()
                response = client.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=self.history,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        temperature=0.8,
                    ),
                )
                reply = response.text
                self.history.append(
                    types.Content(role="model", parts=[types.Part(text=reply)])
                )
                return reply
            except Exception as e:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)  # 5s, 10s, 20s, 40s
                    console.print(f"[yellow]API rate limit hit. Waiting {delay}s before retry {attempt + 1}/{max_retries}...[/yellow]")
                    time.sleep(delay)
                else:
                    raise e

    def _extract_json(self, text: str) -> Optional[dict]:
        """Extract JSON block from Gemini response."""
        match = re.search(r"```json\s*([\s\S]+?)\s*```", text)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return None
        return None

    def load_resume_from_pdf(self, pdf_path: str) -> str:
        """Extract text from a PDF resume."""
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(pdf_path)
            text = ""
            for page in doc:
                text += page.get_text()
            doc.close()
            return text.strip()
        except Exception as e:
            console.print(f"[red]Could not read PDF: {e}[/red]")
            return ""

    def run_interview(self) -> dict:
        """Conduct the full onboarding interview."""
        console.print(Panel(
            "[bold cyan]Welcome to Emissary — Your Autonomous Cold Outreach Agent[/bold cyan]\n\n"
            "I need to understand who you are to craft messages that actually get responses.\n"
            "This one-time interview takes about 5 minutes. Let's make it count.\n\n"
            "[dim]Type your answers naturally. When the AI outputs a JSON block, you're done.[/dim]",
            title="🚀 Emissary Onboarding",
            border_style="cyan"
        ))

        # Step 1: Get resume
        console.print("\n[bold yellow]Step 1: Your Resume[/bold yellow]")
        console.print("Options: [1] Paste resume text  [2] Provide PDF path\n")
        choice = Prompt.ask("Choose", choices=["1", "2"], default="1")

        if choice == "2":
            pdf_path = Prompt.ask("Enter the full path to your resume PDF")
            self.resume_text = self.load_resume_from_pdf(pdf_path)
            if self.resume_text:
                console.print(f"[green]✓ Extracted {len(self.resume_text)} characters from PDF[/green]")
            else:
                console.print("[yellow]PDF extraction failed. Please paste your resume text instead.[/yellow]")
                self.resume_text = self._get_multiline_input("Paste your resume text (press Enter twice when done)")
        else:
            self.resume_text = self._get_multiline_input(
                "Paste your resume text below (press Enter twice when done)"
            )

        if not self.resume_text:
            console.print("[red]Resume is required. Please re-run onboard.py[/red]")
            return {}

        console.print("\n[bold yellow]Step 1b: Resume Link[/bold yellow]")
        self.resume_link = Prompt.ask(
            "Enter a Google Drive link to your resume (optional, will be appended to connection notes)",
            default=""
        )

        # Kick off interview
        opening = self._chat(
            f"Here is my resume:\n\n{self.resume_text}\n\n"
            "Please analyse it and then ask me your first question to understand me better. "
            "Start with asking about my projects in depth — I want you to understand what I've actually built."
        )
        console.print(f"\n[bold green]Emissary:[/bold green] {opening}\n")

        # Interview loop
        profile_json = None
        while profile_json is None:
            user_input = Prompt.ask("[bold blue]You[/bold blue]")
            if not user_input.strip():
                continue

            response = self._chat(user_input)
            profile_json = self._extract_json(response)

            if profile_json is None:
                console.print(f"\n[bold green]Emissary:[/bold green] {response}\n")
            else:
                console.print("\n[bold green]✓ Profile complete! Saving...[/bold green]")

        profile_json["resume_text"] = self.resume_text
        profile_json["resume_link"] = self.resume_link
        return profile_json

    def _get_multiline_input(self, prompt: str) -> str:
        """Get multi-line input, ending with a blank line."""
        console.print(f"\n[yellow]{prompt}:[/yellow]")
        lines = []
        while True:
            try:
                line = input()
                if line == "" and lines and lines[-1] == "":
                    break
                lines.append(line)
            except EOFError:
                break
        return "\n".join(lines).strip()

    def save_profile(self, profile: dict) -> None:
        """Save the profile to my_profile.json."""
        DATA_DIR.mkdir(exist_ok=True)
        with open(PROFILE_PATH, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2, ensure_ascii=False)
        console.print(Panel(
            f"[green]Profile saved to: {PROFILE_PATH}[/green]\n\n"
            f"[bold]Name:[/bold] {profile.get('name', 'N/A')}\n"
            f"[bold]College:[/bold] {profile.get('college', 'N/A')} — {profile.get('year', '')}\n"
            f"[bold]Target Roles:[/bold] {', '.join(profile.get('target_roles', []))}\n"
            f"[bold]Hook:[/bold] {profile.get('hook', 'N/A')}",
            title="✅ Profile Saved",
            border_style="green"
        ))

    @staticmethod
    def load_profile() -> dict:
        """Load the existing profile."""
        if not PROFILE_PATH.exists():
            raise FileNotFoundError(
                "Profile not found. Please run: py -3.12 onboard.py"
            )
        with open(PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
