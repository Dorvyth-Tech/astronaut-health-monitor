"""
Telemetry views and API endpoints for Space-Pulse.
"""

from datetime import timedelta
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.views import get_active_astronaut
from telemetry.models import Reading, CheckIn
from alerts.models import Alert
from core.models import Baseline
from core.constants import METRIC_CONFIG, METRICS, METRIC_CHOICES
from core.services import sync_alerts_for_astronaut, reading_statuses, amber_edges, generate_sparkline_svg
from django.core.paginator import Paginator


def api_readings(request):
    """
    JSON API endpoint for Chart.js telemetry fetching.
    Supports ?metric=sleep&range=7d (or 30d, mission)
    """
    astronaut = get_active_astronaut(request)
    if not astronaut:
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    metric = request.GET.get('metric', 'sleep')
    time_range = request.GET.get('range', '7d').lower()

    query = Reading.objects.filter(astronaut=astronaut, metric=metric)

    if time_range == '7d':
        limit = 7
    elif time_range == '30d':
        limit = 30
    else:
        # Full mission cycle
        limit = 142

    readings = list(query.order_by('-mission_day', '-timestamp')[:limit])
    readings.reverse()

    labels = []
    values = []
    for r in readings:
        if r.mission_day == astronaut.current_mission_day:
            labels.append(f"D-{r.mission_day} (Today)")
        else:
            labels.append(f"D-{r.mission_day}")
        values.append(r.value)

    # Fetch astronaut baseline
    baseline_obj = Baseline.objects.filter(astronaut=astronaut, metric=metric).first()
    baseline_val = baseline_obj.baseline_value if baseline_obj else METRIC_CONFIG.get(metric, {}).get('default_baseline', 7.2)

    config = METRIC_CONFIG.get(metric, {})
    # Chart guide lines: where the amber band starts for this baseline (core/rules.py)
    edges = amber_edges(metric, baseline_val)
    caution_low = edges['low']
    caution_high = edges['high']

    return JsonResponse({
        'metric': metric,
        'label': config.get('label', metric),
        'unit': config.get('unit', ''),
        'range': time_range,
        'labels': labels,
        'values': values,
        'baseline': baseline_val,
        'caution_low': caution_low,
        'caution_high': caution_high,
    })


@require_POST
def add_reading(request):
    """
    POST endpoint for manual telemetry recording from the HUD modal.
    CSRF protected, saves to SQLite, recomputes status and alerts.
    """
    astronaut = get_active_astronaut(request)
    if not astronaut:
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    metric = request.POST.get('metric')
    value_str = request.POST.get('value')

    if not metric or not value_str:
        return JsonResponse({'error': 'Metric and value are required.'}, status=400)

    try:
        val = float(value_str)
    except ValueError:
        return JsonResponse({'error': 'Invalid numerical value.'}, status=400)

    # Save to SQLite
    reading = Reading.objects.create(
        astronaut=astronaut,
        metric=metric,
        value=val,
        mission_day=astronaut.current_mission_day,
        timestamp=timezone.now(),
        source='manual'
    )

    # Recompute alerts and thresholds
    sync_alerts_for_astronaut(astronaut)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'status': 'ok',
            'metric': metric,
            'value': val,
            'id': reading.id
        })

    return redirect(request.META.get('HTTP_REFERER', reverse('core:dashboard')))


