"""
Rule test cases, one test per case ID in docs/rule-test-cases.md.

Tests come first: core/rules.py is a stub, so every test here is expected to
fail with NotImplementedError until the logic is written (step 2).

Conventions:
- Each case is a run of daily values, newest last, with the status expected
  after the last reading.
- "# assumes Qn" marks a case whose expected value depends on an open
  question for the Science Lead (see the Questions section of the doc).
- Pure functions only, so SimpleTestCase (no database) is enough.
"""

from django.test import SimpleTestCase

from core.rules import (
    GREEN, AMBER, RED, NO_DATA,
    compute_baseline, evaluate, health_score,
)

START_DAY = 20   # runs start after the 14-day baseline window


def daily(values, start=START_DAY):
    """[v1, v2, ...] -> [(start, v1), (start+1, v2), ...] on consecutive days."""
    return [(start + i, v) for i, v in enumerate(values)]


def hourly(values, start=1):
    """Same as daily(), but the first number is the hour."""
    return [(start + i, v) for i, v in enumerate(values)]


class RuleTestCase(SimpleTestCase):
    def check(self, expected, indicator, readings, baseline=None, **extra):
        result = evaluate(indicator, readings, baseline, **extra)
        self.assertEqual(result.status, expected, result.reason)


# --- 1. Resting heart rate: baseline 60 bpm --------------------------------
class RestingHeartRateTests(RuleTestCase):
    def test_RHR_1_all_within_10_percent_is_green(self):
        self.check(GREEN, "resting_hr", daily([61, 63, 65]), 60)

    def test_RHR_2_three_days_10_to_20_percent_above_is_amber(self):
        self.check(AMBER, "resting_hr", daily([67, 68, 69]), 60)

    def test_RHR_3_run_breaks_on_day_3_is_green(self):
        self.check(GREEN, "resting_hr", daily([67, 68, 64]), 60)

    def test_RHR_4_three_days_above_20_percent_is_red(self):
        self.check(RED, "resting_hr", daily([73, 74, 75]), 60)

    def test_RHR_5_above_10_percent_but_not_all_above_20_is_amber(self):
        self.check(AMBER, "resting_hr", daily([73, 74, 70]), 60)

    def test_RHR_6_single_reading_above_100_is_red(self):
        # absolute rule: needs no duration
        self.check(RED, "resting_hr", daily([101]), 60)

    def test_RHR_7_exactly_10_percent_is_green(self):
        # assumes Q1 (green includes its edge). Watch the float edge:
        # 60 * 1.1 is 66.00000000000001 in Python.
        self.check(GREEN, "resting_hr", daily([66, 66, 66]), 60)

    def test_RHR_8_no_baseline_is_no_data(self):
        # day 9, value 70, baseline not ready
        self.check(NO_DATA, "resting_hr", [(9, 70)], None)

    def test_RHR_9_absolute_rule_needs_no_baseline_is_red(self):
        # day 9, value 102, baseline not ready
        self.check(RED, "resting_hr", [(9, 102)], None)


# --- 2. HRV (RMSSD): baseline 50 ms ----------------------------------------
class HrvTests(RuleTestCase):
    def test_HRV_1_within_15_percent_is_green(self):
        self.check(GREEN, "hrv", daily([48, 46, 45]), 50)

    def test_HRV_2_three_days_15_to_30_percent_below_is_amber(self):
        self.check(AMBER, "hrv", daily([42, 40, 38]), 50)

    def test_HRV_3_below_30_percent_for_only_3_days_is_amber(self):
        # assumes Q2 (red needs 5 days, so 3 days of "below 30%" is amber)
        self.check(AMBER, "hrv", daily([34, 33, 32]), 50)

    def test_HRV_4_below_30_percent_for_5_days_is_red(self):
        self.check(RED, "hrv", daily([34, 33, 32, 31, 30]), 50)

    def test_HRV_5_amber_plus_red_heart_rate_is_red(self):
        # combination rule: HRV amber + resting HR red on the same days
        self.check(RED, "hrv", daily([40, 39, 41]), 50, rhr_status=RED)


