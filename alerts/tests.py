from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.urls import reverse
from core.models import AstronautProfile, Baseline
from alerts.models import Alert
from telemetry.models import Reading


class AlertsTests(TestCase):
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
        self.user = User.objects.create_user(
            username="amara.reyes",
            email="amara.reyes@mission.example",
            password="validAccess2026"
        )
        self.client.force_login(self.user)
        Reading.objects.create(astronaut=self.astronaut, metric='sleep', value=5.8, mission_day=142)
        self.alert = Alert.objects.create(
            astronaut=self.astronaut,
            metric='sleep',
            severity='caution',
            title='Consecutive Sleep Duration Decline',
            message='Sleep has dropped four days in a row.',
            mission_day=142,
            acknowledged=False
        )

    def test_routes_use_documented_paths(self):
        self.assertEqual(reverse('alerts:list'), '/alerts/')
        self.assertEqual(reverse('alerts:detail', args=[self.alert.id]), f'/alerts/{self.alert.id}/')

    def test_alerts_list(self):
        resp = self.client.get(reverse('alerts:list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Consecutive Sleep Duration Decline")

    def test_alert_detail_diagnostic(self):
        resp = self.client.get(reverse('alerts:detail', args=[self.alert.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Diagnostic Pod")
        self.assertContains(resp, "What to do")

    def test_acknowledge_alert(self):
        resp = self.client.post(reverse('alerts:acknowledge', args=[self.alert.id]))
        self.assertEqual(resp.status_code, 302)
        self.alert.refresh_from_db()
        self.assertTrue(self.alert.acknowledged)

    def test_acknowledge_all_alerts(self):
        Alert.objects.create(
            astronaut=self.astronaut,
            metric='cabin_co2',
            severity='critical',
            title='High CO2',
            message='Elevated levels',
            mission_day=142,
            acknowledged=False
        )
        resp = self.client.post(reverse('alerts:acknowledge_all'))
        self.assertEqual(resp.status_code, 302)
        unack = Alert.objects.filter(astronaut=self.astronaut, acknowledged=False).count()
        self.assertEqual(unack, 0)
