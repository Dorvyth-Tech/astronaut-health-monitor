"""
Services for the Environment Monitor & Space Weather module:
- Earth live weather fetching from OpenWeather API with caching and resilient fallback
- NASA DONKI space weather API integration (FLR, CME, GST, SEP) with 30-minute caching
- Rule-based space weather severity mapping (Low, Moderate, High)
- Hybrid radiation calculations combining physical baselines with live solar events
- Clinical atmospheric threshold checks and alert generation
- Symptom-environment correlation diagnostics for astronauts
"""

import json
import logging
import random
from datetime import datetime, timedelta
import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from decouple import config

from .models import EnvironmentReading, EnvironmentAlert, SpaceWeatherEvent

logger = logging.getLogger(__name__)

# Standard human clinical safe ranges for atmospheric parameters
SAFE_RANGES = {
    'temperature': {
        'min_nominal': 18.0,
        'max_nominal': 26.0,
        'min_caution': 15.0,
        'max_caution': 30.0,
        'unit': '°C',
        'label': 'Temperature',
    },
    'pressure': {
        'min_nominal': 70.0,
        'max_nominal': 105.0,
        'min_caution': 55.0,
        'max_caution': 115.0,
        'unit': 'kPa',
        'label': 'Pressure',
    },
    'humidity': {
        'min_nominal': 30.0,
        'max_nominal': 60.0,
        'min_caution': 20.0,
        'max_caution': 70.0,
        'unit': '%',
        'label': 'Humidity',
    },
    'oxygen_percent': {
        'min_nominal': 19.5,
        'max_nominal': 24.5,
        'min_caution': 18.0,
        'max_caution': 30.0,
        'unit': '%',
        'label': 'Oxygen',
    },
    'co2_ppm': {
        'min_nominal': 350.0,
        'max_nominal': 2500.0,
        'min_caution': 300.0,
        'max_caution': 4000.0,
        'unit': 'ppm',
        'label': 'Carbon Dioxide',
    },
    'radiation_level': {
        'min_nominal': 0.0,
        'max_nominal': 5.0,
        'min_caution': 0.0,
        'max_caution': 25.0,
        'unit': 'µSv/h',
        'label': 'Radiation',
    },
}

# Baseline profile boundaries for random-walk drift in simulated environments
SIMULATION_PROFILES = {
    'orbit': {
        'name': 'Low Earth Orbit (ISS / Crew Dragon)',
        'temp': {'target': 21.5, 'step': 0.3, 'min': 19.0, 'max': 24.0},
        'pressure': {'target': 101.3, 'step': 0.2, 'min': 98.0, 'max': 102.5},
        'humidity': {'target': 45.0, 'step': 0.8, 'min': 36.0, 'max': 55.0},
        'o2': {'target': 21.0, 'step': 0.1, 'min': 20.5, 'max': 21.8},
        'co2': {'target': 2650.0, 'step': 70.0, 'min': 1800.0, 'max': 3600.0},
        'rad': {'target': 18.5, 'step': 0.7, 'min': 12.0, 'max': 24.0},
    },
    'moon': {
        'name': 'Lunar Artemis Surface Base (Shackleton Crater)',
        'temp': {'target': 20.2, 'step': 0.4, 'min': 17.5, 'max': 23.5},
        'pressure': {'target': 70.3, 'step': 0.3, 'min': 68.0, 'max': 74.0},  # 8.2 psi exploration atmosphere
        'humidity': {'target': 36.0, 'step': 0.7, 'min': 28.0, 'max': 46.0},
        'o2': {'target': 32.0, 'step': 0.2, 'min': 30.5, 'max': 34.0},       # 32% O2 compensating for 70 kPa
        'co2': {'target': 2300.0, 'step': 60.0, 'min': 1600.0, 'max': 3400.0},
        'rad': {'target': 46.0, 'step': 1.5, 'min': 32.0, 'max': 62.0},      # High unshielded lunar surface dose
    },
    'mars': {
        'name': 'Mars Transit & Surface Habitat (Ares Hab-01)',
        'temp': {'target': 19.8, 'step': 0.4, 'min': 16.5, 'max': 23.0},
        'pressure': {'target': 56.5, 'step': 0.3, 'min': 53.0, 'max': 60.0},  # Low-pressure hab
        'humidity': {'target': 34.0, 'step': 0.6, 'min': 25.0, 'max': 42.0},
        'o2': {'target': 28.0, 'step': 0.2, 'min': 26.5, 'max': 30.5},
        'co2': {'target': 3200.0, 'step': 80.0, 'min': 2400.0, 'max': 4300.0}, # Scrubbing backlog
        'rad': {'target': 24.0, 'step': 0.9, 'min': 16.0, 'max': 35.0},
    },
}