# --- 3. Resistance exercise: plan 150 min/week -----------------------------
# readings = [(week_number, minutes)] for completed weeks only (assumes Q3).
# pain = [(mission_day, score)] for the most recent days.
class ExerciseTests(RuleTestCase):
    PLAN = 150

    def weeks(self, minutes):
        return daily(minutes, start=1)

    def test_EX_1_both_weeks_near_plan_is_green(self):
        self.check(GREEN, "exercise", self.weeks([140, 150]), self.PLAN,
                   pain=daily([2, 2, 1]))

    def test_EX_2_two_weeks_at_70_to_90_percent_is_amber(self):
        self.check(AMBER, "exercise", self.weeks([120, 110]), self.PLAN,
                   pain=daily([2]))

    def test_EX_3_only_one_low_week_is_green(self):
        self.check(GREEN, "exercise", self.weeks([100, 140]), self.PLAN,
                   pain=daily([2]))

    def test_EX_4_two_weeks_below_70_percent_is_red(self):
        self.check(RED, "exercise", self.weeks([90, 100]), self.PLAN,
                   pain=daily([2]))

    def test_EX_5_pain_4_to_6_for_3_days_is_amber(self):
        self.check(AMBER, "exercise", self.weeks([140, 140]), self.PLAN,
                   pain=daily([5, 5, 6]))

    def test_EX_6_pain_7_plus_for_2_days_is_red(self):
        self.check(RED, "exercise", self.weeks([140, 140]), self.PLAN,
                   pain=daily([7, 8]))

    def test_EX_7_one_day_of_pain_7_is_green(self):
        # assumes Q4 (a single day of pain 7+ raises nothing)
        self.check(GREEN, "exercise", self.weeks([140, 140]), self.PLAN,
                   pain=daily([7, 3]))


# --- 4. Body temperature: baseline 36.7 C ----------------------------------
class BodyTemperatureTests(RuleTestCase):
    BASE = 36.7

    def test_T_1_close_to_baseline_is_green(self):
        self.check(GREEN, "body_temp", daily([36.8, 36.9]), self.BASE)

    def test_T_2_two_days_half_to_one_degree_above_is_amber(self):
        self.check(AMBER, "body_temp", daily([37.4, 37.5]), self.BASE)

    def test_T_3_new_symptom_alone_is_amber(self):
        # one normal reading plus a new symptom ("sore throat") that day
        self.check(AMBER, "body_temp", daily([36.8]), self.BASE,
                   new_symptom_days={START_DAY})

    def test_T_4_single_reading_1_1_above_is_red(self):
        self.check(RED, "body_temp", daily([37.8]), self.BASE)

    def test_T_5_amber_two_more_days_with_symptoms_is_red(self):
        days = daily([37.4, 37.5, 37.3, 37.4])
        self.check(RED, "body_temp", days, self.BASE,
                   new_symptom_days={d for d, _ in days})

    def test_T_6_amber_needs_two_days_is_green(self):
        self.check(GREEN, "body_temp", daily([37.4]), self.BASE)


# --- 5. Sleep: baseline 7.2 h ----------------------------------------------
class SleepTests(RuleTestCase):
    BASE = 7.2

    def test_S_1_within_1_hour_of_baseline_is_green(self):
        self.check(GREEN, "sleep", daily([7.0, 6.8, 6.5]), self.BASE)

    def test_S_2_three_nights_more_than_1_hour_below_is_amber(self):
        self.check(AMBER, "sleep", daily([6.0, 5.9, 5.8]), self.BASE)

    def test_S_3_four_nights_more_than_2_hours_below_is_red(self):
        self.check(RED, "sleep", daily([5.1, 5.0, 5.1, 5.0]), self.BASE)

    def test_S_4_under_5_hours_two_nights_in_a_row_is_red(self):
        # absolute rule
        self.check(RED, "sleep", daily([4.8, 4.9]), self.BASE)

    def test_S_5_short_nights_not_consecutive_but_all_below_6_2_is_amber(self):
        self.check(AMBER, "sleep", daily([4.8, 6.0, 4.9]), self.BASE)

    def test_S_HUD_four_day_decline_is_green_by_the_science_table(self):
        # assumes Q5: the science table says green (only one night is MORE
        # than 1 h below 7.2; 6.2 is exactly 1 h). The HUD mockup / SRS FR-16
        # shows Caution. If the Science Lead adds a separate trend rule,
        # this expectation changes. Watch the float edge: 7.2 - 1 vs 6.2.
        self.check(GREEN, "sleep", daily([7.3, 7.1, 7.0, 6.6, 6.2, 5.8]),
                   self.BASE)

    def test_S_status_uses_baseline_not_a_hardcoded_six_hour_band(self):
        # 5.5 h for 3 nights is under a clock-time 6.0 h band, but only 0.7 h
        # below a 6.2 h baseline, so the Science Lead rule stays green.
        self.check(GREEN, "sleep", daily([5.5, 5.5, 5.5]), 6.2)
        # The same 5.5 h nights are more than 1 h below an 8.0 h baseline.
        self.check(AMBER, "sleep", daily([5.5, 5.5, 5.5]), 8.0)


