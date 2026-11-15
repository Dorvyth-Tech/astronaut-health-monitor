"""
Tests for `manage.py seed_demo --scenario=sleep_decline`.

They run against Django's test database through call_command, so Theodore's
db.sqlite3 is never touched. The story under the NEW rules:
- baselines come from days 1-14,
- sleep is amber (3 nights more than 1 h below baseline, ending on day 142),
- cabin CO2 is hourly, fully covered for 48 h and green,
- the only active alert is the one the rules create for sleep,
- one older resting-HR alert is already acknowledged (history).
"""

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from alerts.models import Alert
from core.alert_texts import message_for, title_for
from core import rules
from core.constants import METRICS
from core.models import AstronautProfile, Baseline
from core.services import calculate_health_score, evaluate_astronaut_metrics
from telemetry.models import CheckIn, Reading


class SleepDeclineScenarioTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', scenario='sleep_decline', verbosity=0)
        cls.cmdr = AstronautProfile.objects.get(email='amara.reyes@mission.example')
        cls.evals = evaluate_astronaut_metrics(cls.cmdr)

    def setUp(self):
        # the seed creates a login for every astronaut (username = part of the email before @)
        self.client.login(username='amara.reyes', password='validAccess2026')

    def test_baselines_come_from_days_1_to_14_for_every_indicator(self):
        for astro in AstronautProfile.objects.all():
            for metric in METRICS:
                early = list(Reading.objects.filter(astronaut=astro, metric=metric, mission_day__lte=14)
                             .values_list('mission_day', 'value'))
                self.assertEqual(len(early), 14, f"{astro.name}/{metric}: all 14 days must exist")
                expected = round(rules.compute_baseline(early), 1)
                stored = Baseline.objects.get(astronaut=astro, metric=metric).baseline_value
                self.assertEqual(stored, expected, f"{astro.name}/{metric}")

    def test_sleep_baseline_is_about_7_2_hours(self):
        baseline = Baseline.objects.get(astronaut=self.cmdr, metric='sleep').baseline_value
        self.assertGreater(baseline, 7.0)   # so 6.0 / 5.9 / 5.8 are each > 1 h below
        self.assertLess(baseline, 7.4)

    def test_sleep_is_caution_with_the_rule_reason(self):
        sleep = self.evals['sleep']
        self.assertEqual(sleep['status'], 'caution')
        self.assertEqual(sleep['rule_status'], rules.AMBER)
        self.assertIn('below baseline for 3 nights', sleep['reason'])
        self.assertEqual(sleep['value'], 5.8)

    def test_cabin_co2_has_full_hourly_coverage_and_is_green(self):
        co2 = self.evals['cabin_co2']
        self.assertNotEqual(co2['status'], 'no_data', co2['reason'])
        self.assertEqual(co2['status'], 'nominal')
        self.assertEqual(co2['value'], 2450.0)
        hourly = Reading.objects.filter(astronaut=self.cmdr, metric='cabin_co2', mission_day__gte=141)
        self.assertEqual(hourly.count(), 48)

    def test_the_other_indicators_are_green_and_stress_is_display_only(self):
        for metric in ('resting_hr', 'hrv', 'exercise', 'body_temp', 'mood'):
            self.assertEqual(self.evals[metric]['status'], 'nominal', self.evals[metric]['reason'])
        self.assertEqual(self.evals['mood_stress']['status'], 'no_data')

    def test_mood_comes_from_the_daily_checkin_score(self):
        # the seed writes CheckIn.mood and the matching 'mood' Reading (1-5 scale)
        mood = self.evals['mood']
        self.assertEqual(mood['rule_status'], rules.GREEN)
        checkin = CheckIn.objects.get(astronaut=self.cmdr, mission_day=142)
        self.assertEqual(mood['value'], float(checkin.mood))
        self.assertTrue(1 <= mood['value'] <= 5)

    def test_every_other_crew_member_scores_100_with_no_alerts(self):
        for astro in AstronautProfile.objects.exclude(id=self.cmdr.id):
            score = calculate_health_score(evaluate_astronaut_metrics(astro))
            self.assertEqual(score['score'], 100, astro.name)
            self.assertFalse(Alert.objects.filter(astronaut=astro, acknowledged=False).exists(), astro.name)

    def test_exactly_one_active_alert_and_it_comes_from_the_rules(self):
        active = list(Alert.objects.filter(acknowledged=False).values_list('astronaut__email', 'metric', 'severity'))
        self.assertEqual(active, [('amara.reyes@mission.example', 'sleep', 'caution')])
        alert = Alert.objects.get(acknowledged=False)
        self.assertEqual(alert.title, title_for('sleep', 'amber'))
        self.assertEqual(alert.message, message_for('sleep', 'amber'))
        self.assertEqual(alert.mission_day, 142)
        self.assertFalse(Alert.objects.filter(title='Sleep is trending down').exists())

    def test_one_older_acknowledged_alert_for_history(self):
        old = list(Alert.objects.filter(acknowledged=True))
        self.assertEqual(len(old), 1)
        self.assertEqual((old[0].metric, old[0].severity, old[0].mission_day), ('resting_hr', 'caution', 130))
        self.assertEqual(old[0].title, title_for('resting_hr', 'amber'))
        self.assertEqual(old[0].message, message_for('resting_hr', 'amber'))

    def test_health_score_is_a_number(self):
        score = calculate_health_score(self.evals)
        # 6 green + 1 amber (sleep), stress is excluded: (600 + 60) / 7 = 94
        self.assertEqual(score['score'], 94)
        self.assertEqual(score['status'], 'nominal')

    def test_dashboard_and_history_pages_render_the_story(self):
        resp = self.client.get(reverse('core:dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Sleep', resp.context['headline_summary'])
        cards = {c['key']: c['status_label'] for c in resp.context['metric_cards']}
        self.assertEqual(cards['sleep'], 'Caution')
        self.assertEqual(cards['cabin_co2'], 'Nominal')
        for url in (reverse('alerts:list'), reverse('telemetry:history'),
                    reverse('telemetry:history') + '?severity=caution',
                    reverse('alerts:detail', args=[Alert.objects.get(acknowledged=False).id])):
            self.assertEqual(self.client.get(url).status_code, 200, url)


class SeedDemoOptionsTests(TestCase):
    def test_scenario_is_reproducible(self):
        def snapshot():
            sleep = list(Reading.objects.filter(metric='sleep').order_by('astronaut_id', 'mission_day')
                         .values_list('mission_day', 'value'))
            co2 = list(Reading.objects.filter(metric='cabin_co2', mission_day__gte=141)
                       .order_by('astronaut_id', 'timestamp').values_list('value', flat=True))
            baselines = sorted(Baseline.objects.values_list('metric', 'baseline_value'))
            return sleep, co2, baselines

        call_command('seed_demo', scenario='sleep_decline', verbosity=0)
        first = snapshot()
        call_command('seed_demo', scenario='sleep_decline', verbosity=0)
        self.assertEqual(snapshot(), first)

    def test_unknown_scenario_is_rejected_before_anything_is_deleted(self):
        AstronautProfile.objects.create(name="Existing Crew", email="existing@mission.example")
        with self.assertRaises(CommandError):
            call_command('seed_demo', scenario='not_a_scenario', verbosity=0)
        self.assertTrue(AstronautProfile.objects.filter(email="existing@mission.example").exists())

    def test_plain_seed_demo_keeps_the_old_behaviour(self):
        call_command('seed_demo', verbosity=0)
        titles = set(Alert.objects.values_list('title', flat=True))
        self.assertIn('Sleep is trending down', titles)
        self.assertEqual(Alert.objects.count(), 3)
