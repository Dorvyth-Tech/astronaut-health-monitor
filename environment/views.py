"""
Views for the Environment Monitor & Space Weather module:
- HTML dashboard view with tabbed interface for Earth, Orbit, Moon, and Mars
- JSON API endpoint (/api/environment/?location=...) supporting async live polling
- JSON API endpoint (/api/space-weather/) delivering 7-day NASA DONKI event timeline
"""

from datetime import timedelta
from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.http import require_GET

from core.views import get_active_astronaut
from .models import EnvironmentReading, EnvironmentAlert, SpaceWeatherEvent
from .services import (
    fetch_earth_weather,
    fetch_space_weather,
    simulate_environment,
    calculate_radiation_level,
    check_thresholds,
    get_metric_status,
    SAFE_RANGES,
    detect_symptom_environment_correlation,
)


def _compute_trends(history_readings):
    """
    Computes simple trend direction ('up', 'down', 'steady') for each metric
    based on chronological readings.
    """
    if len(history_readings) < 2:
        return {
            'temperature': 'steady',
            'pressure': 'steady',
            'humidity': 'steady',
            'oxygen_percent': 'steady',
            'co2_ppm': 'steady',
            'radiation_level': 'steady',
        }

    latest = history_readings[-1]
    prior = history_readings[-2]

    def trend(val_now, val_prev, min_diff):
        diff = val_now - val_prev
        if abs(diff) < min_diff:
            return 'steady'
        return 'up' if diff > 0 else 'down'

    return {
        'temperature': trend(latest.temperature, prior.temperature, 0.2),
        'pressure': trend(latest.pressure, prior.pressure, 0.2),
        'humidity': trend(latest.humidity, prior.humidity, 0.5),
        'oxygen_percent': trend(latest.oxygen_percent, prior.oxygen_percent, 0.1),
        'co2_ppm': trend(latest.co2_ppm, prior.co2_ppm, 25.0),
        'radiation_level': trend(latest.radiation_level, prior.radiation_level, 0.5),
    }


def _serialize_reading(reading, trends=None):
    """Formats an EnvironmentReading into clean JSON-serializable structure."""
    trends = trends or {
        'temperature': 'steady',
        'pressure': 'steady',
        'humidity': 'steady',
        'oxygen_percent': 'steady',
        'co2_ppm': 'steady',
        'radiation_level': 'steady',
    }

    statuses = {
        'temperature': get_metric_status('temperature', reading.temperature),
        'pressure': get_metric_status('pressure', reading.pressure),
        'humidity': get_metric_status('humidity', reading.humidity),
        'oxygen_percent': get_metric_status('oxygen_percent', reading.oxygen_percent),
        'co2_ppm': get_metric_status('co2_ppm', reading.co2_ppm),
        'radiation_level': get_metric_status('radiation_level', reading.radiation_level),
    }

    return {
        'id': reading.id,
        'location': reading.location,
        'location_name': reading.get_location_display(),
        'temperature': round(reading.temperature, 1),
        'pressure': round(reading.pressure, 1),
        'humidity': round(reading.humidity, 1),
        'oxygen_percent': round(reading.oxygen_percent, 1),
        'co2_ppm': round(reading.co2_ppm, 0),
        'radiation_level': round(reading.radiation_level, 2),
        'is_simulated': reading.is_simulated,
        'data_source': reading.data_source,
        'timestamp': reading.timestamp.isoformat(),
        'time_display': reading.timestamp.strftime('%H:%M UTC'),
        'trends': trends,
        'statuses': statuses,
    }


