"""
Tests for the SPACE-PULSE companion (layer 1).

Part 1 tests core/companion.py as a pure function with hand-made evaluations.
Part 2 tests the JSON endpoint and the dashboard panel on Django's test database
(never db.sqlite3).
"""

import json

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core import companion
from core.models import AstronautProfile
from core.companion import build_companion_answers
from core.constants import METRICS
from telemetry.models import Reading


# --- helpers ----------------------------------------------------------------

LABELS = {
    'resting_hr': 'Resting HR', 'hrv': 'HRV', 'exercise': 'Exercise',
    'body_temp': 'Body Temp', 'sleep': 'Sleep', 'mood': 'Mood', 'cabin_co2': 'Cabin CO2',
}


def make_evals(**statuses):
    """All 7 indicators green, except the ones named, e.g. make_evals(sleep='caution')."""
    evals = {}
    for key in METRICS:
        status = statuses.get(key, 'nominal')
        evals[key] = {
            'status': status,
            'reason': f"{LABELS[key]} reason text for {status}.",
            'display_label': LABELS[key],
        }
    return evals


def by_id(answers):
    return {a['id']: a for a in answers}


def all_text(answers):
    """Everything a person could read in the answers, apart from the disclaimer."""
    parts = []
    for a in answers:
        parts += [a['question'], a['answer'], a['source'], a['simulated_note'], a['escalation']]
    return " ".join(parts).lower()


GREEN_SCORE = {'score': 100, 'label': 'Nominal'}


# --- part 1: the pure function ----------------------------------------------

