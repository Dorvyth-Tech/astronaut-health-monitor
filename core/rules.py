"""
Rules engine for the 7 health indicators (Science Lead table).

PURE FUNCTIONS ONLY: no database, no Django imports, no clock. Everything the
rules need is passed in, so every rule can be tested with plain lists.
Case IDs in core/tests_rules.py (RHR-1, S-3, ...) come from
docs/rule-test-cases.md.

Data shapes
-----------
readings : list of (t, value) pairs.
    t is the mission day for daily indicators, the week number for
    "exercise" (completed weeks only) and the hour number for "cabin_co2".
baseline : float or None. None means "baseline not ready yet" (before day 15).

How a rule reads
----------------
Each indicator has one small function (_resting_hr, _hrv, ...). It checks the
worst status first (red), then amber, and otherwise returns green. With no
readings, or no baseline for a relative rule, the result is NO_DATA (fail
closed: never green by accident).
"""

from dataclasses import dataclass

# --- statuses -------------------------------------------------------------
GREEN = "green"
AMBER = "amber"
RED = "red"
NO_DATA = "no_data"

# --- indicators (one rule set each) --------------------------------------
# Note: "mood" is the new 1-5 mood/fatigue rule. The old 'mood_stress' key in
# core/constants.py is stress on a 1-10 scale, which is a different thing.
INDICATORS = (
    "resting_hr",
    "hrv",
    "exercise",
    "body_temp",
    "sleep",
    "mood",
    "cabin_co2",
)

BASELINE_DAYS = 14          # baseline = mean of mission days 1..14
MIN_SCORED_INDICATORS = 3   # fewer indicators with data -> no health score
STATUS_POINTS = {GREEN: 100, AMBER: 60, RED: 20}   # no_data is excluded


@dataclass(frozen=True)
class RuleResult:
    """Outcome of one rule check.

    status: GREEN, AMBER, RED or NO_DATA.
    reason: one plain sentence saying which rule fired (or why there is no
            data). It will become the 'why' text of an alert.
    """
    status: str
    reason: str


# =========================================================================
# Shared helpers
# =========================================================================

def _r(x):
    """Round before comparing, so float noise never decides a boundary.

    Example: 60 * 1.1 is 66.00000000000001 in Python, but a reading of 66 is
    exactly 10% above 60 and must count as inside the 10% band.
    All comparisons on computed numbers go through this one helper.
    """
    return round(x, 6)


def _pct_change(value, baseline):
    """Percent change of value against baseline (+ is above, - is below)."""
    return _r((value - baseline) / baseline * 100)


def _run(readings, predicate):
    """The latest unbroken run of readings that satisfy predicate.

    Walks backwards from the newest reading. The run stops at the first
    reading that fails the test OR at a gap in t (a missing day breaks it).
    Returns the run as a list, oldest first. len() is the number of days.
    """
    run = []
    for t, value in reversed(readings):
        if not predicate(value):
            break
        if run and run[0][0] - t != 1:
            break
        run.insert(0, (t, value))
    return run


def _worst(*statuses):
    """Pick the most severe status. Order: red > amber > green > no_data."""
    for status in (RED, AMBER, GREEN):
        if status in statuses:
            return status
    return NO_DATA


def _no_baseline(name):
    return RuleResult(NO_DATA, f"{name}: baseline not ready yet (needs 14 days).")


# =========================================================================
# One function per indicator. Each gets (readings, baseline, extras) where
# readings are sorted oldest first and extras is a dict of optional inputs.
# =========================================================================