def check_in_view(request):
    """
    Daily self-reported evaluation form view.
    Saves to SQLite, updates Reading records, and recomputes health score & alerts.
    """
    astronaut = get_active_astronaut(request)
    if not astronaut:
        return redirect('core:login')

    submitted_success = False
    packet_id = f"SOL{astronaut.current_mission_day}-06"

    if request.method == 'POST':
        try:
            mood = int(request.POST.get('mood', 4))
            stress = int(request.POST.get('stress', 3))
            sleep_hours = float(request.POST.get('sleep_hours', 7.2))
            exercise_minutes = int(float(request.POST.get('exercise_minutes', 60)))
            
            resting_hr_raw = request.POST.get('resting_hr')
            resting_hr = float(resting_hr_raw) if resting_hr_raw else None
            
            hrv_raw = request.POST.get('hrv')
            hrv = float(hrv_raw) if hrv_raw else None
            
            body_temp_raw = request.POST.get('body_temp')
            body_temp = float(body_temp_raw) if body_temp_raw else None
            
            notes = request.POST.get('notes', '').strip()
            symptoms = request.POST.get('symptoms', '').strip()
            if symptoms:
                notes = f"Symptoms logged: {symptoms}. {notes}".strip()

            # Save CheckIn in SQLite
            checkin = CheckIn.objects.create(
                astronaut=astronaut,
                mission_day=astronaut.current_mission_day,
                mood=mood,
                stress=stress,
                sleep_hours=sleep_hours,
                exercise_minutes=exercise_minutes,
                resting_hr=resting_hr,
                hrv=hrv,
                body_temp=body_temp,
                notes=notes,
                timestamp=timezone.now()
            )

            # Sync to Reading telemetry
            now = timezone.now()
            Reading.objects.create(
                astronaut=astronaut, metric='sleep', value=sleep_hours,
                mission_day=astronaut.current_mission_day, timestamp=now, source='manual'
            )
            Reading.objects.create(
                astronaut=astronaut, metric='mood', value=float(mood),
                mission_day=astronaut.current_mission_day, timestamp=now, source='manual'
            )
            Reading.objects.create(
                astronaut=astronaut, metric='mood_stress', value=float(stress),
                mission_day=astronaut.current_mission_day, timestamp=now, source='manual'
            )
            Reading.objects.create(
                astronaut=astronaut, metric='exercise', value=float(exercise_minutes),
                mission_day=astronaut.current_mission_day, timestamp=now, source='manual'
            )
            if resting_hr:
                Reading.objects.create(
                    astronaut=astronaut, metric='resting_hr', value=resting_hr,
                    mission_day=astronaut.current_mission_day, timestamp=now, source='manual'
                )
            if hrv:
                Reading.objects.create(
                    astronaut=astronaut, metric='hrv', value=hrv,
                    mission_day=astronaut.current_mission_day, timestamp=now, source='manual'
                )
            if body_temp:
                Reading.objects.create(
                    astronaut=astronaut, metric='body_temp', value=body_temp,
                    mission_day=astronaut.current_mission_day, timestamp=now, source='manual'
                )

            # Recompute active signals and thresholds
            sync_alerts_for_astronaut(astronaut)

            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'status': 'ok',
                    'packet_id': packet_id,
                    'checkin_id': checkin.id
                })

            submitted_success = True
        except Exception as e:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

    # Defaults for form fields
    latest_checkin = CheckIn.objects.filter(astronaut=astronaut).order_by('-mission_day', '-timestamp').first()

    return render(request, 'telemetry/check_in.html', {
        'active_nav': 'check_in',
        'current_astronaut': astronaut,
        'latest_checkin': latest_checkin,
        'submitted_success': submitted_success,
        'packet_id': packet_id,
    })