# ==============================================================================
# NASA DONKI SPACE WEATHER SEVERITY RULES
# Designed for easy explanation to judges and students:
# - Solar Flare (FLR): X-class = High, M-class = Moderate, C/B/A = Low.
# - Geomagnetic Storm (GST): Kp >= 7 = High (G3-G5), Kp 5-6 = Moderate (G1-G2), Kp < 5 = Low.
# CME and solar-particle events are retained for display, but left unclassified until
# mission-approved criteria are available.
# ==============================================================================
SEVERITY_RULES = {
    'flare': {
        'high': lambda intensity: intensity.upper().startswith('X'),
        'moderate': lambda intensity: intensity.upper().startswith('M'),
        'low': lambda intensity: intensity.upper().startswith(('C', 'B', 'A')),
    },
    'geomagnetic_storm': {
        'high': lambda kp: kp >= 7.0,
        'moderate': lambda kp: 5.0 <= kp < 7.0,
        'low': lambda kp: kp < 5.0,
    },
}


def map_severity(event_type, raw_data):
    """
    Maps supported event metrics to low/moderate/high; otherwise leaves them unclassified.
    """
    event_type = event_type.lower()
    if event_type in ('cme', 'solar_particle'):
        return 'unclassified'

    rules = SEVERITY_RULES.get(event_type)

    if not rules:
        return 'low'

    if event_type == 'flare':
        class_type = ''
        if isinstance(raw_data, dict):
            class_type = str(raw_data.get('classType') or raw_data.get('intensity') or '')
        else:
            class_type = str(raw_data)
        if rules['high'](class_type):
            return 'high'
        if rules['moderate'](class_type):
            return 'moderate'
        return 'low'

    elif event_type == 'geomagnetic_storm':
        kp_val = 0.0
        if isinstance(raw_data, dict):
            kp_list = raw_data.get('allKpIndex', [])
            if kp_list:
                kp_val = max(float(item.get('kpIndex', 0.0)) for item in kp_list)
            else:
                kp_val = float(raw_data.get('intensity', 0.0) or 0.0)
        else:
            try:
                kp_val = float(raw_data)
            except (ValueError, TypeError):
                kp_val = 0.0

        if rules['high'](kp_val):
            return 'high'
        if rules['moderate'](kp_val):
            return 'moderate'
        return 'low'

    return 'low'


def calculate_radiation_level(location):
    """
    Returns a prototype radiation estimate from a location baseline and classified,
    non-sample NASA event context. This is not a direct dosimeter measurement.
    Returns: (radiation_level: float, source_label: str, causal_event: str or None)
    """
    location = location.lower()
    base_levels = {
        'earth': 0.14,
        'orbit': 18.5,
        'moon': 46.0,
        'mars': 24.0,
    }
    base_rad = base_levels.get(location, 18.5)

    if location == 'earth':
        # Earth's dense atmosphere and strong magnetosphere shield sea-level from major bursts
        return base_rad, 'live' if location == 'earth' else 'simulated', None

    # Check for active moderate or high space weather events in the last 48 hours
    cutoff = timezone.now() - timedelta(hours=48)
    active_events = list(
        SpaceWeatherEvent.objects.filter(
            start_time__gte=cutoff,
            is_sample=False,
            severity__in=['moderate', 'high']
        ).order_by('-start_time')
    )

    if not active_events:
        # Nominal baseline with slight random walk
        drift = round(random.uniform(-0.5, 0.5), 2)
        return max(0.1, round(base_rad + drift, 2)), 'simulated', None

    # Pick the most impactful active event
    primary_event = next((e for e in active_events if e.severity == 'high'), active_events[0])

    # Elevation factors based on event severity and extraterrestrial shielding:
    # - Moon has zero magnetosphere/atmosphere (highest exposure)
    # - Mars has thin atmosphere (partial protection)
    # - Orbit is inside Earth's magnetosphere (moderate protection)
    if primary_event.severity == 'high':
        boost_map = {
            'orbit': 18.0,
            'moon': 52.0,
            'mars': 34.0,
        }
    else:  # moderate
        boost_map = {
            'orbit': 6.5,
            'moon': 18.0,
            'mars': 11.0,
        }

    elevated_level = round(base_rad + boost_map.get(location, 10.0) + random.uniform(-0.5, 0.5), 2)
    causal_event = f"{primary_event.intensity} {primary_event.get_event_type_display()} ({primary_event.start_time.strftime('%d %b')})"

    return elevated_level, 'nasa_live', causal_event