def _resting_hr(readings, baseline, extras):
    """Resting heart rate. Percent above baseline.

    Amber: 10-20% above for 3 days.
    Red:   more than 20% above for 3 days, or above 100 bpm at rest (absolute).
    """
    latest = readings[-1][1]
    if latest > 100:
        return RuleResult(RED, f"Resting HR {latest:g} bpm is above 100 bpm.")
    if baseline is None:
        return _no_baseline("Resting HR")

    def above(pct):
        return lambda v: _pct_change(v, baseline) > pct

    red_days = len(_run(readings, above(20)))
    amber_days = len(_run(readings, above(10)))
    pct = _pct_change(latest, baseline)
    if red_days >= 3:
        return RuleResult(RED, f"Resting HR {pct:.0f}% above baseline for {red_days} days (red above 20%).")
    if amber_days >= 3:
        return RuleResult(AMBER, f"Resting HR {pct:.0f}% above baseline for {amber_days} days.")
    return RuleResult(GREEN, f"Resting HR {pct:+.0f}% from baseline; no 3-day run more than 10% above.")


def _hrv(readings, baseline, extras):
    """Heart rate variability (RMSSD). Percent below baseline.

    Amber: 15-30% below for 3 days.
    Red:   more than 30% below for 5 days, or amber plus a red resting HR.
    """
    if baseline is None:
        return _no_baseline("HRV")

    def below(pct):
        return lambda v: -_pct_change(v, baseline) > pct

    red_days = len(_run(readings, below(30)))
    amber_days = len(_run(readings, below(15)))
    drop = -_pct_change(readings[-1][1], baseline)
    if red_days >= 5:
        return RuleResult(RED, f"HRV {drop:.0f}% below baseline for {red_days} days (red needs 5 days below 30%).")
    if amber_days >= 3 and extras["rhr_status"] == RED:
        return RuleResult(RED, f"HRV {drop:.0f}% below baseline for {amber_days} days while resting HR is red.")
    if amber_days >= 3:
        return RuleResult(AMBER, f"HRV {drop:.0f}% below baseline for {amber_days} days.")
    return RuleResult(GREEN, f"HRV {-drop:+.0f}% from baseline; no 3-day run more than 15% below.")


def _exercise(readings, baseline, extras):
    """Resistance exercise. readings = completed weeks, baseline = weekly plan.

    Amber: 70-90% of plan for 2 weeks, or pain 4-6 for 3 days.
    Red:   under 70% of plan for 2 weeks, or pain 7+ for 2 days.
    The weekly check and the pain check are made separately; the worse wins.
    """
    # Weekly minutes against the plan.
    if baseline is None or not readings:
        weekly = RuleResult(NO_DATA, "Exercise: no weekly plan or no completed week yet.")
    else:
        def of_plan(pct):
            return lambda v: _r(v / baseline * 100) < pct

        red_weeks = len(_run(readings, of_plan(70)))
        amber_weeks = len(_run(readings, of_plan(90)))
        latest_pct = _r(readings[-1][1] / baseline * 100)
        if red_weeks >= 2:
            weekly = RuleResult(RED, f"Resistance exercise at {latest_pct:.0f}% of plan, under 70% for {red_weeks} weeks.")
        elif amber_weeks >= 2:
            weekly = RuleResult(AMBER, f"Resistance exercise at {latest_pct:.0f}% of plan, under 90% for {amber_weeks} weeks.")
        else:
            weekly = RuleResult(GREEN, f"Resistance exercise at {latest_pct:.0f}% of plan.")

    # Pain scores (0-10) on recent days. Pain 4 and above counts toward amber.
    pain = sorted(extras["pain"] or [])
    severe_days = len(_run(pain, lambda p: p >= 7))
    sore_days = len(_run(pain, lambda p: p >= 4))
    if severe_days >= 2:
        pain_result = RuleResult(RED, f"Exercise pain 7 or more for {severe_days} days.")
    elif sore_days >= 3:
        pain_result = RuleResult(AMBER, f"Exercise pain 4 or more for {sore_days} days.")
    else:
        pain_result = RuleResult(GREEN, "Exercise pain is low.")

    status = _worst(weekly.status, pain_result.status)
    if status == GREEN and weekly.status == NO_DATA:
        return weekly       # pain is fine but we cannot judge the plan: stay closed
    if status == weekly.status:
        return weekly
    return pain_result


