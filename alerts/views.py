"""
Alerts views and acknowledge actions for Space-Pulse.
Includes integration with environmental safety alerts.
"""

import json

from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from core.views import get_active_astronaut
from alerts.models import Alert
from telemetry.models import Reading
from core.constants import METRIC_CONFIG
from core.services import (
    sync_alerts_for_astronaut,
    amber_edges,
    get_baselines,
    evaluate_astronaut_metrics,
)
from core.alert_texts import parts_for
from core.ground_delay import ground_panel_for


NO_DATA_LABEL = 'No data'


def _related_card(evals, metric_key):
    """Latest related-indicator value and status_label, or No data."""
    ev = evals.get(metric_key) or {}
    config = METRIC_CONFIG.get(metric_key, {})
    unit = ev.get('unit') or config.get('unit', '')
    label = ev.get('display_label') or config.get('label', metric_key)
    value = ev.get('value')
    if value is None:
        return {
            'label': label,
            'value_str': NO_DATA_LABEL,
            'status_label': NO_DATA_LABEL,
            'has_data': False,
        }
    value_str = f"{value:g} {unit}".strip()
    return {
        'label': label,
        'value_str': value_str,
        'status_label': ev.get('label') or NO_DATA_LABEL,
        'has_data': True,
    }


def _four_day_delta(values, unit):
    """Change from the earliest of the last 4 readings to the latest."""
    recent = values[-4:] if values else []
    if len(recent) < 2:
        return NO_DATA_LABEL
    start, end = recent[0], recent[-1]
    if start != 0:
        return f"{((end - start) / start) * 100:+.1f}%"
    return f"{end - start:+.1f} {unit}".strip()


def _caution_range_label(edges, unit):
    low, high = edges.get('low'), edges.get('high')
    if low is not None and high is not None:
        return f"Caution range: {low:g} \u2013 {high:g} {unit}".strip()
    if low is not None:
        return f"Caution below {low:g} {unit}".strip()
    if high is not None:
        return f"Caution above {high:g} {unit}".strip()
    return NO_DATA_LABEL


def _chart_y_bounds(values, baseline, edges):
    nums = list(values)
    if baseline is not None:
        nums.append(baseline)
    for edge in (edges.get('low'), edges.get('high')):
        if edge is not None:
            nums.append(edge)
    if not nums:
        return 0.0, 1.0
    lo, hi = min(nums), max(nums)
    pad = (hi - lo) * 0.15 if hi != lo else (abs(hi) * 0.1 or 1.0)
    return lo - pad, hi + pad


def alerts_list(request):
    """
    View showing all alerts with severity filtering, acknowledgment status,
    diagnostic drill-down links, and integrated environmental hazards.
    """
    astronaut = get_active_astronaut(request)
    if not astronaut:
        return redirect('core:login')

    # Keep SQLite synced with latest trends and thresholds
    sync_alerts_for_astronaut(astronaut)

    severity_filter = request.GET.get('severity', 'all').lower()
    status_filter = request.GET.get('status', 'all').lower()

    alerts_query = Alert.objects.filter(astronaut=astronaut).order_by('-timestamp', '-mission_day')

    # Severity filter
    if severity_filter in ('critical', 'caution', 'nominal'):
        alerts_query = alerts_query.filter(severity=severity_filter)

    # Status filter
    if status_filter == 'active':
        alerts_query = alerts_query.filter(acknowledged=False)
    elif status_filter == 'acknowledged':
        alerts_query = alerts_query.filter(acknowledged=True)

    # Total counts across all astronaut alerts (unfiltered)
    base_all = Alert.objects.filter(astronaut=astronaut)
    total_count = base_all.count()
    critical_count = base_all.filter(severity='critical').count()
    caution_count = base_all.filter(severity='caution').count()
    nominal_count = base_all.filter(severity='nominal').count()
    unack_count = base_all.filter(acknowledged=False).count()

    alerts_data = []
    for a in alerts_query:
        cfg = METRIC_CONFIG.get(a.metric, {})
        icon = cfg.get('icon', 'notifications')

        if a.severity == 'critical':
            badge_bg = 'bg-error-container/20'
            badge_text = 'text-error'
            color_hex = '#ffb4ab'
            severity_label = 'Critical'
            border_accent = 'border-l-4 border-l-error'
        elif a.severity == 'caution':
            badge_bg = 'bg-[#FFB020]/20'
            badge_text = 'text-[#FFB020]'
            color_hex = '#FFB020'
            severity_label = 'Caution'
            border_accent = 'border-l-4 border-l-[#FFB020]'
        else:
            badge_bg = 'bg-[#22E3A0]/20'
            badge_text = 'text-[#22E3A0]'
            color_hex = '#22E3A0'
            severity_label = 'Nominal'
            border_accent = 'border-l-4 border-l-[#22E3A0]'

        alerts_data.append({
            'id': a.id,
            'metric': a.metric,
            'metric_label': cfg.get('label', a.metric),
            'icon': icon,
            'title': a.title,
            'message': a.message,
            'severity': a.severity,
            'severity_label': severity_label,
            'badge_bg': badge_bg,
            'badge_text': badge_text,
            'color_hex': color_hex,
            'border_accent': border_accent,
            'mission_day': a.mission_day,
            'is_today': a.mission_day == astronaut.current_mission_day,
            'timestamp': a.timestamp,
            'acknowledged': a.acknowledged,
        })

    # Integrated Environmental Hazards from Environment Monitor
    try:
        from environment.models import EnvironmentAlert
        env_query = EnvironmentAlert.objects.select_related('reading').order_by('-created_at')
        if status_filter == 'active':
            env_query = env_query.filter(resolved=False)
        elif status_filter == 'acknowledged':
            env_query = env_query.filter(resolved=True)

        if severity_filter == 'critical':
            env_query = env_query.filter(severity='danger')
        elif severity_filter == 'caution':
            env_query = env_query.filter(severity='caution')
        elif severity_filter == 'nominal':
            env_query = env_query.none()

        environment_alerts = list(env_query[:10])
    except Exception:
        environment_alerts = []

    return render(request, 'alerts/list.html', {
        'active_nav': 'alerts',
        'current_astronaut': astronaut,
        'alerts': alerts_data,
        'environment_alerts': environment_alerts,
        'selected_severity': severity_filter,
        'selected_status': status_filter,
        'total_count': total_count + len(environment_alerts),
        'critical_count': critical_count,
        'caution_count': caution_count,
        'nominal_count': nominal_count,
        'unacknowledged_count': unack_count,
    })


