"""
Unit and integration tests for the Environment Monitor module.
Designed for clarity and educational value for the NASA Space Apps Challenge 2026.
"""

from unittest.mock import patch, MagicMock
from django.test import SimpleTestCase, TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.core.management import call_command
from django.contrib.auth.models import User

from core.models import AstronautProfile
from telemetry.models import CheckIn
from environment.models import EnvironmentReading, EnvironmentAlert, SpaceWeatherEvent
from environment.services import (
    check_thresholds,
    simulate_environment,
    fetch_earth_weather,
    map_severity,
    calculate_radiation_level,
    _process_donki_records,
    _seed_realistic_donki_sample_events,
    _sync_space_weather_alerts,
    get_metric_status,
    detect_symptom_environment_correlation,
    SIMULATION_PROFILES,
    SAFE_RANGES,
)


class SpaceWeatherClassificationTests(SimpleTestCase):
    def test_cme_speed_and_solar_particle_events_remain_unclassified(self):
        cme = {'cmeAnalyses': [{'speed': 2000.0}]}

        self.assertEqual(map_severity('cme', cme), 'unclassified')
        self.assertEqual(map_severity('solar_particle', {'eventTime': '2026-11-15'}), 'unclassified')

    def test_unclassified_event_advice_does_not_claim_acute_hazard(self):
        event = SpaceWeatherEvent(event_type='cme', severity='unclassified')

        self.assertIn('no crew action is inferred', event.plain_language_advice)


class EnvironmentThresholdTests(TestCase):
    """Verifies that life-support threshold breaches trigger appropriate clinical alerts."""

    def test_nominal_reading_triggers_no_alerts(self):
        """Standard sea-level / nominal habitat atmosphere should generate zero alarms."""
        reading = EnvironmentReading.objects.create(
            location='orbit',
            temperature=22.0,
            pressure=101.3,
            humidity=45.0,
            oxygen_percent=21.0,
            co2_ppm=800.0,
            radiation_level=2.0,  # Below 5.0 µSv/h nominal threshold
            is_simulated=True,
        )
        alerts = check_thresholds(reading)
        self.assertEqual(len(alerts), 0)
        self.assertEqual(EnvironmentAlert.objects.count(), 0)

    def test_elevated_co2_triggers_caution_and_danger(self):
        """CO2 between 2,500 and 4,000 ppm triggers Caution; >4,000 ppm triggers Danger."""
        # Caution check: only CO2 is non-nominal
        caution_reading = EnvironmentReading.objects.create(
            location='mars',
            temperature=21.0,
            pressure=101.3,
            humidity=45.0,
            oxygen_percent=21.0,
            co2_ppm=3200.0,
            radiation_level=2.0,
            is_simulated=True,
        )
        alerts = check_thresholds(caution_reading)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, 'caution')
        self.assertIn('Elevated CO2', alerts[0].message)

        # Danger check: only CO2 is in danger zone
        danger_reading = EnvironmentReading.objects.create(
            location='mars',
            temperature=21.0,
            pressure=101.3,
            humidity=45.0,
            oxygen_percent=21.0,
            co2_ppm=4500.0,
            radiation_level=2.0,
            is_simulated=True,
        )
        danger_alerts = check_thresholds(danger_reading)
        self.assertEqual(len(danger_alerts), 1)
        self.assertEqual(danger_alerts[0].severity, 'danger')
        self.assertIn('Critical CO2 buildup', danger_alerts[0].message)

    def test_hypoxia_and_radiation_alerts(self):
        """Oxygen dropping below 18% or radiation exceeding 25 µSv/h must generate danger alarms."""
        hazard_reading = EnvironmentReading.objects.create(
            location='moon',
            temperature=20.0,
            pressure=70.0,
            humidity=40.0,
            oxygen_percent=16.5,   # Severe hypoxia
            co2_ppm=1000.0,
            radiation_level=55.0,  # Lunar solar particle event
            is_simulated=True,
        )
        alerts = check_thresholds(hazard_reading)
        severities = [a.severity for a in alerts]
        self.assertIn('danger', severities)
        self.assertTrue(any('Hypoxic' in a.message for a in alerts))
        self.assertTrue(any('Hazardous radiation' in a.message for a in alerts))


