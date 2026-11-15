"""
SPACE-PULSE companion, layer 1: calm, rule-based answers.

This module is PURE: no database, no Django imports, no clock, no network.
Everything it says is built from results the app has ALREADY computed:

- evaluations:  the dict from core.services.evaluate_astronaut_metrics
- health_score: the dict from core.services.calculate_health_score
- source_counts: how many readings came from each source, e.g. {'simulated': 120}
- open_alerts:  how many alerts are still unacknowledged

It never invents a threshold, never detects mood or hormones, and never
diagnoses. The 'why' text of each indicator is the rule's own reason sentence
(core/rules.py), word for word. The rest is fixed, friendly wording.

Layer 2 (optional, built in core/llm.py): rephrase_with_llm(answers) can ask a
language model (Groq or Gemini) to reword ONLY the main answer text. It is off
unless COMPANION_LLM_ENABLED=True, it is called from core.views.companion_answers_for,
and it falls back to these exact answers on any failure or unsafe rewrite.

Each answer is a dict with these keys:
  id              short stable name: overall, why_<indicator>, what_to_do, data_source
  question        the chip text
  answer          the friendly text
  indicator       the indicator key (e.g. 'sleep') or None for general answers
  severity        nominal / caution / critical / no_data, or 'info' for plain facts
  source          where the rule comes from (see RULE_SOURCES)
  simulated_note  a warning when simulated readings are involved, else ''
  disclaimer      the "not a medical device" line
  escalation      'Contact your flight surgeon now.' on anything critical, else ''
"""

from core import rules

# Same words the rest of the app uses (core/services.py STATUS_TO_SEVERITY).
NOMINAL = 'nominal'
CAUTION = 'caution'
CRITICAL = 'critical'
NO_DATA = 'no_data'
INFO = 'info'

DISCLAIMER = "Not a medical device; does not diagnose."
FLIGHT_SURGEON_LINE = "Contact your flight surgeon now."
SIMULATED_NOTE = (
    "Some of the readings behind this are simulated demo data, "
    "not measurements from real sensors."
)

# Where each rule comes from. Rules with no entry here use DEFAULT_SOURCE.
# When the Science Lead confirms a source, add it here and nothing else changes.
DEFAULT_SOURCE = "Science Lead demo estimate — pending review"
RULE_SOURCES = {
    # The 150 min/week plan is not in the Science Lead's table: it is ours.
    'exercise': "Team assumption (150 min/week plan) — pending Science Lead review",
}

# Reading.SOURCE_CHOICES values -> words for a sentence (tested to stay in step).
SOURCE_WORDS = {
    'manual': 'self-reported',
    'csv': 'CSV upload',
    'simulated': 'simulated demo data',
    'dataset': 'research dataset',
}

MOOD_NOTE = "Mood is your own 1-5 rating from the daily check-in; I don't detect mood from anything else."


# =========================================================================
# Small helpers
# =========================================================================

def source_for(indicator):
    """The source line for one indicator's rule."""
    return RULE_SOURCES.get(indicator, DEFAULT_SOURCE)


def _join_names(names):
    """['Sleep'] -> 'Sleep'; two -> 'Sleep and Mood'; three -> 'A, B and C'."""
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _is_are(names):
    return "is" if len(names) == 1 else "are"


def _worst(severities):
    """Most serious severity: critical > caution > nominal. No_data if none has data."""
    for severity in (CRITICAL, CAUTION, NOMINAL):
        if severity in severities:
            return severity
    return NO_DATA


def _make_answer(answer_id, question, text, severity, indicator=None,
                 source=DEFAULT_SOURCE, simulated_note=''):
    """Every answer has the same shape, so templates and JSON never guess."""
    return {
        'id': answer_id,
        'question': question,
        'answer': text,
        'indicator': indicator,
        'severity': severity,
        'source': source,
        'simulated_note': simulated_note,
        'disclaimer': DISCLAIMER,
        'escalation': FLIGHT_SURGEON_LINE if severity == CRITICAL else '',
    }


def _sources_line(indicators):
    """The distinct sources of these indicators' rules, in order, joined with ';'."""
    sources = []
    for key in indicators:
        if source_for(key) not in sources:
            sources.append(source_for(key))
    return "; ".join(sources) if sources else DEFAULT_SOURCE


# =========================================================================
# The four kinds of answer
# =========================================================================

def _how_am_i_doing(rows, health_score, open_alerts, simulated_note):
    """Overall picture. rows is [(indicator, evaluation), ...]."""
    with_data = [(key, ev) for key, ev in rows if ev['status'] != NO_DATA]
    critical = [ev['display_label'] for key, ev in with_data if ev['status'] == CRITICAL]
    caution = [ev['display_label'] for key, ev in with_data if ev['status'] == CAUTION]
    missing = [ev['display_label'] for key, ev in rows if ev['status'] == NO_DATA]
    severity = _worst([ev['status'] for key, ev in with_data])

    if not with_data:
        text = ("I don't have any readings to look at yet. "
                "Log a daily check-in and I'll have something to tell you.")
    else:
        if critical:
            text = f"I want to point out that {_join_names(critical)} {_is_are(critical)} critical under the rules."
            if caution:
                text += f" {_join_names(caution)} {_is_are(caution)} in caution."
        elif caution:
            text = f"Things are mostly steady, but {_join_names(caution)} {_is_are(caution)} in caution."
        else:
            text = "Things look steady. Every indicator I have data for is within your own baseline rules."
        if missing:
            text += f" I don't have enough data yet for {_join_names(missing)}."

    score = (health_score or {}).get('score')
    if isinstance(score, int):
        text += f" Your health score is {score} ({health_score.get('label', '')})."
    if open_alerts:
        word = "alert" if open_alerts == 1 else "alerts"
        text += f" You have {open_alerts} open {word} on the Alerts page."

    flagged = [key for key, ev in with_data if ev['status'] in (CAUTION, CRITICAL)]
    sources = _sources_line(flagged or [key for key, ev in rows])
    return _make_answer('overall', "How am I doing?", text, severity,
                        source=sources, simulated_note=simulated_note)


