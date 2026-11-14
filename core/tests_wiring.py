"""
Tests for the glue between core/rules.py and the app (core/services.py).

The rules themselves are tested in core/tests_rules.py. Here we test:
- the status vocabulary mapping,
- turning stored readings into the (t, value) shape the rules expect,
- the health score / risk vectors with "no data",
- sync_alerts_for_astronaut (alerts created from rules, dedupe, escalation).
"""

from datetime import datetime, timedelta, timezone as dt_timezone

from django.test import SimpleTestCase, TestCase

from core import rules
from core.models import AstronautProfile, Baseline
from core.services import (
    STATUS_TO_SEVERITY,
    SEVERITY_TO_STATUS,
    amber_edges,
    calculate_health_score,
    calculate_risk_vectors,
    evaluate_metric_status,
    readings_to_series,
    rule_baseline,
    sync_alerts_for_astronaut,
    weekly_totals,
)
from alerts.models import Alert
from telemetry.models import Reading


class StatusMappingTests(SimpleTestCase):
    def test_rule_statuses_map_to_stored_severities(self):
        self.assertEqual(STATUS_TO_SEVERITY[rules.GREEN], 'nominal')
        self.assertEqual(STATUS_TO_SEVERITY[rules.AMBER], 'caution')
        self.assertEqual(STATUS_TO_SEVERITY[rules.RED], 'critical')
        self.assertEqual(STATUS_TO_SEVERITY[rules.NO_DATA], 'no_data')

    def test_mapping_round_trips(self):
        for status, severity in STATUS_TO_SEVERITY.items():
            self.assertEqual(SEVERITY_TO_STATUS[severity], status)


class SeriesShapeTests(SimpleTestCase):
    def test_daily_metric_uses_mission_day_and_newest_reading_of_a_day_wins(self):
        ts = datetime(2026, 11, 14, 7, 0, tzinfo=dt_timezone.utc)
        rows = [
            (140, ts, 6.0),
            (141, ts, 5.9),
            (141, ts + timedelta(hours=3), 5.5),   # second reading the same day
        ]
        self.assertEqual(readings_to_series('sleep', rows), [(140, 6.0), (141, 5.5)])

    def test_cabin_co2_uses_the_hour_of_the_reading(self):
        ts = datetime(2026, 11, 14, 7, 0, tzinfo=dt_timezone.utc)
        rows = [(142, ts + timedelta(hours=h), 4000.0) for h in range(3)]
        series = readings_to_series('cabin_co2', rows)
        hours = [t for t, _ in series]
        self.assertEqual(hours, [hours[0], hours[0] + 1, hours[0] + 2])

    def test_weekly_totals_counts_only_complete_weeks(self):
        # days 1-7 complete (60/day = 420), days 8-10 are a partial week
        daily = [(d, 60.0) for d in range(1, 11)]
        self.assertEqual(weekly_totals(daily), [(1, 420.0)])

    def test_weekly_totals_skips_a_week_with_a_gap(self):
        daily = [(d, 60.0) for d in range(1, 8) if d != 4]
        self.assertEqual(weekly_totals(daily), [])


class EvaluateMetricStatusTests(SimpleTestCase):
    def test_no_readings_is_no_data_not_nominal(self):
        res = evaluate_metric_status('sleep', [], 7.2)
        self.assertEqual(res['status'], 'no_data')
        self.assertEqual(res['label'], 'No data')

    def test_no_baseline_is_no_data(self):
        res = evaluate_metric_status('resting_hr', [(140, 70), (141, 70), (142, 70)], None)
        self.assertEqual(res['status'], 'no_data')

    def test_mood_stress_is_display_only(self):
        res = evaluate_metric_status('mood_stress', [(142, 9.0)], 3.0)
        self.assertEqual(res['status'], 'no_data')
        self.assertEqual(res['label'], 'Self-report')
        self.assertEqual(res['value'], 9.0)

    def test_hrv_combination_rule_uses_the_heart_rate_status(self):
        series = [(140, 40), (141, 39), (142, 41)]
        self.assertEqual(evaluate_metric_status('hrv', series, 50)['status'], 'caution')
        red = evaluate_metric_status('hrv', series, 50, rhr_status=rules.RED)
        self.assertEqual(red['status'], 'critical')

    def test_exercise_weekly_plan_is_seven_times_the_daily_baseline(self):
        # baseline 60 min/day -> plan 420/week; two weeks at 50/day = 350 = 83% -> amber
        series = [(d, 50.0) for d in range(1, 15)]
        self.assertEqual(evaluate_metric_status('exercise', series, 60.0)['status'], 'caution')

    def test_reason_is_a_sentence_with_numbers(self):
        res = evaluate_metric_status('sleep', [(140, 6.0), (141, 5.9), (142, 5.8)], 7.2)
        self.assertIn('1.4 h below baseline', res['reason'])


class HealthScoreTests(SimpleTestCase):
    @staticmethod
    def evals(**statuses):
        return {key: {'status': status, 'value': 1.0} for key, status in statuses.items()}

    def test_fewer_than_three_indicators_with_data_gives_no_score(self):
        score = calculate_health_score(self.evals(sleep='nominal', hrv='caution', resting_hr='no_data'))
        self.assertEqual(score['score'], '--')
        self.assertEqual(score['status'], 'no_data')
        self.assertEqual(score['dashoffset'], 440)

    def test_no_data_is_left_out_of_the_mean(self):
        # 5 green + 1 amber + 1 no_data -> (500 + 60) / 6 = 93 (case SC-2)
        evals = self.evals(sleep='nominal', hrv='nominal', resting_hr='nominal', exercise='nominal',
                           body_temp='nominal', cabin_co2='caution', mood_stress='no_data')
        self.assertEqual(calculate_health_score(evals)['score'], 93)

    def test_a_red_indicator_never_sits_under_a_nominal_ring(self):
        # 6 green + 1 red = 88 would be "Nominal" by the score alone
        evals = self.evals(sleep='nominal', hrv='nominal', resting_hr='nominal', exercise='nominal',
                           body_temp='nominal', cabin_co2='nominal', mood_stress='critical')
        result = calculate_health_score(evals)
        self.assertEqual(result['score'], 89)
        self.assertEqual(result['status'], 'caution')


