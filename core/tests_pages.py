"""
Page smoke tests on the seeded demo data.

call_command('seed_demo') runs against the TEST database only (Django builds a
separate one), so Theodore's db.sqlite3 is never touched.
"""

import re

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from alerts.models import Alert
from core.constants import METRICS
from core.models import AstronautProfile
from core.services import evaluate_astronaut_metrics


class SeededPagesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', verbosity=0)
        cls.astro = AstronautProfile.objects.get(email='amara.reyes@mission.example')

    def setUp(self):
        resp = self.client.post(reverse('core:login'), {
            'email': 'amara.reyes@mission.example',
            'password': 'validAccess2026',
        })
        self.assertEqual(resp.status_code, 302)

    def get_ok(self, url):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200, url)
        return resp

    def test_dashboard_renders(self):
        resp = self.get_ok(reverse('core:dashboard'))
        self.assertContains(resp, 'Cmdr. Amara Reyes')
        self.assertEqual(len(resp.context['metric_cards']), 7)
        self.assertIn('score', resp.context['health_score'])

    def test_dashboard_labels_experimental_and_simulated_tiles(self):
        resp = self.get_ok(reverse('core:dashboard'))
        html = resp.content.decode('utf-8')
        self.assertIn('Experimental', html)
        self.assertIn('not a validated measure', html)
        self.assertIn('Physio Reserve', html)
        self.assertIn('Cumulative Stress', html)
        self.assertIn('Hydration index: 96% (simulated)', html)
        self.assertTrue(resp.context['health_score']['physio_reserve_experimental'])
        self.assertTrue(resp.context['health_score']['cumulative_stress_experimental'])

    def test_dashboard_sleep_caution_is_baseline_relative(self):
        resp = self.get_ok(reverse('core:dashboard'))
        html = resp.content.decode('utf-8')
        self.assertNotIn('Caution range (<6.0 h)', html)
        self.assertNotIn('Caution range (&lt;6.0 h)', html)
        self.assertIn('more than 1 h below baseline', html)
        # Seeded commander sleep baseline is 7.2 h, so the amber edge is 6.2 h.
        self.assertEqual(resp.context['sleep_caution_low'], 6.2)
        self.assertNotIn('apiData.caution_low || 6.0', html)

    def test_dashboard_body_temp_unit_is_degree_c(self):
        # Regression: the unit must be a real degree sign, not the mojibake 'Â°'
        # that appears when UTF-8 '°C' is decoded as Windows-1252 / Latin-1.
        resp = self.get_ok(reverse('core:dashboard'))
        html = resp.content.decode('utf-8')
        self.assertIn('°C', html)
        self.assertNotIn('Â°', html)
        check_in = self.get_ok(reverse('telemetry:check_in'))
        check_html = check_in.content.decode('utf-8')
        self.assertIn('°C', check_html)
        self.assertNotIn('Â°', check_html)

    def test_history_renders_in_every_view_and_filter(self):
        base = reverse('telemetry:history')
        for query in ('', '?view=timeline', '?severity=caution', '?severity=critical',
                      '?severity=nominal', '?range=30d', '?range=mission', '?indicator=sleep',
                      '?indicator=cabin_co2&range=30d', '?indicator=mood',
                      '?indicator=mood_stress'):
            self.get_ok(base + query)

    def test_alerts_list_and_every_alert_detail_render(self):
        self.get_ok(reverse('alerts:list'))
        for severity in ('critical', 'caution', 'nominal'):
            self.get_ok(reverse('alerts:list') + f'?severity={severity}')
        for alert in Alert.objects.filter(astronaut=self.astro):
            resp = self.get_ok(reverse('alerts:detail', args=[alert.id]))
            # the template prints this straight into JavaScript: it must be a number
            self.assertRegex(resp.content.decode(), r'const cautionLow = -?\d')

    def test_alert_detail_chart_value_is_numeric_for_every_metric(self):
        for metric in METRICS:
            alert = Alert.objects.create(astronaut=self.astro, metric=metric, severity='caution',
                                         title=f'{metric} test', message='test', mission_day=142)
            resp = self.get_ok(reverse('alerts:detail', args=[alert.id]))
            self.assertRegex(resp.content.decode(), r'const cautionLow = -?\d', metric)

    def test_other_pages_render(self):
        for name in ('core:profile', 'core:ground_support', 'telemetry:check_in'):
            self.get_ok(reverse(name))

    def test_ground_support_sleep_threshold_is_baseline_relative(self):
        resp = self.get_ok(reverse('core:ground_support'))
        html = resp.content.decode('utf-8')
        self.assertNotIn('ALERT THRESHOLD (6.0h)', html)
        self.assertNotIn('Threshold: &lt;6.0h', html)
        self.assertNotIn('Threshold: <6.0h', html)
        self.assertIn('1 h below baseline', html)
        self.assertIn('Experimental', html)
        self.assertIn('not a validated measure', html)

    def test_api_readings_for_every_metric(self):
        for metric in METRICS:
            data = self.get_ok(reverse('telemetry:api_readings') + f'?metric={metric}&range=30d').json()
            self.assertEqual(data['metric'], metric)
            self.assertTrue(data['values'])
            self.assertIn('caution_low', data)
            self.assertIn('caution_high', data)

    def test_seeded_demo_story_snapshot(self):
        # SNAPSHOT of today's behaviour on the seed data, not a requirement.
        # Each line is an open decision (see report): change the test when the
        # decision is made.
        evals = evaluate_astronaut_metrics(self.astro)
        # Q5: the 4-day sleep decline (7.3 -> 5.8, baseline 7.2) is green by the science table
        self.assertEqual(evals['sleep']['status'], 'nominal')
        # daily CO2 readings cannot satisfy an hourly 24 h rule
        self.assertEqual(evals['cabin_co2']['status'], 'no_data')
        # stress (1-10) has no science rule
        self.assertEqual(evals['mood_stress']['status'], 'no_data')