def fetch_space_weather(days=7):
    """
    Queries NASA DONKI API for Solar Flares (FLR), Coronal Mass Ejections (CME),
    Geomagnetic Storms (GST), and Solar Energetic Particles (SEP) across the past `days`.
    Results are cached for 30 minutes (1800s).
    If the API fails or key is missing, uses stored events and flags 'NASA data unavailable'.
    """
    cache_key = f"nasa_donki_events_{days}d"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached['events'], cached['api_available']

    api_key = getattr(settings, 'NASA_API_KEY', '').strip() or config('NASA_API_KEY', default='DEMO_KEY')
    now = timezone.now()
    start_date = (now - timedelta(days=days)).strftime('%Y-%m-%d')
    end_date = now.strftime('%Y-%m-%d')

    endpoints = [
        ('flare', 'https://api.nasa.gov/DONKI/FLR'),
        ('cme', 'https://api.nasa.gov/DONKI/CME'),
        ('geomagnetic_storm', 'https://api.nasa.gov/DONKI/GST'),
        ('solar_particle', 'https://api.nasa.gov/DONKI/SEP'),
    ]

    fetched_any = False
    api_available = True

    for event_type, url in endpoints:
        try:
            resp = requests.get(
                url,
                params={'startDate': start_date, 'endDate': end_date, 'api_key': api_key},
                headers={'User-Agent': 'Space-Pulse-Health-Monitor/1.0'},
                timeout=8
            )
            if resp.status_code == 200:
                fetched_any = True
                data = resp.json()
                if isinstance(data, list):
                    _process_donki_records(event_type, data)
            elif resp.status_code == 404:
                # 404 on DONKI often indicates zero events recorded in the target window
                fetched_any = True
            else:
                logger.warning(f"DONKI {event_type} returned status {resp.status_code}")
                api_available = False
        except Exception as e:
            logger.warning(f"Failed to fetch NASA DONKI {event_type}: {e}")
            api_available = False

    events = list(
        SpaceWeatherEvent.objects.filter(
            start_time__gte=now - timedelta(days=days)
        ).order_by('-start_time')
    )

    # If no events found after query, provide clearly labelled sample events for demos.
    if not events:
        _seed_realistic_donki_sample_events()
        events = list(
            SpaceWeatherEvent.objects.filter(
                start_time__gte=now - timedelta(days=days)
            ).order_by('-start_time')
        )

    # Cache for 30 minutes
    cache.set(cache_key, {'events': events, 'api_available': api_available}, timeout=1800)

    # Only classified, non-sample NASA events can generate environment alerts.
    _sync_space_weather_alerts(events)

    return events, api_available


def _process_donki_records(event_type, records):
    """Parses raw DONKI JSON entries and upserts them into SpaceWeatherEvent."""
    for item in records:
        ext_id = item.get('flrID') or item.get('activityID') or item.get('gstID') or item.get('sepID')
        if not ext_id:
            continue

        raw_time = item.get('beginTime') or item.get('startTime') or item.get('eventTime')
        try:
            # Handles ISO format like '2024-05-10T06:54Z' or '2024-05-10 06:54'
            start_dt = datetime.fromisoformat(raw_time.replace('Z', '+00:00'))
        except Exception:
            start_dt = timezone.now()

        # Format intensity
        if event_type == 'flare':
            intensity = item.get('classType', 'M-Class')
        elif event_type == 'geomagnetic_storm':
            kp_list = item.get('allKpIndex', [])
            max_kp = max([float(k.get('kpIndex', 0)) for k in kp_list]) if kp_list else 5.0
            intensity = f"Kp {max_kp:.1f}"
        elif event_type == 'cme':
            analyses = item.get('cmeAnalyses', [])
            spd = max([float(a.get('speed', 0)) for a in analyses]) if analyses else 650.0
            intensity = f"{int(spd)} km/s"
        else:
            intensity = "Solar Energetic Proton"

        summary = item.get('note') or item.get('summary') or f"{event_type.replace('_', ' ').title()} recorded by NASA Heliophysics fleet."
        severity = map_severity(event_type, item)

        SpaceWeatherEvent.objects.update_or_create(
            external_id=ext_id,
            defaults={
                'event_type': event_type,
                'start_time': start_dt,
                'intensity': intensity,
                'summary': summary,
                'severity': severity,
                'is_sample': False,
                'fetched_at': timezone.now(),
            }
        )


