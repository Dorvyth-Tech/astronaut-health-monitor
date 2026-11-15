"""
Tests for provenance labels, login-required, labelled baselines and the demo-only
crew switcher.

Everything runs on Django's test database, never on db.sqlite3.
"""

import importlib

from django.apps import apps
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from alerts.models import Alert
from core.models import AstronautProfile, Baseline
from core.services import baseline_details, get_baselines
from telemetry.models import Reading


PASSWORD = "validAccess2026"


def make_crew_member(name, email):
    """An astronaut profile plus the login linked to it (same email)."""
    astronaut = AstronautProfile.objects.create(
        name=name, email=email, rank="Cmdr.", habitat="HAB-01"
    )
    user = User.objects.create_user(username=email.split('@')[0], email=email, password=PASSWORD)
    return astronaut, user


def add_early_readings(astronaut, metric, values):
    """One reading per mission day, starting on day 1."""
    for day, value in enumerate(values, start=1):
        Reading.objects.create(astronaut=astronaut, metric=metric, value=value, mission_day=day)


# --- 1. provenance ---------------------------------------------------------

class ProvenanceTests(TestCase):
    def setUp(self):
        self.astro, self.user = make_crew_member("Amara Reyes", "amara@mission.example")
        self.client.force_login(self.user)

    def test_allowed_sources_follow_the_srs(self):
        values = [value for value, _label in Reading.SOURCE_CHOICES]
        self.assertEqual(values, ['manual', 'csv', 'simulated', 'dataset'])

    def test_check_in_and_manual_entry_write_manual(self):
        self.client.post(reverse('telemetry:check_in'), {
            'mood': '4', 'stress': '3', 'sleep_hours': '7.0', 'exercise_minutes': '40',
        })
        self.client.post(reverse('telemetry:add_reading'), {'metric': 'resting_hr', 'value': '60'},
                         HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        sources = set(Reading.objects.filter(astronaut=self.astro).values_list('source', flat=True))
        self.assertEqual(sources, {'manual'})
        self.assertGreater(Reading.objects.filter(astronaut=self.astro).count(), 2)

    def test_seed_demo_marks_every_reading_simulated(self):
        call_command('seed_demo', verbosity=0)
        sources = set(Reading.objects.values_list('source', flat=True))
        self.assertEqual(sources, {'simulated'})

    def test_banner_only_when_the_data_is_simulated(self):
        Reading.objects.create(astronaut=self.astro, metric='sleep', value=7.0, mission_day=1,
                               source='manual')
        resp = self.client.get(reverse('core:dashboard'))
        self.assertFalse(resp.context['is_simulated_data'])
        self.assertNotContains(resp, 'SIMULATED DATA')

        Reading.objects.create(astronaut=self.astro, metric='sleep', value=7.1, mission_day=2,
                               source='simulated')
        resp = self.client.get(reverse('core:dashboard'))
        self.assertTrue(resp.context['is_simulated_data'])
        self.assertContains(resp, 'SIMULATED DATA')

    def test_history_labels_simulated_rows(self):
        Reading.objects.create(astronaut=self.astro, metric='sleep', value=7.1, mission_day=2,
                               source='simulated')
        resp = self.client.get(reverse('telemetry:history') + '?metric=sleep&range=all')
        self.assertContains(resp, 'Simulated')

    def test_data_migration_turns_old_sensor_rows_into_simulated(self):
        migration = importlib.import_module('telemetry.migrations.0003_alter_reading_source')
        old = Reading.objects.create(astronaut=self.astro, metric='sleep', value=7.0,
                                     mission_day=1, source='sensor')
        typed = Reading.objects.create(astronaut=self.astro, metric='sleep', value=6.5,
                                       mission_day=2, source='manual')
        migration.sensor_to_simulated(apps, None)
        old.refresh_from_db()
        typed.refresh_from_db()
        self.assertEqual(old.source, 'simulated')
        self.assertEqual(typed.source, 'manual')


# --- 2. login required -----------------------------------------------------

class LoginRequiredTests(TestCase):
    def setUp(self):
        self.astro, self.user = make_crew_member("Amara Reyes", "amara@mission.example")
        self.alert = Alert.objects.create(
            astronaut=self.astro, metric='sleep', severity='caution',
            title='t', message='m', mission_day=1,
        )

    def test_every_app_page_redirects_anonymous_users_to_login(self):
        urls = [
            reverse('core:dashboard'), reverse('core:dashboard_alias'),
            reverse('core:profile'), reverse('core:ground_support'),
            reverse('telemetry:check_in'), reverse('telemetry:history'),
            reverse('telemetry:history_alias'), reverse('telemetry:api_readings'),
            reverse('alerts:list'), reverse('alerts:list_alias'),
            reverse('alerts:detail', args=[self.alert.id]),
        ]
        for url in urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, url)
            self.assertTrue(resp.headers['Location'].startswith(reverse('core:login')), url)

    def test_write_endpoints_are_protected_too(self):
        for url in (reverse('telemetry:add_reading'),
                    reverse('alerts:acknowledge', args=[self.alert.id]),
                    reverse('alerts:acknowledge_all')):
            resp = self.client.post(url, {'metric': 'sleep', 'value': '7'})
            self.assertEqual(resp.status_code, 302, url)
            self.assertTrue(resp.headers['Location'].startswith(reverse('core:login')), url)
        self.alert.refresh_from_db()
        self.assertFalse(self.alert.acknowledged)
        self.assertEqual(Reading.objects.count(), 0)

    def test_login_page_and_logout_work_without_a_session(self):
        self.assertEqual(self.client.get(reverse('core:login')).status_code, 200)
        self.assertEqual(self.client.get(reverse('core:logout')).status_code, 302)

    def test_login_then_back_to_the_page_that_was_asked_for(self):
        resp = self.client.get(reverse('telemetry:history'))
        login_url = resp.headers['Location']          # /login/?next=/history/
        resp = self.client.post(login_url, {'email': 'amara@mission.example', 'password': PASSWORD})
        self.assertRedirects(resp, reverse('telemetry:history'), fetch_redirect_response=False)

    def test_next_pointing_to_another_site_is_ignored(self):
        resp = self.client.post(reverse('core:login') + '?next=https://evil.example/', {
            'email': 'amara@mission.example', 'password': PASSWORD,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('evil.example', resp.headers['Location'])

    def test_seeded_demo_login_still_works(self):
        call_command('seed_demo', verbosity=0)
        resp = self.client.post(reverse('core:login'), {
            'email': 'amara.reyes@mission.example', 'password': PASSWORD,
        })
        self.assertRedirects(resp, reverse('core:dashboard'), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse('core:dashboard')).status_code, 200)

    def test_wrong_password_is_rejected(self):
        resp = self.client.post(reverse('core:login'), {
            'email': 'amara@mission.example', 'password': 'not-the-password',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get(reverse('core:dashboard')).status_code, 302)

    def test_logging_out_closes_the_session(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('core:dashboard')).status_code, 200)
        self.client.get(reverse('core:logout'))
        self.assertEqual(self.client.get(reverse('core:dashboard')).status_code, 302)


# --- 3. one astronaut per user, crew switcher only in DEMO_MODE -------------

class OwnDataOnlyTests(TestCase):
    def setUp(self):
        self.amara, self.amara_user = make_crew_member("Amara Reyes", "amara@mission.example")
        self.ben, self.ben_user = make_crew_member("Ben Okoye", "ben@mission.example")
        self.ben_alert = Alert.objects.create(
            astronaut=self.ben, metric='sleep', severity='caution',
            title='Ben only', message='private', mission_day=1,
        )

    def test_demo_mode_defaults_to_off(self):
        from django.conf import settings
        self.assertFalse(settings.DEMO_MODE)

    @override_settings(DEMO_MODE=False)
    def test_switch_parameter_is_ignored_outside_demo_mode(self):
        self.client.force_login(self.amara_user)
        resp = self.client.get(reverse('core:profile') + f'?switch={self.ben.id}')
        self.assertEqual(resp.status_code, 200)            # no redirect: nothing was switched
        self.assertEqual(resp.context['current_astronaut'], self.amara)
        self.assertNotIn('active_astronaut_id', self.client.session)

    @override_settings(DEMO_MODE=False)
    def test_crew_switcher_is_hidden_outside_demo_mode(self):
        self.client.force_login(self.amara_user)
        resp = self.client.get(reverse('core:profile'))
        self.assertFalse(resp.context['demo_mode'])
        self.assertEqual(len(resp.context['all_crew']), 0)
        self.assertNotContains(resp, 'Ben Okoye')

    @override_settings(DEMO_MODE=False)
    def test_a_user_cannot_open_another_astronauts_alert(self):
        self.client.force_login(self.amara_user)
        resp = self.client.get(reverse('alerts:detail', args=[self.ben_alert.id]))
        self.assertEqual(resp.status_code, 404)
        resp = self.client.get(reverse('alerts:list'))
        self.assertNotContains(resp, 'Ben only')

    @override_settings(DEMO_MODE=False)
    def test_each_user_sees_their_own_dashboard(self):
        self.client.force_login(self.ben_user)
        resp = self.client.get(reverse('core:dashboard'))
        self.assertContains(resp, 'Ben Okoye')
        self.assertNotContains(resp, 'Amara Reyes')

    @override_settings(DEMO_MODE=False)
    def test_a_login_without_an_astronaut_profile_gets_nothing(self):
        User.objects.create_user(username='stranger', email='stranger@example.com', password=PASSWORD)
        resp = self.client.post(reverse('core:login'), {
            'email': 'stranger@example.com', 'password': PASSWORD,
        })
        self.assertEqual(resp.status_code, 200)            # login refused, form shown again
        self.assertContains(resp, 'not linked to an astronaut profile')
        self.assertEqual(self.client.get(reverse('core:dashboard')).status_code, 302)

    @override_settings(DEMO_MODE=False)
    def test_a_blank_email_never_matches_a_profile_with_a_blank_email(self):
        AstronautProfile.objects.create(name="No Email", email="")
        User.objects.create_user(username='noemail', email='', password=PASSWORD)
        self.client.force_login(User.objects.get(username='noemail'))
        self.assertEqual(self.client.get(reverse('core:dashboard')).status_code, 302)

    @override_settings(DEMO_MODE=False)
    def test_master_password_is_rejected_outside_demo_mode(self):
        User.objects.create_user(username='ben2', email='ben2@mission.example', password='something-else-9')
        AstronautProfile.objects.create(name="Ben Two", email='ben2@mission.example')
        resp = self.client.post(reverse('core:login'), {
            'email': 'ben2@mission.example', 'password': PASSWORD,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get(reverse('core:dashboard')).status_code, 302)

    @override_settings(DEMO_MODE=True)
    def test_crew_switcher_works_in_demo_mode(self):
        self.client.force_login(self.amara_user)
        resp = self.client.get(reverse('core:profile') + f'?switch={self.ben.id}')
        self.assertEqual(resp.status_code, 302)
        resp = self.client.get(reverse('core:profile'))
        self.assertTrue(resp.context['demo_mode'])
        self.assertEqual(resp.context['current_astronaut'], self.ben)
        self.assertEqual(len(resp.context['all_crew']), 2)

    @override_settings(DEMO_MODE=True)
    def test_login_page_shows_demo_buttons_only_in_demo_mode(self):
        self.assertTrue(self.client.get(reverse('core:login')).context['demo_mode'])
        with override_settings(DEMO_MODE=False):
            self.assertFalse(self.client.get(reverse('core:login')).context['demo_mode'])


# --- 4. baselines: computed by default, labelled manual override ------------

class BaselineTests(TestCase):
    def setUp(self):
        self.astro, self.user = make_crew_member("Amara Reyes", "amara@mission.example")
        self.client.force_login(self.user)
        add_early_readings(self.astro, 'sleep', [7.0, 7.2, 7.4] * 4 + [7.0, 7.2])   # mean 7.2

    def post_profile(self, **fields):
        return self.client.post(reverse('core:profile'), fields)

    def sleep_item(self, resp):
        return next(i for i in resp.context['baseline_items'] if i['key'] == 'sleep')

    def test_baseline_is_computed_from_days_1_to_14_by_default(self):
        details = baseline_details(self.astro)['sleep']
        self.assertEqual(details['kind'], 'computed')
        self.assertEqual(details['value'], 7.2)
        self.assertEqual(get_baselines(self.astro)['sleep'], 7.2)

    def test_not_enough_days_means_no_computed_baseline(self):
        add_early_readings(self.astro, 'hrv', [60, 62, 61])
        self.assertNotIn('hrv', baseline_details(self.astro))

    def test_profile_page_says_computed(self):
        resp = self.client.get(reverse('core:profile'))
        self.assertEqual(self.sleep_item(resp)['kind'], 'computed')
        self.assertContains(resp, 'Computed from mission days 1-14')
        self.assertNotContains(resp, 'Manual override')

    def test_pressing_save_without_changes_creates_no_override(self):
        resp = self.post_profile(baseline_sleep='7.2', name='Amara Reyes')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Baseline.objects.filter(is_manual_override=True).exists())

    def test_manual_override_is_stored_with_who_and_when(self):
        before = timezone.now()
        resp = self.post_profile(baseline_sleep='7.8')
        row = Baseline.objects.get(astronaut=self.astro, metric='sleep')
        self.assertTrue(row.is_manual_override)
        self.assertEqual(row.baseline_value, 7.8)
        self.assertEqual(row.overridden_by, self.user)
        self.assertGreaterEqual(row.overridden_at, before)
        self.assertEqual(get_baselines(self.astro)['sleep'], 7.8)
        item = self.sleep_item(resp)
        self.assertEqual(item['kind'], 'manual')
        self.assertContains(resp, 'Manual override')
        self.assertContains(resp, self.user.username)

    def test_override_beats_the_computed_value_in_the_rules(self):
        self.post_profile(baseline_sleep='6.0')
        self.assertEqual(get_baselines(self.astro)['sleep'], 6.0)

    def test_use_computed_removes_the_override(self):
        self.post_profile(baseline_sleep='7.8')
        resp = self.post_profile(baseline_sleep='7.8', reset_sleep='on')
        self.assertFalse(Baseline.objects.filter(astronaut=self.astro, metric='sleep').exists())
        self.assertEqual(self.sleep_item(resp)['kind'], 'computed')
        self.assertEqual(get_baselines(self.astro)['sleep'], 7.2)

    def test_out_of_range_values_are_rejected_and_nothing_is_saved(self):
        for bad in ('0', '72', '-3', '99999'):
            resp = self.post_profile(baseline_sleep=bad, name='Changed Name')
            self.assertContains(resp, 'plausible range', msg_prefix=bad)
        self.assertFalse(Baseline.objects.filter(astronaut=self.astro).exists())
        self.astro.refresh_from_db()
        self.assertEqual(self.astro.name, 'Amara Reyes')   # the whole save was refused

    def test_text_instead_of_a_number_is_rejected(self):
        resp = self.post_profile(baseline_sleep='lots')
        self.assertContains(resp, 'is not a number')
        self.assertFalse(Baseline.objects.filter(astronaut=self.astro).exists())

    def test_range_check_applies_to_every_indicator(self):
        resp = self.post_profile(baseline_resting_hr='500', baseline_body_temp='12')
        self.assertContains(resp, 'Resting Heart Rate')
        self.assertContains(resp, 'plausible range')
        self.assertFalse(Baseline.objects.exists())