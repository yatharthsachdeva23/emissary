# Emissary 🚀
**Autonomous LinkedIn Cold Outreach Agent for Internship Hunting**

Emissary runs every morning, discovers relevant hiring managers & startup founders via Google Search Dorks, drafts hyper-personalised 300-char LinkedIn connection notes using Gemini AI, and sends them automatically — all while keeping your LinkedIn account safe.

> **Daily flow:** Find 20 fresh leads → Craft personalised notes → Send via Playwright → Log to Google Sheet → Learn from your feedback → Repeat.

---

## 📋 Prerequisites

- Python 3.11+
- A LinkedIn account
- Windows PC (for Task Scheduler automation)

---

## ⚡ Quick Start

### Step 1 — Clone & Install

```bash
cd "c:\Desktop\Antigravity Projects\emissary"
pip install -r requirements.txt
playwright install chromium
```

### Step 2 — Configure API Keys

Copy `.env.example` to `.env`:
```bash
copy .env.example .env
```

Fill in your keys (instructions below for each):
```
GEMINI_API_KEY=...
SERPER_API_KEY=...
GOOGLE_SHEET_ID=...
```

### Step 3 — Run Onboarding (one time only)

```bash
python onboard.py
```

This conducts a 5-minute Gemini-powered interview to understand your skills, projects, and goals. Creates `data/my_profile.json`.

### Step 4 — Set Up Google Sheets (one time)

