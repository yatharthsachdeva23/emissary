"""
Ghostwriter Agent — Emissary
Bulk-drafts both:
  1. A 280-char connection hook (stored for CRM reference)
  2. A Meta-Flex DM (sent when they accept the connection)
Uses a single Gemini API call to stay within free-tier daily limits.
"""

import json
import os
import re
import time
import math
from pathlib import Path
from typing import Optional

from google import genai
from dotenv import load_dotenv
from utils.gemini_client import get_client_with_rotation, mark_key_exhausted
from utils.text_cleaner import clean_first_name, clean_company_name
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

load_dotenv()
console = Console()

DATA_DIR = Path(__file__).parent.parent / "data"
LEADS_PATH = DATA_DIR / "leads_today.json"
INSTRUCTIONS_PATH = DATA_DIR / "prompt_instructions.json"
MAX_NOTE_LENGTH = 280

# ─── Multi-Track Pitch Configurations ─────────────────────────────────────────
TRACK_CONFIGS = {
    "proptech_sales_tech": {
        "title": "Dubai & Indian PropTech (Sales + Tech Execution)",
        "role_pitch": "Sales & Tech Execution (Commercial Acceleration)",
        "background_summary": (
            "- Candidate: Yatharth Sachdeva, 4th-year student at Delhi Technological University (DTU, 9.3 CGPA).\n"
            "- Target Focus: Driving deal velocity, pipeline growth, and user conversion over the next two months.\n"
            "- Past Experience: Intern at NoBrokerHood (India's leading PropTech unicorn).\n"
            "- Key Value Proposition: Rare hybrid capability combining property sales acumen with technical automation.\n"
            "- Expanded NoBrokerHood Outcomes (MUST HIGHLIGHT):\n"
            "  1. Shipped automated B2B sales outreach workflows capturing 25+ extra qualified leads/month with zero manual touch.\n"
            "  2. Optimized property search and discovery algorithms, delivering 1.5x output coverage within identical credit/cost constraints.\n"
            "  3. Built intelligent prospect research pipelines to gather real estate market data and accelerate high-value deal closures.\n"
            "- Unfair Advantage: Understands real estate deal mechanics and user conversion, with the technical ability to automate lead routing, build custom qualification workflows, and plug pipeline leaks directly without dev bandwidth."
        ),
        "focus_instruction": (
            "CRITICAL RULES FOR DUBAI & INDIAN PROPTECH:\n"
            "- DO NOT state any specific job title or role name (NEVER use words like 'Product Manager', 'PM', 'Founder's Office', or 'Chief of Staff').\n"
            "- Pitch strictly through the angle of a high-leverage SALES + TECH COMBO: you understand property sales, deal conversion, and buyer velocity, AND you have the technical knowledge to automate lead routing, optimize discovery, and build internal tools directly.\n"
            "- EXPAND ON NOBROKERHOOD: Emphasize NoBrokerHood (PropTech unicorn) prominently — automated sales outreach (25+ extra qualified leads/month) and search discovery revamp (1.5x output coverage).\n"
            "- STRICTLY NO CORPORATE FLUFF: Avoid robotic phrases like 'Once this execution is locked in, the upside is immediate', 'critical operational friction', 'seamless synergy'. Use clear, direct, professional human words.\n"
            "- PROPTECH SYSTEM DISCLOSURE: Since this is PropTech, you CAN state at the end of Paragraph 3 that this outreach was researched and delivered by an autonomous system you built to test pipeline execution live."
        ),
        "p1_instruction": (
            "- Choose naturally among these real estate tech opening styles:\n"
            "  Style A (Speed-to-Lead & Qualification): \"Looking at how {clean_company} is scaling property transactions, in fast-moving property markets deals are won or lost on response time and lead qualification—making sure high-intent buyers and tenants are engaged before inquiries go cold on WhatsApp and portals.\"\n"
            "  Style B (Search & Listing Discovery): \"While following {clean_company}'s platform, an immediate lever that stands out is streamlining property search discovery and buyer matching so portal traffic converts directly into scheduled viewings.\"\n"
            "  Style C (Transaction Velocity): \"In scaling {clean_company}'s marketplace, a major revenue driver is tightening the conversion cadence from initial property search to confirmed broker engagement.\""
        ),
        "p2_instruction": (
            "- Connect this directly to real estate commercial gains using professional human language:\n"
            "  - Framing example: \"Closing this gap directly increases booked viewings, shortens the sales cycle, and ensures no high-intent inquiries slip through portal or WhatsApp channels.\""
        ),
        "p3_instruction": (
            "- Present the Sales + Tech combo with expanded NoBrokerHood experience:\n"
            "  - Framing example: \"I bring a rare sales and tech combo: I understand property sales and deal conversion, and I have the technical ability to automate lead routing, build custom qualification workflows, and optimize discovery directly. At NoBrokerHood (India's leading PropTech unicorn), I worked directly on transaction velocity—shipping automated B2B sales outreach capturing 25+ extra qualified leads/month and overhauling search discovery logic for 1.5x output coverage. (I'm a 4th-year student at DTU, 9.3 CGPA, and in fact, this message was researched and delivered by an autonomous pipeline I built to test execution live).\""
        ),
        "ai_disclosure_allowed": True,
        "hackathon_allowed": False,
    },
    "property_sales": {
        "title": "Dubai & Indian Real Estate & Property (Sales + Tech Execution)",
        "role_pitch": "Sales & Client Acquisition (Commercial Execution)",
        "background_summary": (
            "- Candidate: Yatharth Sachdeva, 4th-year student at Delhi Technological University (DTU, 9.3 CGPA).\n"
            "- Target Focus: Driving buyer/investor acquisition, speed-to-lead, and sales pipeline conversion over the next two months.\n"
            "- Past Experience: Intern at NoBrokerHood (India's premier PropTech unicorn).\n"
            "- Key Value Proposition: High-velocity client acquisition, pipeline conversion, rapid lead follow-up, and international/NRI investor prospecting.\n"
            "- Expanded NoBrokerHood Outcomes (MUST HIGHLIGHT):\n"
            "  1. Shipped zero-touch B2B sales outreach capturing 25+ extra qualified enterprise leads per month.\n"
            "  2. Built intelligence cadences to research property prospects and accelerate high-value deal closures.\n"
            "  3. Sales + Tech Advantage: Combines sales execution hunger with the ability to structure disciplined lead qualification cadences and automate WhatsApp/CRM follow-ups so zero inquiries leak."
        ),
        "focus_instruction": (
            "CRITICAL RULES FOR DUBAI & INDIAN REAL ESTATE / BROKERAGES / DEVELOPERS:\n"
            "- DO NOT state any specific job title or role name (NEVER use words like 'Product Manager', 'PM', 'Founder's Office', or 'Chief of Staff'). Real estate executives care about money, deals, and agents.\n"
            "- Pitch strictly through the angle of a SALES + TECH COMBO: high-tempo client acquisition, speed-to-lead, WhatsApp and portal follow-ups, with the technical ability to automate qualification workflows so brokers only talk to serious buyers.\n"
            "- EXPAND ON NOBROKERHOOD: Emphasize NoBrokerHood (PropTech unicorn) prominently — B2B sales outreach capturing 25+ extra leads/month and accelerating deal closures.\n"
            "- ABSOLUTELY FORBIDDEN: NEVER state or imply that this message was sent by AI, an autonomous system, or a bot! Keep it 100% human and authentic.\n"
            "- STRICTLY NO CORPORATE FLUFF: Avoid robotic clichés like 'Once this execution is locked in, the upside is immediate', 'critical operational friction'. Use practical, deal-driven language."
        ),
        "p1_instruction": (
            "- Choose naturally among these property sales opening styles:\n"
            "  Style A (Buyer/Investor Acquisition): \"Looking at {clean_company}'s portfolio across the property market, in high-end real estate deals are won on speed-to-lead—specifically qualifying inbound portal and WhatsApp inquiries before buyers and investors engage another brokerage.\"\n"
            "  Style B (Speed-to-Lead & Follow-up): \"While following {clean_company}'s market expansion, an immediate opportunity in property sales is responding to inbound portal inquiries fast enough on WhatsApp to maximize viewing conversion rates.\"\n"
            "  Style C (Outbound Investor Pipeline): \"In expanding {clean_company}'s buyer base, a major growth driver is maintaining a consistent outbound pipeline of qualified property buyers and HNI/NRI investors.\""
        ),
        "p2_instruction": (
            "- Connect resolving this to measurable real estate sales gains:\n"
            "  - Framing example: \"Tightening this execution directly drives transaction volume: higher viewing-to-close ratios, faster response times, and a predictable monthly pipeline of qualified buyers and investors.\""
        ),
        "p3_instruction": (
            "- Present how Yatharth drives this from a Sales + Tech standpoint with expanded NoBrokerHood proof:\n"
            "  - Framing example: \"I combine a strong grip on property sales with the technical ability to structure disciplined lead qualification cadences and automate follow-ups so zero inquiries slip through. During my internship at NoBrokerHood (India's premier PropTech unicorn), I drove B2B sales outreach workflows capturing 25+ extra qualified leads per month and built prospect research pipelines to accelerate high-value deal closures. (I'm a 4th-year student at DTU, 9.3 CGPA).\""
        ),
        "ai_disclosure_allowed": False,
        "hackathon_allowed": False,
    },
    "tech_pm": {
        "title": "Tech & Software Startups (Product Management & APM)",
        "role_pitch": "Associate Product Manager (APM) / AI Product Manager (Intern)",
        "background_summary": (
            "- Candidate: Yatharth Sachdeva, 4th-year student at Delhi Technological University (DTU, 9.3 CGPA, Information Technology).\n"
            "- Past Experience: AI Product Management Intern at NoBrokerHood.\n"
            "- Key Value Proposition: Built 15+ end-to-end projects from scratch with obsessive focus on exceptional user experience (UX) and solving practical, real-world problems. Experienced across user activation, onboarding flow optimization, and product-led growth (PLG) loops.\n"
            "- Proven PM Outcomes:\n"
            "  1. Shipped 15+ complete end-to-end software and AI projects from zero to one, never building anything that does not solve a tangible real-life problem, always ensuring exceptional UX.\n"
            "  2. Revamped search & discovery user experience and product logic at NoBrokerHood to deliver 1.5x output coverage within identical constraints.\n"
            "  3. Shipped automated B2B product features and conversion flows capturing 25+ extra qualified leads per month.\n"
            "  4. Secured 4th rank in Agentic AI Hackathon by NMG Labs & 1st place in IIT Delhi ONDC DebugXBecon'25 Hackathon."
        ),
        "focus_instruction": (
            "Frame the entire message strictly from a PRODUCT MANAGEMENT (PM) perspective.\n"
            "Focus purely on user experience (UX), customer onboarding flows, product friction, activation bottlenecks, and conversion metrics.\n"
            "Emphasize Yatharth's track record of 15+ end-to-end projects built with exceptional UX to solve real problems.\n"
            "STRICT RULES:\n"
            "- ABSOLUTELY FORBIDDEN: DO NOT mention any college society fest, sponsorships, or college deals!\n"
            "- NO deep infrastructure jargon (no database indexing, compute costs, microservices, latency benchmarks)."
        ),
        "p1_instruction": (
            "- Choose naturally among these high-agency PM opening styles tailored for startups (vary dynamically across leads):\n"
            "  Style A (Onboarding & Activation): \"Looking at how {clean_company} is scaling its core workflows, a critical product bottleneck is user onboarding friction and activation drop-offs before users reach the core 'aha' moment.\"\n"
            "  Style B (Feature Adoption & Workflow UX): \"While tracking {clean_company}'s product evolution, an immediate challenge that often stands out is reducing workflow friction so new users quickly become sticky, daily active users.\"\n"
            "  Style C (Conversion Funnel): \"In building out {clean_company}'s product experience, a key hurdle teams usually run into is conversion leakage in the core signup-to-activation funnel.\""
        ),
        "p2_instruction": (
            "- Connect resolving this to measurable product metrics:\n"
            "  - Framing example: \"Once this friction is resolved, the upside is immediate: faster time-to-first-value, higher onboarding completion rates, and turning casual signups into sticky active users without relying on manual handoffs.\""
        ),
        "p3_instruction": (
            "- Present how Yatharth solves this from a PM perspective:\n"
            "  - Framing example: \"I can help tackle this from a product standpoint by [specific PM approach: e.g. designing frictionless user activation triggers and streamlining the core onboarding journey]. I've built 15+ end-to-end projects from scratch with an obsessive focus on exceptional UX and solving practical real-life problems. At NoBrokerHood as an AI PM Intern, I worked cross-functionally across engineering, design, and growth to ship automated B2B features capturing 25+ extra qualified leads/month and revamped search discovery logic for 1.5x output coverage. (I'm a 4th-year IT student at DTU, 9.3 CGPA, 4th rank in NMG Labs Agentic AI Hackathon, and in fact, this entire outreach system was researched and delivered autonomously by a product engine I built).\""
        ),
        "ai_disclosure_allowed": True,
        "hackathon_allowed": True,
    },
    "non_tech_pm": {
        "title": "Non-Tech Startups (Product Management & Operations Optimization)",
        "role_pitch": "Associate Product Manager (APM) / Product Operations Intern",
        "background_summary": (
            "- Candidate: Yatharth Sachdeva, 4th-year student at Delhi Technological University (DTU, 9.3 CGPA).\n"
            "- Past Experience: Product Intern at NoBrokerHood.\n"
            "- Key Value Proposition: Built 15+ end-to-end projects with exceptional user experience (UX) solving practical real-world problems. Expertise in digital product execution, user checkout/order funnel optimization, catalog discovery, and conversion rate optimization (CRO).\n"
            "- Proven Product Outcomes:\n"
            "  1. Built 15+ complete end-to-end projects from scratch, never building anything that doesn't solve a tangible real-life problem, always ensuring exceptional UX.\n"
            "  2. Revamped search & discovery user experience and product catalog flows at NoBrokerHood delivering 1.5x output coverage within identical constraints.\n"
            "  3. Shipped automated B2B customer conversion features and workflows capturing 25+ extra qualified leads per month.\n"
            "  4. Worked cross-functionally across operations, business, and engineering to eliminate customer workflow friction."
        ),
        "focus_instruction": (
            "Frame the entire message strictly around PRODUCT MANAGEMENT (PM) and DIGITAL USER EXPERIENCE for a non-tech / consumer / operations-focused startup.\n"
            "Focus on: customer checkout & ordering journey, catalog/product discovery, repeat purchase/retention loops, user onboarding, or operations workflows.\n"
            "Highlight Yatharth's craft of 15+ end-to-end projects built with exceptional UX that solve practical problems.\n"
            "STRICT RULES:\n"
            "- ABSOLUTELY FORBIDDEN: DO NOT mention any college society fest, sponsorships, or college deals!\n"
            "- ABSOLUTELY FORBIDDEN: NEVER mention that this message or outreach was done by AI, an autonomous system, or a bot!\n"
            "- ABSOLUTELY FORBIDDEN: DO NOT mention any Agentic AI Hackathon!\n"
            "- Keep language grounded in customer experience, conversion funnels, and operational product efficiency."
        ),
        "p1_instruction": (
            "- Choose naturally among these product problem opening styles tailored for non-tech / consumer startups:\n"
            "  Style A (Conversion Funnel & Drop-off): \"Looking at how {clean_company} is scaling its customer journey, a common friction point in consumer and operational platforms is drop-off between product discovery and completed checkout/booking.\"\n"
            "  Style B (Search & Catalog Discovery UX): \"While tracking {clean_company}'s operations, an immediate opportunity that stands out is streamlining how customers search, discover, and select items to maximize repeat ordering.\"\n"
            "  Style C (Operations & Fulfillment Workflows): \"In expanding {clean_company}'s business, a key bottleneck is often the digital workflow between user demand and backend fulfillment operations.\""
        ),
        "p2_instruction": (
            "- Connect resolving this to measurable business and product metrics:\n"
            "  - Framing example: \"Once this friction is resolved, the upside is immediate: higher checkout completion rates, smoother customer onboarding, and fewer operational drop-offs without requiring manual customer support interventions.\""
        ),
        "p3_instruction": (
            "- Present how Yatharth solves this from a PM perspective:\n"
            "  - Framing example: \"I can help tackle this from a product standpoint by [specific PM mechanism: e.g. mapping user drop-off triggers, redesigning catalog discovery flows, and running targeted conversion experiments]. I've built 15+ end-to-end projects from scratch with an obsessive focus on exceptional UX and solving practical real-world problems. During my internship at NoBrokerHood, I streamlined search and discovery flows to deliver 1.5x output coverage and shipped automated conversion features capturing 25+ extra qualified leads/month. (I'm a 4th-year student at DTU, 9.3 CGPA).\""
        ),
        "ai_disclosure_allowed": False,
        "hackathon_allowed": False,
    },
    "non_tech_growth_sales": {
        "title": "Non-Tech Startups (Sales, Business Development & Growth)",
        "role_pitch": "B2B Sales, Business Development & Growth Intern",
        "background_summary": (
            "- Candidate: Yatharth Sachdeva, 4th-year student at Delhi Technological University (DTU, 9.3 CGPA).\n"
            "- Past Experience: Intern at NoBrokerHood driving sales outreach, client acquisition, and pipeline growth.\n"
            "- Key Value Proposition: High-tempo client acquisition, outbound B2B pipeline generation, partnership outreach, deal conversion, and funnel UX optimization.\n"
            "- Proven Sales & Growth Outcomes:\n"
            "  1. Shipped automated B2B sales outreach workflows at NoBrokerHood capturing 25+ extra qualified leads per month.\n"
            "  2. For Indian Sales Startups: Personally spearheaded college society fest corporate sponsorships, closing ₹3–10 Lakh deals each year through disciplined cold outbound pitching and high-stakes deal negotiations.\n"
            "  3. Obsessive focus on user experience (UX) applied to sales funnels, user conversion, and customer touchpoints.\n"
            "  4. Executed prospect qualification cadences and proactive pipeline follow-ups to accelerate deal closures."
        ),
        "focus_instruction": (
            "Frame the message strictly around SALES, BUSINESS DEVELOPMENT, or GROWTH:\n"
            "- If the lead is in Sales / BD / Commercial -> Pitch outbound prospecting, client acquisition, and pipeline expansion.\n"
            "- If the lead is a Founder / CEO / Ops -> Pitch revenue growth, corporate partnerships, and reliable customer pipeline.\n"
            "SALES PROOF RULES:\n"
            "- IF THE TARGET COMPANY IS INDIAN: Show proof through BOTH NoBrokerHood B2B sales outreach (25+ extra qualified leads/month) AND personal college society fest corporate work closing ₹3–10 Lakh deals each year via cold outreach and deal negotiations. (Also mention user experience focus on conversion funnels).\n"
            "- IF THE TARGET COMPANY IS OUTSIDE INDIA (e.g. Dubai, US): NEVER mention college society fest or fest sponsorship deals! Only cite NoBrokerHood B2B sales pipeline outcomes.\n"
            "STRICT RULES:\n"
            "- ABSOLUTELY FORBIDDEN: NEVER mention that this message or outreach was done by AI, an autonomous system, or a bot!\n"
            "- ABSOLUTELY FORBIDDEN: DO NOT mention any Agentic AI Hackathon!\n"
            "- Keep language focused on business execution, revenue pipeline, and client conversion."
        ),
        "p1_instruction": (
            "- Choose naturally among these business growth problem opening styles:\n"
            "  Style A (Customer & Client Acquisition): \"Looking at how {clean_company} is expanding its market presence, a central challenge in scaling operations is maintaining a consistent outbound pipeline of qualified clients and commercial partners.\"\n"
            "  Style B (Pipeline Conversion & Follow-up): \"While tracking {clean_company}'s commercial operations, an immediate opportunity that stands out is tightening the conversion cadence from initial prospect interest to confirmed deal closures.\"\n"
            "  Style C (B2B Partnerships & Outbound): \"In expanding {clean_company}'s reach, a major hurdle is building predictable outbound outreach cadences that generate qualified meetings consistently without high acquisition costs.\""
        ),
        "p2_instruction": (
            "- Connect resolving this to measurable business outcomes:\n"
            "  - Framing example: \"Solving this directly accelerates business velocity: higher lead-to-client conversion, shorter sales cycles, and a predictable monthly pipeline of commercial accounts.\""
        ),
        "p3_instruction": (
            "- Present how Yatharth solves this with hands-on sales execution:\n"
            "  - For Indian startups framing example: \"I can help tackle this from a sales standpoint by [specific sales mechanism: e.g. implementing high-tempo outbound outreach cadences and structured prospect follow-ups]. During my internship at NoBrokerHood, I executed outreach workflows that brought in 25+ extra qualified leads per month, and I've also personally driven corporate sponsorships for our college society fest, closing ₹3–10 Lakh deals each year through cold outbound pitching and deal negotiations. (I'm a 4th-year student at DTU, 9.3 CGPA).\"\n"
            "  - For Non-Indian / International startups framing example: \"I can help tackle this from a sales standpoint by [specific sales mechanism: e.g. implementing high-tempo outbound outreach cadences and structured prospect follow-ups]. During my internship at NoBrokerHood, I executed outreach workflows that brought in 25+ extra qualified leads per month and accelerated deal closures. (I'm a 4th-year student at DTU, 9.3 CGPA).\""
        ),
        "ai_disclosure_allowed": False,
        "hackathon_allowed": False,
    },
    "founders_office": {
        "title": "Early-Stage Startups (Founder's Office / Generalist / Growth & Ops)",
        "role_pitch": "Founder's Office Intern (Generalist / Growth & Ops)",
        "background_summary": (
            "- Candidate: Yatharth Sachdeva, 4th-year student at Delhi Technological University (DTU, 9.3 CGPA).\n"
            "- Past Experience: Intern at NoBrokerHood (unicorn).\n"
            "- Key Value Proposition: High-agency generalist capable of wearing multiple hats across product UX, sales pipeline execution, and zero-to-one operations.\n"
            "- Proven Outcomes:\n"
            "  1. Built 15+ end-to-end projects from scratch, obsessively focused on exceptional user experience (UX) and solving practical, real-life problems (never building anything without a tangible purpose).\n"
            "  2. Shipped automated B2B sales outreach workflows capturing 25+ extra qualified leads/month at NoBrokerHood.\n"
            "  3. Revamped search and discovery product logic delivering 1.5x output coverage within identical constraints.\n"
            "  4. For Indian startups: Spearheaded corporate sponsorships closing ₹3–10 Lakh deals each year through disciplined cold outbound."
        ),
        "focus_instruction": (
            "Frame the entire message around being an agile, high-agency generalist for the Founder's Office at an early-stage startup.\n"
            "Position Yatharth as someone who operates without silos: taking full ownership across product user experience (UX), customer pipeline execution, and zero-to-one operational tasks without requiring handholding.\n"
            "STRICT RULES:\n"
            "- If the startup is Indian, you may mention closing ₹3–10 Lakh deals each year in corporate sponsorships alongside NoBrokerHood.\n"
            "- If the startup is outside India (Dubai, US): NEVER mention college fest or sponsorship deals!\n"
            "- ABSOLUTELY FORBIDDEN: NEVER mention that this message or outreach was done by AI, an autonomous system, or a bot!\n"
            "- ABSOLUTELY FORBIDDEN: DO NOT mention any Agentic AI Hackathon!\n"
            "- Emphasize extreme ownership, user experience craft, and high execution speed."
        ),
        "p1_instruction": (
            "- Choose naturally among high-agency Founder's Office problem opening styles:\n"
            "  Style A (Execution Bandwidth): \"Looking at how {clean_company} is scaling its zero-to-one operations, a recurring challenge for early founding teams is balancing high-level strategy with day-to-day execution across product UX, customer acquisition, and operational fires.\"\n"
            "  Style B (Product UX & Customer Discovery): \"While following {clean_company}'s trajectory, an immediate bottleneck early teams face is rapidly iterating on product user experience while simultaneously executing outbound pipeline and talking to early customers.\"\n"
            "  Style C (Cross-Functional Velocity): \"In scaling {clean_company}, early-stage teams often run into bandwidth constraints—moving fast across product workflows, customer onboarding, and pipeline execution simultaneously.\""
        ),
        "p2_instruction": (
            "- Connect resolving this to founder velocity:\n"
            "  - Framing example: \"Having dedicated Founder's Office execution directly frees up leadership bandwidth: faster product UX iteration cycles, zero lead drop-off in early customer pipelines, and agile execution across cross-functional priorities without adding bulky headcount.\""
        ),
        "p3_instruction": (
            "- Present how Yatharth solves this as a high-agency generalist:\n"
            "  - Framing example: \"I can plug into the Founder's Office as a high-agency generalist to tackle [specific execution area: e.g. refining product user experience, executing customer outreach pipelines, or setting up operational workflows]. I've built 15+ end-to-end projects from scratch with an obsessive focus on exceptional UX and solving practical real-world problems. During my internship at NoBrokerHood, I shipped automated B2B sales outreach capturing 25+ extra leads/month and optimized discovery logic for 1.5x output coverage. (I'm a 4th-year student at DTU, 9.3 CGPA).\""
        ),
        "ai_disclosure_allowed": False,
        "hackathon_allowed": False,
    }
}

