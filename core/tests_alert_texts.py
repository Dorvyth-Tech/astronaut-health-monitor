"""Science Lead alert wording is stored and used verbatim.

Expected paragraphs are copied from "Science Lead Deliverable: Astronaut
Health Monitoring", section 2. Alert wording set (Step 3). They are the
source of truth for these tests: core.alert_texts must match them, and
raising an alert must store them.
"""

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from alerts.models import Alert
from core.alert_texts import (
    ALERT_TEXTS,
    INDICATOR_TITLES,
    message_for,
    parts_for,
    title_for,
)
from core.constants import METRICS
from core.models import AstronautProfile, Baseline
from core.services import sync_alerts_for_astronaut
from telemetry.models import Reading


# Verbatim Science Lead alert paragraphs (en-dash, degree sign, subscript 2).
EXPECTED_TEXTS = {
    ("resting_hr", "amber"): (
        "Your resting heart rate has been 10\u201320% above your baseline for 3 days. "
        "A lasting rise can mean your body is under extra strain. "
        "Rest, drink fluids and log how you feel. "
        "If it continues 3 more days, contact the flight surgeon."
    ),
    ("resting_hr", "red"): (
        "Your resting heart rate has stayed more than 20% above your baseline. "
        "This can mean your heart and body are working harder than usual. "
        "Pause strenuous activity and log symptoms. "
        "Please contact the flight surgeon today."
    ),
    ("hrv", "amber"): (
        "Your heart rate variability has dipped below your usual range for 3 days. "
        "This often reflects stress or incomplete recovery. "
        "Review your rest schedule and log how you feel. "
        "If it continues 3 more days, contact the flight surgeon."
    ),
    ("hrv", "red"): (
        "Your heart rate variability has stayed well below your baseline for 5 days. "
        "This can mean your body is not recovering well. "
        "Prioritize rest and note any symptoms. "
        "Please contact the flight surgeon today."
    ),
    ("exercise", "amber"): (
        "Your resistance exercise has been below plan for 2 weeks. "
        "Regular loading helps protect bone and muscle in microgravity. "
        "Add one short session this week and log any pain. "
        "If it continues 2 more weeks, contact the flight surgeon."
    ),
    ("exercise", "red"): (
        "Your resistance exercise has been well below plan, or pain has been high for 2 days. "
        "Missed training and pain can both affect bone and muscle health. "
        "Rest sore areas and log details. "
        "Please contact the flight surgeon today."
    ),
    ("body_temp", "amber"): (
        "Your temperature has been slightly above baseline for 2 days. "
        "Small rises can be an early sign your immune system is active. "
        "Rest, drink fluids and log any symptoms. "
        "If it continues 2 more days, contact the flight surgeon."
    ),
    ("body_temp", "red"): (
        "Your temperature is about 1 \u00b0C or more above your baseline. "
        "This can point to your body fighting something. "
        "Rest, drink fluids, log symptoms and limit close contact with crewmates. "
        "Contact the flight surgeon now."
    ),
    ("sleep", "amber"): (
        "Your sleep has dropped over the last 3 nights. "
        "Poor sleep can affect mood, focus and immune function. "
        "Review your rest schedule and log how you feel tomorrow. "
        "If this continues 3 more days, contact the flight surgeon."
    ),
    ("sleep", "red"): (
        "Your sleep has stayed well below your usual for several nights. "
        "Ongoing sleep loss can affect safety, mood and health. "
        "Protect your next rest period and reduce non-essential tasks. "
        "Please contact the flight surgeon today."
    ),
    ("mood", "amber"): (
        "Your mood and energy ratings have slipped over the last 3 days. "
        "Stress and fatigue build up gradually in long missions. "
        "Try a short break or a chat with a crewmate. "
        "If it continues 3 more days, contact the flight surgeon."
    ),
    ("mood", "red"): (
        "Your mood and energy have stayed low compared with your usual. "
        "Prolonged low mood or fatigue can affect wellbeing and performance. "
        "You are not alone: reach out to the crew or ground support. "
        "Please contact the flight surgeon today."
    ),
    ("cabin_co2", "amber"): (
        "Cabin CO\u2082 has been above the usual range for 24 hours. "
        "Raised CO\u2082 can cause headaches and tiredness. "
        "Check ventilation status and log any symptoms. "
        "If it continues another 12 hours, contact ground support."
    ),
    ("cabin_co2", "red"): (
        "Cabin CO\u2082 is well above the safe range. "
        "High levels can affect alertness and breathing comfort. "
        "Follow your cabin air procedures and move to a better-ventilated area if possible. "
        "Contact ground support now."
    ),
}