def _seed_realistic_donki_sample_events():
    """Seeds clearly labelled sample events for demo and offline use."""
    now = timezone.now()
    sample_records = [
        {
            'event_type': 'flare',
            'external_id': f"{now.strftime('%Y-%m-%d')}T14:22:00-FLR-001",
            'start_time': now - timedelta(hours=14),
            'intensity': 'X1.8',
            'summary': 'SAMPLE DATA — X1.8 solar flare example for interface demonstration.',
            'severity': 'unclassified',
        },
        {
            'event_type': 'geomagnetic_storm',
            'external_id': f"{(now - timedelta(days=2)).strftime('%Y-%m-%d')}T08:15:00-GST-001",
            'start_time': now - timedelta(days=2, hours=3),
            'intensity': 'Kp 6.3',
            'summary': 'SAMPLE DATA — Kp 6.3 geomagnetic-storm example for interface demonstration.',
            'severity': 'unclassified',
        },
        {
            'event_type': 'cme',
            'external_id': f"{(now - timedelta(days=3)).strftime('%Y-%m-%d')}T20:45:00-CME-001",
            'start_time': now - timedelta(days=3, hours=5),
            'intensity': '820 km/s',
            'summary': 'SAMPLE DATA — CME speed example; this prototype does not assess crew risk from CME speed.',
            'severity': 'unclassified',
        },
        {
            'event_type': 'solar_particle',
            'external_id': f"{(now - timedelta(days=5)).strftime('%Y-%m-%d')}T11:00:00-SEP-001",
            'start_time': now - timedelta(days=5, hours=2),
            'intensity': 'S2 Event (>10 MeV)',
            'summary': 'SAMPLE DATA — solar-particle event example; severity is not assessed by this prototype.',
            'severity': 'unclassified',
        },
    ]

    for s in sample_records:
        s['is_sample'] = True
        SpaceWeatherEvent.objects.update_or_create(
            external_id=s['external_id'],
            defaults=s
        )


def _sync_space_weather_alerts(events):
    """Creates alerts for classified NASA events; samples and unclassified events are informational."""
    reading = EnvironmentReading.objects.filter(location__in=['orbit', 'moon', 'mars']).order_by('-timestamp').first()
    if not reading:
        return

    for e in events:
        if not e.is_sample and e.severity in ('moderate', 'high'):
            sev_label = 'danger' if e.severity == 'high' else 'caution'
            msg = f"NASA Space Weather [{e.get_severity_display()}]: {e.intensity} {e.get_event_type_display()}. {e.plain_language_advice}"
            # Check if this alert was already logged to prevent duplicates
            if not EnvironmentAlert.objects.filter(message=msg).exists():
                EnvironmentAlert.objects.create(
                    reading=reading,
                    message=msg,
                    severity=sev_label,
                    created_at=e.start_time,
                )


def get_metric_status(metric_key, value):
    """
    Evaluates an environmental value against clinical tolerance limits.
    Returns: 'nominal', 'caution', or 'danger'
    """
    cfg = SAFE_RANGES.get(metric_key)
    if not cfg:
        return 'nominal'

    if metric_key == 'co2_ppm':
        if value > cfg['max_caution']:
            return 'danger'
        elif value > cfg['max_nominal']:
            return 'caution'
        return 'nominal'

    if metric_key == 'radiation_level':
        if value > cfg['max_caution']:
            return 'danger'
        elif value > cfg['max_nominal']:
            return 'caution'
        return 'nominal'

    if value < cfg['min_caution'] or value > cfg['max_caution']:
        return 'danger'
    elif value < cfg['min_nominal'] or value > cfg['max_nominal']:
        return 'caution'
    return 'nominal'


