"""Verbatim alert wording from the Science Lead deliverable (Step 3).

Source: "Science Lead Deliverable: Astronaut Health Monitoring",
NASA Space Apps Challenge 2026, prepared by the Science Lead (Biologist),
section 2. Alert wording set (Step 3).

Each alert follows the four parts named in that section: what changed, why it
matters, what to do, when to escalate. Wording is copied exactly. The only
edit is a PDF line-wrap hyphenation repair: "better-\\nventilated" is stored
as "better-ventilated".

Keys are (indicator, state) where indicator matches core.rules.INDICATORS
and state is the science-table value "amber" or "red". The Alert model stores
severity as "caution" or "critical"; use state_from_severity() / severity_from_state()
to translate.
"""

STATE_TO_SEVERITY = {"amber": "caution", "red": "critical"}
SEVERITY_TO_STATE = {"caution": "amber", "critical": "red"}

# Indicator names as they appear in the Science Lead alert-wording table.
INDICATOR_TITLES = {
    "resting_hr": "Resting heart rate",
    "hrv": "Heart rate variability",
    "exercise": "Resistance exercise",
    "body_temp": "Body temperature",
    "sleep": "Sleep",
    "mood": "Mood and fatigue",
    "cabin_co2": "Cabin CO\u2082",
}

# (indicator, "amber"|"red") -> the four parts, each a verbatim sentence.
ALERT_TEXTS = {
    ("resting_hr", "amber"): {
        "what": "Your resting heart rate has been 10\u201320% above your baseline for 3 days.",
        "why": "A lasting rise can mean your body is under extra strain.",
        "action": "Rest, drink fluids and log how you feel.",
        "escalate": "If it continues 3 more days, contact the flight surgeon.",
    },
    ("resting_hr", "red"): {
        "what": "Your resting heart rate has stayed more than 20% above your baseline.",
        "why": "This can mean your heart and body are working harder than usual.",
        "action": "Pause strenuous activity and log symptoms.",
        "escalate": "Please contact the flight surgeon today.",
    },
    ("hrv", "amber"): {
        "what": "Your heart rate variability has dipped below your usual range for 3 days.",
        "why": "This often reflects stress or incomplete recovery.",
        "action": "Review your rest schedule and log how you feel.",
        "escalate": "If it continues 3 more days, contact the flight surgeon.",
    },
    ("hrv", "red"): {
        "what": "Your heart rate variability has stayed well below your baseline for 5 days.",
        "why": "This can mean your body is not recovering well.",
        "action": "Prioritize rest and note any symptoms.",
        "escalate": "Please contact the flight surgeon today.",
    },
    ("exercise", "amber"): {
        "what": "Your resistance exercise has been below plan for 2 weeks.",
        "why": "Regular loading helps protect bone and muscle in microgravity.",
        "action": "Add one short session this week and log any pain.",
        "escalate": "If it continues 2 more weeks, contact the flight surgeon.",
    },
    ("exercise", "red"): {
        "what": "Your resistance exercise has been well below plan, or pain has been high for 2 days.",
        "why": "Missed training and pain can both affect bone and muscle health.",
        "action": "Rest sore areas and log details.",
        "escalate": "Please contact the flight surgeon today.",
    },
    ("body_temp", "amber"): {
        "what": "Your temperature has been slightly above baseline for 2 days.",
        "why": "Small rises can be an early sign your immune system is active.",
        "action": "Rest, drink fluids and log any symptoms.",
        "escalate": "If it continues 2 more days, contact the flight surgeon.",
    },
    ("body_temp", "red"): {
        "what": "Your temperature is about 1 \u00b0C or more above your baseline.",
        "why": "This can point to your body fighting something.",
        "action": "Rest, drink fluids, log symptoms and limit close contact with crewmates.",
        "escalate": "Contact the flight surgeon now.",
    },
    ("sleep", "amber"): {
        "what": "Your sleep has dropped over the last 3 nights.",
        "why": "Poor sleep can affect mood, focus and immune function.",
        "action": "Review your rest schedule and log how you feel tomorrow.",
        "escalate": "If this continues 3 more days, contact the flight surgeon.",
    },
    ("sleep", "red"): {
        "what": "Your sleep has stayed well below your usual for several nights.",
        "why": "Ongoing sleep loss can affect safety, mood and health.",
        "action": "Protect your next rest period and reduce non-essential tasks.",
        "escalate": "Please contact the flight surgeon today.",
    },
    ("mood", "amber"): {
        "what": "Your mood and energy ratings have slipped over the last 3 days.",
        "why": "Stress and fatigue build up gradually in long missions.",
        "action": "Try a short break or a chat with a crewmate.",
        "escalate": "If it continues 3 more days, contact the flight surgeon.",
    },
    ("mood", "red"): {
        "what": "Your mood and energy have stayed low compared with your usual.",
        "why": "Prolonged low mood or fatigue can affect wellbeing and performance.",
        "action": "You are not alone: reach out to the crew or ground support.",
        "escalate": "Please contact the flight surgeon today.",
    },
    ("cabin_co2", "amber"): {
        "what": "Cabin CO\u2082 has been above the usual range for 24 hours.",
        "why": "Raised CO\u2082 can cause headaches and tiredness.",
        "action": "Check ventilation status and log any symptoms.",
        "escalate": "If it continues another 12 hours, contact ground support.",
    },
    ("cabin_co2", "red"): {
        "what": "Cabin CO\u2082 is well above the safe range.",
        "why": "High levels can affect alertness and breathing comfort.",
        "action": "Follow your cabin air procedures and move to a better-ventilated area if possible.",
        "escalate": "Contact ground support now.",
    },
}


def state_from_severity(severity):
    """Map Alert.severity (caution/critical) or a science state to amber/red."""
    if severity in SEVERITY_TO_STATE:
        return SEVERITY_TO_STATE[severity]
    if severity in STATE_TO_SEVERITY:
        return severity
    return None


def severity_from_state(state):
    """Map a science state (amber/red) to Alert.severity (caution/critical)."""
    return STATE_TO_SEVERITY.get(state)


def parts_for(indicator, severity):
    """Return the four-part wording dict, or None if there is no Science Lead text."""
    state = state_from_severity(severity)
    if state is None:
        return None
    return ALERT_TEXTS.get((indicator, state))


def message_for(indicator, severity):
    """Full verbatim alert paragraph (the four parts joined with spaces), or None."""
    parts = parts_for(indicator, severity)
    if parts is None:
        return None
    return " ".join((parts["what"], parts["why"], parts["action"], parts["escalate"]))


def title_for(indicator, severity):
    """Title from the Science Lead indicator name and state, e.g. 'Sleep · Amber'."""
    state = state_from_severity(severity)
    name = INDICATOR_TITLES.get(indicator)
    if state is None or name is None or (indicator, state) not in ALERT_TEXTS:
        return None
    return f"{name} \u00b7 {state.title()}"