def _why(key, ev, simulated_note):
    """Why one indicator is caution, critical or no data: the rule's own reason."""
    label = ev['display_label']
    status = ev['status']
    reason = ev['reason']

    if status == CAUTION:
        question = f"Why is {label} caution?"
        text = f"Here's what I'm seeing: {reason} That's enough for a caution, so it's worth keeping an eye on."
    elif status == CRITICAL:
        question = f"Why is {label} critical?"
        text = f"Here's what I'm seeing: {reason} That meets the rule for a critical flag."
    else:
        question = f"Why does {label} show no data?"
        text = f"I can't judge this one yet. {reason} I'd rather say so than guess."

    if key == 'mood':
        text += " " + MOOD_NOTE
    return _make_answer(f'why_{key}', question, text, status, indicator=key,
                        source=source_for(key), simulated_note=simulated_note)


def _what_to_do(rows, simulated_note):
    """Calm next step. It only repeats what the rules say; it adds no medical advice."""
    severity = _worst([ev['status'] for key, ev in rows])
    critical = [ev['display_label'] for key, ev in rows if ev['status'] == CRITICAL]
    caution = [ev['display_label'] for key, ev in rows if ev['status'] == CAUTION]

    if severity == CRITICAL:
        text = (f"The rules have flagged {_join_names(critical)} as critical, "
                "so the next step is to talk to your flight surgeon. "
                "I can't tell you more than what the rules show.")
    elif severity == CAUTION:
        text = (f"Keep logging your daily check-in so we can keep watching {_join_names(caution)} "
                "against your baseline. "
                "If you feel unwell or something worries you, talk to your flight surgeon.")
    elif severity == NOMINAL:
        text = ("Nothing needs changing right now. "
                "Keep up your daily check-in so your baselines stay reliable.")
    else:
        text = "Log a daily check-in or add a reading so I have something to look at."

    flagged = [key for key, ev in rows if ev['status'] in (CAUTION, CRITICAL)]
    return _make_answer('what_to_do', "What should I do?", text, severity,
                        source=_sources_line(flagged or [key for key, ev in rows]),
                        simulated_note=simulated_note)


def _data_source(source_counts, simulated_note):
    """Where the numbers come from: reading sources plus where the rules come from."""
    total = sum(source_counts.values())
    if total == 0:
        text = "There are no readings yet, so there is nothing to trace."
    else:
        # Known sources first, in a fixed order; anything unexpected is shown as-is.
        used = [s for s in list(SOURCE_WORDS) + sorted(source_counts) if source_counts.get(s)]
        used = list(dict.fromkeys(used))    # drop repeats, keep order
        words = {s: SOURCE_WORDS.get(s, s) for s in used}
        if len(used) == 1:
            text = f"All {total} of your readings are {words[used[0]]}."
        else:
            parts = [f"{source_counts[s]} {words[s]}" for s in used]
            text = f"Your {total} readings are: {_join_names(parts)}."
    text += (" Statuses come from the Science Lead's rules, compared with your own baseline "
             "(the average of mission days 1-14) where a rule uses one. "
             "Those thresholds are demo estimates still pending review, not official NASA limits, "
             "and the 150 min/week exercise plan is a team assumption.")
    return _make_answer('data_source', "Where does this data come from?", text, INFO,
                        source=DEFAULT_SOURCE, simulated_note=simulated_note)


# =========================================================================
# Public API
# =========================================================================

def build_companion_answers(evaluations, health_score=None, source_counts=None, open_alerts=0):
    """All the companion's answers for one astronaut, as a list of dicts.

    evaluations: metric key -> evaluation dict (needs 'status', 'reason',
        'display_label'). Only the 7 science indicators are used; the stress
        self-report has no rule, so the companion never talks about it.
    health_score: dict with 'score' and 'label', or None.
    source_counts: {'simulated': 120, 'manual': 6}. None means unknown (empty).
    open_alerts: number of unacknowledged alerts.

    Order: How am I doing? / one 'Why is ...?' per indicator that is not green /
    What should I do? / Where does this data come from?
    """
    source_counts = source_counts or {}
    rows = [(key, evaluations[key]) for key in rules.INDICATORS if key in evaluations]
    simulated_note = SIMULATED_NOTE if source_counts.get('simulated') else ''

    answers = [_how_am_i_doing(rows, health_score, open_alerts, simulated_note)]
    for key, ev in rows:
        if ev['status'] != NOMINAL:
            answers.append(_why(key, ev, simulated_note))
    answers.append(_what_to_do(rows, simulated_note))
    answers.append(_data_source(source_counts, simulated_note))
    return answers