# Space-Pulse 🚀
**Deep-Space Astronaut Health Monitoring Web App**
*NASA Space Apps Challenge 2026 — Challenge: Health Monitoring Software for Astronauts on Space Missions*

Space-Pulse is a deep-space astronaut health monitoring HUD and diagnostic web application designed for long-duration transit missions (e.g. Earth-Mars Transfer, 540-day mission cycle). It provides real-time bio-telemetry visualization, threshold and trend detection, NASA Human Research Program (HRP) 4-domain spaceflight risk matrix evaluations, daily check-in evaluations, historical analysis with Chart.js, personal baselines management, ground support comm packets, and signal alerts.

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
   - Dynamic 0–100 Health Score composite radial ring with Physio Reserve and Cumulative Stress metrics.
   - Dynamic natural-language trend detection headline (e.g. *"Mostly nominal. Sleep Duration has dropped four days in a row."*).
   - 7 Vital & Environmental Indicator cards with SVG sparklines calculated from the last 7 readings.
   - Interactive Chart.js Sleep Trend line chart with baseline dashed line, shaded caution band, and 7d/30d/mission cycle range toggles.
   - NASA HRP 4-Domain Spaceflight Risk Matrix (Altered Gravity, Isolation, Closed Environment, and Radiation prototype indicator).
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

### 6. Configuration (optional)
Copy `.env.example` to `.env` to change settings. Today there is one:

| Variable | Default | Meaning |
|---|---|---|
| `DEMO_MODE` | `False` | `True` turns on the crew switcher, the login preset buttons and the shared demo password. Leave it off for anything that is not a demo. |

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

## Disclaimer
*Prototype for demonstration purposes. Not a medical device.*