@login_not_required
@require_GET
def environment_api_view(request):
    """
    GET /api/environment/?location=earth|orbit|moon|mars
    Returns current reading, 24-hour history, active alerts, active space weather,
    radiation breakdown (NASA live vs simulated), and comparison vs launch site.
    """
    location = request.GET.get('location', 'earth').lower().strip()
    if location not in ('earth', 'orbit', 'moon', 'mars'):
        location = 'earth'

    api_available = True
    is_cached = False

    # Obtain or generate current reading
    if location == 'earth':
        reading, api_available, is_cached = fetch_earth_weather()
    else:
        reading = simulate_environment(location)

    # Check thresholds and log any caution/danger alerts
    check_thresholds(reading)

    # Query active space weather events (last 48h) and radiation breakdown
    rad_level, rad_source, causal_event = calculate_radiation_level(location)
    cutoff_48h = timezone.now() - timedelta(hours=48)
    active_sw_events = list(
        SpaceWeatherEvent.objects.filter(start_time__gte=cutoff_48h)
        .order_by('-start_time')[:5]
    )

    active_sw_data = [
        {
            'id': e.id,
            'event_type': e.event_type,
            'event_type_display': e.get_event_type_display(),
            'external_id': e.external_id,
            'intensity': e.intensity,
            'severity': e.severity,
            'severity_display': e.get_severity_display(),
            'is_sample': e.is_sample,
            'start_time': e.start_time.isoformat(),
            'time_display': e.start_time.strftime('%d %b %H:%M UTC'),
            'advice': e.plain_language_advice,
        }
        for e in active_sw_events
    ]

    # 24-hour history query
    since_24h = timezone.now() - timedelta(hours=24)
    history_queryset = list(
        EnvironmentReading.objects.filter(
            location=location,
            timestamp__gte=since_24h
        ).order_by('timestamp')
    )

    if not history_queryset:
        history_queryset = [reading]

    trends = _compute_trends(history_queryset)
    current_payload = _serialize_reading(reading, trends)
    current_payload['radiation_level'] = round(rad_level, 2)
    current_payload['radiation_source'] = rad_source
    current_payload['radiation_causal_event'] = causal_event

    # Serialize 24h history for Chart.js
    history_data = [
        {
            'timestamp': r.timestamp.isoformat(),
            'time': r.timestamp.strftime('%H:%M'),
            'temperature': round(r.temperature, 1),
            'pressure': round(r.pressure, 1),
            'humidity': round(r.humidity, 1),
            'oxygen_percent': round(r.oxygen_percent, 1),
            'co2_ppm': round(r.co2_ppm, 0),
            'radiation_level': round(r.radiation_level, 2),
        }
        for r in history_queryset
    ]

    # Active alerts for this location
    active_alerts = list(
        EnvironmentAlert.objects.filter(
            reading__location=location,
            resolved=False
        ).order_by('-created_at')[:8]
    )

    alerts_data = [
        {
            'id': a.id,
            'message': a.message,
            'severity': a.severity,
            'created_at': a.created_at.strftime('%H:%M UTC'),
        }
        for a in active_alerts
    ]

    # Fetch Earth baseline comparison (Cape Canaveral)
    earth_latest = EnvironmentReading.objects.filter(location='earth').order_by('-timestamp').first()
    earth_comparison = None
    if earth_latest and location != 'earth':
        earth_comparison = {
            'earth_temp': round(earth_latest.temperature, 1),
            'earth_pressure': round(earth_latest.pressure, 1),
            'earth_co2': round(earth_latest.co2_ppm, 0),
            'earth_rad': round(earth_latest.radiation_level, 2),
            'temp_diff': round(reading.temperature - earth_latest.temperature, 1),
            'pressure_diff': round(reading.pressure - earth_latest.pressure, 1),
            'co2_diff': round(reading.co2_ppm - earth_latest.co2_ppm, 0),
            'rad_diff': round(rad_level - earth_latest.radiation_level, 2),
        }

    return JsonResponse({
        'status': 'success',
        'location': location,
        'location_name': reading.get_location_display(),
        'data_source': reading.data_source,
        'is_simulated': reading.is_simulated,
        'api_available': api_available,
        'is_cached': is_cached,
        'radiation_source': rad_source,
        'radiation_event_note': causal_event,
        'active_space_weather_events': active_sw_data,
        'current': current_payload,
        'history_24h': history_data,
        'active_alerts': alerts_data,
        'earth_comparison': earth_comparison,
        'safe_ranges': SAFE_RANGES,
    })


@login_not_required
@require_GET
def space_weather_api_view(request):
    """
    GET /api/space-weather/
    Returns the last 7 days of recorded NASA DONKI events for timeline visualization.
    """
    days = int(request.GET.get('days', 7))
    events, api_available = fetch_space_weather(days=days)

    events_data = [
        {
            'id': e.id,
            'event_type': e.event_type,
            'event_type_display': e.get_event_type_display(),
            'external_id': e.external_id,
            'start_time': e.start_time.isoformat(),
            'time_display': e.start_time.strftime('%d %b %H:%M UTC'),
            'intensity': e.intensity,
            'summary': e.summary,
            'severity': e.severity,
            'severity_display': e.get_severity_display(),
            'is_sample': e.is_sample,
            'advice': e.plain_language_advice,
        }
        for e in events
    ]

    return JsonResponse({
        'status': 'success',
        'nasa_api_available': api_available,
        'events_count': len(events_data),
        'events': events_data,
    })


def dashboard_view(request):
    """
    Main Environment Monitor dashboard matching the dark deep-space HUD theme.
    Loads initial Earth and planetary baseline data and provides interactive client tabs.
    """
    astronaut = get_active_astronaut(request)
    if not astronaut:
        return redirect('core:login')

    # Fetch initial reading for Earth
    earth_reading, api_available, _ = fetch_earth_weather()
    check_thresholds(earth_reading)

    # Pre-fetch recent NASA space weather events
    sw_events, nasa_api_available = fetch_space_weather(days=7)

    # Initial current payload for default tab
    history = list(
        EnvironmentReading.objects.filter(
            location='earth',
            timestamp__gte=timezone.now() - timedelta(hours=24)
        ).order_by('timestamp')
    ) or [earth_reading]

    trends = _compute_trends(history)
    initial_reading = _serialize_reading(earth_reading, trends)

    # Symptom-environment correlation check
    symptom_note = detect_symptom_environment_correlation(astronaut)

    return render(request, 'environment/dashboard.html', {
        'active_nav': 'environment',
        'current_astronaut': astronaut,
        'initial_reading': initial_reading,
        'api_available': api_available,
        'nasa_api_available': nasa_api_available,
        'sw_events': sw_events,
        'symptom_note': symptom_note,
        'safe_ranges': SAFE_RANGES,
    })
