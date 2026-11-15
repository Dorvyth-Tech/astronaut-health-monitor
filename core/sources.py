"""Provenance catalog for the public /sources/ page.

Every string shown to judges is pulled from existing code constants, model
labels, or a rule function's own docstring, so the page cannot drift from
the rules engine. This module does not invent thresholds or diagnoses.
"""

from __future__ import annotations

import inspect

from core import companion, donki, llm, rules
from core.constants import (
    EXERCISE_WEEKLY_PLAN_MINUTES,
    METRIC_CHOICES,
    METRIC_CONFIG,
    METRICS,
)
from core.services import (
    BIO_INDEX_HELP,
    COMM_DELAY_NOTE,
    CUMULATIVE_STRESS_HELP,
    EXPERIMENTAL_TAG,
    NASA_DISTANCE_HAZARD_URL,
    NASA_HRP_HAZARDS_URL,
    NASA_MARS_COMMS_DELAY_URL,
    PHYSIO_RESERVE_HELP,
)
from telemetry.models import Reading

# Wording from the Science Lead deliverable cover note, tightened to one citation line.
SCIENCE_LEAD_CITATION = (
    "Science Lead deliverable (demo values from published spaceflight literature; "
    "estimates marked)"
)

# README disclaimer, kept here so the page and the README can stay in step.
PROTOTYPE_DISCLAIMER = "Prototype for demonstration purposes. Not a medical device."

OSDR_URL = "https://osdr.nasa.gov"
OSDR_NOTE = (
    "Cited as a research reference only. The demo does not load crew telemetry from OSDR."
)

# How each of the 7 indicators actually enters the app today (verified in
# telemetry/views.py check-in and add_reading, telemetry/csv_import.py, and
# core/management/commands/seed_demo.py). The pain and new-symptom branches
# exist in core/rules.py but are not passed in by core/services.py yet.
INDICATOR_INPUTS = {
    "resting_hr": (
        "Optional field on the daily check-in, a typed reading, a CSV upload, "
        "or the simulated demo generator. Stored as a Reading."
    ),
    "hrv": (
        "Optional field on the daily check-in, a typed reading, a CSV upload, "
        "or the simulated demo generator. Stored as a Reading."
    ),
    "exercise": (
        "Daily minutes from the check-in, a typed reading, a CSV upload, or the "
        "simulated demo generator. The rule totals completed weeks and compares "
        "them to the weekly plan."
    ),
    "body_temp": (
        "Optional field on the daily check-in, a typed reading, a CSV upload, "
        "or the simulated demo generator. Stored as a Reading."
    ),
    "sleep": (
        "Daily check-in, a typed reading, a CSV upload, or the simulated demo "
        "generator. Stored as a Reading."
    ),
    "mood": (
        "Daily check-in (score 1-5), also stored as a Reading. May also arrive "
        "via a typed reading, a CSV upload, or the simulated demo generator."
    ),
    "cabin_co2": (
        "Not on the daily check-in form. A typed reading, a CSV upload, or the "
        "simulated demo generator. Stored as a Reading."
    ),
}

UNWIRED_RULE_INPUTS_NOTE = (
    "The exercise rule also describes pain scores, and the body-temperature rule "
    "describes newly logged symptoms. Those branches are defined in core/rules.py "
    "but are not yet passed in from live check-ins (symptoms are stored in "
    "check-in notes only)."
)

# Metric keys that have a Science Lead rule. Order matches core.rules.INDICATORS.
_LABEL_BY_KEY = dict(METRIC_CHOICES)


def rule_band_text(indicator):
    """Amber and red summaries, taken word for word from the rule function docstring.

    Each rule in core/rules.py documents its bands as 'Amber:' and 'Red:' lines.
    Wrapped continuation lines (the sleep red band) are joined with a space.
    """
    if indicator not in rules.RULES:
        raise ValueError(f"Unknown indicator: {indicator!r}")
    doc = inspect.getdoc(rules.RULES[indicator]) or ""
    bands = {"amber": [], "red": []}
    current = None
    for raw_line in doc.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                current = None
            continue
        lower = line.lower()
        if lower.startswith("amber:"):
            current = "amber"
            bands["amber"] = [line.split(":", 1)[1].strip()]
            continue
        if lower.startswith("red:"):
            current = "red"
            bands["red"] = [line.split(":", 1)[1].strip()]
            continue
        # Join a wrapped continuation only when the previous line is unfinished
        # (does not end with '.') or this line is a parenthetical wrap. This
        # keeps later docstring notes such as extras["new_symptom_days"] out.
        if current and bands[current]:
            previous = bands[current][-1]
            if (not previous.endswith(".")) or line.startswith("("):
                bands[current].append(line)
                continue
        current = None
    return {
        "amber": " ".join(bands["amber"]).strip(),
        "red": " ".join(bands["red"]).strip(),
    }


