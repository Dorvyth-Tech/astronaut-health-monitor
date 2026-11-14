from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth.models import User
from core.models import AstronautProfile, Baseline, GroundMessage
from telemetry.models import Reading, CheckIn
from alerts.models import Alert
from core.services import (
    evaluate_metric_status,
    detect_consecutive_trend,
    calculate_health_score,
    calculate_risk_vectors,
)


class ClinicalAlgorithmTests(TestCase):
    # The status rules are now baseline-relative and need a short history, so
    # these three tests pass a series [(t, value), ...] instead of one value.
    # See docs/rule-test-cases.md and core/tests_rules.py for the full rule cases.

    def test_sleep_thresholds(self):
        # Sleep baseline 7.2: caution = >1 h below for 3 nights,
        # critical = <5 h on 2 nights in a row.
        nominal = evaluate_metric_status('sleep', [(140, 7.0), (141, 6.8), (142, 6.5)], 7.2)
        self.assertEqual(nominal['status'], 'nominal')

        caution = evaluate_metric_status('sleep', [(140, 6.0), (141, 5.9), (142, 5.8)], 7.2)
        self.assertEqual(caution['status'], 'caution')

        critical = evaluate_metric_status('sleep', [(141, 4.8), (142, 4.9)], 7.2)
        self.assertEqual(critical['status'], 'critical')

    def test_cabin_co2_thresholds(self):
        # CO2 is hourly and absolute: caution = 3,500 ppm or more for 24 h,
        # critical = above 5,000 ppm for 6 h (or above 7,000 at any time).
        steady = [(h, 2450.0) for h in range(1, 25)]
        nominal = evaluate_metric_status('cabin_co2', steady, 2450.0)
        self.assertEqual(nominal['status'], 'nominal')

        high_day = [(h, 4000.0) for h in range(1, 25)]
        caution = evaluate_metric_status('cabin_co2', high_day, 2450.0)
        self.assertEqual(caution['status'], 'caution')

        six_hours = [(h, 5500.0) for h in range(1, 7)]
        critical = evaluate_metric_status('cabin_co2', six_hours, 2450.0)
        self.assertEqual(critical['status'], 'critical')

    def test_body_temp_thresholds(self):
        # Body temp baseline 36.7: caution = +0.5 to +1.0 C for 2 days,
        # critical = +1.0 C or more. The science table has no low-temperature
        # band, so the old "35.8 is caution (hypothermia)" assertion was removed.
        nominal = evaluate_metric_status('body_temp', [(141, 36.8), (142, 36.9)], 36.7)
        self.assertEqual(nominal['status'], 'nominal')

        caution_fever = evaluate_metric_status('body_temp', [(141, 37.4), (142, 37.5)], 36.7)
        self.assertEqual(caution_fever['status'], 'caution')

        critical_fever = evaluate_metric_status('body_temp', [(142, 37.8)], 36.7)
        self.assertEqual(critical_fever['status'], 'critical')

    def test_consecutive_trend_detection(self):
        # 4 consecutive drops
        vals = [7.3, 6.9, 6.3, 5.8]
        trend = detect_consecutive_trend(vals)
        self.assertTrue(trend['flagged'])
        self.assertEqual(trend['streak_days'], 4)
        self.assertEqual(trend['direction'], 'down')
        self.assertIn("dropped four days in a row", trend['message'])

        # Non-consecutive / alternating
        alternating = [7.0, 6.8, 7.1, 6.9]
        trend_alt = detect_consecutive_trend(alternating)
        self.assertFalse(trend_alt['flagged'])

    def test_health_score_calculation(self):
        evals = {
            'sleep': {'status': 'nominal', 'ratio': 1.0},
            'resting_hr': {'status': 'nominal', 'ratio': 1.0},
            'hrv': {'status': 'nominal', 'ratio': 1.0},
            'exercise': {'status': 'nominal', 'ratio': 1.0},
            'body_temp': {'status': 'nominal', 'ratio': 1.0},
            'mood_stress': {'status': 'nominal', 'ratio': 1.0},
            'cabin_co2': {'status': 'nominal', 'ratio': 1.0},
        }
        score = calculate_health_score(evals)
        self.assertGreaterEqual(score['score'], 80)
        self.assertEqual(score['status'], 'nominal')


class CoreViewsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.astronaut = AstronautProfile.objects.create(
            name="Amara Reyes",
            email="amara.reyes@mission.example",
            rank="Cmdr.",
            habitat="HAB-01",
            is_active_commander=True
        )
        self.user = User.objects.create_user(
            username="amara.reyes",
            email="amara.reyes@mission.example",
            password="validAccess2026"
        )
        Baseline.objects.create(astronaut=self.astronaut, metric='sleep', baseline_value=7.2)

    def test_dashboard_view(self):
        resp = self.client.get(reverse('core:dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Cmdr. Amara Reyes")

    def test_login_and_redirect(self):
        resp = self.client.post(reverse('core:login') + '?next=' + reverse('telemetry:history'), {
            'email': 'amara.reyes@mission.example',
            'password': 'validAccess2026'
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('telemetry:history'), resp.headers['Location'])

    def test_profile_update(self):
        resp = self.client.post(reverse('core:profile'), {
            'baseline_sleep': '7.4',
            'name': 'Amara Reyes Updated',
            'rank': 'Commander',
            'habitat': 'HAB-01',
            'role_description': 'Lead Astrobiologist',
        })
        self.assertEqual(resp.status_code, 200)
        base = Baseline.objects.get(astronaut=self.astronaut, metric='sleep')
        self.assertEqual(base.baseline_value, 7.4)

    def test_ground_support_uplink(self):
        resp = self.client.post(reverse('core:ground_support'), {
            'recipient': 'Flight Surgeon (GND-SURG-2)',
            'priority': 'Routine',
            'subject': 'Telemetry Status Test',
            'body': 'Hab-01 operational and stable.',
        })
        self.assertEqual(resp.status_code, 200)
        msg = GroundMessage.objects.filter(subject='Telemetry Status Test').first()
        self.assertIsNotNone(msg)
        self.assertFalse(msg.is_incoming)
