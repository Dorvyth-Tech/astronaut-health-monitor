from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from alerts.models import Alert
from core import llm
from core.demo import DEMO_USERNAME_PREFIX, READ_ONLY_MESSAGE, is_public_demo_user
from core.models import AstronautProfile, Baseline
from telemetry.models import CheckIn, Reading


def _seed():
    commander = AstronautProfile.objects.create(
        name='Amara Reyes', email='amara.reyes@mission.example', is_active_commander=True)
    other = AstronautProfile.objects.create(name='Kenji Sato', email='kenji@mission.example')
    Baseline.objects.create(astronaut=commander, metric='sleep', baseline_value=7.2)
    alert = Alert.objects.create(astronaut=commander, metric='sleep', severity='caution',
                                 title='t', message='m', mission_day=1)
    return commander, other, alert


@override_settings(PUBLIC_DEMO=True)
class PublicDemoEntryTests(TestCase):
    def setUp(self):
        self.commander, self.other, self.alert = _seed()

    def test_demo_link_opens_the_dashboard_without_a_login(self):
        resp = self.client.get(reverse('core:public_demo'))
        self.assertRedirects(resp, reverse('core:dashboard'), fetch_redirect_response=False)
        page = self.client.get(reverse('core:dashboard'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Amara Reyes')
        self.assertTrue(page.context['public_demo'])

    def test_every_read_page_works_for_a_demo_visitor(self):
        self.client.get(reverse('core:public_demo'))
        for url in (reverse('core:profile'), reverse('core:ground_support'),
                    reverse('telemetry:check_in'), reverse('telemetry:history'),
                    reverse('alerts:list'), reverse('alerts:detail', args=[self.alert.id]),
                    reverse('core:sources')):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_demo_user_has_no_usable_password_and_no_staff_rights(self):
        self.client.get(reverse('core:public_demo'))
        user = User.objects.get(username__startswith=DEMO_USERNAME_PREFIX)
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_staff or user.is_superuser)

    def test_each_visitor_gets_their_own_user_and_a_revisit_reuses_it(self):
        self.client.get(reverse('core:public_demo'))
        self.client.get(reverse('core:public_demo'))
        self.assertEqual(User.objects.filter(username__startswith=DEMO_USERNAME_PREFIX).count(), 1)
        self.client_class().get(reverse('core:public_demo'))
        self.assertEqual(User.objects.filter(username__startswith=DEMO_USERNAME_PREFIX).count(), 2)

    def test_a_real_logged_in_user_is_signed_out_first(self):
        real = User.objects.create_user('kenji', email='kenji@mission.example', password='pw-tests-only')
        self.client.force_login(real)
        self.client.get(reverse('core:public_demo'))
        page = self.client.get(reverse('core:dashboard'))
        self.assertTrue(is_public_demo_user(page.context['user']))
        self.assertContains(page, 'Amara Reyes')

    def test_post_to_demo_link_is_not_allowed(self):
        self.assertEqual(self.client.post(reverse('core:public_demo')).status_code, 405)


@override_settings(PUBLIC_DEMO=True)
class PublicDemoReadOnlyTests(TestCase):
    def setUp(self):
        self.commander, self.other, self.alert = _seed()
        self.client.get(reverse('core:public_demo'))

    def test_writes_are_refused_and_nothing_changes(self):
        before_readings = Reading.objects.count()
        self.client.post(reverse('telemetry:add_reading'), {'metric': 'sleep', 'value': '3'})
        self.client.post(reverse('alerts:acknowledge', args=[self.alert.id]))
        self.client.post(reverse('alerts:acknowledge_all'))
        self.client.post(reverse('telemetry:check_in'), {'mood': '1', 'sleep_hours': '2'})
        self.client.post(reverse('core:profile'), {'sleep': '1'})
        self.alert.refresh_from_db()
        self.assertFalse(self.alert.acknowledged)
        self.assertEqual(Reading.objects.count(), before_readings)
        self.assertEqual(CheckIn.objects.count(), 0)
        self.assertEqual(Baseline.objects.get(astronaut=self.commander, metric='sleep').baseline_value, 7.2)

    def test_refused_form_post_redirects_with_a_notice(self):
        resp = self.client.post(reverse('telemetry:add_reading'), {'metric': 'sleep', 'value': '3'}, follow=True)
        msgs = [str(m) for m in resp.context['messages']]
        self.assertIn(READ_ONLY_MESSAGE, msgs)

    def test_refused_json_post_gets_403_json(self):
        resp = self.client.post(reverse('alerts:acknowledge_all'), HTTP_ACCEPT='application/json')
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(resp.json()['read_only_demo'])

    def test_asking_pulse_is_still_allowed(self):
        resp = self.client.post(reverse('core:companion_ask'), {'question': 'How am I doing?'})
        self.assertNotEqual(resp.status_code, 403)
        self.assertNotIn('read_only_demo', resp.content.decode())

    def test_demo_visitor_never_sees_another_astronaut(self):
        page = self.client.get(reverse('core:dashboard') + f'?switch={self.other.id}')
        self.assertContains(page, 'Amara Reyes')

    def test_admin_is_not_reachable(self):
        resp = self.client.get('/admin/')
        self.assertNotEqual(resp.status_code, 200)


class PublicDemoOffTests(TestCase):
    def setUp(self):
        _seed()

    @override_settings(PUBLIC_DEMO=False)
    def test_demo_link_is_404_when_off(self):
        self.assertEqual(self.client.get(reverse('core:public_demo')).status_code, 404)

    def test_off_by_default(self):
        from django.conf import settings
        self.assertFalse(settings.PUBLIC_DEMO)

    def test_leftover_demo_user_sees_nothing_once_demo_is_turned_off(self):
        with override_settings(PUBLIC_DEMO=True):
            self.client.get(reverse('core:public_demo'))
        with override_settings(PUBLIC_DEMO=False):
            resp = self.client.get(reverse('core:dashboard'))
            self.assertEqual(resp.status_code, 302)


@override_settings(PUBLIC_DEMO=True, PUBLIC_DEMO_LLM_RATE_LIMIT=2, COMPANION_LLM_RATE_LIMIT=20,
                   COMPANION_LLM_RATE_WINDOW=3600)
class PublicDemoSharedLLMBudgetTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_all_demo_visitors_share_one_budget(self):
        from core.demo import create_demo_user
        a, b, c = create_demo_user(), create_demo_user(), create_demo_user()
        self.assertTrue(llm._allow_live_llm(a))
        self.assertTrue(llm._allow_live_llm(b))
        self.assertFalse(llm._allow_live_llm(c))

    def test_real_users_keep_their_own_budget(self):
        from core.demo import create_demo_user
        for _ in range(3):
            llm._allow_live_llm(create_demo_user())
        real = User.objects.create_user('amara', password='pw-tests-only')
        self.assertTrue(llm._allow_live_llm(real))