See [Google Sheets Setup](#google-sheets-setup) section below.

### Step 5 — Capture LinkedIn Session (one time)

```bash
python main.py --setup-session
```

A browser window opens. Log into LinkedIn normally. Press Enter when done. Your session cookies are saved — your **password is never stored**.

### Step 6 — Test the Pipeline

```bash
# Safe dry-run (no browser, no sends)
python main.py --dry-run

# Visit profiles but don't send
python main.py --test-mode

# Discover + draft only, no sends
python main.py --skip-send
```

### Step 7 — Schedule Daily Automation

```bash
python setup_scheduler.py
```

Creates a Windows Task that runs `main.py` every day at 10:00 AM. That's it — fully autonomous from here.

---

## 🔑 Getting Your API Keys

### Gemini API Key (Free)
1. Go to [https://aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)
2. Sign in with Google
3. Click **Create API key**
4. Copy into `.env` as `GEMINI_API_KEY`

Free tier: 15 requests/minute, 1M tokens/day — plenty for 20 contacts/day.

### Serper.dev API Key (Free — 2,500 searches)
1. Go to [https://serper.dev](https://serper.dev)
2. Sign up (no credit card required)
3. Copy your API key into `.env` as `SERPER_API_KEY`

2,500 free searches ≈ 8 months at current usage rate.

---

## 📊 Google Sheets Setup

This is a one-time 10-minute setup.

### 1. Create a Google Cloud Project
1. Go to [https://console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (name it "Emissary")
3. Enable **Google Sheets API** and **Google Drive API**
   - Search "Sheets API" → Enable
   - Search "Drive API" → Enable

### 2. Create a Service Account
1. Go to **APIs & Services → Credentials**
2. Click **Create Credentials → Service Account**
3. Name it `emissary-bot`, click Create
4. Skip the optional fields, click Done
5. Click on the service account → **Keys** tab → **Add Key → JSON**
6. Download the JSON file → rename it to `credentials.json`
7. Place `credentials.json` in the project root: `emissary/credentials.json`

### 3. Create & Share the Google Sheet
1. Create a blank Google Sheet at [https://sheets.google.com](https://sheets.google.com)
2. Name it **"Emissary CRM"**
3. Copy the Sheet ID from the URL:
   ```
   https://docs.google.com/spreadsheets/d/[THIS_IS_YOUR_ID]/edit
   ```
4. Paste into `.env` as `GOOGLE_SHEET_ID`
5. Share the sheet with your service account email (found in `credentials.json` as `client_email`):
   - Click Share → paste the email → Editor access

Emissary will auto-create a tab called "Emissary CRM" on first run.

---

## 📅 Daily Workflow

Once set up, every morning at 10:00 AM:

```
1. Feedback Loop      → Reads your sheet feedback → Updates message rules
2. Discovery          → Runs Google Dorks → Scores 20 best leads with Gemini
3. Ghostwriter        → Drafts personalised 300-char notes for each lead
4. Messenger          → Sends 5 at a time → Waits 15-25 min → Repeat
5. CRM Logger         → Logs all sent leads to Google Sheet
6. Notification       → Desktop popup: "Done: 20 sent"
```

**Your only job:** Open the Google Sheet occasionally, write feedback in the "Your Feedback" column. The system learns from it.

---

## 💬 The Feedback Loop

The Google Sheet has a **"Your Feedback"** column. After reviewing sent messages:

| Your Feedback | What happens |
|---|---|
| `Too formal, sounds like a template` | Gemini adds a DON'T rule for formal language |
| `Too long, trim it` | Gemini adjusts the structure rule |
| `Good one!` | Positive signal, current rules are reinforced |
| `Wrong domain, this person does hardware` | Gemini improves the scoring filter |

Next morning's messages will reflect your feedback automatically.

---

## 🛡️ LinkedIn Safety Protocol

Your account safety is the #1 priority. Here's what protects you:

| Guardrail | Detail |
|---|---|
| **No password stored** | Only session cookies saved in `linkedin_session.json` |
| **Visible browser** | Runs in a real Chrome window (non-headless) |
| **Hard daily cap** | 20 max/day, 10 on weekends |
| **Batch delays** | 5 sent → 15-25 min random wait → next 5 |
| **Human typing** | Each character typed with 50-150ms random delay |
| **Profile visit first** | Scrolls the profile for 5-10 seconds before connecting |
| **CAPTCHA detection** | Immediately stops and sends desktop alert |
| **Random mouse movement** | Not robotic straight-line clicks |
| **Session refresh reminder** | Notifies you after ~30 days to re-login |

> **Important:** LinkedIn sessions expire every 30 days. Run `python main.py --setup-session` monthly to refresh.

---

## 🖥️ CLI Reference

```bash
python onboard.py                   # Run persona interview (first time or update)
python onboard.py --update          # Force re-run interview

python main.py                      # Full daily pipeline
python main.py --dry-run            # Simulate without browser or sends
python main.py --test-mode          # Open browser, visit profiles, DON'T send
python main.py --skip-send          # Discover + draft only, skip Playwright
python main.py --skip-feedback      # Skip feedback loop step
python main.py --setup-session      # Capture LinkedIn cookies (run once)

python setup_scheduler.py           # Create daily Task Scheduler task
python setup_scheduler.py --status  # Check scheduler task status
python setup_scheduler.py --remove  # Remove the scheduled task
```

---

## 📁 Project Structure

```
emissary/
├── main.py                  # 🎯 Daily pipeline orchestrator
├── onboard.py               # 🧠 One-time persona interview
├── setup_scheduler.py       # 📅 Windows Task Scheduler setup
├── requirements.txt
├── .env.example             # Template — copy to .env
├── .env                     # Your secrets (gitignored)
├── credentials.json         # Google service account (gitignored)
│
├── agents/
│   ├── persona_agent.py     # Resume analysis + interview
│   ├── discovery_agent.py   # Serper search + Gemini scoring
│   ├── ghostwriter_agent.py # 300-char note drafting
│   ├── messenger_agent.py   # Playwright LinkedIn sender
│   └── feedback_agent.py    # Feedback → prompt updates
│
├── utils/
│   ├── sheets.py            # Google Sheets CRM
│   ├── safety.py            # Rate limits + LinkedIn safety
│   └── notifier.py          # Desktop notifications
│
├── data/
│   ├── my_profile.json      # Your persona (generated by onboard.py)
│   ├── prompt_instructions.json  # Living message rules (updated by feedback)
│   ├── leads_today.json     # Today's lead queue
│   └── seen_profiles.json   # Dedup store (gitignored)
│
└── logs/
    └── runs.jsonl           # Append-only run history
```

---

## 💰 Cost Breakdown

| Component | Tool | Cost |
|---|---|---|
| AI Brain | Gemini Flash 2.0 | **Free** (15 RPM / 1M TPD) |
| Web Search | Serper.dev | **Free** (2,500 searches ≈ 8 months) |
| Automation | Playwright | **Free** (open source) |
| CRM | Google Sheets | **Free** |
| Scheduler | Windows Task Scheduler | **Free** (built-in) |
| Notifications | plyer | **Free** (open source) |

**Total: ₹0/month** for ~8 months.

---

## ❓ FAQ

**Q: Will LinkedIn ban my account?**
Every safety guardrail is in place — daily caps, human-like delays, visible browser, cookie sessions. The risk is non-zero (it's automation) but minimised. If LinkedIn ever shows a CAPTCHA, the system stops immediately and notifies you.

**Q: The session expired. What do I do?**
Run `python main.py --setup-session` to log in again. Takes 2 minutes.

**Q: Serper ran out of searches. Now what?**
Create a new Serper account with a different email. You get another 2,500 free searches.

**Q: Can I run this on a different PC?**
Copy the entire project folder. Run `pip install -r requirements.txt` and `playwright install chromium`. Then run `python main.py --setup-session` on the new machine.

**Q: I want to update my profile after 3 months.**
Run `python onboard.py --update`. It re-runs the interview and overwrites `my_profile.json`.