# --- 6. Mood / fatigue (1-5): baseline 4.0 ---------------------------------
class MoodTests(RuleTestCase):
    def test_M_1_small_dip_is_green(self):
        self.check(GREEN, "mood", daily([4, 3.5, 4]), 4.0)

    def test_M_2_drop_of_1_for_3_days_is_amber(self):
        self.check(AMBER, "mood", daily([3, 3, 3]), 4.0)

    def test_M_3_drop_of_2_for_3_days_is_red(self):
        # FR-25 support message is a guidance-layer concern, not tested here
        self.check(RED, "mood", daily([2, 2, 2]), 4.0)

    def test_M_4_drop_of_1_for_7_days_is_red(self):
        self.check(RED, "mood", daily([3] * 7), 4.0)

    def test_M_5_drop_of_0_9_for_3_days_stays_green(self):
        # Science Lead amber is a drop of 1 point or more; 0.9 is in the table gap.
        self.check(GREEN, "mood", daily([3.1, 3.1, 3.1]), 4.0)

    def test_M_6_drop_of_1_0_for_3_days_is_amber(self):
        self.check(AMBER, "mood", daily([3.0, 3.0, 3.0]), 4.0)

    def test_M_7_drop_of_0_6_in_the_table_gap_is_green(self):
        # Was Q6 (amber at a drop of more than 0.5). The Science Lead table
        # keeps that gap green until the drop reaches 1 point.
        self.check(GREEN, "mood", daily([3, 3, 3]), 3.6)


# --- 7. Cabin CO2: hourly, absolute (no baseline) --------------------------
# readings = [(hour_number, ppm)]
class CabinCo2Tests(RuleTestCase):
    def test_C_1_steady_2450_is_green(self):
        self.check(GREEN, "cabin_co2", hourly([2450] * 24))

    def test_C_2_4000_for_24_hours_is_amber(self):
        self.check(AMBER, "cabin_co2", hourly([4000] * 24))

    def test_C_3_4000_for_20_hours_is_green(self):
        self.check(GREEN, "cabin_co2", hourly([4000] * 20))

    def test_C_4_5500_for_6_hours_is_red(self):
        self.check(RED, "cabin_co2", hourly([5500] * 6))

    def test_C_5_5500_for_4_hours_is_green(self):
        # assumes Q7 (green by the table: a short spike shows nothing)
        self.check(GREEN, "cabin_co2", hourly([5500] * 4))

    def test_C_6_single_reading_above_7000_is_red(self):
        self.check(RED, "cabin_co2", hourly([7200]))

    def test_C_7_24_hour_window_with_6_hours_missing_is_no_data(self):
        # assumes Q8 (a 24 h window needs at least 80% of its hours;
        # 18 of 24 is 75%). Hours 7-12 are missing.
        present = [(h, 4000) for h in range(1, 25) if not 7 <= h <= 12]
        self.check(NO_DATA, "cabin_co2", present)


# --- 8. Health score -------------------------------------------------------
class HealthScoreTests(SimpleTestCase):
    def test_SC_1_all_green_is_100(self):
        self.assertEqual(health_score([GREEN] * 7), 100)

    def test_SC_2_no_data_is_excluded(self):
        # (5*100 + 60) / 6 = 93.3
        self.assertEqual(health_score([GREEN] * 5 + [AMBER, NO_DATA]), 93)

    def test_SC_3_amber_and_red_pull_the_score_down(self):
        # (4*100 + 2*60 + 20) / 7 = 77.1
        self.assertEqual(health_score([GREEN] * 4 + [AMBER] * 2 + [RED]), 77)

    def test_SC_4_fewer_than_3_indicators_with_data_is_none(self):
        self.assertIsNone(health_score([GREEN, AMBER] + [NO_DATA] * 5))


# --- Extras (not in the case doc) ------------------------------------------
# Added because the doc states them as shared assumptions but has no case ID.
class SharedAssumptionTests(RuleTestCase):
    def test_EXTRA_missing_day_breaks_the_run(self):
        # assumes Q8 (every day present for day-runs): days 20, 21, 23 are
        # not 3 consecutive days, so the amber run does not form.
        self.check(GREEN, "resting_hr", [(20, 67), (21, 68), (23, 69)], 60)

    def test_EXTRA_baseline_is_mean_of_days_1_to_14(self):
        readings = [(d, 60 + (d % 2)) for d in range(1, 15)]   # 7x60, 7x61
        self.assertAlmostEqual(compute_baseline(readings), 60.5)

    def test_EXTRA_baseline_ignores_days_after_14(self):
        readings = [(d, 60) for d in range(1, 15)] + [(15, 100), (16, 100)]
        self.assertAlmostEqual(compute_baseline(readings), 60.0)

    def test_EXTRA_baseline_needs_all_14_days(self):
        # assumes Q8
        readings = [(d, 60) for d in range(1, 14)]               # day 14 missing
        self.assertIsNone(compute_baseline(readings))

    def test_EXTRA_unknown_indicator_is_rejected(self):
        with self.assertRaises(ValueError):
            evaluate("hydration", daily([1, 2, 3]), 1.0)