"""Front-end tests for the public demo banner, message toasts and the ground-delay panel."""

from django.contrib.messages import constants as message_levels
from django.contrib.messages.storage.base import Message
from django.core.management import call_command
from django.template import Context, Template
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse

from alerts.models import Alert
from core.alert_texts import ALERT_TEXTS
from core.models import AstronautProfile

BANNER_TEXT = 'Public demo, read-only. Simulated crew data. Not a medical device.'
FORCE_PUBLIC_DEMO = {'value': False}


def force_public_demo(request):
    """Stand-in for the context processor that feat/public-demo adds."""
    return {'public_demo': FORCE_PUBLIC_DEMO['value']}


TEST_PROCESSORS = [
    'django.template.context_processors.request',
    'django.contrib.auth.context_processors.auth',
    'django.contrib.messages.context_processors.messages',
    'core.context_processors.spacepulse_context',
    'core.tests_demo_ui.force_public_demo',
]


def _templates_with_test_processor():
    from django.conf import settings

    config = dict(settings.TEMPLATES[0])
    config['OPTIONS'] = dict(config['OPTIONS'], context_processors=TEST_PROCESSORS)
    return [config]


class DemoUiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', verbosity=0)
        cls.astro = AstronautProfile.objects.get(email='amara.reyes@mission.example')

    def setUp(self):
        FORCE_PUBLIC_DEMO['value'] = False
        self.addCleanup(FORCE_PUBLIC_DEMO.update, value=False)
        resp = self.client.post(reverse('core:login'), {
            'email': 'amara.reyes@mission.example',
            'password': 'validAccess2026',
        })
        self.assertEqual(resp.status_code, 302)

    # --- banner -------------------------------------------------------------

    def test_banner_shown_only_when_public_demo(self):
        with self.settings(TEMPLATES=_templates_with_test_processor()):
            html = self.client.get(reverse('core:dashboard')).content.decode()
            self.assertNotIn('id="public-demo-banner"', html)
            FORCE_PUBLIC_DEMO['value'] = True
            for url in (reverse('core:dashboard'), reverse('alerts:list')):
                html = self.client.get(url).content.decode()
                self.assertIn('id="public-demo-banner"', html, url)
                self.assertIn(BANNER_TEXT, html, url)
                self.assertIn('role="status"', html)

    # --- messages toast -----------------------------------------------------

    def test_messages_partial_renders_levels_accessibly(self):
        msgs = [
            Message(message_levels.INFO, 'Info text'),
            Message(message_levels.WARNING, 'Read-only warning'),
            Message(message_levels.ERROR, 'Error text'),
        ]
        html = render_to_string('partials/messages.html', {'messages': msgs})
        self.assertIn('Read-only warning', html)
        self.assertEqual(html.count('role="alert"'), 2)   # warning + error
        self.assertEqual(html.count('role="status"'), 1)  # info
        self.assertEqual(html.count('aria-label="Dismiss message"'), 3)

    def test_messages_partial_empty_renders_nothing(self):
        self.assertEqual(render_to_string('partials/messages.html', {'messages': []}).strip(), '')

    def test_base_layout_includes_messages_partial(self):
        resp = self.client.get(reverse('core:dashboard'))
        self.assertTemplateUsed(resp, 'partials/messages.html')
        self.assertNotContains(resp, 'id="message-toasts"')  # nothing queued, so no toast container

    # --- ground-delay panel -------------------------------------------------

    def _render_tag(self, tag_args, context=None):
        template = Template('{% load ground_panel %}{% ground_delay_panel ' + tag_args + ' %}')
        return template.render(Context(context or {}))

    def test_panel_absent_without_alerts(self):
        self.assertNotIn('id="ground-panel"', self._render_tag('alerts=alerts', {'alerts': []}))

    def test_panel_uses_worst_alert_with_verbatim_science_lead_text(self):
        caution = Alert(astronaut=self.astro, metric='sleep', severity='caution', title='t', message='m', mission_day=142)
        critical = Alert(astronaut=self.astro, metric='resting_hr', severity='critical', title='t', message='m', mission_day=142)
        html = self._render_tag('alerts=alerts', {'alerts': [caution, critical]})
        parts = ALERT_TEXTS[('resting_hr', 'red')]
        for key in ('what', 'why', 'action', 'escalate'):
            self.assertIn(parts[key], html)
        self.assertNotIn(ALERT_TEXTS[('sleep', 'amber')]['what'], html)
        self.assertIn('Ground is ~20 minutes away', html)
        self.assertIn('Act now on board', html)
        self.assertIn('Packet to flight surgeon', html)
        self.assertIn('6&ndash;44 min', html)  # round trip of the 3-22 min one-way range
        self.assertIn('Not sent (simulated uplink)', html)
        self.assertIn('not a diagnosis', html)

    def test_panel_skips_acknowledged_and_nominal_alerts(self):
        acked = Alert(astronaut=self.astro, metric='sleep', severity='caution', title='t', message='m', acknowledged=True)
        nominal = Alert(astronaut=self.astro, metric='hrv', severity='nominal', title='t', message='m')
        self.assertNotIn('id="ground-panel"', self._render_tag('alerts=alerts', {'alerts': [acked, nominal]}))

    def test_backend_override_wins_over_fallbacks(self):
        alert = Alert(astronaut=self.astro, metric='sleep', severity='caution', title='t', message='m')
        html = self._render_tag('alert=alert', {
            'alert': alert,
            'ground_panel': {
                'summary': ['Line from backend'],
                'sent_at': '2026-11-15T10:00:00Z',
                'packet_id': 'MED-1',
                'one_way_min': 4,
                'one_way_max': 20,
            },
        })
        self.assertIn('Line from backend', html)
        self.assertIn('data-sent-at="2026-11-15T10:00:00Z"', html)
        self.assertIn('#MED-1', html)
        self.assertIn('8&ndash;40 min', html)

    def test_pages_render_panel_for_seeded_active_alert(self):
        alert = Alert.objects.filter(astronaut=self.astro, acknowledged=False).exclude(severity='nominal').first()
        self.assertIsNotNone(alert, 'seed data should contain an active caution/critical alert')
        for url in (reverse('core:dashboard'), reverse('alerts:detail', args=[alert.id])):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, url)
            self.assertContains(resp, 'id="ground-panel"')
            self.assertContains(resp, 'Packet to flight surgeon')