def _body_temp(readings, baseline, extras):
    """Body temperature. Degrees C above baseline.

    Amber: +0.5 to +1.0 C for 2 days, or a new symptom (on its own).
    Red:   +1.0 C or more, or amber for 2 more days (4 in a row) with symptoms.
    extras["new_symptom_days"] is the set of days a new symptom was logged.
    """
    symptom_days = extras["new_symptom_days"] or set()
    latest_day, latest = readings[-1]
    symptom_today = latest_day in symptom_days

    if baseline is None:
        if symptom_today:
            return RuleResult(AMBER, "New symptom logged today.")
        return _no_baseline("Body temperature")

    def above(delta):
        return lambda v: _r(v - baseline) > delta

    rise = _r(latest - baseline)
    if rise >= 1.0:
        return RuleResult(RED, f"Body temperature {rise:.1f} C above baseline (red at 1.0 C or more).")
    warm = _run(readings, above(0.5))
    with_symptoms = any(day in symptom_days for day, _ in warm[-4:])
    if len(warm) >= 4 and with_symptoms:
        return RuleResult(RED, f"Body temperature {rise:.1f} C above baseline for {len(warm)} days with symptoms.")
    if len(warm) >= 2:
        return RuleResult(AMBER, f"Body temperature {rise:.1f} C above baseline for {len(warm)} days.")
    if symptom_today:
        return RuleResult(AMBER, "New symptom logged today.")
    return RuleResult(GREEN, f"Body temperature {rise:+.1f} C from baseline; no 2-day run more than 0.5 C above.")


def _sleep(readings, baseline, extras):
    """Sleep hours. Hours below baseline.

    Amber: more than 1 h below for 3 nights.
    Red:   more than 2 h below for 4 nights, or under 5 h on 2 nights in a row
           (the 5 h rule is absolute and needs no baseline).
    """
    latest = readings[-1][1]
    short_nights = len(_run(readings, lambda v: v < 5.0))
    if short_nights >= 2:
        return RuleResult(RED, f"Sleep under 5 h for {short_nights} nights in a row (latest {latest:g} h).")
    if baseline is None:
        return _no_baseline("Sleep")

    def below(hours):
        return lambda v: _r(baseline - v) > hours

    red_nights = len(_run(readings, below(2)))
    amber_nights = len(_run(readings, below(1)))
    short = _r(baseline - latest)
    if red_nights >= 4:
        return RuleResult(RED, f"Sleep {short:.1f} h below baseline for {red_nights} nights (red above 2 h).")
    if amber_nights >= 3:
        return RuleResult(AMBER, f"Sleep {short:.1f} h below baseline for {amber_nights} nights.")
    return RuleResult(GREEN, f"Sleep {latest:g} h against a {baseline:g} h baseline; no 3-night run more than 1 h below.")


def _mood(readings, baseline, extras):
    """Mood / fatigue score (1-5). Points below baseline.

    Amber: drop of 1 point or more for 3 days (Science Lead table, verbatim).
    Red:   drop of 2 points or more for 3 days, or drop of 1 point or more for 7 days.
    A drop of less than 1 point stays green, including the 0.5-1.0 gap in the table.
    """
    if baseline is None:
        return _no_baseline("Mood")

    def dropped(points):
        return lambda v: _r(baseline - v) >= points

    deep_days = len(_run(readings, dropped(2)))
    long_days = len(_run(readings, dropped(1)))
    drop = _r(baseline - readings[-1][1])
    if deep_days >= 3:
        return RuleResult(RED, f"Mood {drop:g} points below baseline for {deep_days} days.")
    if long_days >= 7:
        return RuleResult(RED, f"Mood {drop:g} points below baseline for {long_days} days.")
    if long_days >= 3:
        return RuleResult(AMBER, f"Mood {drop:g} points below baseline for {long_days} days.")
    return RuleResult(GREEN, f"Mood {-drop:+g} points from baseline; no 3-day run with a drop of 1 point or more.")

