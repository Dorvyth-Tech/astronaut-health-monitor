from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.utils import timezone

from alerts.models import Alert
from core.context_processors import spacepulse_context
from core.ground_delay import (
    COMM_LATENCY, ONE_WAY_MAX_MINUTES, ONE_WAY_MIN_MINUTES, ROUND_TRIP_DELAY,
    GROUND_RING_CIRCUMFERENCE, comm_context, ground_panel_for, ground_ring,
)
from core.models import AstronautProfile, GroundMessage


class CommContextTests(TestCase):
    def test_nasa_range_replaces_made_up_latency(self):
        ctx = comm_context()
        self.assertEqual((ONE_WAY_MIN_MINUTES, ONE_WAY_MAX_MINUTES), (3, 22))
        self.assertEqual(ctx['comm_latency'], COMM_LATENCY)
        self.assertIn('3', COMM_LATENCY)
        self.assertIn('22', COMM_LATENCY)
        self.assertIn('44', ROUND_TRIP_DELAY)
        self.assertNotIn('18m 42s', str(ctx))

    def test_values_without_a_source_are_labelled_simulated(self):
        ctx = comm_context()
        for key in ('next_ground_pass', 'next_pass_countdown', 'orbital_sync', 'signal_integrity'):
            self.assertIn('(sim)', ctx[key], key)

    def test_context_processor_uses_it(self):
        request = RequestFactory().get('/')
        from django.contrib.auth.models import AnonymousUser
        request.user = AnonymousUser()
        ctx = spacepulse_context(request)
        self.assertEqual(ctx['comm_latency'], COMM_LATENCY)
        self.assertIn('(sim)', ctx['orbital_sync'])


class GroundPanelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.astronaut = AstronautProfile.objects.create(
            name='Amara Reyes', email='amara.reyes@mission.example')

    def _alert(self, minutes_ago):
        return Alert.objects.create(
            astronaut=self.astronaut,
            timestamp=timezone.now() - timedelta(minutes=minutes_ago),
            **self._alert_fields())

    def _alert_fields(self):
        return {'metric': 'sleep', 'severity': 'critical', 'title': 'Sleep',
                'message': 'Sleep well below baseline.'}

    def _packet(self, minutes_ago, incoming=False, body='Line one\n\nLine two'):
        return GroundMessage.objects.create(
            astronaut=self.astronaut, subject='Health packet', body=body,
            timestamp=timezone.now() - timedelta(minutes=minutes_ago),
            is_incoming=incoming)

    def test_none_without_alerts(self):
        self._packet(1)
        self.assertIsNone(ground_panel_for(self.astronaut, []))

    def test_none_when_packet_predates_alerts(self):
        self._packet(120)
        alerts = [self._alert(30)]
        self.assertIsNone(ground_panel_for(self.astronaut, alerts))

    def test_incoming_messages_are_not_packets(self):
        alerts = [self._alert(30)]
        self._packet(5, incoming=True)
        self.assertIsNone(ground_panel_for(self.astronaut, alerts))

    def test_uplinked_packet_after_alert_fills_panel(self):
        alerts = [self._alert(30)]
        packet = self._packet(5)
        panel = ground_panel_for(self.astronaut, alerts)
        self.assertEqual(panel['summary'], ['Line one', 'Line two'])
        self.assertEqual(panel['packet_id'], f'GM-{packet.pk}')
        self.assertEqual(panel['sent_at'], packet.timestamp.isoformat())
        self.assertEqual((panel['one_way_min'], panel['one_way_max']), (3, 22))

    def test_latest_packet_wins(self):
        alerts = [self._alert(30)]
        self._packet(20, body='old')
        self._packet(2, body='new')
        self.assertEqual(ground_panel_for(self.astronaut, alerts)['summary'], ['new'])


