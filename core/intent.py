"""
SPACE-PULSE companion, "Ask Pulse": work out WHICH of the companion's known
questions a typed sentence is asking. It never writes an answer.

This module is PURE: no database, no Django imports, no network. The answer
itself always comes from core/companion.py (computed results); this module only
picks an intent id from a fixed list:

  overall, what_to_do, data_source, and one per indicator:
  resting_hr, hrv, exercise, body_temp, sleep, mood, cabin_co2

Step 1 (here): deterministic keyword matching, case-insensitive.
Step 2 (core/llm.py, only if step 1 finds nothing and the AI is switched on):
  classify_intent_with_llm() may pick one id from the same list. Its output is
  validated against INTENT_IDS, so the model can never invent an intent.
Anything else is out of scope and gets the fixed reply below.

Radiation / space weather: the app has no radiation data yet (the HRP radiation
risk shows "No dosimeter data"), so there is no answer to give. Those questions
are recognised so the reply can say so honestly, but they are never answered.
"""

import re

MAX_QUESTION_CHARS = 300

OUT_OF_SCOPE_MESSAGE = "I can only answer questions about your monitored indicators."
NO_RADIATION_DATA_NOTE = "I don't have any radiation or space-weather data yet."
TOO_LONG_MESSAGE = f"Please keep your question under {MAX_QUESTION_CHARS} characters."
EMPTY_MESSAGE = "Type a question first."

# Methods reported to the browser.
KEYWORD = 'keyword'
AI = 'ai'
NONE = 'none'

# intent id -> the sentence shown to the model (and used as a supported question).
INDICATOR_LABELS = {
    'resting_hr': 'Resting HR',
    'hrv': 'HRV',
    'exercise': 'Exercise',
    'body_temp': 'Body Temp',
    'sleep': 'Sleep',
    'mood': 'Mood',
    'cabin_co2': 'Cabin CO2',
}

INTENT_DESCRIPTIONS = {
    'overall': "overall status: how the astronaut is doing, health score, general check",
    'what_to_do': "what the astronaut should do next",
    'data_source': "where the data and thresholds come from, whether it is real or simulated",
    'resting_hr': "resting heart rate / pulse",
    'hrv': "heart rate variability (HRV)",
    'exercise': "exercise, workouts, training",
    'body_temp': "body temperature",
    'sleep': "sleep, rest, tiredness",
    'mood': "mood, feelings, stress",
    'cabin_co2': "cabin air, CO2",
}
INTENT_IDS = tuple(INTENT_DESCRIPTIONS)

# Questions we can answer, in the order they are listed to the person.
SUPPORTED_QUESTIONS = (
    "How am I doing?",
    "What should I do?",
    "Where does this data come from?",
    *(f"How is my {label}?" for label in INDICATOR_LABELS.values()),
)


def _rx(pattern):
    return re.compile(pattern, re.IGNORECASE)


# One pattern per indicator. When several match, the one mentioned first wins
# (ties go to the earlier entry, so "heart rate variability" is HRV, not resting HR).
INDICATOR_PATTERNS = (
    ('hrv', _rx(r"\bhrv\b|heart[ -]?rate variability|\bvariability\b")),
    ('resting_hr', _rx(r"resting (?:hr|heart)|heart[ -]?rate|\bpulse\b|\bbpm\b|\bhr\b|heartbeat")),
    ('sleep', _rx(r"\bsleep\w*|\bslept\b|\brest\b|\btired\b|\bfatigue\w*|\binsomnia\b|\bnap(?:s|ping)?\b|\bexhaust\w*")),
    ('mood', _rx(r"\bmood\b|\bstress\w*|\bsad\b|\bhappy\b|\banxious\b|\bemotion\w*|\bmental\b")),
    ('exercise', _rx(r"\bexercis\w*|\bworkouts?\b|\bwork out\b|\btraining\b|\btrain\b|\bfitness\b|\bresistance\b|\bgym\b|\btreadmill\b")),
    ('body_temp', _rx(r"\btemperature\b|\btemp\b|\bfever\b|body heat")),
    ('cabin_co2', _rx(r"\bco2\b|carbon dioxide|\bcabin\b|air quality|\bair\b")),
)

# Strong data-source phrases win over everything ("where does my sleep data come from").
DATA_SOURCE_STRONG = _rx(
    r"data source|\bsources?\b|where (?:does|do|did|is)\b|\bcomes? from\b|\borigin\b|\bsimulated\b|"
    r"real data|real sensors?|\bcsv\b|how accurate|\bfake\b|made up|demo data"
)
# "feel" is only a hint of mood ("I feel tired" is about sleep), so it is checked after the indicators.
MOOD_WEAK = _rx(r"\bfeel\w*")
DATA_SOURCE_WEAK = _rx(r"\bdata\b|\breadings?\b")
WHAT_TO_DO = _rx(
    r"what (?:should|can|do) i do|what to do|next steps?|\badvice\b|recommend\w*|\bwhat now\b|what should happen"
)
OVERALL = _rx(
    r"how am i\b|how are things|how.s everything|\boverall\b|\bstatus\b|\bscore\b|\bhealth\b|"
    r"\bsummary\b|am i (?:ok|okay|fine|alright|healthy)"
)
RADIATION = _rx(r"radiat\w*|\bsolar\b|\bflares?\b|space weather|\bdonki\b|\bcosmic\b|\bsun\b|\bcme\b|\bsunspots?\b")


def clean_question(raw):
    """(question, error). Strips the text; an error message is returned for empty or too long input."""
    if not isinstance(raw, str):
        return '', EMPTY_MESSAGE
    question = raw.strip()
    if not question:
        return '', EMPTY_MESSAGE
    if len(question) > MAX_QUESTION_CHARS:
        return '', TOO_LONG_MESSAGE
    return question, ''


def classify_by_keywords(question):
    """Return an intent id, 'radiation', or None. Pure text matching, no AI.

    Order: strong data-source phrases, radiation, the indicator mentioned first,
    "feel", the weak word 'data', what-to-do phrases, then the overall-status words.
    """
    if DATA_SOURCE_STRONG.search(question):
        return 'data_source'
    if RADIATION.search(question):
        return 'radiation'

    best = None     # (position, order, id)
    for order, (intent, pattern) in enumerate(INDICATOR_PATTERNS):
        match = pattern.search(question)
        if match and (best is None or (match.start(), order) < best[:2]):
            best = (match.start(), order, intent)
    if best:
        return best[2]

    if MOOD_WEAK.search(question):
        return 'mood'
    if DATA_SOURCE_WEAK.search(question):
        return 'data_source'
    if WHAT_TO_DO.search(question):
        return 'what_to_do'
    if OVERALL.search(question):
        return 'overall'
    return None
