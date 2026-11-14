"""
Core telemetry processing and clinical algorithms for Space-Pulse.
NASA Human Research Program (HRP) spaceflight health assessment logic.

The status rules themselves live in core/rules.py (pure functions, baseline
relative, from the Science Lead table). This module is the glue: it loads
readings from the database, hands them to the rules, and turns the answer into
what the views and templates already understand.
"""

from django.utils import timezone
from core import rules
from core.constants import EXERCISE_WEEKLY_PLAN_MINUTES, METRIC_CONFIG, METRICS


NUMBER_WORDS = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}

# --- status vocabulary ----------------------------------------------------
# core/rules.py speaks green / amber / red / no_data. The database
# (Alert.severity), the templates and the URL filters already speak
# nominal / caution / critical, so we translate in ONE place instead of
# migrating data. 'no_data' is new: it is never stored on an Alert, it only
# shows on cards and history rows so a missing signal is never shown as green.
STATUS_TO_SEVERITY = {
    rules.GREEN: 'nominal',
    rules.AMBER: 'caution',
    rules.RED: 'critical',
    rules.NO_DATA: 'no_data',
}
SEVERITY_TO_STATUS = {sev: status for status, sev in STATUS_TO_SEVERITY.items()}

NO_DATA_COLOR = '#8E9AAF'

# severity -> (badge_bg, badge_text, color_hex, label)
# Nominal: #22E3A0 (Emerald green), Caution: #FFB020 (Amber), Critical: #ffb4ab (Red/Coral)
SEVERITY_STYLE = {
    'nominal': ('bg-[rgba(34,227,160,0.15)]', 'text-[#22E3A0]', '#22E3A0', 'Nominal'),
    'caution': ('bg-[rgba(255,176,32,0.2)]', 'text-[#FFB020]', '#FFB020', 'Caution'),
    'critical': ('bg-error-container/60', 'text-error', '#ffb4ab', 'Critical'),
    'no_data': ('bg-surface-container-highest', 'text-outline', NO_DATA_COLOR, 'No data'),
}

# Readings with no science rule: the 1-10 stress self-report. It is not one of the
# 7 indicators (METRICS), but it is still evaluated (always no_data) so the
# 'cumulative stress' tile and the history page can use it. It never alerts.
DISPLAY_ONLY_METRICS = ('mood_stress',)


def status_to_severity(status):
    """green/amber/red/no_data -> nominal/caution/critical/no_data."""
    return STATUS_TO_SEVERITY[status]


# --- readings -> the (t, value) shape the rules expect ---------------------

