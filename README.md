# Space-Pulse 🚀
**Deep-Space Astronaut Health Monitoring Web App**
*NASA Space Apps Challenge 2026 — Challenge: Health Monitoring Software for Astronauts on Space Missions*

Space-Pulse is a deep-space astronaut health monitoring HUD and diagnostic web application designed for long-duration transit missions (e.g. Earth-Mars Transfer, 540-day mission cycle). It provides real-time bio-telemetry visualization, threshold and trend detection, NASA Human Research Program (HRP) 5-hazard spaceflight risk matrix evaluations, daily check-in evaluations, historical analysis with Chart.js, personal baselines management, ground support comm packets, and signal alerts.

---

## Tech Stack
- **Backend & Frontend:** Python + Django (Django templates, template inheritance)
- **Styling:** Tailwind CSS (Deep-space HUD theme), HTML5, Vanilla JavaScript
- **Fonts & Icons:** Space Grotesk, Inter, Google Material Symbols Outlined
- **Charts:** Chart.js
- **Database:** SQLite
- **Demo Data:** Faker + deterministic seeding (100% reproducible)

---

## Project Structure
- `spacepulse/`: Project configuration, root settings, URL router, WSGI/ASGI handlers.
- `core/`: Astronaut profiles, personal baselines, ground support messages, HUD context processors, and clinical business logic (`services.py`).
- `telemetry/`: Biometric and habitat readings (`Reading`), daily self-reported logs (`CheckIn`), and JSON chart telemetry endpoints (`/api/readings/`).
- `alerts/`: Diagnostic alert engine (`Alert`) with severity levels (Nominal, Caution, Critical), flight surgeon sync, and diagnostic pod details.
- `environment/`: Habitat condition monitoring, simulated planetary environments, NASA DONKI event context, and environment alerts.
- `design/`: Approved UI designs and layout references (`dashboard.html`, `login.html`, `daily_checkin.html`, `alert_details.html`).
- `templates/`: Django template hierarchy (`base.html`, pages, navigation, modals).
- `static/`: Static assets, stylesheets, scripts.

---

## Complete Pages & Capabilities

1. **Cosmic Glassmorphic Login (`/login/`)**:
   - Every other page needs a login; anonymous visitors are sent here first.
   - Preset simulation credentials for 1-click astronaut cockpit access (only when `DEMO_MODE=True`).
   - Preserves destination routing via `?next=<path>` URL parameter.
   - Default crew login: `amara.reyes@mission.example` / `validAccess2026`.