def reading_source_labels():
    """The four Reading.source values, using the model's own display labels."""
    return [{"key": key, "label": label} for key, label in Reading.SOURCE_CHOICES]


def g_scale_mapping():
    """NOAA G-scale lines built from donki.G_NAMES (Kp 5 -> G1, ... Kp 9 -> G5)."""
    lines = []
    for level, name in sorted(donki.G_NAMES.items()):
        kp = level + 4
        lines.append(f"Kp {kp} = G{level} {name}")
    return lines


def indicator_catalog():
    """One dict per Science Lead indicator, for the sources template."""
    items = []
    for key in rules.INDICATORS:
        config = METRIC_CONFIG[key]
        bands = rule_band_text(key)
        unit = config["unit"]
        # Exercise is logged per day (min/d) but judged per week against the plan.
        if key == "exercise":
            unit = f"{unit} (rule uses minutes/week vs a {EXERCISE_WEEKLY_PLAN_MINUTES:g} min plan)"
        items.append({
            "key": key,
            "name": _LABEL_BY_KEY[key],
            "unit": unit,
            "input_source": INDICATOR_INPUTS[key],
            "amber": bands["amber"],
            "red": bands["red"],
            "citation": SCIENCE_LEAD_CITATION,
        })
    return items


def experimental_tiles():
    """HUD tiles that are not Science Lead indicators and never enter the score."""
    return [
        {
            "name": "Physio Reserve",
            "tag": EXPERIMENTAL_TAG,
            "help": PHYSIO_RESERVE_HELP,
        },
        {
            "name": "Cumulative Stress",
            "tag": EXPERIMENTAL_TAG,
            "help": CUMULATIVE_STRESS_HELP,
        },
        {
            "name": "Bio-Index",
            "tag": EXPERIMENTAL_TAG,
            "help": BIO_INDEX_HELP,
        },
        {
            "name": "Hydration",
            "tag": "simulated",
            "help": (
                "The dashboard shows a hardcoded 'Hydration index: 96% (simulated)'. "
                "It has no input, no science rule, and is not part of the health score."
            ),
        },
    ]


def snapshot_info(snapshot):
    """Latest SpaceWeatherSnapshot source and fetched_at, or None if none stored."""
    if snapshot is None:
        return None
    source_key = snapshot.source
    if source_key == donki.SOURCE_SAMPLE:
        source_label = donki.SAMPLE_LABEL
    else:
        source_label = snapshot.get_source_display()
    return {
        "source": source_key,
        "source_label": source_label,
        "fetched_at": snapshot.fetched_at,
        "is_sample": source_key == donki.SOURCE_SAMPLE,
    }


def page_catalog(snapshot=None):
    """Full context dict for the /sources/ template."""
    return {
        "indicators": indicator_catalog(),
        "citation": SCIENCE_LEAD_CITATION,
        "unwired_rule_inputs_note": UNWIRED_RULE_INPUTS_NOTE,
        "reading_sources": reading_source_labels(),
        "experimental_tiles": experimental_tiles(),
        "experimental_tag": EXPERIMENTAL_TAG,
        "space_weather": {
            "label": donki.LABEL,
            "api_base": donki.API_BASE,
            "endpoints": list(donki.ENDPOINTS),
            "endpoint_urls": [f"{donki.API_BASE}{name}" for name in donki.ENDPOINTS],
            "home_url": donki.DONKI_HOME_URL,
            "noaa_scales_url": donki.NOAA_SCALES_URL,
            "g_scale": g_scale_mapping(),
            "sample_label": donki.SAMPLE_LABEL,
            "snapshot": snapshot_info(snapshot),
        },
        "osdr_url": OSDR_URL,
        "hrp_hazards": {
            "hazards_url": NASA_HRP_HAZARDS_URL,
            "distance_url": NASA_DISTANCE_HAZARD_URL,
            "comms_delay_url": NASA_MARS_COMMS_DELAY_URL,
            "comm_delay_note": COMM_DELAY_NOTE,
        },
        "osdr_note": OSDR_NOTE,
        "companion": {
            "disclaimer": companion.DISCLAIMER,
            "llm_off_unless": "COMPANION_LLM_ENABLED=True",
            "default_provider": llm.DEFAULT_PROVIDER,
            "default_groq_model": llm.DEFAULT_GROQ_MODEL,
            "default_gemini_model": llm.DEFAULT_GEMINI_MODEL,
        },
        "team_assumptions": {
            "exercise_plan_minutes": EXERCISE_WEEKLY_PLAN_MINUTES,
            "exercise_source": companion.RULE_SOURCES["exercise"],
            "baseline_days": rules.BASELINE_DAYS,
        },
        "disclaimer": companion.DISCLAIMER,
        "prototype_disclaimer": PROTOTYPE_DISCLAIMER,
        "scored_indicators": list(rules.INDICATORS),
        "metric_keys": list(METRICS),
    }