def fetch_earth_weather(city=None):
    """
    Calls the OpenWeather current weather endpoint with requests, timeout, and robust error handling.
    Returns a tuple: (EnvironmentReading instance, api_available: bool, is_cached: bool)
    """
    city = city or getattr(settings, 'OPENWEATHER_CITY', 'Cape Canaveral,US')
    api_key = getattr(settings, 'OPENWEATHER_API_KEY', '').strip()

    cache_key = f"openweather_{city.lower().replace(' ', '_')}"
    cached_payload = cache.get(cache_key)

    if cached_payload:
        reading = EnvironmentReading.objects.create(
            location='earth',
            temperature=cached_payload['temperature'],
            pressure=cached_payload['pressure'],
            humidity=cached_payload['humidity'],
            oxygen_percent=cached_payload['oxygen_percent'],
            co2_ppm=cached_payload['co2_ppm'],
            radiation_level=cached_payload['radiation_level'],
            is_simulated=False,
            timestamp=timezone.now(),
        )
        return reading, True, True

    if not api_key:
        logger.info("OpenWeather API key missing. Falling back to latest recorded or nominal Earth reading.")
        return _fallback_earth_reading(api_available=False)

    try:
        resp = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={'q': city, 'appid': api_key, 'units': 'metric'},
            headers={'User-Agent': 'Space-Pulse-Health-Monitor/1.0'},
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            main = data.get('main', {})
            temp = float(main.get('temp', 22.0))
            pressure_kpa = round(float(main.get('pressure', 1013)) / 10.0, 2)
            humidity = float(main.get('humidity', 50.0))
            wind_speed = float(data.get('wind', {}).get('speed', 0.0))

            reading_payload = {
                'temperature': temp,
                'pressure': pressure_kpa,
                'humidity': humidity,
                'oxygen_percent': 20.95,
                'co2_ppm': 421.0,
                'radiation_level': 0.14,
                'wind': wind_speed,
            }

            cache.set(cache_key, reading_payload, timeout=600)

            reading = EnvironmentReading.objects.create(
                location='earth',
                temperature=reading_payload['temperature'],
                pressure=reading_payload['pressure'],
                humidity=reading_payload['humidity'],
                oxygen_percent=reading_payload['oxygen_percent'],
                co2_ppm=reading_payload['co2_ppm'],
                radiation_level=reading_payload['radiation_level'],
                is_simulated=False,
                timestamp=timezone.now(),
            )
            return reading, True, False

    except Exception as e:
        logger.warning(f"OpenWeather API fetch failed for {city}: {e}. Engaging fallback telemetry.")

    return _fallback_earth_reading(api_available=False)


def _fallback_earth_reading(api_available=False):
    """Retrieves last saved Earth reading or baseline when API is unreachable."""
    last_saved = EnvironmentReading.objects.filter(location='earth').order_by('-timestamp').first()
    if last_saved:
        reading = EnvironmentReading.objects.create(
            location='earth',
            temperature=last_saved.temperature,
            pressure=last_saved.pressure,
            humidity=last_saved.humidity,
            oxygen_percent=last_saved.oxygen_percent,
            co2_ppm=last_saved.co2_ppm,
            radiation_level=last_saved.radiation_level,
            is_simulated=True,
            timestamp=timezone.now(),
        )
        return reading, api_available, False

    reading = EnvironmentReading.objects.create(
        location='earth',
        temperature=23.5,
        pressure=101.3,
        humidity=58.0,
        oxygen_percent=20.95,
        co2_ppm=418.0,
        radiation_level=0.14,
        is_simulated=True,
        timestamp=timezone.now(),
    )
    return reading, api_available, False