2. **Deep-Space HUD Dashboard (`/`)**:
   - Matches approved `design/dashboard.html` HUD interface.
   - Dynamic 0–100 Health Score (from the 7 Science Lead indicators) plus labelled experimental Physio Reserve and Cumulative Stress tiles.
   - Dynamic natural-language trend detection headline (e.g. *"Mostly nominal. Sleep Duration has dropped four days in a row."*).
   - 7 Vital & Environmental Indicator cards with SVG sparklines calculated from the last 7 readings.
   - Interactive Chart.js Sleep Trend line chart with baseline dashed line, shaded caution band, and 7d/30d/mission cycle range toggles.
   - NASA HRP 5-hazard Spaceflight Risk Matrix (Altered Gravity, Isolation, Closed Environment, Radiation prototype indicator, and Distance from Earth: whether any alert calls for ground contact, with NASA's up-to-22-minute one-way comm delay).
   - Live Mars transit progress indicator (Sol 142 of 540 / 26.3% transit elapsed).
   - Modal dialog for logging ad-hoc manual telemetry records.

3. **Daily Self-Reported Check-In (`/check-in/`)**:
   - Matches approved questionnaire layout `design/daily_checkin.html`.
   - Mood scale (1-5), Stress rating (1-10), Fatigue evaluation, Sleep duration stepper & quality pills, Physical countermeasure exercise minutes.
   - Symptom selector chips and operational notes field.
   - Sensor vital synchronization (Resting HR, HRV, Body Temperature).
   - Form submission automatically synchronizes to SQLite (`CheckIn` and `Reading` tables) and re-evaluates clinical alert triggers.

4. **Telemetry History & Longitudinal Trends (`/history/`)**:
   - Multi-metric selector across all 7 metrics (Sleep, Resting HR, HRV, Exercise, Body Temp, Mood, Cabin CO2).
   - Time-range filters (7 Days, 30 Days, Mission Cycle).
   - Full-width Chart.js telemetry waveform with baseline dashed guides and caution threshold indicators.
   - Statistical KPI summary cards (Latest value, Baseline deviation $\pm\Delta$, Window Mean, Min/Max span).
   - Reverse-chronological historical reading log table with 1-click browser CSV export.

5. **Profile & Calibrated Baselines (`/profile/`)**:
   - Astronaut bio-identity dossier (Rank, Name, Habitat, Role, Email, Mission transit progress bar).
   - Crew roster switcher to view any of the 4 seeded crew members (only when `DEMO_MODE=True`; otherwise you only see your own astronaut).
   - Baseline form for all 7 metrics with clinical rationales and reference ranges. Baselines are **computed** from mission days 1-14 by default. Typing a different value saves a labelled **manual override** (who and when are recorded), values outside a plausible range are refused, and "Use computed" removes the override.
   - Automatic SQLite persistence and dynamic alert recalibration upon saving.

6. **Ground Support Communications (`/ground-support/`)**:
   - Deep Space Network (DSN) node status (Goldstone DSS-14 70m, X/Ka Band 8.4 GHz, 99.8% signal integrity).
   - One-Way Light Time (OWLT) latency display (`18m 42s`) and round-trip delay (`37m 24s`).
   - DSN ground pass countdown (`T-01:14:22`) and upcoming pass schedule table.
   - Interactive transcript feed with filter tabs (All, Flight Surgeon, Incoming, Crew Uplinks).
   - Interactive "Draft Uplink Transmission" form allowing astronauts to compose and queue messages for the next DSN pass.

7. **Active Alerts & Diagnostics Hub (`/alerts/`)**:
   - Complete alarm log with severity filters (Critical, Caution, Nominal) and status filters (Active vs Acknowledged).
   - KPI counters for total alarms, critical flags, caution warnings, and unacknowledged actions.
   - Inline "Acknowledge" actions and batch "Acknowledge All" capability.
   - Link to individual diagnostic pod detail pages.

8. **Diagnostic Pod & Clinical Remediation (`/alerts/<id>/`)**:
   - Matches approved design `design/alert_details.html`.
   - Clinical telemetry analysis: what changed, why it matters, cognitive impact, and physical indicators.
   - Interactive action checklist with counter and status verification.
   - Chart.js 7-day mini trend waveform with baseline reference and caution threshold line.
   - Escalation protocol matrix and dismiss/acknowledge confirmation modal.

9. **Environment Monitor (`/environment/`)**:
   - Tracks habitat conditions and displays NASA DONKI event records for context.
   - Radiation values are prototype model estimates, not direct dosimeter readings.
   - CME and solar-particle events remain unclassified pending mission-approved criteria; fallback events are labeled sample data and do not generate alerts or radiation boosts.
   - Refresh environment space-weather records with `python manage.py fetch_environment`.

---

## Local Setup & Quick Start

### 1. Prerequisites
- Python 3.10+ (Python 3.13 tested)
- Git

### 2. Setup Virtual Environment
```bash
# Clone the repository
cd astronaut-health-monitor

# Create virtual environment
python -m venv venv

# Activate virtual environment
# On Windows (PowerShell):
.\venv\Scripts\Activate.ps1
# On macOS / Linux:
source venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Apply Database Migrations
```bash
python manage.py migrate
```

### 5. Seed Reproducible Demo Telemetry
```bash
python manage.py seed_demo
```
This populates:
- **1 Commander (Cmdr. Amara Reyes)** as active HUD profile + 3 crew members (Dr. Leo Vance, Lt. Maya Chen, Dr. Alex Mercer).
- **142 days** of biometric and habitat telemetry (4,544 readings and 568 daily check-ins).
- Reproducible decline in Commander Reyes's sleep duration over the last 4 days (down to 5.8 h), triggering the Caution trend alert.
- Ground support communication packets with Houston CapCom and Flight Surgeon GND-SURG-2.
- Active and historical signals.
- Every seeded reading is labelled `simulated`, and pages show a "SIMULATED DATA" banner while simulated readings are displayed. Reading sources are `manual` (check-in or typed entry), `csv`, `simulated` and `dataset`.
- A login for each astronaut (e.g. `amara.reyes@mission.example` / `validAccess2026`). A login is linked to its astronaut by the same email.
- Environmental fallback event examples are marked `SAMPLE DATA`; they are not NASA observations or operational alerts.

### 6. Configuration (optional)
Copy `.env.example` to `.env` to change settings. Settings:

| Variable | Default | Meaning |
|---|---|---|
| `DEMO_MODE` | `False` | `True` turns on the crew switcher, the login preset buttons and the shared demo password. Leave it off for anything that is not a demo. |
| `DEBUG` | `False` | Set `DEBUG=True` in your local `.env` for development. Leave it unset on a deploy. |
| `SECRET_KEY` | built-in dev key | Set a long random value on a deploy. |
| `ALLOWED_HOSTS` | `*` | Comma-separated host names. |
| `CSRF_TRUSTED_ORIGINS` | `https://*.onrender.com` | Comma-separated origins allowed to POST over https. |
| `COMPANION_LLM_RATE_LIMIT` | `20` | Max live LLM calls per logged-in user in one window. Extra calls use the rule-based answers. |
| `COMPANION_LLM_RATE_WINDOW` | `3600` | Rate-limit window in seconds (default one hour). Cache hits do not count. |

### Public demo for judges (`/demo/`)
Space Apps requires the demo link to open without a login. With `PUBLIC_DEMO=True`, `GET /demo/` signs the visitor in as a fresh throwaway demo user (no password) that sees the seeded commander's simulated data. Demo visitors can open every page and use Pulse, but every other write (check-in, add reading, CSV upload, acknowledge alert, baseline edits, Pulse memory notes) is refused with a "read-only demo" notice. All demo visitors share one live-model budget (`PUBLIC_DEMO_LLM_RATE_LIMIT` per hour). With `PUBLIC_DEMO` off, `/demo/` returns 404. On Render the SQLite database is rebuilt by `seed_demo` on each deploy.

### Deploying to Render
- Build Command: `pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate && python manage.py seed_demo`
- Start Command: `gunicorn spacepulse.wsgi:application`
- Environment: `DEBUG=False`, `SECRET_KEY` (long random value), `DEMO_MODE=True` for a demo, and the AI keys if you use them.
- SQLite is wiped on every deploy and `seed_demo` rebuilds the demo data each time, so this is for demos only.
### 7. Run Test Suite
```bash
python manage.py test
```
Runs all automated tests (rules, baselines, provenance, login, seeding, telemetry, alerts and views). It takes a couple of minutes because some tests seed the full demo data into a temporary test database; `db.sqlite3` is not touched.

### 8. Run Local Development Server
```bash
python manage.py runserver
```
Open [http://127.0.0.1:8000/](http://127.0.0.1:8000/) in your web browser.

---

## Data sources & citations

A public provenance page at [`/sources/`](/sources/) lists every indicator, rule, citation and disclaimer in one place.

- **NASA DONKI** (Space Weather Database Of Notifications, Knowledge, Information): live space-weather events are requested from [api.nasa.gov](https://api.nasa.gov). The DONKI catalogue is also published by CCMC at [kauai.ccmc.gsfc.nasa.gov/DONKI](https://kauai.ccmc.gsfc.nasa.gov/DONKI/) and [ccmc.gsfc.nasa.gov/tools/DONKI](https://ccmc.gsfc.nasa.gov/tools/DONKI/).
- **NASA HRP hazards** ([five hazards](https://www.nasa.gov/hrp/hazards/), [distance from Earth](https://www.nasa.gov/hrp/hazard-distance-from-earth/), [Mars comm delay paper](https://www.nasa.gov/wp-content/uploads/2024/01/mars-communications-disruption-and-delay.pdf)): the hazard grouping and the comm-delay figure on the Distance from Earth tile.
- **NASA Open Science Data Repository (OSDR)** ([osdr.nasa.gov](https://osdr.nasa.gov)): cited as a research reference only. The demo does not load crew telemetry from OSDR.
- **NOAA Space Weather Scales** ([swpc.noaa.gov/noaa-scales-explanation](https://www.swpc.noaa.gov/noaa-scales-explanation)): used to map DONKI geomagnetic-storm Kp values to the NOAA G-scale (G1–G5).

Crew biometric and habitat data in the demo is **simulated** and labelled as such (reading source `simulated`, plus a "SIMULATED DATA" banner while simulated readings are shown). Space weather is **context only** and is not part of the health score. The exercise plan of 150 minutes per week is a **team assumption**, not a Science Lead rule. The app makes **no medical diagnosis** and is not a medical device.

## Use of AI

The Pulse Companion's answers are **rule-based** (`core/companion.py`). An optional LLM layer (default provider Groq, model `openai/gpt-oss-20b`, with Gemini `gemini-3.5-flash-lite` as fallback) may **rephrase** those rule-based answers, **classify** a typed question's intent by choosing from a fixed list, and, in Pulse chat only, write short **small-talk replies** (greetings, everyday conversation). The model never writes health facts or the underlying answers: small-talk replies are rejected if they contain numbers, diagnosis-style words or claims of human feelings (a fixed reply is used instead), are labelled "Written by AI", and urgent messages never reach the model (see **Pulse chat** below). A safety validator rejects any rewrite that adds numbers or diagnosis-style words that were not in the original text. The LLM is off unless `COMPANION_LLM_ENABLED=True`.
Live LLM calls are limited per logged-in user by `COMPANION_LLM_RATE_LIMIT` (default 20) in each `COMPANION_LLM_RATE_WINDOW` seconds (default 3600). Over the limit, or when the caller is anonymous, the rule-based answers are used and the model is not called.

Development was AI-assisted: Grok Bot (an AI coding assistant) helped write backend code, tests and documentation. Every change was reviewed, tested and committed by a human team member. No AI-generated images or video are used. The app is hosted on Render, which is not an AI tool.

## License

This project is released under the MIT License. See `LICENSE`.

## Disclaimer
*Prototype for demonstration purposes. Not a medical device.*

## Pulse chat

The dashboard has a **Chat with Pulse** panel under the Pulse Companion section. Text is primary, with optional voice (below). Pulse is an AI companion and says so; it is not a medical device and does not diagnose.

- **What it does.** Health and environment questions ("How am I doing?", "Explain my sleep trend", "Review the cabin environment") use the same routing and the same rule-based answers as Ask Pulse (`core/companion.py`), built from your own data. Sources stay labelled (simulated, self-reported, dataset, CSV), and "no data" or "old data" is shown as such, never as normal. "Start a check-in" links to the existing daily check-in page. Ordinary conversation (hello, "I feel lonely", "tell me something interesting") gets a short, warm reply.
- **Safeguards.**
  - Urgent phrases (for example chest pain or "I can't breathe") and any critical answer get a short fixed message that points to your flight surgeon through Ground Support and the Alerts page, with no small talk first.
  - The model never writes health facts. With `COMPANION_LLM_ENABLED=True` it may write only the plain small-talk replies, under a short persona prompt (honest that it is an AI, no medical facts, numbers or diagnoses, encourages contact with crew and Ground Support). Each model reply must pass a safety check (no numbers, no diagnosis words, no claims of human feelings, short) or a fixed reply is used instead. Model-written replies are labelled "Written by AI (provider)".
  - The model never receives readings, alerts, health answers or other users' data. It only gets your message, your saved notes and the last few turns of ordinary conversation.
  - With the model off or offline, every feature still works with fixed replies.
- **Memory controls.** Pulse remembers only what you explicitly ask it to ("remember that ..." in the chat, or the Remember box under **What Pulse remembers**). You can see every note and delete it at any time. Notes are private to your login (at most 20, 200 characters each) and are used only as context for ordinary conversation.
- **Voice (optional).** A **Talk** button turns speech into a message with the browser's Web Speech API, and **Speak Pulse's replies aloud** reads replies with the browser's on-device text-to-speech. With both on, Pulse listens again after each spoken reply, so you can hold a voice conversation; it stops when you press Stop, stay silent, or get an urgent reply. The transcript goes through the same `send` endpoint and the same safeguards as typed text, and SPACE-PULSE never receives or stores audio. Speech-to-text works in Chrome and Edge, which send audio to the browser maker's speech service (disclosed in the panel and on `/sources/`); in other browsers the button is hidden and text works as usual. No new dependency or API key.
- **Privacy.** Conversations and notes are stored per user and per astronaut; another user's conversation or note answers 404. Message text is never logged.
- **Endpoints** (login required, JSON, CSRF): `POST /companion/chat/send/`, `GET /companion/chat/history/`, `POST /companion/chat/new/`, `GET /companion/chat/memories/`, `POST /companion/chat/memories/add/`, `POST /companion/chat/memories/<id>/delete/`. The code lives in the `companion` app.
- **Use of AI.** In addition to the "Use of AI" section above, the optional model can write short small-talk replies for Pulse chat, as described here. The Pulse avatar is a small hand-written SVG (no generated image, no NASA logos). This feature was written with Grok Bot, an AI coding assistant, and reviewed by a human team member.
