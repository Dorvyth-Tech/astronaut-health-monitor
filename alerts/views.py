"""
Alerts views and acknowledge actions for Space-Pulse.
Includes integration with environmental safety alerts.
"""

from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from core.views import get_active_astronaut
from alerts.models import Alert
from core.models import Baseline
from telemetry.models import Reading
from core.constants import METRIC_CONFIG
from core.services import sync_alerts_for_astronaut, amber_edges, get_baselines


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

    # Baseline & thresholds
    baseline_val = get_baselines(astronaut).get(alert.metric, METRIC_CONFIG.get(alert.metric, {}).get('default_baseline', 7.2))

    config = METRIC_CONFIG.get(alert.metric, {})
    edges = amber_edges(alert.metric, baseline_val)
    caution_low = next((v for v in (edges['low'], edges['high']) if v is not None), baseline_val)
    unit = config.get('unit', 'h')

    latest_val = chart_values[-1] if chart_values else baseline_val
    variance = round(latest_val - baseline_val, 1)
    variance_str = f"{variance:+.1f} {unit}"

    # Calculate 7-day mean
    mean_val = round(sum(chart_values) / len(chart_values), 1) if chart_values else latest_val
    deficit = round(sum(v - baseline_val for v in chart_values), 1) if chart_values else 0.0

    return render(request, 'alerts/detail.html', {
        'active_nav': 'alerts',
        'current_astronaut': astronaut,
        'alert': alert,
        'chart_labels': chart_labels,
        'chart_values': chart_values,
        'baseline_val': baseline_val,
        'caution_low': caution_low,
        'latest_val': latest_val,
        'variance_str': variance_str,
        'mean_val': mean_val,
        'deficit_str': f"{deficit:+.1f} {unit} total",
        'unit': unit,
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