def simulate_environment(location):
    """
    Generates realistic extraterrestrial habitat conditions for Orbit, Moon, and Mars.
    Uses gradual random walk drift and incorporates live NASA space weather radiation.
    """
    location = location.lower()
    profile = SIMULATION_PROFILES.get(location, SIMULATION_PROFILES['orbit'])

    last_reading = EnvironmentReading.objects.filter(location=location).order_by('-timestamp').first()

    def step_value(prev_val, target, step, min_bound, max_bound):
        if prev_val is None:
            drift = random.uniform(-step * 2, step * 2)
            return round(max(min_bound, min(max_bound, target + drift)), 2)

        delta = random.uniform(-step, step)
        reversion = (target - prev_val) * 0.1
        new_val = prev_val + delta + reversion
        return round(max(min_bound, min(max_bound, new_val)), 2)

    prev_temp = last_reading.temperature if last_reading else None
    prev_press = last_reading.pressure if last_reading else None
    prev_hum = last_reading.humidity if last_reading else None
    prev_o2 = last_reading.oxygen_percent if last_reading else None
    prev_co2 = last_reading.co2_ppm if last_reading else None

    # Calculate radiation level driven partly by real NASA DONKI space weather
    rad_level, rad_source, causal_event = calculate_radiation_level(location)

    reading = EnvironmentReading.objects.create(
        location=location,
        temperature=step_value(prev_temp, profile['temp']['target'], profile['temp']['step'], profile['temp']['min'], profile['temp']['max']),
        pressure=step_value(prev_press, profile['pressure']['target'], profile['pressure']['step'], profile['pressure']['min'], profile['pressure']['max']),
        humidity=step_value(prev_hum, profile['humidity']['target'], profile['humidity']['step'], profile['humidity']['min'], profile['humidity']['max']),
        oxygen_percent=step_value(prev_o2, profile['o2']['target'], profile['o2']['step'], profile['o2']['min'], profile['o2']['max']),
        co2_ppm=step_value(prev_co2, profile['co2']['target'], profile['co2']['step'], profile['co2']['min'], profile['co2']['max']),
        radiation_level=rad_level,
        is_simulated=(rad_source == 'simulated'),
        timestamp=timezone.now(),
    )
    return reading


def check_thresholds(reading):
    """
    Compares reading values against safe human physiological limits.
    Returns caution/danger alerts and persists them as EnvironmentAlert.
    """
    alerts = []
    location_name = reading.get_location_display()

    # 1. Carbon Dioxide Checks (Hypercapnia Risk)
    if reading.co2_ppm > SAFE_RANGES['co2_ppm']['max_caution']:
        msg = f"Critical CO2 buildup ({reading.co2_ppm:.0f} ppm) in {location_name} habitat. High hypercapnia and cognitive impairment danger."
        alerts.append(EnvironmentAlert.objects.create(reading=reading, message=msg, severity='danger'))
    elif reading.co2_ppm > SAFE_RANGES['co2_ppm']['max_nominal']:
        msg = f"Elevated CO2 ({reading.co2_ppm:.0f} ppm) in {location_name}. Risk of lethargy, headache, and sleep disruption."
        alerts.append(EnvironmentAlert.objects.create(reading=reading, message=msg, severity='caution'))

    # 2. Oxygen Concentration Checks (Hypoxia / Flammability)
    if reading.oxygen_percent < SAFE_RANGES['oxygen_percent']['min_caution']:
        msg = f"Hypoxic O2 drop ({reading.oxygen_percent:.1f}%) in {location_name}. Acute risk of cognitive slowing and cyanosis."
        alerts.append(EnvironmentAlert.objects.create(reading=reading, message=msg, severity='danger'))
    elif reading.oxygen_percent < SAFE_RANGES['oxygen_percent']['min_nominal']:
        msg = f"Low O2 concentration ({reading.oxygen_percent:.1f}%) in {location_name}. Monitor ECLSS injection rate."
        alerts.append(EnvironmentAlert.objects.create(reading=reading, message=msg, severity='caution'))
    elif reading.oxygen_percent > SAFE_RANGES['oxygen_percent']['max_caution']:
        msg = f"Hyperoxic oxygen spike ({reading.oxygen_percent:.1f}%) in {location_name}. Elevated cabin flammability hazard."
        alerts.append(EnvironmentAlert.objects.create(reading=reading, message=msg, severity='danger'))

    # 3. Pressure Checks (Decompression / Dysbarism)
    if reading.pressure < SAFE_RANGES['pressure']['min_caution']:
        msg = f"Critical hypobaric drop ({reading.pressure:.1f} kPa) in {location_name}. Severe decompression danger."
        alerts.append(EnvironmentAlert.objects.create(reading=reading, message=msg, severity='danger'))
    elif reading.pressure < SAFE_RANGES['pressure']['min_nominal'] and reading.location not in ('moon', 'mars'):
        msg = f"Sub-nominal pressure ({reading.pressure:.1f} kPa) in {location_name}. Monitor habitat seal integrity."
        alerts.append(EnvironmentAlert.objects.create(reading=reading, message=msg, severity='caution'))

    # 4. Radiation Dose Rate Checks (Cosmic Ray / Solar Particle Event)
    if reading.radiation_level > SAFE_RANGES['radiation_level']['max_caution']:
        msg = f"Hazardous radiation dose ({reading.radiation_level:.1f} µSv/h) at {location_name}. High solar particle or cosmic flux detected. Retreat to radiation storm shelter."
        alerts.append(EnvironmentAlert.objects.create(reading=reading, message=msg, severity='danger'))
    elif reading.radiation_level > SAFE_RANGES['radiation_level']['max_nominal']:
        msg = f"Elevated cosmic radiation ({reading.radiation_level:.1f} µSv/h) at {location_name}. Accumulating mission dose exceeds terrestrial baseline."
        alerts.append(EnvironmentAlert.objects.create(reading=reading, message=msg, severity='caution'))

    # 5. Temperature Checks
    if reading.temperature < SAFE_RANGES['temperature']['min_caution'] or reading.temperature > SAFE_RANGES['temperature']['max_caution']:
        msg = f"Extreme thermal anomaly ({reading.temperature:.1f} °C) in {location_name}. Risk of hypothermia or heat stress."
        alerts.append(EnvironmentAlert.objects.create(reading=reading, message=msg, severity='danger'))
    elif reading.temperature < SAFE_RANGES['temperature']['min_nominal'] or reading.temperature > SAFE_RANGES['temperature']['max_nominal']:
        msg = f"Thermal fluctuation ({reading.temperature:.1f} °C) in {location_name}. Environmental loop adjustment recommended."
        alerts.append(EnvironmentAlert.objects.create(reading=reading, message=msg, severity='caution'))

    return alerts