class CompanionAnswerTests(SimpleTestCase):
    def test_all_green_has_only_the_general_chips(self):
        answers = build_companion_answers(make_evals(), GREEN_SCORE)
        self.assertEqual([a['id'] for a in answers], ['overall', 'what_to_do', 'data_source'])
        overall = answers[0]
        self.assertIn("Things look steady", overall['answer'])
        self.assertIn("100", overall['answer'])
        self.assertEqual(overall['severity'], 'nominal')
        self.assertEqual(overall['escalation'], '')

    def test_amber_gets_a_why_chip_with_the_rule_reason(self):
        evals = make_evals(sleep='caution')
        evals['sleep']['reason'] = "Sleep 1.5 h below baseline for 3 nights."
        answers = by_id(build_companion_answers(evals, {'score': 94, 'label': 'Nominal'}))

        why = answers['why_sleep']
        self.assertEqual(why['question'], "Why is Sleep caution?")
        self.assertIn("Sleep 1.5 h below baseline for 3 nights.", why['answer'])
        self.assertEqual(why['indicator'], 'sleep')
        self.assertEqual(why['severity'], 'caution')
        self.assertEqual(why['escalation'], '')
        self.assertEqual(why['source'], "Science Lead demo estimate — pending review")
        self.assertEqual(why['disclaimer'], "Not a medical device; does not diagnose.")
        self.assertEqual(answers['overall']['severity'], 'caution')
        self.assertIn("Sleep is in caution", answers['overall']['answer'])
        self.assertNotIn('why_hrv', answers)     # green indicators get no why chip

    def test_red_adds_the_flight_surgeon_line(self):
        answers = by_id(build_companion_answers(make_evals(resting_hr='critical', sleep='caution'), None))
        for answer_id in ('overall', 'why_resting_hr', 'what_to_do'):
            self.assertEqual(answers[answer_id]['severity'], 'critical', answer_id)
            self.assertEqual(answers[answer_id]['escalation'], "Contact your flight surgeon now.", answer_id)
        # amber alone, and plain facts, never get the line
        self.assertEqual(answers['why_sleep']['escalation'], '')
        self.assertEqual(answers['data_source']['escalation'], '')

    def test_no_data_is_never_called_green(self):
        evals = make_evals(mood='no_data')
        evals['mood']['reason'] = "Mood: baseline not ready yet (needs 14 days)."
        answers = by_id(build_companion_answers(evals, GREEN_SCORE))
        why = answers['why_mood']
        self.assertEqual(why['severity'], 'no_data')
        self.assertIn("baseline not ready yet", why['answer'])
        self.assertIn("I don't have enough data yet for Mood", answers['overall']['answer'])

    def test_everything_no_data(self):
        evals = make_evals(**{key: 'no_data' for key in METRICS})
        answers = by_id(build_companion_answers(evals, {'score': '--', 'label': 'No data'}))
        self.assertIn("don't have any readings", answers['overall']['answer'])
        self.assertEqual(answers['overall']['severity'], 'no_data')
        self.assertNotIn('health score', answers['overall']['answer'])
        self.assertIn("Log a daily check-in", answers['what_to_do']['answer'])

    def test_simulated_note_only_when_simulated_readings_exist(self):
        simulated = build_companion_answers(make_evals(sleep='caution'), None, {'simulated': 90, 'manual': 4})
        for answer in simulated:
            self.assertEqual(answer['simulated_note'], companion.SIMULATED_NOTE, answer['id'])
        source = by_id(simulated)['data_source']['answer']
        self.assertIn("Your 94 readings are: 4 self-reported and 90 simulated demo data.", source)

        real = build_companion_answers(make_evals(sleep='caution'), None, {'manual': 4})
        for answer in real:
            self.assertEqual(answer['simulated_note'], '', answer['id'])

    def test_exercise_is_labelled_a_team_assumption(self):
        answers = by_id(build_companion_answers(make_evals(exercise='caution'), None))
        self.assertIn("Team assumption", answers['why_exercise']['source'])
        self.assertIn("pending", answers['why_exercise']['source'])

    def test_mood_is_described_as_self_report(self):
        answers = by_id(build_companion_answers(make_evals(mood='caution'), None))
        self.assertIn("your own 1-5 rating", answers['why_mood']['answer'])

    def test_stress_self_report_is_ignored(self):
        evals = make_evals()
        evals['mood_stress'] = {'status': 'no_data', 'reason': 'Shown for reference.', 'display_label': 'Stress'}
        ids = [a['id'] for a in build_companion_answers(evals, GREEN_SCORE)]
        self.assertNotIn('why_mood_stress', ids)

    def test_open_alerts_are_mentioned(self):
        one = build_companion_answers(make_evals(), GREEN_SCORE, open_alerts=1)[0]['answer']
        two = build_companion_answers(make_evals(), GREEN_SCORE, open_alerts=2)[0]['answer']
        self.assertIn("1 open alert on", one)
        self.assertIn("2 open alerts on", two)

    def test_every_answer_has_the_same_keys_and_the_disclaimer(self):
        keys = {'id', 'question', 'answer', 'indicator', 'severity', 'source',
                'simulated_note', 'disclaimer', 'escalation'}
        evals = make_evals(sleep='caution', resting_hr='critical', mood='no_data')
        for answer in build_companion_answers(evals, GREEN_SCORE, {'simulated': 5}, 3):
            self.assertEqual(set(answer), keys)
            self.assertEqual(answer['disclaimer'], "Not a medical device; does not diagnose.")

    def test_no_diagnosis_or_hormone_or_mood_detection_claims(self):
        everything = make_evals(sleep='caution', resting_hr='critical', mood='caution',
                                hrv='no_data', exercise='caution')
        text = all_text(build_companion_answers(everything, GREEN_SCORE, {'simulated': 5, 'manual': 2}, 2))
        for word in ('diagnos', 'disease', 'syndrome', 'illness', 'infection', 'depress',
                     'anxiety', 'hormone', 'cortisol', 'biomarker', 'you have a '):
            self.assertNotIn(word, text)

    def test_source_words_match_the_reading_model(self):
        self.assertEqual(set(companion.SOURCE_WORDS), {value for value, _ in Reading.SOURCE_CHOICES})


# --- part 2: endpoint and dashboard ------------------------------------------

PASSWORD = "validAccess2026"


def make_crew_member(name, email):
    astronaut = AstronautProfile.objects.create(name=name, email=email, rank="Cmdr.", habitat="HAB-01")
    user = User.objects.create_user(username=email.split('@')[0], email=email, password=PASSWORD)
    return astronaut, user


def add_daily(astronaut, metric, values, source='manual', start_day=1):
    for day, value in enumerate(values, start=start_day):
        Reading.objects.create(astronaut=astronaut, metric=metric, value=value,
                               mission_day=day, source=source)


def add_sleep_story(astronaut, last_nights, source='manual'):
    """14 baseline nights of 7.0 h, then the given nights."""
    add_daily(astronaut, 'sleep', [7.0] * 14, source)
    add_daily(astronaut, 'sleep', last_nights, source, start_day=15)