class EnvironmentSimulationTests(TestCase):
    """Verifies that simulated planetary physics adhere to plausible physiological constraints."""

    def test_orbit_simulation_ranges(self):
        """Orbit simulation must produce values strictly clamped within ISS bounds."""
        reading = simulate_environment('orbit')
        p = SIMULATION_PROFILES['orbit']
        self.assertTrue(p['temp']['min'] <= reading.temperature <= p['temp']['max'])
        self.assertTrue(p['pressure']['min'] <= reading.pressure <= p['pressure']['max'])
        self.assertTrue(p['co2']['min'] <= reading.co2_ppm <= p['co2']['max'])
        self.assertTrue(reading.is_simulated)

    def test_moon_simulation_ranges(self):
        """Lunar Artemis base simulation matches Exploration Atmosphere (70 kPa / 32% O2)."""
        reading = simulate_environment('moon')
        p = SIMULATION_PROFILES['moon']
        self.assertTrue(p['pressure']['min'] <= reading.pressure <= p['pressure']['max'])
        self.assertTrue(p['o2']['min'] <= reading.oxygen_percent <= p['o2']['max'])
        self.assertTrue(p['rad']['min'] <= reading.radiation_level <= p['rad']['max'])

    def test_mars_simulation_ranges(self):
        """Mars Ares habitat simulation maintains low-pressure hab parameters."""
        reading = simulate_environment('mars')
        p = SIMULATION_PROFILES['mars']
        self.assertTrue(p['pressure']['min'] <= reading.pressure <= p['pressure']['max'])
        self.assertTrue(p['temp']['min'] <= reading.temperature <= p['temp']['max'])

    def test_random_walk_drift_continuity(self):
        """Sequential readings should drift gradually rather than making discontinuous steps."""
        first = simulate_environment('orbit')
        second = simulate_environment('orbit')
        step_max = SIMULATION_PROFILES['orbit']['temp']['step'] * 2.5
        self.assertLessEqual(abs(second.temperature - first.temperature), step_max)