def history_view(request):
    """
    Approved Biometric Telemetry & Audit History view matching design/history.html.
    Supports Table & Timeline views, Filter Matrix by indicator, range, and severity,
    inline SVG sparklines, and real paginated audit logs from SQLite.
    """
    astronaut = get_active_astronaut(request)
    if not astronaut:
        return redirect('core:login')

    # Re-evaluate signals in SQLite
    sync_alerts_for_astronaut(astronaut)

    # Filter params
    indicator_param = request.GET.get('indicator') or request.GET.get('metric') or 'all'
    range_param = request.GET.get('range', '7d').lower()
    severity_param = request.GET.get('severity', 'all').lower()
    view_param = request.GET.get('view', 'table').lower()

    # Determine day cutoff for temporal range
    current_day = astronaut.current_mission_day
    if range_param == '7d':
        day_min = max(1, current_day - 6)
    elif range_param == '30d':
        day_min = max(1, current_day - 29)
    else:
        day_min = 1

    # Baselines dictionary
    baselines = {b.metric: b.baseline_value for b in Baseline.objects.filter(astronaut=astronaut)}

    # Location mappings for realistic audit provenance
    sensor_locations = {
        'sleep': 'Hab-1 Bunk Pod A',
        'resting_hr': 'Biosensor Patch #2',
        'hrv': 'Biosensor Patch #2',
        'cabin_co2': 'ECLSS Scrubber B',
        'exercise': 'ARED / Cycle Ergometer',
        'body_temp': 'Subdermal Temp Sensor',
        'mood': 'Console Alpha',
        'mood_stress': 'EVA Console Alpha',
    }

    # Pre-compute last 7 values per metric for SVG sparklines
    metric_sparklines = {}
    for m_key in METRICS:
        recent_7 = list(
            Reading.objects.filter(astronaut=astronaut, metric=m_key)
            .order_by('-mission_day', '-timestamp')[:7]
        )
        vals_chrono = [r.value for r in reversed(recent_7)]
        metric_sparklines[m_key] = generate_sparkline_svg(vals_chrono, width=100, height=24, padding=4)

    # Assemble Audit Log items
    audit_items = []

    # 1. Alerts
    alerts_qs = Alert.objects.filter(astronaut=astronaut, mission_day__gte=day_min)
    if indicator_param != 'all':
        alerts_qs = alerts_qs.filter(metric=indicator_param)
    if severity_param in ('nominal', 'caution', 'critical'):
        alerts_qs = alerts_qs.filter(severity=severity_param)

    for al in alerts_qs:
        cfg = METRIC_CONFIG.get(al.metric, {})
        unit = cfg.get('unit', '')
        b_val = baselines.get(al.metric, cfg.get('default_baseline', 0.0))

        # Latest reading value for this metric on or before alert day
        latest_r = Reading.objects.filter(
            astronaut=astronaut, metric=al.metric, mission_day__lte=al.mission_day
        ).order_by('-mission_day', '-timestamp').first()
        r_val = latest_r.value if latest_r else b_val
        delta = round(r_val - b_val, 1)

        audit_items.append({
            'sort_key': (al.mission_day, al.timestamp.timestamp()),
            'day': al.mission_day,
            'time_str': al.timestamp.strftime('%H:%M'),
            'location': sensor_locations.get(al.metric, 'Hab-1 Medical Pod'),
            'type': 'alert',
            'type_label': 'Alert',
            'indicator': f"{cfg.get('label', al.metric)} Architecture" if al.metric == 'sleep' else cfg.get('label', al.metric),
            'recorded_value': f"{r_val:.1f} {unit}".strip(),
            'val_number': f"{r_val:.1f}",
            'val_unit': unit,
            'value_highlight': 'text-error' if al.severity == 'critical' else ('text-[#FFB020]' if al.severity == 'caution' else 'text-primary'),
            'sub_text': f"Cumulative delta {delta:+.1f}{unit}" if al.metric != 'cabin_co2' else f"Baseline {int(round(b_val)):,} ppm",
            'status': al.severity,
            'status_label': al.severity.capitalize(),
            'status_icon': 'warning' if al.severity == 'caution' else ('error' if al.severity == 'critical' else 'check_circle'),
            'status_class': 'bg-error-container text-on-error-container' if al.severity == 'critical' else ('bg-[#ffb020]/15 text-[#ffb020]' if al.severity == 'caution' else 'bg-[#22e3a0]/15 text-[#22e3a0]'),
            'sparkline': metric_sparklines.get(al.metric),
            'action_type': 'alert_link',
            'action_url': reverse('alerts:detail', args=[al.id]),
            'action_label': 'View alert',
            'message': al.message,
            'title': al.title,
        })

    # 2. Check-ins (only when indicator is 'all' or 'mood_stress')
    if indicator_param in ('all', 'mood_stress'):
        checkins_qs = CheckIn.objects.filter(astronaut=astronaut, mission_day__gte=day_min)
        for ck in checkins_qs:
            status = 'caution' if (ck.stress >= 7 or ck.mood <= 2) else 'nominal'
            if severity_param != 'all' and severity_param != status:
                continue

            audit_items.append({
                'sort_key': (ck.mission_day, ck.timestamp.timestamp()),
                'day': ck.mission_day,
                'time_str': ck.timestamp.strftime('%H:%M'),
                'location': 'Console Alpha',
                'type': 'checkin',
                'type_label': 'Check-in',
                'indicator': 'Daily Check-in',
                'recorded_value': f"Mood {ck.mood}/5, Stress {ck.stress}/10",
                'val_number': f"{ck.mood}/5",
                'val_unit': f"Stress {ck.stress}/10",
                'value_highlight': 'text-on-surface',
                'sub_text': f"Sleep {ck.sleep_hours:.1f}h · Exercise {ck.exercise_minutes}m",
                'status': status,
                'status_label': status.capitalize(),
                'status_icon': 'check_circle' if status == 'nominal' else 'warning',
                'status_class': 'bg-[#22e3a0]/15 text-[#22e3a0]' if status == 'nominal' else 'bg-[#ffb020]/15 text-[#ffb020]',
                'sparkline': None,
                'action_type': 'text',
                'action_label': 'Archived',
                'title': 'Morning Subjective Assessment Completed',
                'message': f"Astronaut scored Mood {ck.mood}/5, Subjective Stress {ck.stress}/10. {ck.notes or 'Visual clarity confirmed; vestibular adaptation level stable.'}",
            })

    # 3. Readings
    readings_qs = Reading.objects.filter(astronaut=astronaut, mission_day__gte=day_min)
    if indicator_param != 'all':
        readings_qs = readings_qs.filter(metric=indicator_param)

    # Status of each reading as it looked when it arrived (rules need recent history,
    # so they are evaluated per metric once, then looked up by reading id).
    status_by_reading = {}

    def eval_for(reading):
        if reading.metric not in status_by_reading:
            status_by_reading[reading.metric] = reading_statuses(astronaut, reading.metric, baselines)
        return status_by_reading[reading.metric][reading.id]

    # Sample readings to keep log clean (latest 60 readings in window)
    for rd in readings_qs.order_by('-mission_day', '-timestamp')[:60]:
        cfg = METRIC_CONFIG.get(rd.metric, {})
        unit = cfg.get('unit', '')
        b_val = baselines.get(rd.metric, cfg.get('default_baseline', 0.0))
        eval_dict = eval_for(rd)
        status = eval_dict['status']

        if severity_param in ('nominal', 'caution', 'critical') and status != severity_param:
            continue

        if rd.metric == 'cabin_co2':
            fmt_val = f"{int(round(rd.value)):,}"
        elif rd.metric in ('resting_hr', 'hrv', 'exercise', 'mood', 'mood_stress'):
            fmt_val = f"{int(round(rd.value))}"
        else:
            fmt_val = f"{rd.value:.1f}"

        audit_items.append({
            'sort_key': (rd.mission_day, rd.timestamp.timestamp()),
            'day': rd.mission_day,
            'time_str': rd.timestamp.strftime('%H:%M'),
            'location': sensor_locations.get(rd.metric, 'Biosensor Stream'),
            'type': 'reading',
            'type_label': 'Reading',
            'icon': cfg.get('icon', 'monitor_heart'),
            'indicator': cfg.get('label', rd.metric),
            'recorded_value': f"{fmt_val} {unit}".strip(),
            'val_number': fmt_val,
            'val_unit': unit,
            'value_highlight': 'text-error' if status == 'critical' else ('text-[#FFB020]' if status == 'caution' else ('text-outline' if status == 'no_data' else 'text-primary')),
            'sub_text': f"Baseline: {b_val} {unit}".strip(),
            'status': status,
            'status_label': eval_dict['label'],
            'status_icon': {'nominal': 'check_circle', 'caution': 'warning', 'no_data': 'help'}.get(status, 'error'),
            'status_class': {
                'critical': 'bg-error-container text-on-error-container',
                'caution': 'bg-[#ffb020]/15 text-[#ffb020]',
                'no_data': 'bg-surface-container-highest text-outline',
            }.get(status, 'bg-[#22e3a0]/15 text-[#22e3a0]'),
            'sparkline': metric_sparklines.get(rd.metric),
            'action_type': 'text',
            'action_label': f"Telemetry #{rd.id}",
            'title': f"{cfg.get('label', rd.metric)} Synchronized ({fmt_val} {unit})",
            'message': eval_dict['reason'],
        })

    # Sort descending by (day, timestamp)
    audit_items.sort(key=lambda x: x['sort_key'], reverse=True)

    # Metrics summary counts for top strip
    total_logs_count = Reading.objects.filter(astronaut=astronaut).count()
    anomalies_7d_count = Alert.objects.filter(
        astronaut=astronaut,
        mission_day__gte=max(1, current_day - 6),
        severity__in=['caution', 'critical']
    ).count()

    # Timeline recent items (top 6 high-interest items)
    timeline_items = [item for item in audit_items if item['type'] in ('alert', 'checkin') or item['status'] in ('caution', 'critical')][:6]
    if len(timeline_items) < 4:
        timeline_items = audit_items[:6]

    # Pagination for table view
    page_number = request.GET.get('page', 1)
    paginator = Paginator(audit_items, 8)
    page_obj = paginator.get_page(page_number)

    return render(request, 'telemetry/history.html', {
        'active_nav': 'history',
        'current_astronaut': astronaut,
        'total_logs_count': f"{total_logs_count:,}",
        'anomalies_7d_count': f"{anomalies_7d_count} Logged",
        'selected_indicator': indicator_param,
        'selected_range': range_param,
        'selected_severity': severity_param,
        'active_view': view_param,
        'page_obj': page_obj,
        'table_rows': page_obj.object_list,
        'timeline_items': timeline_items,
        'total_filtered_count': len(audit_items),
        'indicators': METRIC_CHOICES,
    })
