"""
Core telemetry processing and clinical algorithms for Space-Pulse.
NASA Human Research Program (HRP) spaceflight health assessment logic.
"""

from django.utils import timezone
from core.constants import METRIC_CONFIG, METRICS


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


def evaluate_metric_status(metric_key, value, baseline_value=None):
    """
    Evaluates a metric's current value against baseline and thresholds.
    Returns status: 'nominal', 'caution', or 'critical' plus UI styling helpers.
    """
    config = METRIC_CONFIG.get(metric_key, {})
    caution_low = config.get('caution_low')
    caution_high = config.get('caution_high')
    critical_low = config.get('critical_low')
    critical_high = config.get('critical_high')

    status = 'nominal'

    # Check critical bounds
    if critical_low is not None and value <= critical_low:
        status = 'critical'
    elif critical_high is not None and value >= critical_high:
        status = 'critical'
    # Check caution bounds
    elif caution_low is not None and value < caution_low:
        status = 'caution'
    elif caution_high is not None and value > caution_high:
        status = 'caution'

    # Style mapping per NASA HUD specification
    # Nominal: #22E3A0 (Emerald green), Caution: #FFB020 (Amber), Critical: #ffb4ab (Red/Coral)
    if status == 'nominal':
        badge_bg = 'bg-[rgba(34,227,160,0.15)]'
        badge_text = 'text-[#22E3A0]'
        color_hex = '#22E3A0'
        label = 'Nominal'
    elif status == 'caution':
        badge_bg = 'bg-[rgba(255,176,32,0.2)]'
        badge_text = 'text-[#FFB020]'
        color_hex = '#FFB020'
        label = 'Caution'
    else:
        badge_bg = 'bg-error-container/60'
        badge_text = 'text-error'
        color_hex = '#ffb4ab'
        label = 'Critical'

    return {
        'status': status,
        'label': label,
        'badge_bg': badge_bg,
        'badge_text': badge_text,
        'color_hex': color_hex,
        'value': value,
        'baseline': baseline_value if baseline_value is not None else config.get('default_baseline'),
        'unit': config.get('unit', ''),
        'display_label': config.get('label', metric_key),
        'icon': config.get('icon', 'vital_signs'),
    }


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
    Computes overall astronaut Health Score (0-100), Physio Reserve, and Cumulative Stress.
    metric_evals: dict of metric_key -> evaluation dict from evaluate_metric_status
    """
    weights = {
        'sleep': 0.22,
        'resting_hr': 0.18,
        'hrv': 0.18,
        'exercise': 0.14,
        'cabin_co2': 0.12,
        'body_temp': 0.08,
        'mood_stress': 0.08,
    }

    total_score = 0.0
    for key, weight in weights.items():
        eval_item = metric_evals.get(key)
        if not eval_item:
            subscore = 85.0
        elif eval_item['status'] == 'nominal':
            subscore = 92.0
        elif eval_item['status'] == 'caution':
            subscore = 65.0
        else:
            subscore = 35.0
        total_score += subscore * weight

    # Rounded integer health score
    score = int(round(total_score))
    # Keep in 0-100 bounds
    score = max(0, min(100, score))

    if score >= 80:
        label = "Nominal"
        status = "nominal"
        color = "#22E3A0"
        bio_index = "Bio-Index: Stable"
    elif score >= 60:
        label = "Caution"
        status = "caution"
        color = "#FFB020"
        bio_index = "Bio-Index: Elevated Workload"
    else:
        label = "Critical"
        status = "critical"
        color = "#ffb4ab"
        bio_index = "Bio-Index: High Fatigue"

    # SVG circle calculation for 160x160 viewBox, r=70 (circumference = 2 * pi * 70 = ~439.82 -> 440)
    circumference = 440
    # dashoffset = circumference - (score / 100) * circumference
    dashoffset = int(round(circumference - (score / 100.0) * circumference))

    # Physio reserve: derive from sleep, HRV and resting HR
    hrv_val = metric_evals.get('hrv', {}).get('value', 62.0)
    sleep_val = metric_evals.get('sleep', {}).get('value', 6.0)
    reserve = 70.0 + (hrv_val / 62.0 * 10.0) + (sleep_val / 7.2 * 8.4)
    reserve = round(max(50.0, min(99.0, reserve)), 1)

    # Cumulative stress: derived from mood_stress metric
    stress_val = metric_evals.get('mood_stress', {}).get('value', 3.0)
    if stress_val <= 3.5:
        cumulative_stress = "Low"
    elif stress_val <= 6.5:
        cumulative_stress = "Moderate"
    else:
        cumulative_stress = "Elevated"

    return {
        'score': score,
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
    - Isolation (Mood & stress, Sleep)
    - Closed Environment (Cabin CO2, Body temp)
    - Radiation (Prototype / No dosimeter data)
    """
    def get_group_status(keys):
        statuses = [metric_evals[k]['status'] for k in keys if k in metric_evals]
        if 'critical' in statuses:
            return 'critical', 'Critical', 'bg-error-container/60 text-error font-bold', '#ffb4ab'
        if 'caution' in statuses:
            return 'caution', 'Caution', 'bg-[rgba(255,176,32,0.2)] text-[#FFB020] font-bold', '#FFB020'
        return 'nominal', 'Nominal', 'bg-[rgba(34,227,160,0.15)] text-[#22E3A0]', '#22E3A0'

    grav_status, grav_label, grav_badge, grav_color = get_group_status(['resting_hr', 'hrv', 'exercise'])
    iso_status, iso_label, iso_badge, iso_color = get_group_status(['mood_stress', 'sleep'])
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
            'indicators': 'Mood & stress, Sleep',
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
    Recomputes active alerts for the astronaut based on:
    1. Metric threshold violations (e.g. sleep < 6.0, cabin co2 > 3000)
    2. Dynamic consecutive trend detections (e.g. sleep dropping 4 days in a row)
    Stores or updates Alert models in SQLite.
    """
    from alerts.models import Alert
    from telemetry.models import Reading
    from core.models import Baseline

    # Get baselines
    baselines = {b.metric: b.baseline_value for b in Baseline.objects.filter(astronaut=astronaut)}

    for metric in METRICS:
        recent_readings = list(
            Reading.objects.filter(astronaut=astronaut, metric=metric)
            .order_by('-mission_day', '-timestamp')[:7]
        )
        if not recent_readings:
            continue

        latest_reading = recent_readings[0]
        # Chronological order
        chronological_vals = [r.value for r in reversed(recent_readings)]
        baseline = baselines.get(metric, METRIC_CONFIG.get(metric, {}).get('default_baseline', 0))

        eval_res = evaluate_metric_status(metric, latest_reading.value, baseline)
        trend = detect_consecutive_trend(chronological_vals)

        if eval_res['status'] in ('caution', 'critical') or trend['flagged']:
            severity = eval_res['status'] if eval_res['status'] != 'nominal' else 'caution'

            # Dynamic alert messaging (never hardcoded)
            if trend['flagged'] and trend['message']:
                title = f"{eval_res['display_label']} is trending {trend['direction']}"
                first_val = chronological_vals[0]
                message = (
                    f"{latest_reading.value:.1f} {eval_res['unit']} today, "
                    f"down from {first_val:.1f} {eval_res['unit']} {trend['streak_days']} days ago. "
                    f"Your baseline is {baseline:.1f} {eval_res['unit']}."
                    if trend['direction'] == 'down' else
                    f"{latest_reading.value:.1f} {eval_res['unit']} today, "
                    f"up from {first_val:.1f} {eval_res['unit']} {trend['streak_days']} days ago. "
                    f"Your baseline is {baseline:.1f} {eval_res['unit']}."
                )
            else:
                title = f"{eval_res['display_label']} threshold {severity}"
                message = (
                    f"Current reading {latest_reading.value:.1f} {eval_res['unit']} is in {severity} range. "
                    f"Baseline: {baseline:.1f} {eval_res['unit']}."
                )

            # Avoid duplicating active unacknowledged alert for the same metric today
            existing_alert = Alert.objects.filter(
                astronaut=astronaut,
                metric=metric,
                mission_day=latest_reading.mission_day,
                acknowledged=False
            ).first()

            if not existing_alert:
                Alert.objects.create(
                    astronaut=astronaut,
                    metric=metric,
                    severity=severity,
                    title=title,
                    message=message,
                    mission_day=latest_reading.mission_day,
                    timestamp=latest_reading.timestamp,
                    acknowledged=False
                )
