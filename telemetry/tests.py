from django.test import TestCase, Client
from django.urls import reverse
from core.models import AstronautProfile, Baseline
from telemetry.models import Reading, CheckIn


class TelemetryTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.astronaut = AstronautProfile.objects.create(
            name="Amara Reyes",
            email="amara.reyes@mission.example",
            rank="Commander",
            habitat="HAB-01",
            is_active_commander=True
        )
        Baseline.objects.create(astronaut=self.astronaut, metric='sleep', baseline_value=7.2)
        Reading.objects.create(
            astronaut=self.astronaut,
            metric='sleep',
            value=7.2,
            mission_day=142
        )

    def test_routes_use_documented_paths(self):
        self.assertEqual(reverse('telemetry:check_in'), '/check-in/')
        self.assertEqual(reverse('telemetry:history'), '/history/')
        self.assertEqual(reverse('telemetry:api_readings'), '/api/readings/')

    def test_check_in_post(self):
        resp = self.client.post(reverse('telemetry:check_in'), {
            'mood': '4',
            'stress': '3',
            'sleep_hours': '6.8',
            'exercise_minutes': '60',
            'resting_hr': '58',
            'hrv': '64',
            'body_temp': '36.7',
            'notes': 'Normal transit day.'
        })
        self.assertEqual(resp.status_code, 200)
        checkin = CheckIn.objects.filter(astronaut=self.astronaut).first()
        self.assertIsNotNone(checkin)
        self.assertEqual(checkin.mood, 4)
        self.assertEqual(checkin.sleep_hours, 6.8)

    def test_check_in_saves_the_mood_score_as_a_mood_reading(self):
        self.client.post(reverse('telemetry:check_in'), {
            'mood': '2', 'stress': '8', 'sleep_hours': '7.0', 'exercise_minutes': '30',
        })
        mood = Reading.objects.get(astronaut=self.astronaut, metric='mood')
        self.assertEqual(mood.value, 2.0)                     # the 1-5 score, not the stress
        stress = Reading.objects.get(astronaut=self.astronaut, metric='mood_stress')
        self.assertEqual(stress.value, 8.0)

    def test_history_view(self):
        resp = self.client.get(reverse('telemetry:history') + '?metric=sleep&range=7d')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Sleep")
        self.assertContains(resp, "Filter Matrix")

    def test_api_readings_endpoint(self):
        resp = self.client.get(reverse('telemetry:api_readings') + '?metric=sleep&range=7d')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['metric'], 'sleep')
        self.assertEqual(len(data['values']), 1)
        self.assertEqual(data['values'][0], 7.2)

    def test_add_reading_modal_endpoint(self):
        resp = self.client.post(reverse('telemetry:add_reading'), {
            'metric': 'resting_hr',
            'value': '62.0'
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 200)
        read = Reading.objects.filter(astronaut=self.astronaut, metric='resting_hr').first()
        self.assertIsNotNone(read)
        self.assertEqual(read.value, 62.0)