EXPECTED_TITLES = {
    ("resting_hr", "amber"): "Resting heart rate \u00b7 Caution",
    ("resting_hr", "red"): "Resting heart rate \u00b7 Critical",
    ("hrv", "amber"): "Heart rate variability \u00b7 Caution",
    ("hrv", "red"): "Heart rate variability \u00b7 Critical",
    ("exercise", "amber"): "Resistance exercise \u00b7 Caution",
    ("exercise", "red"): "Resistance exercise \u00b7 Critical",
    ("body_temp", "amber"): "Body temperature \u00b7 Caution",
    ("body_temp", "red"): "Body temperature \u00b7 Critical",
    ("sleep", "amber"): "Sleep \u00b7 Caution",
    ("sleep", "red"): "Sleep \u00b7 Critical",
    ("mood", "amber"): "Mood and fatigue \u00b7 Caution",
    ("mood", "red"): "Mood and fatigue \u00b7 Critical",
    ("cabin_co2", "amber"): "Cabin CO\u2082 \u00b7 Caution",
    ("cabin_co2", "red"): "Cabin CO\u2082 \u00b7 Critical",
}


class ScienceLeadWordingTests(TestCase):
    def test_there_are_fourteen_alert_texts(self):
        self.assertEqual(len(ALERT_TEXTS), 14)
        self.assertEqual(len(EXPECTED_TEXTS), 14)

    def test_each_text_matches_the_science_lead_paragraph(self):
        for key, expected in EXPECTED_TEXTS.items():
            with self.subTest(indicator=key[0], state=key[1]):
                self.assertEqual(message_for(*key), expected)
                parts = parts_for(*key)
                self.assertEqual(
                    " ".join((parts["what"], parts["why"], parts["action"], parts["escalate"])),
                    expected,
                )
                self.assertEqual(title_for(*key), EXPECTED_TITLES[key])

    def test_every_indicator_has_amber_and_red(self):
        self.assertEqual(set(INDICATOR_TITLES), set(METRICS))
        self.assertEqual(
            set(ALERT_TEXTS),
            {(indicator, state) for indicator in METRICS for state in ("amber", "red")},
        )