class PagesShowRangeTests(TestCase):
    def setUp(self):
        self.astronaut = AstronautProfile.objects.create(
            name='Amara Reyes', email='amara.reyes@mission.example')
        user = get_user_model().objects.create_user(
            username='amara', email='amara.reyes@mission.example', password='x-pass-123')
        self.client.force_login(user)

    def test_ground_support_page_has_no_made_up_latency(self):
        response = self.client.get('/ground-support/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '18m 42s')
        self.assertNotContains(response, '37m 24s')
        self.assertContains(response, COMM_LATENCY)

    def test_login_page_shows_range(self):
        self.client.logout()
        response = self.client.get('/login/', follow=True)
        self.assertNotContains(response, '18m 42s')


class GroundRingTests(TestCase):
    def test_score_fills_ring(self):
        ring = ground_ring({'score': 75, 'label': 'Caution', 'status': 'caution',
                            'color': '#FFB020', 'physio_reserve': '81.2%'})
        self.assertEqual(ring['health_score'], 75)
        self.assertAlmostEqual(ring['health_dashoffset'], GROUND_RING_CIRCUMFERENCE * 0.25, places=1)
        self.assertEqual(ring['physio_reserve'], '81.2')

    def test_no_score_empties_ring(self):
        ring = ground_ring({'score': '--', 'label': 'No data', 'status': 'no_data',
                            'color': '#888', 'physio_reserve': '70.0%'})
        self.assertEqual(ring['health_dashoffset'], GROUND_RING_CIRCUMFERENCE)


class GroundPageScoreTests(TestCase):
    """The ground page must show the dashboard's score, not a fixed 82."""

    def setUp(self):
        from django.core.management import call_command
        call_command('seed_demo', verbosity=0)
        from core.models import AstronautProfile
        astronaut = AstronautProfile.objects.exclude(email='').first()
        user = get_user_model().objects.filter(email=astronaut.email).first()
        if user is None:
            user = get_user_model().objects.create_user(
                username='ground-tester', email=astronaut.email, password='x-pass-123')
        self.client.force_login(user)

    def test_scores_match(self):
        dash = self.client.get('/')
        ground = self.client.get('/ground-support/')
        self.assertEqual(ground.status_code, 200)
        self.assertEqual(ground.context['health_score'], dash.context['health_score']['score'])
        self.assertNotEqual(ground.context['physio_reserve'], 88.4)


class AlertDetailGroundPanelTests(TestCase):
    """The alert page must use the real sent packet, like the dashboard does."""

    def setUp(self):
        self.astronaut = AstronautProfile.objects.create(
            name='Amara Reyes', email='amara.reyes@mission.example')
        user = get_user_model().objects.create_user(
            username='amara', email='amara.reyes@mission.example', password='x-pass-123')
        self.client.force_login(user)
        self.alert = Alert.objects.create(
            astronaut=self.astronaut, timestamp=timezone.now() - timedelta(minutes=30),
            metric='sleep', severity='critical', title='Sleep',
            message='Sleep well below baseline.')

    def _get(self):
        response = self.client.get(f'/alerts/{self.alert.id}/')
        self.assertEqual(response.status_code, 200)
        return response

    def test_no_packet_means_no_panel_override(self):
        self.assertIsNone(self._get().context['ground_panel'])

    def test_sent_packet_after_alert_fills_panel(self):
        packet = GroundMessage.objects.create(
            astronaut=self.astronaut, subject='Health packet', body='Uplinked line',
            timestamp=timezone.now() - timedelta(minutes=5), is_incoming=False)
        response = self._get()
        self.assertEqual(response.context['ground_panel']['packet_id'], f'GM-{packet.pk}')
        self.assertContains(response, 'Uplinked line')

    def test_packet_before_alert_is_ignored(self):
        GroundMessage.objects.create(
            astronaut=self.astronaut, subject='Old', body='Old line',
            timestamp=timezone.now() - timedelta(hours=2), is_incoming=False)
        self.assertIsNone(self._get().context['ground_panel'])