class RiskVectorTests(SimpleTestCase):
    def test_group_without_data_shows_no_data(self):
        vectors = calculate_risk_vectors({'sleep': {'status': 'no_data'}, 'mood_stress': {'status': 'no_data'}})
        isolation = next(v for v in vectors if v['id'] == 'isolation')
        self.assertEqual(isolation['status'], 'nodata')

    def test_group_takes_the_worst_status_with_data(self):
        vectors = calculate_risk_vectors({'sleep': {'status': 'critical'}, 'mood_stress': {'status': 'no_data'}})
        isolation = next(v for v in vectors if v['id'] == 'isolation')
        self.assertEqual(isolation['status'], 'critical')


class AmberEdgeTests(SimpleTestCase):
    def test_edges_come_from_the_baseline(self):
        self.assertEqual(amber_edges('sleep', 7.2), {'low': 6.2, 'high': None})
        self.assertEqual(amber_edges('resting_hr', 60.0), {'low': None, 'high': 66.0})
        self.assertEqual(amber_edges('hrv', 50.0), {'low': 42.5, 'high': None})
        self.assertEqual(amber_edges('body_temp', 36.7), {'low': None, 'high': 37.2})
        self.assertEqual(amber_edges('cabin_co2', 2450.0), {'low': None, 'high': 3500.0})

    def test_no_baseline_means_no_edges(self):
        self.assertEqual(amber_edges('sleep', None), {'low': None, 'high': None})


class DatabaseWiringTests(TestCase):
    def setUp(self):
        self.astro = AstronautProfile.objects.create(
            name="Amara Reyes", email="amara.reyes@mission.example", is_active_commander=True)
        Baseline.objects.create(astronaut=self.astro, metric='sleep', baseline_value=7.2)

    def add_sleep(self, values, first_day=140):
        for i, value in enumerate(values):
            Reading.objects.create(astronaut=self.astro, metric='sleep', value=value, mission_day=first_day + i)

    def test_rule_baseline_prefers_the_stored_value(self):
        self.assertEqual(rule_baseline(self.astro, 'sleep', {'sleep': 7.2}), 7.2)

    def test_rule_baseline_falls_back_to_first_14_days_then_none(self):
        self.assertIsNone(rule_baseline(self.astro, 'resting_hr', {}))
        for day in range(1, 15):
            Reading.objects.create(astronaut=self.astro, metric='resting_hr', value=60.0, mission_day=day)
        self.assertEqual(rule_baseline(self.astro, 'resting_hr', {}), 60.0)

    def test_amber_sleep_run_creates_one_caution_alert_with_the_rule_reason(self):
        self.add_sleep([6.0, 5.9, 5.8])
        sync_alerts_for_astronaut(self.astro)
        alert = Alert.objects.get(astronaut=self.astro, metric='sleep')
        self.assertEqual(alert.severity, 'caution')
        self.assertIn('below baseline for 3 nights', alert.message)

    def test_sync_twice_does_not_duplicate_an_active_alert(self):
        self.add_sleep([6.0, 5.9, 5.8])
        sync_alerts_for_astronaut(self.astro)
        sync_alerts_for_astronaut(self.astro)
        self.assertEqual(Alert.objects.filter(astronaut=self.astro, metric='sleep').count(), 1)

    def test_amber_going_to_red_creates_a_new_critical_alert(self):
        self.add_sleep([6.0, 5.9, 5.8])
        sync_alerts_for_astronaut(self.astro)
        self.add_sleep([4.8, 4.9], first_day=143)       # under 5 h two nights in a row
        sync_alerts_for_astronaut(self.astro)
        severities = sorted(Alert.objects.filter(astronaut=self.astro, metric='sleep').values_list('severity', flat=True))
        self.assertEqual(severities, ['caution', 'critical'])

    def test_four_day_decline_alone_no_longer_creates_an_alert(self):
        # assumes Q5: the science table has no trend rule (the HUD demo sleep run is green)
        self.add_sleep([7.3, 6.9, 6.3, 5.8], first_day=139)
        sync_alerts_for_astronaut(self.astro)
        self.assertEqual(Alert.objects.filter(astronaut=self.astro).count(), 0)

    def test_no_baseline_and_short_history_creates_no_alert(self):
        for i, value in enumerate([70.0, 72.0, 75.0]):
            Reading.objects.create(astronaut=self.astro, metric='resting_hr', value=value, mission_day=20 + i)
        sync_alerts_for_astronaut(self.astro)
        self.assertEqual(Alert.objects.filter(astronaut=self.astro, metric='resting_hr').count(), 0)

    def test_absolute_rule_still_alerts_without_a_baseline(self):
        Reading.objects.create(astronaut=self.astro, metric='resting_hr', value=102.0, mission_day=9)
        sync_alerts_for_astronaut(self.astro)
        self.assertEqual(Alert.objects.get(astronaut=self.astro, metric='resting_hr').severity, 'critical')

    def test_mood_stress_never_alerts(self):
        Reading.objects.create(astronaut=self.astro, metric='mood_stress', value=10.0, mission_day=142)
        sync_alerts_for_astronaut(self.astro)
        self.assertEqual(Alert.objects.filter(astronaut=self.astro, metric='mood_stress').count(), 0)