class BodyFigureTests(TestCase):
    def _cards(self, **statuses):
        keys = ['resting_hr', 'hrv', 'sleep', 'exercise', 'body_temp', 'mood']
        return [
            {'key': k, 'label': k.title(), 'status': statuses.get(k, 'nominal'),
             'formatted_value': '1', 'unit': 'u'}
            for k in keys
        ]

    def _render(self, cards):
        return Template('{% load body_figure %}{% body_figure cards %}').render(Context({'cards': cards}))

    def test_region_takes_worst_status_and_links_to_first_card(self):
        html = self._render(self._cards(mood='critical', sleep='nominal', hrv='caution'))
        self.assertIn('data-region="head" href="#metric-sleep"', html)
        self.assertRegex(html, r'data-status="critical" data-region="head"')
        self.assertRegex(html, r'data-status="caution" data-region="chest"')
        self.assertRegex(html, r'data-status="nominal" data-region="skin"')
        self.assertIn('href="#metric-mood"', html)  # the list links every indicator

    def test_unknown_status_is_no_data(self):
        html = self._render(self._cards(body_temp='no_data', exercise='whatever'))
        self.assertRegex(html, r'data-status="no_data" data-region="skin"')
        self.assertRegex(html, r'data-status="no_data" data-region="limbs"')

    def test_no_cards_renders_nothing(self):
        self.assertNotIn('body-map', self._render([]))

    def test_dashboard_has_body_map_and_card_anchors(self):
        call_command('seed_demo', verbosity=0)
        self.client.post(reverse('core:login'), {'email': 'amara.reyes@mission.example', 'password': 'validAccess2026'})
        html = self.client.get(reverse('core:dashboard')).content.decode()
        self.assertIn('id="body-map"', html)
        for key in ('sleep', 'mood', 'resting_hr', 'hrv', 'exercise', 'body_temp'):
            self.assertIn(f'id="metric-{key}"', html)