class EnvironmentAPITests(TestCase):
    """Tests the JSON endpoint and mocked OpenWeather API handling."""

    def setUp(self):
        self.client = Client()

    def test_api_returns_orbit_simulated_json(self):
        """GET /api/environment/?location=orbit returns full telemetry payload."""
        response = self.client.get(reverse('api_environment') + '?location=orbit')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['location'], 'orbit')
        self.assertEqual(data['data_source'], 'simulated')
        self.assertTrue(data['is_simulated'])
        self.assertIn('current', data)
        self.assertIn('history_24h', data)
        self.assertIn('safe_ranges', data)

    @patch('environment.views.fetch_space_weather')
    def test_space_weather_api_labels_sample_events(self, mock_fetch):
        event = SpaceWeatherEvent.objects.create(
            event_type='cme',
            external_id='sample-api-event',
            start_time=timezone.now(),
            intensity='820 km/s',
            summary='SAMPLE DATA — test event',
            severity='unclassified',
            is_sample=True,
        )
        mock_fetch.return_value = ([event], False)

        response = self.client.get(reverse('environment:space_weather_api'))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['events'][0]['is_sample'])

    @patch('requests.get')
    def test_earth_weather_mocked_success(self, mock_get):
        """Mocked OpenWeather 200 OK response updates Earth reading and sets data_source=live."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "coord": {"lon": -80.6, "lat": 28.39},
            "weather": [{"id": 800, "main": "Clear"}],
            "main": {"temp": 24.5, "pressure": 1014, "humidity": 62},
            "wind": {"speed": 4.1}
        }
        mock_get.return_value = mock_response

        reading, api_available, is_cached = fetch_earth_weather(city='Cape Canaveral')
        self.assertTrue(api_available)
        self.assertFalse(reading.is_simulated)
        self.assertEqual(reading.temperature, 24.5)
        self.assertEqual(reading.pressure, 101.4)  # 1014 hPa = 101.4 kPa
        self.assertEqual(reading.humidity, 62.0)
        self.assertEqual(reading.location, 'earth')

    @patch('requests.get', side_effect=Exception("Connection timeout"))
    def test_earth_weather_fallback_on_api_failure(self, mock_get):
        """When OpenWeather API throws an error, fallback reading is returned with api_available=False."""
        reading, api_available, is_cached = fetch_earth_weather(city='Cape Canaveral')
        self.assertFalse(api_available)
        self.assertIsNotNone(reading)
        self.assertEqual(reading.location, 'earth')


class SymptomCorrelationTests(TestCase):
    """Tests the medical diagnostic bridge between astronaut check-ins and environment."""

    def setUp(self):
        self.astronaut = AstronautProfile.objects.create(
            name="Amara Reyes",
            rank="Cmdr.",
            mission_name="Ares V",
            mission_total_days=540,
            current_mission_day=142,
            habitat="HAB-01",
            is_active_commander=True,
        )

    def test_detects_headache_and_elevated_co2_correlation(self):
        """Headache in check-in + high CO2 triggers correlation warning."""
        # Create elevated CO2 environment reading
        EnvironmentReading.objects.create(
            location='mars',
            temperature=20.0,
            pressure=56.5,
            humidity=35.0,
            oxygen_percent=28.0,
            co2_ppm=3400.0,
            radiation_level=20.0,
            is_simulated=True,
            timestamp=timezone.now(),
        )

        # Create check-in noting persistent headache
        CheckIn.objects.create(
            astronaut=self.astronaut,
            mission_day=142,
            mood=3,
            stress=6,
            sleep_hours=6.0,
            exercise_minutes=45,
            notes="Dull frontal headache developing around midday.",
            timestamp=timezone.now(),
        )

        correlation = detect_symptom_environment_correlation(self.astronaut)
        self.assertIsNotNone(correlation)
        self.assertIn("Headache", correlation)
        self.assertIn("CO2", correlation)


class SeedEnvironmentCommandTests(TestCase):
    """Verifies that the management command seeds 24 hours of data across all locations."""

    def test_seed_environment_command_execution(self):
        call_command('seed_environment', '--clean')
        # 4 locations * 25 hours (from 24 down to 0) = 100 readings
        reading_count = EnvironmentReading.objects.count()
        self.assertGreaterEqual(reading_count, 96)
        # Verify each location was seeded
        for loc in ['earth', 'orbit', 'moon', 'mars']:
            self.assertTrue(EnvironmentReading.objects.filter(location=loc).exists())


class SpaceWeatherSampleDataTests(TestCase):
    def test_api_records_override_sample_status_and_leave_cme_unclassified(self):
        sample = SpaceWeatherEvent.objects.create(
            event_type='cme',
            external_id='test-cme-record',
            start_time=timezone.now(),
            intensity='sample',
            severity='unclassified',
            is_sample=True,
        )

        _process_donki_records('cme', [{
            'activityID': sample.external_id,
            'beginTime': '2026-11-15T00:00Z',
            'cmeAnalyses': [{'speed': 2000.0}],
            'note': 'NASA DONKI record',
        }])

        sample.refresh_from_db()
        self.assertFalse(sample.is_sample)
        self.assertEqual(sample.severity, 'unclassified')

    def test_sample_events_are_labelled_and_do_not_create_alerts_or_radiation_boosts(self):
        _seed_realistic_donki_sample_events()
        events = list(SpaceWeatherEvent.objects.all())
        reading = EnvironmentReading.objects.create(
            location='mars',
            temperature=20.0,
            pressure=56.5,
            humidity=35.0,
            oxygen_percent=28.0,
            co2_ppm=3200.0,
            radiation_level=24.0,
            is_simulated=True,
        )

        _sync_space_weather_alerts(events)
        with patch('environment.services.random.uniform', return_value=0):
            radiation, source, cause = calculate_radiation_level('mars')

        self.assertTrue(all(event.is_sample for event in events))
        self.assertTrue(all(event.severity == 'unclassified' for event in events))
        self.assertEqual(EnvironmentAlert.objects.count(), 0)
        self.assertEqual((radiation, source, cause), (24.0, 'simulated', None))