def readings_to_series(metric_key, rows):
    """[(mission_day, timestamp, value), ...] oldest first -> [(t, value), ...].

    - Daily metrics: t is the mission day. If a day has several readings
      (check-in plus a manual one) the newest wins: the rules need one value a day.
    - cabin_co2: the rule is hourly, so t is the hour the reading was taken.
    """
    by_t = {}
    for mission_day, timestamp, value in rows:
        t = int(timestamp.timestamp() // 3600) if metric_key == 'cabin_co2' else mission_day
        by_t[t] = value
    return sorted(by_t.items())


def weekly_totals(daily_series):
    """[(day, minutes), ...] -> [(week_number, total_minutes), ...].

    The exercise rule compares COMPLETED weeks with the weekly plan. A week
    only counts when all 7 of its days have a reading, so the current partial
    week (and weeks with gaps) are left out. Week 1 is days 1-7.
    """
    days_by_week = {}
    for day, minutes in daily_series:
        days_by_week.setdefault((day - 1) // 7 + 1, {})[day] = minutes
    return [
        (week, sum(days.values()))
        for week, days in sorted(days_by_week.items())
        if len(days) == 7
    ]


def load_series(astronaut, metric_key, limit=60):
    """Latest `limit` readings of one metric as a rule-ready series."""
    from telemetry.models import Reading
    rows = list(
        Reading.objects.filter(astronaut=astronaut, metric=metric_key)
        .order_by('-mission_day', '-timestamp')
        .values_list('mission_day', 'timestamp', 'value')[:limit]
    )
    rows.reverse()
    return readings_to_series(metric_key, rows)


def rule_baseline(astronaut, metric_key, stored_baselines):
    """Baseline the rules should use, or None (rules then fail closed).

    1. The stored Baseline row (set in the profile or by the seed), else
    2. the mean of mission days 1-14 from the readings, else
    3. None. We do NOT fall back to METRIC_CONFIG['default_baseline'] here:
       a generic default is not a personal baseline.
    """
    stored = stored_baselines.get(metric_key)
    if stored is not None:
        return stored
    from telemetry.models import Reading
    early = Reading.objects.filter(
        astronaut=astronaut, metric=metric_key, mission_day__lte=rules.BASELINE_DAYS
    ).values_list('mission_day', 'value')
    return rules.compute_baseline(list(early))


# --- evaluating ------------------------------------------------------------

def evaluate_metric_status(metric_key, series, baseline_value=None, rhr_status=None):
    """Status of one metric from its recent series, via core/rules.py.

    series: [(t, value), ...] oldest first (see readings_to_series).
    baseline_value: the personal baseline, or None when not ready. Not used
        for 'exercise': its plan is the fixed EXERCISE_WEEKLY_PLAN_MINUTES.
    rhr_status: the resting-HR rule status, needed by the HRV combination rule.

    Returns the dict the views/templates use: status is nominal / caution /
    critical / no_data, plus label, badge classes, colour, latest value, etc.
    'rule_status' (green/amber/red/no_data) and 'reason' come from the rules.
    """
    config = METRIC_CONFIG.get(metric_key, {})

    if metric_key in DISPLAY_ONLY_METRICS:
        result = rules.RuleResult(rules.NO_DATA, "Shown for reference. No science rule for this indicator yet.")
    elif metric_key == 'exercise':
        result = rules.evaluate('exercise', weekly_totals(series), EXERCISE_WEEKLY_PLAN_MINUTES)
    else:
        result = rules.evaluate(metric_key, series, baseline_value, rhr_status=rhr_status)

    severity = status_to_severity(result.status)
    badge_bg, badge_text, color_hex, label = SEVERITY_STYLE[severity]
    if metric_key in DISPLAY_ONLY_METRICS:
        label = 'Self-report'

    return {
        'status': severity,
        'rule_status': result.status,
        'reason': result.reason,
        'label': label,
        'badge_bg': badge_bg,
        'badge_text': badge_text,
        'color_hex': color_hex,
        'value': series[-1][1] if series else None,
        'baseline': baseline_value if baseline_value is not None else config.get('default_baseline'),
        'unit': config.get('unit', ''),
        'display_label': config.get('label', metric_key),
        'icon': config.get('icon', 'vital_signs'),
    }


def evaluate_astronaut_metrics(astronaut, stored_baselines=None):
    """metric_key -> evaluation dict for the 7 indicators of one astronaut
    (plus the display-only stress self-report, which is always no_data).

    Resting HR is evaluated first because the HRV rule needs its status.
    """
    if stored_baselines is None:
        from core.models import Baseline
        stored_baselines = {b.metric: b.baseline_value for b in Baseline.objects.filter(astronaut=astronaut)}

    evals = {}
    rhr_status = None
    for metric_key in METRICS + list(DISPLAY_ONLY_METRICS):   # resting_hr comes before hrv
        series = load_series(astronaut, metric_key)
        baseline = rule_baseline(astronaut, metric_key, stored_baselines)
        evals[metric_key] = evaluate_metric_status(metric_key, series, baseline, rhr_status=rhr_status)
        if metric_key == 'resting_hr':
            rhr_status = evals[metric_key]['rule_status']
    return evals


def reading_statuses(astronaut, metric_key, stored_baselines, window=30, history=90):
    """reading id -> evaluation dict, as the status looked when each reading arrived.

    Used by the history page to colour each logged reading. Each reading is
    judged on the `window` readings ending at it, so an old reading is not
    judged with knowledge from the future.
    """
    from telemetry.models import Reading
    rows = list(
        Reading.objects.filter(astronaut=astronaut, metric=metric_key)
        .order_by('-mission_day', '-timestamp')
        .values_list('id', 'mission_day', 'timestamp', 'value')[:history]
    )
    rows.reverse()
    baseline = rule_baseline(astronaut, metric_key, stored_baselines)

    statuses = {}
    for i, row in enumerate(rows):
        recent = [r[1:] for r in rows[max(0, i - window + 1):i + 1]]
        series = readings_to_series(metric_key, recent)
        statuses[row[0]] = evaluate_metric_status(metric_key, series, baseline)
    return statuses


def amber_edges(metric_key, baseline):
    """Where the amber band starts, for chart guide lines: {'low': x, 'high': y}.

    Mirrors core/rules.py (values are None when a side has no band). Always
    derived from the baseline, never hardcoded.
    """
    edges = {'low': None, 'high': None}
    if baseline is None:
        return edges
    if metric_key == 'sleep':
        edges['low'] = baseline - 1.0          # more than 1 h below baseline
    elif metric_key == 'hrv':
        edges['low'] = baseline * 0.85         # more than 15% below
    elif metric_key == 'exercise':
        # The chart shows minutes per day, the plan is per week: 90% of the plan, per day.
        edges['low'] = EXERCISE_WEEKLY_PLAN_MINUTES / 7 * 0.90
    elif metric_key == 'mood':
        edges['low'] = baseline - 0.5          # more than 0.5 points below baseline
    elif metric_key == 'resting_hr':
        edges['high'] = baseline * 1.10        # more than 10% above
    elif metric_key == 'body_temp':
        edges['high'] = baseline + 0.5         # more than 0.5 C above
    elif metric_key == 'cabin_co2':
        edges['high'] = 3500.0                 # absolute
    return {side: (round(v, 2) if v is not None else None) for side, v in edges.items()}


def detect_consecutive_trend(values):
    """
    Detects if values have moved in the same direction for 3 or more consecutive steps.
    values: list of numbers in chronological order [oldest, ..., newest]
    Returns dict:
      direction: 'down', 'up', or 'steady'
      streak_days: number of consecutive days in streak (steps + 1)
      flagged: True if streak_days >= 4 (3 consecutive steps)
      message: Dynamic text describing trend, or None
    """
    if len(values) < 4:
        return {'direction': 'steady', 'streak_days': 0, 'flagged': False, 'message': None}

    # Analyze from end backwards
    diffs = []
    for i in range(len(values) - 1, 0, -1):
        diff = values[i] - values[i - 1]
        diffs.append(diff)

    # Check for downward streak
    down_steps = 0
    for d in diffs:
        if d < -0.01:
            down_steps += 1
        else:
            break

    # Check for upward streak
    up_steps = 0
    for d in diffs:
        if d > 0.01:
            up_steps += 1
        else:
            break

    if down_steps >= 3:
        streak_days = down_steps + 1
        word = NUMBER_WORDS.get(streak_days, str(streak_days))
        return {
            'direction': 'down',
            'streak_days': streak_days,
            'flagged': True,
            'message': f"has dropped {word} days in a row",
        }
    elif up_steps >= 3:
        streak_days = up_steps + 1
        word = NUMBER_WORDS.get(streak_days, str(streak_days))
        return {
            'direction': 'up',
            'streak_days': streak_days,
            'flagged': True,
            'message': f"has risen {word} days in a row",
        }

    # Short direction based on latest 2 values
    latest_diff = values[-1] - values[-2] if len(values) >= 2 else 0
    if latest_diff > 0.05:
        dir_short = 'up'
    elif latest_diff < -0.05:
        dir_short = 'down'
    else:
        dir_short = 'steady'

    return {
        'direction': dir_short,
        'streak_days': 0,
        'flagged': False,
        'message': None,
    }


def generate_sparkline_svg(values, width=70, height=20, padding=3):
    """
    Generates SVG polyline points string for a list of values.
    Fits values into width x height viewBox.
    """
    if not values:
        return ""
    if len(values) == 1 or min(values) == max(values):
        y_mid = height / 2.0
        return f"0,{y_mid:.1f} {width},{y_mid:.1f}"

    min_val = min(values)
    max_val = max(values)
    val_range = max_val - min_val if max_val != min_val else 1.0

    usable_height = height - (padding * 2)
    step_x = width / (len(values) - 1) if len(values) > 1 else width

    points = []
    for idx, val in enumerate(values):
        x = idx * step_x
        # Invert y because SVG 0 is top
        y = (height - padding) - (((val - min_val) / val_range) * usable_height)
        points.append(f"{x:.1f},{y:.1f}")

    return " ".join(points)


def calculate_health_score(metric_evals):
    """
    Overall astronaut Health Score (0-100), Physio Reserve, and Cumulative Stress.
    metric_evals: dict of metric_key -> evaluation dict from evaluate_metric_status

    The score is core/rules.health_score: the mean of green 100 / amber 60 /
    red 20 over the indicators that HAVE data. With fewer than 3 such
    indicators there is no score ("--", an empty ring).
    """
    statuses = [
        SEVERITY_TO_STATUS[item['status']]
        for item in metric_evals.values()
        if item['status'] in SEVERITY_TO_STATUS
    ]
    score = rules.health_score(statuses)

    if score is None:
        label = "No data"
        status = "no_data"
        color = NO_DATA_COLOR
        bio_index = "Bio-Index: Not enough data"
        score_display = "--"
        score_for_ring = 0
    else:
        # Same bands as before: 80+ nominal, 60-79 caution, under 60 critical.
        if score >= 80:
            status = "nominal"
        elif score >= 60:
            status = "caution"
        else:
            status = "critical"
        # A red indicator must never sit under a "Nominal" ring.
        if status == "nominal" and rules.RED in statuses:
            status = "caution"

        if status == "nominal":
            label = "Nominal"
            color = "#22E3A0"
            bio_index = "Bio-Index: Stable"
        elif status == "caution":
            label = "Caution"
            color = "#FFB020"
            bio_index = "Bio-Index: Elevated Workload"
        else:
            label = "Critical"
            color = "#ffb4ab"
            bio_index = "Bio-Index: High Fatigue"
        score_display = score
        score_for_ring = score

    # SVG circle calculation for 160x160 viewBox, r=70 (circumference = 2 * pi * 70 = ~439.82 -> 440)
    circumference = 440
    # dashoffset = circumference - (score / 100) * circumference
    dashoffset = int(round(circumference - (score_for_ring / 100.0) * circumference))

    # Physio reserve: derive from sleep, HRV and resting HR
    hrv_val = metric_evals.get('hrv', {}).get('value') or 62.0
    sleep_val = metric_evals.get('sleep', {}).get('value') or 6.0
    reserve = 70.0 + (hrv_val / 62.0 * 10.0) + (sleep_val / 7.2 * 8.4)
    reserve = round(max(50.0, min(99.0, reserve)), 1)

    # Cumulative stress: derived from mood_stress metric
    stress_val = metric_evals.get('mood_stress', {}).get('value') or 3.0
    if stress_val <= 3.5:
        cumulative_stress = "Low"
    elif stress_val <= 6.5:
        cumulative_stress = "Moderate"
    else:
        cumulative_stress = "Elevated"

    return {
        'score': score_display,
        'label': label,
        'status': status,
        'color': color,
        'bio_index': bio_index,
        'dashoffset': dashoffset,
        'circumference': circumference,
        'physio_reserve': f"{reserve}%",
        'cumulative_stress': cumulative_stress,
    }


def calculate_risk_vectors(metric_evals):
    """
    Derives NASA Human Research Program (HRP) 4-domain risk matrix:
    - Altered Gravity (Resting HR, HRV, Exercise)
    - Isolation (Mood, Sleep)
    - Closed Environment (Cabin CO2, Body temp)
    - Radiation (Prototype / No dosimeter data)
    """
    def get_group_status(keys):
        # Worst status among the linked indicators that HAVE data.
        # If none has data, the group shows "No data" (same 'nodata' key as radiation).
        statuses = [metric_evals[k]['status'] for k in keys if k in metric_evals]
        if 'critical' in statuses:
            return 'critical', 'Critical', 'bg-error-container/60 text-error font-bold', '#ffb4ab'
        if 'caution' in statuses:
            return 'caution', 'Caution', 'bg-[rgba(255,176,32,0.2)] text-[#FFB020] font-bold', '#FFB020'
        if 'nominal' in statuses:
            return 'nominal', 'Nominal', 'bg-[rgba(34,227,160,0.15)] text-[#22E3A0]', '#22E3A0'
        return 'nodata', 'No data', 'bg-surface-container-highest text-outline font-label-caps text-label-caps', NO_DATA_COLOR

    grav_status, grav_label, grav_badge, grav_color = get_group_status(['resting_hr', 'hrv', 'exercise'])
    iso_status, iso_label, iso_badge, iso_color = get_group_status(['mood', 'sleep'])
    env_status, env_label, env_badge, env_color = get_group_status(['cabin_co2', 'body_temp'])

    return [
        {
            'id': 'altered_gravity',
            'title': 'Altered gravity',
            'sub': 'Musculoskeletal & Card',
            'icon': 'public_off',
            'status': grav_status,
            'label': grav_label,
            'badge_class': grav_badge,
            'indicators': 'Resting HR, HRV, Exercise',
            'is_caution': grav_status == 'caution',
        },
        {
            'id': 'isolation',
            'title': 'Isolation',
            'sub': 'Neuro-Behavioral',
            'icon': 'psychology',
            'status': iso_status,
            'label': iso_label,
            'badge_class': iso_badge,
            'indicators': 'Mood, Sleep',
            'is_caution': iso_status == 'caution',
        },
        {
            'id': 'closed_environment',
            'title': 'Closed environment',
            'sub': 'Life Support & Tox',
            'icon': 'filter_alt',
            'status': env_status,
            'label': env_label,
            'badge_class': env_badge,
            'indicators': 'Cabin CO2, Body temp',
            'is_caution': env_status == 'caution',
        },
        {
            'id': 'radiation',
            'title': 'Radiation',
            'sub': 'Galactic Cosmic Rays',
            'icon': 'radiology',
            'status': 'nodata',
            'label': 'No data',
            'badge_class': 'bg-surface-container-highest text-outline font-label-caps text-label-caps',
            'indicators': 'No dosimeter data (prototype)',
            'is_caution': False,
        },
    ]


def sync_alerts_for_astronaut(astronaut):
    """
    Recomputes active alerts for the astronaut from the science rules:
    every metric whose status is caution (amber) or critical (red) gets an Alert.

    Dedupe: the same metric at the same severity does not create a second alert
    while one is still unacknowledged. Amber going to red creates a new red alert.
    Trend-only alerts (four drops in a row) are gone: the science table has no
    trend rule yet (open question Q5). The trend is still shown on the HUD.
    """
    from alerts.models import Alert
    from telemetry.models import Reading

    evals = evaluate_astronaut_metrics(astronaut)

    for metric in METRICS:
        eval_res = evals[metric]
        severity = eval_res['status']
        if severity not in ('caution', 'critical'):
            continue

        already_active = Alert.objects.filter(
            astronaut=astronaut,
            metric=metric,
            severity=severity,
            acknowledged=False,
        ).exists()
        if already_active:
            continue

        latest_reading = (
            Reading.objects.filter(astronaut=astronaut, metric=metric)
            .order_by('-mission_day', '-timestamp')
            .first()
        )
        baseline = eval_res['baseline']
        baseline_text = f" Your baseline is {baseline:.1f} {eval_res['unit']}." if baseline is not None else ""

        Alert.objects.create(
            astronaut=astronaut,
            metric=metric,
            severity=severity,
            title=f"{eval_res['display_label']} {severity}",
            message=f"{eval_res['reason']}{baseline_text}",
            mission_day=latest_reading.mission_day,
            timestamp=latest_reading.timestamp,
            acknowledged=False,
        )