# ─── Company Tier Messaging Configurations ────────────────────────────────────
TIER_INSTRUCTIONS = {
    "enterprise": {
        "title": "Big Giant / Enterprise ({clean_company})",
        "guidance": (
            "COMPANY TIER: BIG GIANT / ENTERPRISE ({clean_company} is an established industry leader / large-scale enterprise).\n"
            "MESSAGING STRATEGY FOR BIG GIANTS:\n"
            "- CRITICAL RULE: DO NOT focus on or point out internal problems, bugs, or platform bottlenecks at {clean_company}!\n"
            "  (It sounds presumptuous and amateurish to tell an established industry leader that their systems are flawed).\n"
            "- INSTEAD, FOCUS HEAVILY ON WHAT YATHARTH CAN PROVIDE AND WHAT HE KNOWS:\n"
            "  * Paragraph 1 (Acknowledge Scale & Leadership): Acknowledge {clean_company}'s premier market footprint, high transaction volume, or enterprise scale without pointing out bugs.\n"
            "  * Paragraph 2 (What We Can Provide): Articulate high-leverage execution firepower—bringing rare hybrid B2B sales execution and technical automation, autonomous pipeline acceleration, and immediate execution without ramp-up friction.\n"
            "  * Paragraph 3 (What We Know & Proof): Present proven outcomes: automated B2B sales outreach at NoBrokerHood (25+ extra leads/mo), search discovery optimization (1.5x output coverage), 15+ end-to-end UX projects, DTU IT (9.3 CGPA).\n"
            "  * Paragraph 4: Friendly, low-friction CTA with resume link."
        ),
        "p1_style": (
            "- Open with: \"Hi {first_name},\"\n"
            "- Acknowledge their scale and leadership in the market directly without corporate buzzwords or presuming bugs:\n"
            "  * PropTech/Property: \"Following {clean_company}'s footprint across large-scale property developments and transaction volume, in real estate deals are won on response speed and lead qualification—making sure high-intent buyers and investors are engaged before they look elsewhere.\"\n"
            "  * Tech/PM: \"Following {clean_company}'s product ecosystem and user scale, delivering frictionless customer experiences while driving user activation at your scale requires rigorous product execution.\"\n"
            "  * Growth/Sales: \"Following {clean_company}'s premier market presence and brand leadership across the industry, scaling transaction velocity and client acquisition requires disciplined, high-tempo execution.\""
        ),
        "p2_style": (
            "- Focus purely on WHAT YATHARTH CAN PROVIDE (high-leverage execution firepower):\n"
            "  * PropTech/Property: \"Closing this gap directly increases booked viewings, shortens the sales cycle, and ensures no high-intent inquiries slip through portal or WhatsApp channels.\"\n"
            "  * Other: \"I can provide dedicated high-leverage execution to accelerate outbound initiatives and streamline pipeline workflows without requiring ramp-up time.\""
        ),
        "p3_style": (
            "- Focus on the Sales + Tech combo with expanded NoBrokerHood proof:\n"
            "  * PropTech: \"I bring a rare sales and tech combo: I understand property buyer acquisition and deal conversion, and I have the technical ability to automate lead routing, build custom qualification workflows, and optimize discovery directly. At NoBrokerHood (India's premier PropTech unicorn), I worked directly on transaction velocity—shipping automated B2B sales outreach capturing 25+ extra qualified leads/month and overhauling search discovery logic for 1.5x output coverage. (I'm a 4th-year student at DTU, 9.3 CGPA, and in fact, this message was researched and delivered by an autonomous pipeline I built to test execution live).\"\n"
            "  * Property/Real Estate: \"I combine a strong grip on property sales with the technical ability to structure disciplined lead qualification cadences and automate follow-ups so zero inquiries slip through. During my internship at NoBrokerHood (India's premier PropTech unicorn), I drove B2B sales outreach workflows capturing 25+ extra qualified leads per month and built prospect research pipelines to accelerate high-value deal closures. (I'm a 4th-year student at DTU with a 9.3 CGPA).\"\n"
            "  * Other: \"During my internship at NoBrokerHood, I shipped automated B2B sales outreach capturing 25+ extra qualified leads/month and optimized search discovery logic for 1.5x output coverage. (I'm a 4th-year student at DTU, 9.3 CGPA).\""
        )
    },
    "mid_level": {
        "title": "Mid-Level / Scale-Up ({clean_company})",
        "guidance": (
            "COMPANY TIER: MID-LEVEL / MEDIUM COMPANY ({clean_company} is an established, high-growth scale-up / mid-market firm).\n"
            "MESSAGING STRATEGY FOR MID-LEVEL:\n"
            "- Use a balanced blend of strategic scaling momentum AND what Yatharth can provide and knows:\n"
            "  * Paragraph 1 (Growth & Scale Context): Grounded awareness of their scaling momentum in property/market expansion.\n"
            "  * Paragraph 2 (What We Can Provide & Unlock): Operational acceleration (faster inquiry-to-viewing conversion, zero lead leakage, rapid follow-up).\n"
            "  * Paragraph 3 (What We Know & Proof): Highlight expanded NoBrokerHood proven metrics (25+ extra leads/mo, 1.5x search efficiency), sales + tech combo.\n"
            "  * Paragraph 4: Friendly, low-friction CTA with resume link."
        ),
        "p1_style": (
            "- Open with: \"Hi {first_name},\"\n"
            "- Blend market momentum with deal execution context:\n"
            "  * PropTech/Property: \"Looking at how {clean_company} is accelerating its growth across the property market, in fast-moving real estate deals are won on response speed and lead qualification—making sure high-intent buyers and tenants are matched before inquiries go cold.\"\n"
            "  * Tech/PM: \"Looking at how {clean_company} is scaling its product workflows, a key priority is streamlining user activation and onboarding flows to convert growing traffic into daily active users.\"\n"
            "  * Growth/Sales: \"Looking at how {clean_company} is expanding its market footprint, maintaining a consistent outbound pipeline of qualified clients without rising acquisition costs is a central growth driver.\""
        ),
        "p2_style": (
            "- Connect resolving friction with what we can provide and unlock:\n"
            "  * PropTech/Property: \"Closing this gap directly drives top-line volume: faster inquiry-to-viewing conversion, zero lead leakage across portal channels, and higher transaction velocity without adding operational drag.\"\n"
            "  * Other: \"Solving this directly accelerates business velocity: higher lead-to-client conversion, shorter sales cycles, and a predictable monthly pipeline of commercial accounts.\""
        ),
        "p3_style": (
            "- Present what Yatharth provides and knows (Sales + Tech combo with expanded NoBrokerHood):\n"
            "  * PropTech: \"I bring a rare sales and tech combo: I understand property buyer acquisition and deal conversion, and I have the technical ability to automate lead routing, build custom qualification workflows, and optimize discovery directly. At NoBrokerHood (India's premier PropTech unicorn), I worked directly on transaction velocity—shipping automated B2B sales outreach capturing 25+ extra qualified leads/month and overhauling search discovery logic for 1.5x output coverage. (I'm a 4th-year student at DTU, 9.3 CGPA, and in fact, this message was researched and delivered by an autonomous pipeline I built to test execution live).\"\n"
            "  * Property/Real Estate: \"I combine a strong grip on property sales with the technical ability to structure disciplined lead qualification cadences and automate follow-ups so zero inquiries slip through. During my internship at NoBrokerHood (India's premier PropTech unicorn), I drove B2B sales outreach workflows capturing 25+ extra qualified leads per month and built prospect research pipelines to accelerate high-value deal closures. (I'm a 4th-year student at DTU with a 9.3 CGPA).\"\n"
            "  * Other: \"During my internship at NoBrokerHood, I shipped automated B2B sales outreach workflows capturing 25+ extra qualified leads per month and optimized search discovery logic for 1.5x output coverage. (I'm a 4th-year student at DTU, 9.3 CGPA).\""
        )
    },
    "startup": {
        "title": "Startup ({clean_company})",
        "guidance": (
            "COMPANY TIER: STARTUP (0-5 years operating, early-stage agility).\n"
            "MESSAGING STRATEGY FOR STARTUPS:\n"
            "- Follow the classic startup problem-solving flow:\n"
            "  * Paragraph 1: Pinpoint specific operational friction, bottleneck, or drop-off point at {clean_company}.\n"
            "  * Paragraph 2: Upside and tangible business/pipeline gains once resolved.\n"
            "  * Paragraph 3: Concrete mechanism of how Yatharth solves this combining sales & tech + proven NoBrokerHood and 15+ UX projects proof.\n"
            "  * Paragraph 4: Friendly, low-friction CTA with resume link."
        ),
        "p1_style": None,
        "p2_style": None,
        "p3_style": None,
    }
}