def alert_detail(request, alert_id):
    """
    Detailed diagnostic view matching approved design design/alert_details.html.
    Shows telemetry analysis, cognitive impact, action checklist, escalation protocol,
    and 7-day mini trend Chart.js.
    """
    astronaut = get_active_astronaut(request)
    alert = get_object_or_404(Alert, id=alert_id, astronaut=astronaut)

    # Fetch last 7 readings for this alert's metric
    readings = list(
        Reading.objects.filter(astronaut=astronaut, metric=alert.metric)
        .order_by('-mission_day', '-timestamp')[:7]
    )
    readings.reverse()

    chart_labels = [f"D-{r.mission_day}" if r.mission_day != astronaut.current_mission_day else f"D-{r.mission_day} (Today)" for r in readings]
    chart_values = [r.value for r in readings]

    config = METRIC_CONFIG.get(alert.metric, {})
    unit = config.get('unit', '')
    metric_label = config.get('label', alert.metric)

    real_baseline = get_baselines(astronaut).get(alert.metric)
    has_baseline = real_baseline is not None
    # Guide line for the chart: where the amber band starts (core/rules.py).
    # Always a number, because the template prints it straight into JavaScript.
    edges = amber_edges(alert.metric, real_baseline)
    caution_low = next(
        (v for v in (edges['low'], edges['high'], real_baseline) if v is not None),
        0.0,
    )

    if chart_values:
        latest_val = chart_values[-1]
        latest_str = f"{latest_val:g} {unit}".strip()
        mean_val = round(sum(chart_values) / len(chart_values), 1)
        mean_str = f"{mean_val} {unit}".strip()
    else:
        latest_val = None
        latest_str = NO_DATA_LABEL
        mean_val = None
        mean_str = NO_DATA_LABEL

    if has_baseline and latest_val is not None:
        variance = round(latest_val - real_baseline, 1)
        variance_str = f"{variance:+.1f} {unit}".strip()
    else:
        variance_str = NO_DATA_LABEL

    if has_baseline and chart_values:
        deficit = round(sum(v - real_baseline for v in chart_values), 1)
        deficit_str = f"{deficit:+.1f} {unit} total".strip()
    else:
        deficit_str = NO_DATA_LABEL

    evals = evaluate_astronaut_metrics(astronaut)
    chart_y_min, chart_y_max = _chart_y_bounds(chart_values, real_baseline, edges)

    def js_num(value, digits=2):
        if value is None:
            return 'null'
        return f"{value:.{digits}f}"

    return render(request, 'alerts/detail.html', {
        'active_nav': 'alerts',
        'current_astronaut': astronaut,
        'alert': alert,
        'alert_copy': parts_for(alert.metric, alert.severity),
        # Only a packet really uplinked after this alert fills the panel;
        # otherwise the template drafts one and shows it as not sent.
        'ground_panel': ground_panel_for(astronaut, [alert]),
        'chart_labels': chart_labels,
        'chart_values': chart_values,
        'baseline_val': real_baseline,
        'has_baseline': has_baseline,
        'baseline_js': js_num(real_baseline, 1),
        'caution_low': caution_low,
        'caution_band_low_js': js_num(edges['low']),
        'caution_band_high_js': js_num(edges['high']),
        'caution_range_str': _caution_range_label(edges, unit),
        'latest_val': latest_val,
        'latest_str': latest_str,
        'variance_str': variance_str,
        'mean_val': mean_val,
        'mean_str': mean_str,
        'deficit_str': deficit_str,
        'four_day_delta_str': _four_day_delta(chart_values, unit),
        'unit': unit,
        'unit_json': json.dumps(unit),
        'metric_label': metric_label,
        'metric_label_json': json.dumps(metric_label),
        'related_hrv': _related_card(evals, 'hrv'),
        'related_stress': _related_card(evals, 'mood_stress'),
        'chart_y_min': chart_y_min,
        'chart_y_max': chart_y_max,
    })

@require_POST
def acknowledge_alert(request, alert_id):
    """Marks an alert as acknowledged in SQLite."""
    astronaut = get_active_astronaut(request)
    alert = get_object_or_404(Alert, id=alert_id, astronaut=astronaut)
    alert.acknowledged = True
    alert.save()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'status': 'ok', 'alert_id': alert.id})

    return redirect('alerts:list')


@require_POST
def acknowledge_all_alerts(request):
    """Marks all unacknowledged alerts as acknowledged for current astronaut."""
    astronaut = get_active_astronaut(request)
    Alert.objects.filter(astronaut=astronaut, acknowledged=False).update(acknowledged=True)
    return redirect('alerts:list')