class CompanionEndpointTests(TestCase):
    def setUp(self):
        self.url = reverse('core:companion_answers')
        self.amara, self.amara_user = make_crew_member("Amara Reyes", "amara@mission.example")
        self.ben, self.ben_user = make_crew_member("Ben Okoye", "ben@mission.example")
        # Amara: simulated readings and an amber sleep run. Ben: his own manual readings, steady sleep.
        add_sleep_story(self.amara, [5.8, 5.8, 5.8], source='simulated')
        add_sleep_story(self.ben, [7.0, 7.0, 7.0], source='manual')

    def get_answers(self, user):
        self.client.force_login(user)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        return json.loads(resp.content)['answers']

    def test_anonymous_is_sent_to_login(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('core:login'), resp['Location'])

    def test_only_get_is_allowed(self):
        self.client.force_login(self.amara_user)
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_login_without_an_astronaut_gets_nothing(self):
        stranger = User.objects.create_user(username='stranger', email='stranger@elsewhere.example', password=PASSWORD)
        self.client.force_login(stranger)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn('answers', json.loads(resp.content))

    def test_answers_describe_the_logged_in_astronaut_only(self):
        amara = by_id(self.get_answers(self.amara_user))
        ben = by_id(self.get_answers(self.ben_user))

        self.assertIn('why_sleep', amara)
        self.assertEqual(amara['why_sleep']['severity'], 'caution')
        self.assertIn("1.2 h below baseline", amara['why_sleep']['answer'])   # her own numbers
        self.assertEqual(amara['why_sleep']['simulated_note'], companion.SIMULATED_NOTE)

        self.assertNotIn('why_sleep', ben)                          # his sleep is steady
        self.assertEqual(ben['overall']['simulated_note'], '')      # none of his data is simulated
        self.assertIn("All 17 of your readings are self-reported", ben['data_source']['answer'])

    def test_red_sleep_shows_the_flight_surgeon_line_in_json(self):
        carla, carla_user = make_crew_member("Carla Mendez", "carla@mission.example")
        add_sleep_story(carla, [4.5, 4.5])
        answers = by_id(self.get_answers(carla_user))
        self.assertEqual(answers['why_sleep']['severity'], 'critical')
        self.assertEqual(answers['why_sleep']['escalation'], "Contact your flight surgeon now.")


class CompanionDashboardTests(TestCase):
    def setUp(self):
        self.astro, self.user = make_crew_member("Amara Reyes", "amara@mission.example")
        add_sleep_story(self.astro, [5.8, 5.8, 5.8], source='simulated')
        self.client.force_login(self.user)

    def test_dashboard_renders_the_panel_with_chips(self):
        resp = self.client.get(reverse('core:dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Pulse Companion')
        for question in ("How am I doing?", "Why is Sleep caution?", "What should I do?",
                         "Where does this data come from?"):
            self.assertContains(resp, question)
        self.assertContains(resp, "Not a medical device; does not diagnose.")

    def test_chip_links_pick_the_answer_without_javascript(self):
        resp = self.client.get(reverse('core:dashboard') + '?ask=why_sleep')
        selected = [a['id'] for a in resp.context['companion_answers'] if a['selected']]
        self.assertEqual(selected, ['why_sleep'])

    def test_unknown_chip_falls_back_to_the_overall_answer(self):
        resp = self.client.get(reverse('core:dashboard') + '?ask=<script>')
        selected = [a['id'] for a in resp.context['companion_answers'] if a['selected']]
        self.assertEqual(selected, ['overall'])
        self.assertNotContains(resp, '?ask=<script>')

    def test_simulated_note_shows_on_the_dashboard(self):
        resp = self.client.get(reverse('core:dashboard'))
        self.assertContains(resp, companion.SIMULATED_NOTE)


class CompanionDemoScenarioTests(TestCase):
    """The 30-second demo story (seed_demo --scenario=sleep_decline, test database only)."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', scenario='sleep_decline', verbosity=0)

    def test_commander_gets_a_sensible_sleep_answer(self):
        self.client.post(reverse('core:login'), {
            'email': 'amara.reyes@mission.example', 'password': PASSWORD,
        })
        resp = self.client.get(reverse('core:dashboard') + '?ask=why_sleep')
        answers = by_id(resp.context['companion_answers'])

        why = answers['why_sleep']
        self.assertEqual(why['question'], "Why is Sleep caution?")
        self.assertIn("Sleep 1.", why['answer'])
        self.assertIn("below baseline for 3 nights", why['answer'])
        self.assertEqual(why['escalation'], '')
        self.assertEqual(why['simulated_note'], companion.SIMULATED_NOTE)
        self.assertContains(resp, "Why is Sleep caution?")