# ─── Deep-Dive Intelligence & Personalized DM Drafting Prompt ────────────────
DEEP_DIVE_RESEARCH_PROMPT = """You are the personalized outreach drafting engine representing Yatharth Sachdeva.
Yatharth is applying for an immediate 2-month internship at an ambitious venture.

TRACK & POSITIONING FOR THIS TARGET:
- Pitch Track: {track_title}
- Role Targeted: {role_pitch}
- Geographic Target: {geo_segment}
- Company Tier: {company_tier}
- Candidate Background & Key Outcomes:
{background_summary}

TARGET LEAD INFORMATION (VERIFIED LIVE FROM LINKEDIN):
- Name: {lead_name}
- Clean First Name: {first_name}
- Current Company (Entity): {lead_company}
- Conversational Company Name: {clean_company}
- Verified Current Role/Title: {lead_role}
- LinkedIn Top Card / Headline:
\"\"\"
{top_card_text}
\"\"\"
- Scraped Live Experience Section:
\"\"\"
{scraped_experience}
\"\"\"

YOUR DEEP-DIVE RESEARCH & DRAFTING INSTRUCTIONS:
{focus_instruction}

{tier_messaging_instruction}

DETAILED MESSAGE STRUCTURE (drafted_dm):
Write a 4-paragraph direct message formatted with \\n\\n between paragraphs:

Paragraph 1: Context & Opening
- Open with: "Hi {first_name},"
{p1_instruction}
- Ground it in what {clean_company} actually does.

Paragraph 2: Strategic Value / Upside / What We Can Provide
{p2_instruction}

Paragraph 3: "I Can Solve / Deliver This Like This" (Concrete Mechanism + What We Know + Proof)
{p3_instruction}

Paragraph 4: Friendly, Low-Friction Call to Action
- "Would love to share a few actionable ideas on a quick 10-12 min call this week if you're open to it. You can check my resume and a quick brief about me here: {resume_link}\\n\\nLet me know what time works best for you!"

CRITICAL RULES:
- Separate the 4 paragraphs with \\n\\n in the JSON string.
- Address the person by their clean first name: "Hi {first_name},". Never use formal titles like "Hi Dr," or "Hi Mr,".
- Use the clean company name "{clean_company}" (NEVER formal suffixes like "Pvt. Ltd.", "Inc", "LLC").
- STRICTEST RULE - DO NOT MENTION PREVIOUS COMPANIES: If the lead recently changed companies or has older jobs, you must NEVER mention or reference their previous company. Treat them purely as a leader at {clean_company}.
- NEVER use buzzwords like: "imagine", "what if", "pleasure", "honored", "aspiring", "hope", "delve", "apologize", "sincerely", "opportunity", "passionate", "revolutionize", "synergy".
- Keep length around 130-160 words. Punchy, authentic, and persuasive builder-to-builder tone.

Return ONLY a valid JSON object wrapped in ```json ... ``` tags:
{{
  "company_analysis": "Crisp 1-2 sentence breakdown of what {clean_company} builds and their user base/market.",
  "identified_pain_point": "The strategic focus area or bottleneck at {clean_company}.",
  "expected_benefits": "Concrete metrics, business gains, or value delivered.",
  "solution_approach": "The specific mechanism or contribution Yatharth would bring.",
  "drafted_dm": "The complete 4-paragraph direct message formatted with \\n\\n between paragraphs."
}}
"""