def detect_symptom_environment_correlation(astronaut):
    """
    Cross-checks the astronaut's most recent self-reported check-in notes/symptoms
    against active environmental anomalies and active NASA space weather events.
    Returns a diagnostic message if a correlation is identified, else None.
    """
    if not astronaut:
        return None

    from telemetry.models import CheckIn
    latest_checkin = CheckIn.objects.filter(astronaut=astronaut).order_by('-mission_day', '-timestamp').first()
    if not latest_checkin or not latest_checkin.notes:
        return None

    notes_lower = latest_checkin.notes.lower()

    # 1. Check for Space Weather Radiation correlation (fatigue, nausea, headache, dizziness)
    active_high_event = SpaceWeatherEvent.objects.filter(
        start_time__gte=timezone.now() - timedelta(hours=48),
        severity__in=['high', 'moderate']
    ).order_by('-start_time').first()

    if active_high_event and any(s in notes_lower for s in ['fatigue', 'nausea', 'headache', 'dizzy', 'dizziness', 'vomiting', 'malaise']):
        return (
            f"Space Weather Radiation Correlation: {latest_checkin.astronaut.rank} {latest_checkin.astronaut.name} reported "
            f"symptoms ('{latest_checkin.notes[:40]}...') while NASA Space Weather Event "
            f"({active_high_event.intensity} {active_high_event.get_event_type_display()}) is actively elevating habitat radiation. "
            f"Dosimetry review and storm shelter protocol recommended."
        )

    # 2. Atmospheric CO2 & Hypoxia correlations
    recent_readings = {
        r.location: r for r in EnvironmentReading.objects.filter(
            timestamp__gte=timezone.now() - timedelta(hours=6)
        ).order_by('location', '-timestamp')
    }

    primary_env = recent_readings.get('mars') or recent_readings.get('orbit') or recent_readings.get('earth')
    if not primary_env:
        return None

    if any(s in notes_lower for s in ['headache', 'migraine', 'head pressure', 'lethargic', 'fatigue']):
        if primary_env.co2_ppm >= 2800.0:
            return f"Headache / fatigue reported in check-in while habitat CO2 is elevated ({primary_env.co2_ppm:.0f} ppm). Possible hypercapnia effect."

    if any(s in notes_lower for s in ['shortness of breath', 'breathing', 'dyspnea', 'chest tightness']):
        if primary_env.oxygen_percent < 20.0 or primary_env.pressure < 70.0:
            return f"Breathing strain reported while environmental O2 ({primary_env.oxygen_percent:.1f}%) or pressure ({primary_env.pressure:.1f} kPa) is reduced."

    if any(s in notes_lower for s in ['chills', 'cold', 'shivering', 'hypothermia']):
        if primary_env.temperature < 18.0:
            return f"Cold sensitivity reported while cabin temperature is depressed ({primary_env.temperature:.1f} °C)."

    return None