def _cabin_co2(readings, baseline, extras):
    """Cabin CO2 in ppm, hourly readings. Absolute: no baseline needed.

    Amber: 3,500 ppm or more for 24 h.
    Red:   above 5,000 ppm for 6 h in a row, or above 7,000 at any time.
    A short spike under 6 h stays green (assumes Q7).
    A 24 h window needs at least 80% of its hours (assumes Q8), else NO_DATA.
    """
    last_hour, latest = readings[-1]
    if latest > 7000:
        return RuleResult(RED, f"Cabin CO2 {latest:g} ppm is above 7,000 ppm.")
    high_hours = len(_run(readings, lambda v: v > 5000))
    if high_hours >= 6:
        return RuleResult(RED, f"Cabin CO2 above 5,000 ppm for {high_hours} hours.")

    window = [(h, v) for h, v in readings if h > last_hour - 24]
    covers_24h = readings[0][0] <= last_hour - 23
    if not covers_24h:
        return RuleResult(GREEN, f"Cabin CO2 {latest:g} ppm, under 24 h of high readings.")
    if len(window) < 0.8 * 24:
        return RuleResult(NO_DATA, f"Cabin CO2: only {len(window)} of the last 24 hours have readings.")
    if all(v >= 3500 for _, v in window):
        return RuleResult(AMBER, f"Cabin CO2 at 3,500 ppm or more for 24 hours (latest {latest:g} ppm).")
    return RuleResult(GREEN, f"Cabin CO2 {latest:g} ppm; not 3,500 ppm or more for the whole last 24 hours.")


# indicator name -> its rule function
RULES = {
    "resting_hr": _resting_hr,
    "hrv": _hrv,
    "exercise": _exercise,
    "body_temp": _body_temp,
    "sleep": _sleep,
    "mood": _mood,
    "cabin_co2": _cabin_co2,
}


# =========================================================================
# Public API
# =========================================================================

def compute_baseline(readings):
    """Personal baseline = mean of the values on mission days 1..14.

    Returns None unless every one of the 14 days has a reading
    (assumes Q8: every day must be present). Days after 14 are ignored.
    """
    by_day = dict(readings)
    days = range(1, BASELINE_DAYS + 1)
    if any(day not in by_day for day in days):
        return None
    return sum(by_day[day] for day in days) / BASELINE_DAYS


def evaluate(indicator, readings, baseline=None, *,
             pain=None, new_symptom_days=None, rhr_status=None):
    """Status of one indicator after its latest reading.

    indicator: one of INDICATORS.
    readings: [(t, value), ...] (see module docstring).
    baseline: personal baseline, or the weekly plan in minutes for
        "exercise", or None when not ready. Relative rules return NO_DATA
        without a baseline; absolute rules (RHR > 100, sleep < 5 h twice,
        all CO2 rules) still fire.

    Extra inputs, each used by one indicator only:
    pain: [(mission_day, 0-10), ...]            -> exercise
    new_symptom_days: set of mission days on which a new symptom was logged
                                                -> body_temp
    rhr_status: status of resting_hr over the same days -> hrv
        (the "HRV amber + heart rate red" combination rule).

    Rules: "for N days" means N consecutive mission days ending at the latest
    reading; a missing day breaks the run. Green includes its edge. Precedence
    is red > amber > green > no_data.

    Returns a RuleResult. Raises ValueError for an unknown indicator.
    """
    if indicator not in RULES:
        raise ValueError(f"Unknown indicator: {indicator!r}")
    readings = sorted(readings)
    if not readings:
        return RuleResult(NO_DATA, f"No readings yet for {indicator}.")
    extras = {
        "pain": pain,
        "new_symptom_days": new_symptom_days,
        "rhr_status": rhr_status,
    }
    return RULES[indicator](readings, baseline, extras)


def health_score(statuses):
    """Overall score 0-100 from a list of indicator statuses.

    Mean of STATUS_POINTS (green 100, amber 60, red 20), rounded to an int.
    NO_DATA entries are left out. Returns None ("not enough data") when
    fewer than MIN_SCORED_INDICATORS indicators have data.
    """
    points = [STATUS_POINTS[s] for s in statuses if s != NO_DATA]
    if len(points) < MIN_SCORED_INDICATORS:
        return None
    return int(round(sum(points) / len(points)))