class GhostwriterAgent:
    def __init__(self):
        self.resume_link = os.getenv("RESUME_LINK", "").strip()
        if not self.resume_link or self.resume_link == "[ADD_YOUR_RESUME_LINK_HERE]":
            profile_path = DATA_DIR / "my_profile.json"
            if profile_path.exists():
                try:
                    with open(profile_path, "r", encoding="utf-8") as f:
                        prof = json.load(f)
                        self.resume_link = prof.get("resume_link", "").strip()
                except Exception:
                    pass
        if not self.resume_link:
            self.resume_link = "https://drive.google.com/drive/folders/14NkmTzo2gvSRtooHocBblueGXDqvQWFq"

    def _load_instructions(self) -> dict:
        if INSTRUCTIONS_PATH.exists():
            with open(INSTRUCTIONS_PATH, encoding="utf-8") as f:
                return json.load(f)
        return {"rules": {"dos": [], "donts": [], "tone": "", "structure": ""}}

    def _extract_json(self, text: str) -> Optional[list]:
        match = re.search(r"```json\s*([\s\S]+?)\s*```", text)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return None
        # Fallback: try to parse entire response as JSON
        try:
            return json.loads(text.strip())
        except Exception:
            return None

    def _truncate(self, note: str, max_len: int = MAX_NOTE_LENGTH) -> str:
        """Truncate a note at word boundary if over limit."""
        if len(note) <= max_len:
            return note
        truncated = note[:max_len - 3]
        last_space = truncated.rfind(" ")
        if last_space > 0:
            truncated = truncated[:last_space]
        return truncated + "..."

    @staticmethod
    def _infer_geo_segment(lead: dict) -> str:
        geo = lead.get("geo_segment")
        if geo:
            return geo.lower()
        text = f"{lead.get('company', '')} {lead.get('role', '')} {lead.get('snippet', '')} {lead.get('source_query', '')}".lower()
        if any(w in text for w in ["dubai", "uae", "emirates", "abu dhabi", "sharjah"]):
            return "dubai"
        if any(w in text for w in ["new york", "nyc", "manhattan", "brooklyn"]):
            return "new_york"
        if any(w in text for w in ["united states", "usa", "san francisco", "austin", "seattle", "california"]):
            return "us"
        if any(w in text for w in ["bangalore", "bengaluru", "delhi", "gurgaon", "gurugram", "noida", "mumbai", "pune", "hyderabad", "india"]):
            return "india"
        return "other"

    @staticmethod
    def _infer_company_tier(lead: dict) -> str:
        """
        Infer company tier: 'enterprise' (big giants), 'mid_level', or 'startup'.
        For Indian companies, strictly 'startup'.
        For Non-Indian (Dubai, US, etc.), can be startup, mid_level, or enterprise.
        """
        tier = lead.get("company_tier")
        if tier in ("enterprise", "mid_level", "startup"):
            return tier

        geo = (lead.get("geo_segment") or GhostwriterAgent._infer_geo_segment(lead)).lower()
        if geo in ("india", "in"):
            return "startup"

        company = (lead.get("company") or "").lower().strip()
        role = (lead.get("role") or "").lower()
        snippet = (lead.get("snippet") or "").lower()
        combined = f"{company} {role} {snippet}"

        # Big Giants / Enterprise
        ENTERPRISE_KEYWORDS = [
            "emaar", "damac", "aldar", "sobha", "nakheel", "meraas", "omniyat",
            "danube", "deyaar", "binghatti", "mag lifestyle", "azizi", "cbre",
            "jll", "colliers", "savills", "knight frank", "cushman",
            "google", "microsoft", "amazon", "apple", "meta", "salesforce",
            "oracle", "stripe", "uber", "airbnb", "netflix", "adobe",
            "compass", "zillow", "redfin", "costar", "opendoor",
            "enterprise", "multinational", "conglomerate", "fortune 500", "publicly traded",
            "global real estate", "nasdaq", "nyse"
        ]
        if any(kw in company for kw in ENTERPRISE_KEYWORDS):
            return "enterprise"

        # Mid-Level / Established Medium Companies
        MID_LEVEL_KEYWORDS = [
            "betterhomes", "allsopp", "haus & haus", "cavendish", "propsearch",
            "bayut", "propertyfinder", "fäm properties", "fam properties",
            "dacha", "driven properties", "provident estate",
            "series b", "series c", "series d", "scale-up", "scaleup",
            "200+ employees", "500+ employees", "mid-market", "established",
            "medium"
        ]
        if any(kw in combined for kw in MID_LEVEL_KEYWORDS):
            return "mid_level"

        return "startup"

    def _infer_pitch_track(self, lead: dict, top_card: str = "", exp: str = "") -> str:
        """Infer appropriate pitch track ensuring Dubai and Real Estate leads strictly use sales-tech combo."""
        geo = (lead.get("geo_segment") or GhostwriterAgent._infer_geo_segment(lead)).lower()
        comp_type = lead.get("company_type", "").lower()
        role = (lead.get("role") or "").lower()
        combined = f"{lead.get('company', '')} {role} {top_card} {exp} {lead.get('snippet', '')}".lower()

        # Check if PropTech or Property / Real Estate
        is_proptech = comp_type == "proptech" or any(w in combined for w in ["proptech", "real estate tech", "property tech"])
        is_property = comp_type == "property" or any(w in combined for w in ["real estate", "property", "residential", "brokerage", "realtor", "housing", "developer", "realty", "properties"])

        # For ALL Dubai leads OR Indian/Global Real Estate & PropTech leads:
        # Strictly use proptech_sales_tech or property_sales (Sales + Tech combo, NO PM / Founder's Office fancy words!)
        if geo in ("dubai", "uae") or is_proptech or is_property:
            if is_proptech:
                return "proptech_sales_tech"
            return "property_sales"

        track = lead.get("pitch_track")
        live_text = f"{lead.get('company', '')} {top_card} {exp}".lower()
        agency_keywords = ["design studio", "ui/ux studio", "creative agency", "digital agency", "design agency", "studio", "branding agency"]
        if any(w in live_text for w in agency_keywords) and track in ("proptech_sales_tech", "property_sales"):
            return "tech_pm"

        if track in TRACK_CONFIGS:
            return track

        # 1. Founder's Office / Chief of Staff role check (for non-real estate startups)
        fo_patterns = [r"\bfounder'?s?\s+office\b", r"\bchief\s+of\s+staff\b"]
        if any(re.search(pat, role) or re.search(pat, combined) for pat in fo_patterns):
            return "founders_office"

        if comp_type == "tech" or any(re.search(pat, combined) for pat in [r'\bsoftware\b', r'\bsaas\b', r'\bai\b', r'\bgenai\b', r'\bplatform\b', r'\bcloud\b', r'\btech\b', r'\bfintech\b', r'\bdeveloper\b']):
            sales_patterns = [r'\bsales\b', r'\bbusiness development\b', r'\bbd\b', r'\bcommercial\b']
            if any(re.search(pat, role) for pat in sales_patterns):
                return "non_tech_growth_sales"
            return "tech_pm"

        # Non-tech startup: differentiate PM vs Sales/Growth using word boundaries
        pm_patterns = [r'\bproduct\b', r'\bpm\b', r'\bapm\b', r'\bcpo\b', r'\buser experience\b', r'\bux\b']
        if any(re.search(pat, role) or re.search(pat, combined) for pat in pm_patterns):
            return "non_tech_pm"
        return "non_tech_growth_sales"

    def _generate_fallback(
        self,
        pitch_track: str,
        first_name: str,
        clean_comp: str,
        geo_segment: str = "india",
        company_tier: str = "startup"
    ) -> dict:
        """Track- and tier-specific deterministic fallback when Gemini is unavailable."""
        # ── 1. Enterprise (Big Giants) Fallback: What we provide & know (no internal bugs) ──
        if company_tier == "enterprise":
            if pitch_track == "proptech_sales_tech":
                return {
                    "company_analysis": f"{clean_comp} premier property technology ecosystem and market reach.",
                    "identified_pain_point": "Scaling transaction velocity and high-intent buyer qualification at scale",
                    "expected_benefits": "faster inquiry-to-viewing conversion, zero lead leakage, and higher transaction velocity",
                    "solution_approach": "combining commercial property sales execution with technical pipeline automation",
                    "drafted_dm": (
                        f"Hi {first_name},\n\n"
                        f"Looking at how {clean_comp} is scaling property transactions, in fast-moving property markets deals are won or lost on response time and lead qualification—making sure high-intent buyers and tenants are engaged before inquiries go cold on WhatsApp and portals.\n\n"
                        f"Closing this gap directly increases booked viewings, shortens the sales cycle, and ensures no valuable inquiries slip through portal channels.\n\n"
                        f"I bring a rare sales and tech combo: I understand property buyer acquisition and deal conversion, and I have the technical ability to automate lead routing, build custom qualification workflows, and optimize discovery directly. At NoBrokerHood (India's premier PropTech unicorn), I worked directly on transaction velocity—shipping automated B2B sales outreach capturing 25+ extra qualified leads/month and overhauling search discovery logic for 1.5x output coverage. (I'm a 4th-year student at DTU, 9.3 CGPA, and in fact, this message was researched and delivered by an autonomous pipeline I built to test execution live).\n\n"
                        f"Would love to connect for 10 minutes this week to share a few practical ways to accelerate buyer conversion and see if there's a strong fit to work together. You can check my resume here: {self.resume_link}\n\n"
                        f"Let me know what time works best for you!"
                    )
                }
            elif pitch_track == "property_sales":
                return {
                    "company_analysis": f"{clean_comp} premier real estate portfolio and market presence.",
                    "identified_pain_point": "Speed-to-lead and capturing high-intent property buyers before competing brokerages",
                    "expected_benefits": "higher viewing-to-close ratios, rapid WhatsApp response, and zero buyer leakage",
                    "solution_approach": "disciplined client acquisition cadences and automated lead qualification",
                    "drafted_dm": (
                        f"Hi {first_name},\n\n"
                        f"Looking at {clean_comp}'s portfolio across the property market, in high-end real estate deals are won on speed-to-lead—specifically qualifying inbound portal and WhatsApp inquiries before buyers and investors engage another brokerage.\n\n"
                        f"Tightening this follow-up directly drives transaction volume: higher viewing-to-close ratios, faster response times, and a predictable monthly pipeline of qualified buyers.\n\n"
                        f"I combine a strong grip on property sales with the technical ability to structure disciplined lead qualification cadences and automate follow-ups so zero inquiries slip through. During my internship at NoBrokerHood (India's premier PropTech unicorn), I drove B2B sales outreach workflows capturing 25+ extra qualified leads per month and built prospect research pipelines to accelerate high-value deal closures. (I'm a 4th-year student at DTU with a 9.3 CGPA).\n\n"
                        f"Would love to connect for 10 minutes this week to share a few practical ways to accelerate buyer conversion and see if there's a strong fit to work together. You can check my resume here: {self.resume_link}\n\n"
                        f"Let me know what time works best for you!"
                    )
                }
            elif pitch_track in ("tech_pm", "non_tech_pm"):
                return {
                    "company_analysis": f"{clean_comp} enterprise product ecosystem and user scale.",
                    "identified_pain_point": "Delivering frictionless customer experiences while driving activation and retention at enterprise scale",
                    "expected_benefits": "faster user time-to-value, higher retention, and high-tempo product-led execution",
                    "solution_approach": "delivering autonomous PM execution across user journey mapping, onboarding UX, and conversion optimization",
                    "drafted_dm": (
                        f"Hi {first_name},\n\n"
                        f"Following {clean_comp}'s product ecosystem and user scale, delivering frictionless customer experiences while driving activation and retention at enterprise scale requires rigorous product execution.\n\n"
                        f"I can provide dedicated high-leverage product execution to streamline customer journeys and optimize user conversion. I bring an obsessive focus on exceptional UX, enabling me to step in, identify core activation drop-offs, and design frictionless user flows autonomously without ramp-up overhead.\n\n"
                        f"I've built 15+ end-to-end projects from scratch with an obsessive focus on exceptional UX and solving practical real-world problems. At NoBrokerHood as an AI PM Intern, I worked cross-functionally across engineering, design, and growth to ship automated B2B features capturing 25+ extra qualified leads/month and revamped search discovery logic for 1.5x output coverage. (I'm a 4th-year IT student at DTU, 9.3 CGPA).\n\n"
                        f"Would love to share a few actionable ideas on a quick 10-12 min call this week if you're open to it. You can check my resume and a quick brief about me here: {self.resume_link}\n\n"
                        f"Let me know what time works best for you!"
                    )
                }
            else:  # Sales / Growth
                return {
                    "company_analysis": f"{clean_comp} premier market presence and enterprise client base.",
                    "identified_pain_point": "Scaling transaction velocity and client acquisition with high-tempo execution",
                    "expected_benefits": "expanded qualified pipeline, accelerated deal cycles, and zero-touch outbound acceleration",
                    "solution_approach": "executing disciplined outbound qualification and pipeline acceleration",
                    "drafted_dm": (
                        f"Hi {first_name},\n\n"
                        f"Following {clean_comp}'s premier market presence and brand leadership across the industry, scaling transaction velocity and client acquisition requires high-tempo, disciplined execution.\n\n"
                        f"I can provide dedicated high-leverage execution to accelerate outbound pipelines and expand client acquisition. I bring a structured approach to B2B outbound workflows, enabling me to step in, identify high-intent buyer targets, and drive outbound pipeline momentum autonomously without requiring ramp-up time.\n\n"
                        f"During my internship at NoBrokerHood, I executed outreach workflows that brought in 25+ extra qualified leads per month and accelerated deal closures through disciplined follow-ups. (I'm a 4th-year student at DTU, 9.3 CGPA).\n\n"
                        f"Would love to share a few actionable ideas on a quick 10-12 min call this week if you're open to it. You can check my resume and a quick brief about me here: {self.resume_link}\n\n"
                        f"Let me know what time works best for you!"
                    )
                }

        # ── 2. Mid-Level (Scale-ups) Fallback: Balanced mix of scale momentum + what we provide ──
        if company_tier == "mid_level":
            if pitch_track == "proptech_sales_tech":
                return {
                    "company_analysis": f"{clean_comp} high-growth property platform and market momentum.",
                    "identified_pain_point": "Maintaining transaction velocity while streamlining buyer and investor inquiries",
                    "expected_benefits": "faster lead-to-viewing conversion, reduced sales response latency, and turning search traffic into transactional pipeline",
                    "solution_approach": "automating high-intent buyer qualification cadences and streamlining property listing workflows",
                    "drafted_dm": (
                        f"Hi {first_name},\n\n"
                        f"Looking at how {clean_comp} is accelerating its growth across the property market, in fast-moving real estate deals are won on response speed and lead qualification—making sure high-intent buyers and tenants are matched before inquiries go cold.\n\n"
                        f"Closing this gap directly drives top-line volume: faster inquiry-to-viewing conversion, zero lead leakage, and higher transaction velocity without adding operational drag.\n\n"
                        f"I bring a rare sales and tech combo: I understand property buyer acquisition and deal conversion, and I have the technical ability to automate lead routing, build custom qualification workflows, and optimize discovery directly. At NoBrokerHood (India's premier PropTech unicorn), I worked directly on transaction velocity—shipping automated B2B sales outreach capturing 25+ extra qualified leads/month and overhauling search discovery logic for 1.5x output coverage. (I'm a 4th-year student at DTU, 9.3 CGPA, and in fact, this message was researched and delivered by an autonomous pipeline I built to test execution live).\n\n"
                        f"Would love to connect for 10 minutes this week to share a few practical ways to accelerate buyer conversion and see if there's a strong fit to work together. You can check my resume here: {self.resume_link}\n\n"
                        f"Let me know what time works best for you!"
                    )
                }
            elif pitch_track == "property_sales":
                return {
                    "company_analysis": f"{clean_comp} high-growth real estate brokerage and portfolio expansion.",
                    "identified_pain_point": "Responding to inbound portal inquiries fast enough on WhatsApp to maximize viewing conversion rates",
                    "expected_benefits": "higher viewing-to-close ratios, faster response times, and a predictable monthly pipeline of qualified buyers",
                    "solution_approach": "structuring disciplined lead qualification cadences and rapid lead response workflows",
                    "drafted_dm": (
                        f"Hi {first_name},\n\n"
                        f"Looking at {clean_comp}'s portfolio across the property market, in high-end real estate deals are won on speed-to-lead—specifically qualifying inbound portal and WhatsApp inquiries before buyers and investors engage another brokerage.\n\n"
                        f"Tightening this follow-up directly drives transaction volume: higher viewing-to-close ratios, faster response times, and a predictable monthly pipeline of qualified buyers.\n\n"
                        f"I combine a strong grip on property sales with the technical ability to structure disciplined lead qualification cadences and automate follow-ups so zero inquiries slip through. During my internship at NoBrokerHood (India's premier PropTech unicorn), I drove B2B sales outreach workflows capturing 25+ extra qualified leads per month and built prospect research pipelines to accelerate high-value deal closures. (I'm a 4th-year student at DTU with a 9.3 CGPA).\n\n"
                        f"Would love to connect for 10 minutes this week to share a few practical ways to accelerate buyer conversion and see if there's a strong fit to work together. You can check my resume here: {self.resume_link}\n\n"
                        f"Let me know what time works best for you!"
                    )
                }
            elif pitch_track in ("tech_pm", "non_tech_pm"):
                return {
                    "company_analysis": f"{clean_comp} scaling product workflows and user base.",
                    "identified_pain_point": "Streamlining user activation and onboarding flows to convert growing traffic into daily active users",
                    "expected_benefits": "faster time-to-first-value, higher onboarding completion rates, and sticky active user retention",
                    "solution_approach": "designing frictionless user activation triggers and streamlining the core onboarding journey",
                    "drafted_dm": (
                        f"Hi {first_name},\n\n"
                        f"Looking at how {clean_comp} is scaling its product workflows, a key priority is streamlining user activation and onboarding flows to convert growing traffic into daily active users.\n\n"
                        f"Once this friction is resolved, the upside is immediate: faster time-to-first-value, higher onboarding completion rates, and turning casual signups into sticky active users without relying on manual handoffs.\n\n"
                        f"I can help tackle this from a product standpoint by designing frictionless user activation triggers, streamlining the core onboarding journey, and running rapid conversion experiments. I've built 15+ end-to-end projects from scratch with an obsessive focus on exceptional UX and solving practical real-world problems. At NoBrokerHood as an AI PM Intern, I worked cross-functionally across engineering, design, and growth to ship automated B2B features capturing 25+ extra qualified leads/month and revamped search discovery logic for 1.5x output coverage. (I'm a 4th-year IT student at DTU, 9.3 CGPA, 4th rank in NMG Labs Agentic AI Hackathon, and in fact, this entire outreach system was researched and delivered autonomously by a product engine I built).\n\n"
                        f"Would love to share a few actionable product ideas on a quick 10-12 min call this week if you're open to it. You can check my resume and a quick brief about me here: {self.resume_link}\n\n"
                        f"Let me know what time works best for you!"
                    )
                }
            else:  # Sales / Growth
                return {
                    "company_analysis": f"{clean_comp} expanding market presence and customer acquisition.",
                    "identified_pain_point": "Maintaining a consistent outbound pipeline of qualified clients without rising acquisition costs",
                    "expected_benefits": "higher lead-to-client conversion, reduced acquisition friction, and a predictable monthly revenue pipeline",
                    "solution_approach": "implementing high-tempo outreach pipelines and conversion cadences to accelerate client acquisition",
                    "drafted_dm": (
                        f"Hi {first_name},\n\n"
                        f"Looking at how {clean_comp} is expanding its market footprint, maintaining a consistent outbound pipeline of qualified clients without rising acquisition costs is a central growth driver.\n\n"
                        f"Solving this directly accelerates business velocity: higher lead-to-client conversion, reduced acquisition friction, and a predictable monthly revenue pipeline.\n\n"
                        f"I can help tackle this by implementing high-tempo outreach pipelines and conversion cadences to accelerate client acquisition. During my internship at NoBrokerHood, I executed outreach workflows that brought in 25+ extra qualified leads per month and accelerated deal closures through disciplined follow-ups. (I'm a 4th-year student at DTU, 9.3 CGPA).\n\n"
                        f"Would love to share a few actionable ideas on a quick 10-12 min call this week if you're open to it. You can check my resume and a quick brief about me here: {self.resume_link}\n\n"
                        f"Let me know what time works best for you!"
                    )
                }

        # ── 3. Startup Fallback: Classic Problem-First / Friction-Resolution Flow ──
        if pitch_track == "proptech_sales_tech":
            return {
                "company_analysis": f"{clean_comp} proptech platform and property operations.",
                "identified_pain_point": "Property inquiry response latency and buyer qualification drop-offs",
                "expected_benefits": "faster lead-to-viewing conversion, reduced sales response latency, and higher transaction velocity",
                "solution_approach": "automating lead qualification cadences and streamlining property listing workflows",
                "drafted_dm": (
                    f"Hi {first_name},\n\n"
                    f"Looking at how {clean_comp} is scaling property transactions, a critical operational friction in proptech is qualifying high-intent buyers and tenants before inquiries go cold.\n\n"
                    f"Once this friction is resolved, the upside is immediate: faster lead-to-viewing conversion, reduced sales response latency, and turning search traffic into high-intent transactional pipeline without expanding headcount.\n\n"
                    f"I can help tackle this from a sales and tech standpoint by automating high-intent buyer qualification cadences and streamlining property listing workflows. During my internship at NoBrokerHood (PropTech unicorn), I shipped automated B2B sales outreach workflows capturing 25+ extra qualified leads/month and optimized search discovery logic for 1.5x output coverage. (I'm a 4th-year IT student at DTU, 9.3 CGPA, and in fact, this entire outreach system was researched and delivered autonomously by a system I built).\n\n"
                    f"Would love to share a few actionable ideas on a quick 10-12 min call this week if you're open to it. You can check my resume and a quick brief about me here: {self.resume_link}\n\n"
                    f"Let me know what time works best for you!"
                )
            }
        elif pitch_track == "property_sales":
            return {
                "company_analysis": f"{clean_comp} residential property and real estate operations.",
                "identified_pain_point": "Capturing and converting high-intent property buyers before they engage competing brokers",
                "expected_benefits": "higher viewing-to-close ratios, shorter sales cycle durations, and a predictable monthly pipeline of qualified buyers",
                "solution_approach": "structuring disciplined outbound qualification cadences and rapid lead response workflows to ensure zero buyer leakage",
                "drafted_dm": (
                    f"Hi {first_name},\n\n"
                    f"Looking at {clean_comp}'s portfolio in residential and property sales, a persistent challenge in high-end real estate is capturing and converting high-intent buyers before they engage competing brokers.\n\n"
                    f"Solving this directly drives top-line revenue: higher viewing-to-close ratios, shorter sales cycle durations, and a predictable monthly pipeline of qualified buyers and tenants.\n\n"
                    f"I can help drive this from a sales standpoint by structuring disciplined outbound qualification cadences and rapid lead response workflows to ensure zero buyer leakage. During my internship at NoBrokerHood, I drove B2B sales outreach workflows capturing 25+ extra qualified leads per month and accelerated deal closure timelines through proactive pipeline follow-ups. (I'm a 4th-year student at DTU, 9.3 CGPA).\n\n"
                    f"Would love to share a few actionable ideas on a quick 10-12 min call this week if you're open to it. You can check my resume and a quick brief about me here: {self.resume_link}\n\n"
                    f"Let me know what time works best for you!"
                )
            }
        elif pitch_track == "non_tech_pm":
            return {
                "company_analysis": f"{clean_comp} customer product and digital operations.",
                "identified_pain_point": "Customer drop-off between product discovery and completed checkout/booking",
                "expected_benefits": "higher checkout completion rates, smoother customer onboarding, and fewer operational drop-offs",
                "solution_approach": "mapping customer conversion funnels, redesigning catalog discovery flows, and running targeted user experience experiments",
                "drafted_dm": (
                    f"Hi {first_name},\n\n"
                    f"Looking at how {clean_comp} is scaling its customer journey, a common friction point in consumer and operational platforms is drop-off between product discovery and completed checkout.\n\n"
                    f"Once this friction is resolved, the upside is immediate: higher checkout completion rates, smoother customer onboarding, and fewer operational drop-offs without requiring manual customer support interventions.\n\n"
                    f"I can help tackle this from a product standpoint by mapping user drop-off triggers, redesigning catalog discovery flows, and running targeted conversion experiments. I've built 15+ end-to-end projects from scratch with an obsessive focus on exceptional UX and solving practical real-world problems. During my internship at NoBrokerHood, I streamlined search and discovery flows to deliver 1.5x output coverage and shipped automated conversion features capturing 25+ extra qualified leads/month. (I'm a 4th-year student at DTU, 9.3 CGPA).\n\n"
                    f"Would love to share a few actionable ideas on a quick 10-12 min call this week if you're open to it. You can check my resume and a quick brief about me here: {self.resume_link}\n\n"
                    f"Let me know what time works best for you!"
                )
            }
        elif pitch_track == "tech_pm":
            return {
                "company_analysis": f"{clean_comp} software platform and user workflows.",
                "identified_pain_point": "User onboarding friction and activation drop-offs",
                "expected_benefits": "faster time-to-first-value, higher Day-30 user retention, and compounding product-led activation",
                "solution_approach": "designing streamlined activation triggers, instrumenting user journey telemetry, and running rapid conversion experiments",
                "drafted_dm": (
                    f"Hi {first_name},\n\n"
                    f"Looking at how {clean_comp} is scaling its core product workflows, a critical product bottleneck is user onboarding friction and activation drop-offs before users reach the core 'aha' moment.\n\n"
                    f"Once this friction is resolved, the upside is immediate: faster time-to-first-value, higher onboarding completion rates, and turning casual signups into sticky active users without relying on manual handoffs.\n\n"
                    f"I can help tackle this from a product standpoint by designing frictionless user activation triggers, streamlining the core onboarding journey, and running rapid conversion experiments. I've built 15+ end-to-end projects from scratch with an obsessive focus on exceptional UX and solving practical real-world problems. At NoBrokerHood as an AI PM Intern, I worked cross-functionally across engineering, design, and growth to ship automated B2B features capturing 25+ extra qualified leads/month and revamped search discovery logic for 1.5x output coverage. (I'm a 4th-year IT student at DTU, 9.3 CGPA, 4th rank in NMG Labs Agentic AI Hackathon, and in fact, this entire outreach system was researched and delivered autonomously by a product engine I built).\n\n"
                    f"Would love to share a few actionable product ideas on a quick 10-12 min call this week if you're open to it. You can check my resume and a quick brief about me here: {self.resume_link}\n\n"
                    f"Let me know what time works best for you!"
                )
            }
        elif pitch_track == "founders_office":
            return {
                "company_analysis": f"{clean_comp} early-stage product operations and growth.",
                "identified_pain_point": "Bandwidth constraints across product UX iteration, early customer pipeline, and day-to-day zero-to-one execution",
                "expected_benefits": "faster product iteration cycles, zero drop-off in early pipeline, and cross-functional operational velocity without adding headcount",
                "solution_approach": "plugging into the Founder's Office as a high-agency generalist across product UX, customer acquisition, and operational workflows",
                "drafted_dm": (
                    f"Hi {first_name},\n\n"
                    f"Looking at how {clean_comp} is scaling its zero-to-one operations, a recurring challenge for early founding teams is balancing high-level strategy with day-to-day execution across product UX, customer acquisition, and operational fires.\n\n"
                    f"Having dedicated Founder's Office execution directly frees up leadership bandwidth: faster product UX iteration cycles, zero lead drop-off in early customer pipelines, and agile execution across cross-functional priorities without adding bulky headcount.\n\n"
                    f"I can plug into the Founder's Office as a high-agency generalist to tackle product user experience, customer outreach pipelines, or operational workflows. I've built 15+ end-to-end projects from scratch with an obsessive focus on exceptional UX and solving practical real-world problems. During my internship at NoBrokerHood, I shipped automated B2B sales outreach capturing 25+ extra leads/month and optimized discovery logic for 1.5x output coverage. (I'm a 4th-year student at DTU, 9.3 CGPA).\n\n"
                    f"Would love to share a few actionable ideas on a quick 10-12 min call this week if you're open to it. You can check my resume and a quick brief about me here: {self.resume_link}\n\n"
                    f"Let me know what time works best for you!"
                )
            }
        else:  # non_tech_growth_sales
            if geo_segment == "india":
                p3 = (
                    f"I can help tackle this from a sales standpoint by implementing high-tempo outbound outreach cadences and structured prospect follow-ups. "
                    f"During my internship at NoBrokerHood, I executed outreach workflows that brought in 25+ extra qualified leads per month. "
                    f"Additionally, I've personally driven corporate sponsorships for our college society fest, closing ₹3–10 Lakh deals each year through disciplined cold outbound pitching and deal negotiations. "
                    f"(I'm a 4th-year student at DTU, 9.3 CGPA)."
                )
            else:
                p3 = (
                    f"I can help tackle this by implementing high-tempo outreach pipelines and conversion cadences to accelerate client acquisition. "
                    f"During my internship at NoBrokerHood, I executed outreach workflows that brought in 25+ extra qualified leads per month and streamlined pipeline conversions. "
                    f"(I'm a 4th-year student at DTU, 9.3 CGPA)."
                )
            return {
                "company_analysis": f"{clean_comp} business operations and customer growth.",
                "identified_pain_point": "Maintaining a predictable outbound pipeline of qualified clients without high customer acquisition costs",
                "expected_benefits": "higher lead-to-client conversion, reduced acquisition friction, and a predictable monthly revenue pipeline",
                "solution_approach": "implementing high-tempo outreach pipelines and conversion cadences to accelerate client acquisition",
                "drafted_dm": (
                    f"Hi {first_name},\n\n"
                    f"Looking at how {clean_comp} is expanding its market presence, a central growth hurdle is maintaining a consistent outbound pipeline of qualified clients without high customer acquisition costs.\n\n"
                    f"Solving this directly accelerates business velocity: higher lead-to-client conversion, reduced acquisition friction, and a predictable monthly revenue pipeline.\n\n"
                    f"{p3}\n\n"
                    f"Would love to share a few actionable ideas on a quick 10-12 min call this week if you're open to it. You can check my resume and a quick brief about me here: {self.resume_link}\n\n"
                    f"Let me know what time works best for you!"
                )
            }

    def draft_single_lead(
        self,
        lead: dict,
        profile: Optional[dict] = None,
        top_card_text: str = "",
        scraped_experience: str = ""
    ) -> dict:
        """
        Dedicated 1-by-1 deep-dive company analysis & DM drafting for a verified lead.
        Enforces multi-track positioning (Proptech, Property Sales, Tech PM, Non-Tech PM, Non-Tech Growth, Founder's Office)
        and tier-specific messaging (Enterprise/Big Giants, Mid-Level/Scale-ups, Startups)
        with strict AI disclosure, Hackathon, and college fest rules.
        """
        name = lead.get("name", "Unknown")
        company = lead.get("company", "Unknown")
        role = lead.get("role", "Unknown")

        # Clean first name and company name
        first_name = clean_first_name(name)
        clean_comp = clean_company_name(company)

        # Resolve geo segment, pitch track, and company tier
        geo_segment = self._infer_geo_segment(lead)
        lead["geo_segment"] = geo_segment

        pitch_track = self._infer_pitch_track(lead, top_card_text, scraped_experience)
        lead["pitch_track"] = pitch_track
        track_cfg = TRACK_CONFIGS.get(pitch_track, TRACK_CONFIGS["tech_pm"])

        company_tier = self._infer_company_tier(lead)
        lead["company_tier"] = company_tier
        tier_cfg = TIER_INSTRUCTIONS.get(company_tier, TIER_INSTRUCTIONS["startup"])

        tier_messaging_instruction = tier_cfg["guidance"].format(clean_company=clean_comp)

        # Select p1, p2, p3 instructions based on tier:
        # If enterprise or mid_level provides tier-specific styles, use them; otherwise use track_cfg defaults
        p1_inst = tier_cfg["p1_style"].format(first_name=first_name, clean_company=clean_comp) if tier_cfg.get("p1_style") else track_cfg["p1_instruction"].format(clean_company=clean_comp)
        p2_inst = tier_cfg["p2_style"].format(clean_company=clean_comp) if tier_cfg.get("p2_style") else track_cfg["p2_instruction"]
        p3_inst = tier_cfg["p3_style"].format(clean_company=clean_comp) if tier_cfg.get("p3_style") else track_cfg["p3_instruction"]

        prompt = DEEP_DIVE_RESEARCH_PROMPT.format(
            track_title=track_cfg["title"],
            role_pitch=track_cfg["role_pitch"],
            geo_segment=geo_segment.upper(),
            company_tier=company_tier.upper(),
            background_summary=track_cfg["background_summary"],
            focus_instruction=track_cfg["focus_instruction"],
            tier_messaging_instruction=tier_messaging_instruction,
            p1_instruction=p1_inst,
            p2_instruction=p2_inst,
            p3_instruction=p3_inst,
            lead_name=name,
            first_name=first_name,
            lead_company=company,
            clean_company=clean_comp,
            lead_role=role,
            top_card_text=top_card_text.strip() if top_card_text else "Not available",
            scraped_experience=scraped_experience.strip() if scraped_experience else "Not available",
            resume_link=self.resume_link,
        )

        data = {}
        try:
            from utils.gemini_client import generate_with_rotation
            model_name = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
            resp_text = generate_with_rotation(prompt, model=model_name)

            match = re.search(r"```json\s*([\s\S]+?)\s*```", resp_text)
            if match:
                data = json.loads(match.group(1))
            else:
                data = json.loads(resp_text.strip())
        except Exception as e:
            console.print(f"  [yellow]  ⚠ Gemini drafting error for {name} ({pitch_track} - {company_tier}): {e}. Using track-specific fallback.[/yellow]")
            data = self._generate_fallback(pitch_track, first_name, clean_comp, geo_segment=geo_segment, company_tier=company_tier)

        dm = data.get("drafted_dm", "")

        # ── Deterministic Safety Guard: Scrub AI Disclosure & Hackathon mentions if forbidden ──
        if not track_cfg["ai_disclosure_allowed"]:
            dm = re.sub(r'[^.\n]*?\b(?:in fact,?\s*this entire outreach|outreach system was researched|delivered autonomously|autonomous|bot|ai system|ai engine)[^.\n]*?(?:\.|\n|$)', '', dm, flags=re.IGNORECASE)

        if not track_cfg["hackathon_allowed"]:
            dm = re.sub(r'[^.\n]*?\b(?:hackathon|agentic ai)[^.\n]*?(?:\.|\n|$)', '', dm, flags=re.IGNORECASE)

        # ── Deterministic Safety Guard: College fest deals ONLY for Indian sales companies ──
        # Strictly scrub from PropTech, Property, Tech PM, Non-Tech PM, and any foreign leads
        is_indian_sales = (pitch_track in ("non_tech_growth_sales", "founders_office")) and (geo_segment == "india")
        if not is_indian_sales or pitch_track in ("proptech_sales_tech", "property_sales", "tech_pm", "non_tech_pm"):
            dm = re.sub(r'[^.\n]*?\b(?:college\s+society|society\s+fest|\bfest\b|corporate\s+sponsorships?|lakh\s+deals?|3[-–to\s]+10\s*lakh|₹?\s*3\s*[-–to]\s*10\s*lakh)[^.\n]*?(?:\.|\n|$)', '', dm, flags=re.IGNORECASE)

        # Cleanup formatting artifacts: double horizontal spaces, dangling commas/parentheses
        dm = re.sub(r'\(\s*,?\s*\)', '', dm)
        dm = re.sub(r',\s*\.', '.', dm)
        dm = re.sub(r'[^\S\r\n]{2,}', ' ', dm)
        dm = re.sub(r'\r\n', '\n', dm)
        dm = re.sub(r'\n{3,}', '\n\n', dm)

        # Deterministic enforcement: ensure exact resume_link
        if self.resume_link and self.resume_link != "[ADD_YOUR_RESUME_LINK_HERE]":
            dm = re.sub(r'https?://drive\.google\.com/drive/folders/[a-zA-Z0-9_-]+', self.resume_link, dm)

        data["drafted_dm"] = dm.strip()

        lead["connection_note"] = ""
        lead["note_length"] = 0
        lead["pitch_track"] = pitch_track
        lead["geo_segment"] = geo_segment
        lead["company_tier"] = company_tier
        lead["drafted_dm"] = data["drafted_dm"]
        lead["company_analysis"] = data.get("company_analysis", "")
        lead["identified_pain_point"] = data.get("identified_pain_point", "")
        lead["expected_benefits"] = data.get("expected_benefits", "")
        lead["solution_approach"] = data.get("solution_approach", "")
        lead["grounded_vision"] = data.get("expected_benefits", "")

        return data

    def run(self, leads: list, profile: dict, dry_run: bool = False) -> list:
        console.print("\n[bold cyan]━━━ Phase 2: Ghostwriter (1-by-1 Deep Dive) ━━━[/bold cyan]")
        instructions = self._load_instructions()
        console.print(f"[cyan]Prompt instructions v{instructions.get('version', 1)}[/cyan]")

        if not leads:
            return []

        # Check for leads that already have a drafted DM
        already_drafted = [l for l in leads if l.get("drafted_dm")]
        needs_drafting = [l for l in leads if not l.get("drafted_dm")]

        if not needs_drafting:
            console.print(f"[green]✓ All {len(leads)} leads already have drafted DMs. Skipping Gemini drafting.[/green]")
            return leads

        if already_drafted:
            console.print(f"[cyan]ℹ {len(already_drafted)}/{len(leads)} leads already have drafted DMs. Drafting remaining {len(needs_drafting)} leads 1-by-1...[/cyan]")
        else:
            console.print(f"[cyan]Drafting {len(needs_drafting)} leads 1-by-1 with deep-dive company & bottleneck audit...[/cyan]")

        enriched = []
        for idx, lead in enumerate(leads, 1):
            name = lead.get("name") or "Unknown"
            if lead.get("drafted_dm"):
                lead.setdefault("connection_note", "")
                lead.setdefault("note_length", 0)
                lead.setdefault("status", "queued")
                enriched.append(lead)
                continue

            console.print(f"  [{idx}/{len(leads)}] [cyan]▶ Deep-dive research for {name} @ {lead.get('company', '?')}...[/cyan]")
            self.draft_single_lead(lead, profile)
            pain_point = lead.get("identified_pain_point") or "Operational growth"
            console.print(f"  [green]  ✓ DM drafted targeting: {pain_point}[/green]")

            lead["status"] = "queued"
            enriched.append(lead)

            if dry_run:
                console.print(Panel(
                    f"[bold]{name}[/bold] @ {lead.get('company', '?')}\n\n"
                    f"[bold yellow]Identified Bottleneck:[/bold yellow] {lead.get('identified_pain_point')}\n"
                    f"[bold yellow]Expected Benefits:[/bold yellow] {lead.get('expected_benefits')}\n"
                    f"[bold yellow]Solution Approach:[/bold yellow] {lead.get('solution_approach')}\n"
                    f"[bold yellow]Company Analysis:[/bold yellow] {lead.get('company_analysis')}\n\n"
                    f"[bold yellow]DM:[/bold yellow]\n[green]{lead.get('drafted_dm')}[/green]",
                    title=f"Draft #{len(enriched)}", border_style="blue",
                ))

        console.print(f"[green]✓ Drafted {len(enriched)}/{len(leads)} DMs[/green]")

        # Persist enriched leads
        if LEADS_PATH.exists() or enriched:
            DATA_DIR.mkdir(exist_ok=True)
            existing = {}
            if LEADS_PATH.exists():
                try:
                    with open(LEADS_PATH, encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    pass
            existing["leads"] = enriched
            with open(LEADS_PATH, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)

        return enriched