class RaisingAnAlertUsesScienceLeadWordingTests(TestCase):
    def setUp(self):
        self.astro = AstronautProfile.objects.create(
            name="Amara Reyes",
            email="amara.reyes@mission.example",
            is_active_commander=True,
        )
        Baseline.objects.create(astronaut=self.astro, metric="sleep", baseline_value=7.2)

    def test_amber_sleep_alert_stores_the_science_lead_wording(self):
        for i, value in enumerate([6.0, 5.9, 5.8]):
            Reading.objects.create(
                astronaut=self.astro, metric="sleep", value=value, mission_day=140 + i
            )
        sync_alerts_for_astronaut(self.astro)
        alert = Alert.objects.get(astronaut=self.astro, metric="sleep")
        self.assertEqual(alert.severity, "caution")
        self.assertEqual(alert.title, EXPECTED_TITLES[("sleep", "amber")])
        self.assertEqual(alert.message, EXPECTED_TEXTS[("sleep", "amber")])

    def test_red_resting_hr_alert_stores_the_science_lead_wording(self):
        # Absolute red rule: above 100 bpm at rest, no baseline needed.
        Reading.objects.create(
            astronaut=self.astro, metric="resting_hr", value=102.0, mission_day=9
        )
        sync_alerts_for_astronaut(self.astro)
        alert = Alert.objects.get(astronaut=self.astro, metric="resting_hr")
        self.assertEqual(alert.severity, "critical")
        self.assertEqual(alert.title, EXPECTED_TITLES[("resting_hr", "red")])
        self.assertEqual(alert.message, EXPECTED_TEXTS[("resting_hr", "red")])

    def test_alert_detail_shows_the_four_science_lead_parts(self):
        from django.contrib.auth.models import User

        User.objects.create_user(
            username="amara.reyes",
            email="amara.reyes@mission.example",
            password="validAccess2026",
        )
        for i, value in enumerate([6.0, 5.9, 5.8]):
            Reading.objects.create(
                astronaut=self.astro, metric="sleep", value=value, mission_day=140 + i
            )
        sync_alerts_for_astronaut(self.astro)
        alert = Alert.objects.get(astronaut=self.astro, metric="sleep")
        self.client.login(username="amara.reyes", password="validAccess2026")
        resp = self.client.get(reverse("alerts:detail", args=[alert.id]))
        self.assertEqual(resp.status_code, 200)
        parts = parts_for("sleep", "amber")
        self.assertContains(resp, parts["what"])
        self.assertContains(resp, parts["why"])
        self.assertContains(resp, parts["action"])
        self.assertContains(resp, parts["escalate"])
        self.assertNotContains(resp, "Dim cabin lights")
        self.assertNotContains(resp, "Today is in your")
        self.assertNotContains(resp, "Amber Range")
        self.assertNotContains(resp, "Amber range, 5.0")
        self.assertNotContains(resp, "REM Cycle")
        self.assertNotContains(resp, "61 min")
        self.assertNotContains(resp, "-20.5%")
        self.assertNotContains(resp, "62 ms")
        self.assertNotContains(resp, "Neuro-Stress")
        self.assertContains(resp, "Sleep (7 Days)")
        self.assertContains(resp, "Caution below 6.2")
        self.assertContains(resp, "-3.3%")
        # No HRV or stress readings in this setup: those slots stay empty-safe.
        self.assertContains(resp, "No data")



    def test_alert_detail_uses_related_indicator_readings(self):
        from django.contrib.auth.models import User

        User.objects.create_user(
            username="amara.reyes",
            email="amara.reyes@mission.example",
            password="validAccess2026",
        )
        for i, value in enumerate([6.0, 5.9, 5.8]):
            Reading.objects.create(
                astronaut=self.astro, metric="sleep", value=value, mission_day=140 + i
            )
        Reading.objects.create(astronaut=self.astro, metric="hrv", value=55.0, mission_day=142)
        Reading.objects.create(astronaut=self.astro, metric="mood_stress", value=7.0, mission_day=142)
        sync_alerts_for_astronaut(self.astro)
        alert = Alert.objects.get(astronaut=self.astro, metric="sleep")
        self.client.login(username="amara.reyes", password="validAccess2026")
        resp = self.client.get(reverse("alerts:detail", args=[alert.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "55 ms")
        self.assertContains(resp, "7 /10")
        self.assertNotContains(resp, "62 ms")
        self.assertNotContains(resp, "Neuro-Stress")

class SeedDemoUsesScienceLeadWordingTests(TestCase):
    def test_sleep_decline_scenario_stores_the_science_lead_texts(self):
        call_command("seed_demo", scenario="sleep_decline", verbosity=0)
        sleep = Alert.objects.get(acknowledged=False)
        self.assertEqual(sleep.metric, "sleep")
        self.assertEqual(sleep.severity, "caution")
        self.assertEqual(sleep.title, EXPECTED_TITLES[("sleep", "amber")])
        self.assertEqual(sleep.message, EXPECTED_TEXTS[("sleep", "amber")])

        old = Alert.objects.get(acknowledged=True)
        self.assertEqual(old.metric, "resting_hr")
        self.assertEqual(old.severity, "caution")
        self.assertEqual(old.title, EXPECTED_TITLES[("resting_hr", "amber")])
        self.assertEqual(old.message, EXPECTED_TEXTS[("resting_hr", "amber")])
