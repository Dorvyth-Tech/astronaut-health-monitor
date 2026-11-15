"""
Tests for the NASA DONKI space-weather card (core/donki.py).

No network: urllib.request.urlopen is replaced by a fake in every test, and the NASA key
is patched so a real .env can't leak in. Space weather is context only; the last group of
tests checks it never touches the health status, the score or the rules.
"""

import io
import json
import urllib.error
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from core import donki, intent
from core.models import AstronautProfile, SpaceWeatherSnapshot
from core.services import calculate_health_score, evaluate_astronaut_metrics
from core.tests_companion_ask import AskViewTestCase, ASK_URL, smart_urlopen
from core.tests_llm import FAKE_GROQ_KEY, FakeResponse, env, wrap

FAKE_NASA_KEY = 'fake-nasa-key-123'
LINK = 'https://ccmc.gsfc.nasa.gov/DONKI/view/{kind}/{n}/-1'

FLR_EVENTS = [
    {'flrID': 'f1', 'classType': 'C2.1', 'beginTime': '2026-11-10T00:50Z', 'peakTime': '2026-11-10T01:00Z',
     'link': LINK.format(kind='FLR', n=1)},
    {'flrID': 'f2', 'classType': 'M1.2', 'beginTime': '2026-11-12T04:00Z', 'peakTime': '2026-11-12T04:10Z',
     'link': LINK.format(kind='FLR', n=2)},
    {'flrID': 'f3', 'classType': 'M0.9', 'beginTime': '2026-11-13T02:00Z', 'peakTime': '2026-11-13T02:20Z',
     'link': LINK.format(kind='FLR', n=3)},
    {'flrID': 'f4', 'classType': 'B5.0', 'beginTime': '2026-11-13T05:00Z', 'peakTime': '2026-11-13T05:05Z',
     'link': LINK.format(kind='FLR', n=4)},
]
SEP_EVENTS = [
    {'sepID': 's1', 'eventTime': '2026-11-11T05:00Z', 'link': LINK.format(kind='SEP', n=1)},
    {'sepID': 's2', 'eventTime': '2026-11-13T09:30Z', 'link': LINK.format(kind='SEP', n=2)},
]
GST_EVENTS = [
    {'gstID': 'g1', 'startTime': '2026-11-11T18:00Z', 'link': LINK.format(kind='GST', n=1),
     'allKpIndex': [{'kpIndex': 4.67, 'observedTime': '2026-11-11T21:00Z', 'source': 'NOAA'}]},
    {'gstID': 'g2', 'startTime': '2026-11-12T18:00Z', 'link': LINK.format(kind='GST', n=2),
     'allKpIndex': [{'kpIndex': 5.0, 'observedTime': '2026-11-12T18:00Z', 'source': 'NOAA'},
                    {'kpIndex': 6.33, 'observedTime': '2026-11-12T21:00Z', 'source': 'NOAA'}]},
]
GOOD = {'FLR': FLR_EVENTS, 'SEP': SEP_EVENTS, 'GST': GST_EVENTS}


def http_error(code):
    return urllib.error.HTTPError('https://api.nasa.gov/DONKI/X', code, 'err', {}, None)


def routed_urlopen(routes):
    """A urlopen fake. routes: endpoint -> list (JSON), bytes (raw body) or an Exception to raise.

    .calls records (request, timeout) for every call.
    """
    calls = []

    def _open(request, timeout=None):
        calls.append((request, timeout))
        endpoint = request.full_url.split('/DONKI/')[1].split('?')[0]
        route = routes[endpoint]
        if isinstance(route, Exception):
            raise route
        body = route if isinstance(route, bytes) else json.dumps(route).encode('utf-8')
        return FakeResponse(body)

    _open.calls = calls
    return _open


def patch_net(routes):
    fake = routed_urlopen(routes)
    return mock.patch('core.donki.urllib.request.urlopen', fake), fake


def stored_view(routes=GOOD):
    """Run a real (mocked-network) refresh and return the stored view."""
    patcher, _ = patch_net(routes)
    with patcher:
        return donki.refresh_space_weather()


class FakeKeyMixin:
    def setUp(self):
        super().setUp()
        patcher = mock.patch('core.donki.config', return_value=FAKE_NASA_KEY)
        patcher.start()
        self.addCleanup(patcher.stop)
        cache.clear()
        self.addCleanup(cache.clear)


# =========================================================================
# Pure parsing
# =========================================================================

class FlareClassTests(SimpleTestCase):
    def test_letters_rank_x_over_m_over_c_over_b_over_a(self):
        ranked = ['A9.9', 'B1.0', 'C9.9', 'M1.0', 'X1.0']
        shuffled = ['M1.0', 'A9.9', 'X1.0', 'C9.9', 'B1.0']
        self.assertEqual(sorted(shuffled, key=donki.parse_flare_class), ranked)

    def test_number_ranks_within_a_class(self):
        self.assertGreater(donki.parse_flare_class('M5.0'), donki.parse_flare_class('M1.2'))
        self.assertGreater(donki.parse_flare_class('X10.5'), donki.parse_flare_class('X2.0'))

    def test_a_weak_x_beats_a_strong_m(self):
        self.assertGreater(donki.parse_flare_class('X0.1'), donki.parse_flare_class('M9.9'))

    def test_unreadable_classes_are_none(self):
        for bad in ('', 'Z1.0', 'M-1', None, 5, 'flare', 'MM1'):
            with self.subTest(bad=bad):
                self.assertIsNone(donki.parse_flare_class(bad))

    def test_lowercase_and_letter_only_are_accepted(self):
        self.assertEqual(donki.parse_flare_class('m2.5'), (3, 2.5))
        self.assertEqual(donki.parse_flare_class('X'), (4, 0.0))


class KpToGLevelTests(SimpleTestCase):
    def test_noaa_g_scale(self):
        expected = {5.0: 1, 5.33: 1, 5.67: 1, 6.0: 2, 6.67: 2, 7.0: 3, 7.67: 3, 8.0: 4, 8.67: 4, 9.0: 5}
        for kp, level in expected.items():
            with self.subTest(kp=kp):
                self.assertEqual(donki.kp_to_g_level(kp), level)

    def test_below_g1_and_bad_values_are_none(self):
        for kp in (0, 4.0, 4.67, None, 'x', float('nan')):
            with self.subTest(kp=kp):
                self.assertIsNone(donki.kp_to_g_level(kp))

    def test_never_above_g5(self):
        self.assertEqual(donki.kp_to_g_level(9.5), 5)


class SummarizeTests(SimpleTestCase):
    def test_flares_counted_by_class_with_strongest_and_link(self):
        out = donki.summarize_flares(FLR_EVENTS)
        self.assertEqual(out['count'], 4)
        self.assertEqual(out['by_class'], {'X': 0, 'M': 2, 'C': 1, 'B': 1, 'A': 0})
        self.assertEqual(out['strongest'], {
            'class_type': 'M1.2', 'peak_time': '2026-11-12T04:10Z', 'link': LINK.format(kind='FLR', n=2)})
        self.assertTrue(out['available'])

    def test_empty_flares(self):
        out = donki.summarize_flares([])
        self.assertEqual((out['count'], out['strongest'], out['available']), (0, None, True))

    def test_flares_skip_junk_entries(self):
        out = donki.summarize_flares([None, 'x', {'classType': 'nonsense'}, {}, {'classType': 'C1.0'}])
        self.assertEqual(out['count'], 1)
        self.assertEqual(out['strongest']['class_type'], 'C1.0')

    def test_a_non_https_link_is_dropped(self):
        out = donki.summarize_flares([{'classType': 'M1.0', 'link': 'javascript:alert(1)'}])
        self.assertEqual(out['strongest']['link'], '')

    def test_sep_count_latest_time_and_link(self):
        out = donki.summarize_sep(SEP_EVENTS)
        self.assertEqual((out['count'], out['latest_time']), (2, '2026-11-13T09:30Z'))
        self.assertEqual(out['latest_link'], LINK.format(kind='SEP', n=2))

    def test_sep_empty(self):
        out = donki.summarize_sep([])
        self.assertEqual((out['count'], out['latest_time'], out['latest_link']), (0, '', ''))

    def test_gst_max_kp_across_events_and_g_level(self):
        out = donki.summarize_gst(GST_EVENTS)
        self.assertEqual(out['storm_count'], 2)
        self.assertEqual(out['max_kp'], 6.33)
        self.assertEqual(out['g_level'], 2)
        self.assertEqual(out['kp_time'], '2026-11-12T21:00Z')
        self.assertEqual(out['link'], LINK.format(kind='GST', n=2))

    def test_gst_empty_and_malformed_kp(self):
        empty = donki.summarize_gst([])
        self.assertEqual((empty['storm_count'], empty['max_kp'], empty['g_level']), (0, None, None))
        junk = donki.summarize_gst([{'allKpIndex': 'nope'}, {'allKpIndex': [{'kpIndex': 'x'}, {}]}, 7])
        self.assertIsNone(junk['max_kp'])

    def test_gst_below_g1(self):
        out = donki.summarize_gst([{'allKpIndex': [{'kpIndex': 4.33, 'observedTime': '2026-11-12T21:00Z'}]}])
        self.assertEqual(out['max_kp'], 4.33)
        self.assertIsNone(out['g_level'])


# =========================================================================
# The HTTP client
# =========================================================================

class FetchEndpointTests(FakeKeyMixin, SimpleTestCase):
    def fetch(self, route, endpoint='FLR'):
        fake = routed_urlopen({endpoint: route})
        with mock.patch('core.donki.urllib.request.urlopen', fake):
            return donki.fetch_endpoint(endpoint, '2026-11-08', '2026-11-15'), fake

    def test_url_path_dates_key_headers_and_timeout(self):
        data, fake = self.fetch(FLR_EVENTS)
        self.assertEqual(data, FLR_EVENTS)
        request, timeout = fake.calls[0]
        self.assertTrue(request.full_url.startswith('https://api.nasa.gov/DONKI/FLR?'))
        self.assertIn('startDate=2026-11-08', request.full_url)
        self.assertIn('endDate=2026-11-15', request.full_url)
        self.assertIn(f'api_key={FAKE_NASA_KEY}', request.full_url)
        self.assertIn('SPACE-PULSE', request.get_header('User-agent'))
        self.assertEqual(timeout, donki.TIMEOUT_SECONDS)
        self.assertEqual(donki.TIMEOUT_SECONDS, 5)

    def test_key_falls_back_to_demo_key(self):
        for blank in ('', '   ', None):
            with self.subTest(blank=blank), mock.patch('core.donki.config', return_value=blank):
                self.assertEqual(donki.api_key(), 'DEMO_KEY')

    def test_real_key_is_used_when_set(self):
        self.assertEqual(donki.api_key(), FAKE_NASA_KEY)

    def test_public_url_has_no_key(self):
        url = donki.public_url('GST', '2026-11-08', '2026-11-15')
        self.assertEqual(url, 'https://api.nasa.gov/DONKI/GST?startDate=2026-11-08&endDate=2026-11-15')

    def assert_error(self, route, fragment):
        with self.assertRaises(donki.DonkiError) as ctx:
            self.fetch(route)
        self.assertIn(fragment, str(ctx.exception))
        self.assertNotIn(FAKE_NASA_KEY, str(ctx.exception))
        self.assertNotIn('api_key', str(ctx.exception))

    def test_http_429_and_404_and_500(self):
        for code in (429, 404, 500):
            with self.subTest(code=code):
                self.assert_error(http_error(code), f'HTTP {code}')

    def test_timeout(self):
        self.assert_error(TimeoutError('timed out'), 'TimeoutError')

    def test_network_error(self):
        self.assert_error(urllib.error.URLError('dns failure'), 'URLError')

    def test_malformed_json(self):
        self.assert_error(b'<html>not json</html>', 'not valid JSON')

    def test_a_json_object_instead_of_a_list(self):
        self.assert_error(b'{"error": "nope"}', 'unexpected response shape')

    def test_empty_list_is_fine(self):
        data, _ = self.fetch([])
        self.assertEqual(data, [])


class BuildSummaryTests(FakeKeyMixin, SimpleTestCase):
    def build(self, routes):
        fake = routed_urlopen(routes)
        with mock.patch('core.donki.urllib.request.urlopen', fake):
            return donki.build_summary(), fake

    def test_full_summary(self):
        summary, fake = self.build(GOOD)
        self.assertEqual(sorted(r.full_url.split('/DONKI/')[1].split('?')[0] for r, _ in fake.calls),
                         ['FLR', 'GST', 'SEP'])
        self.assertEqual(summary['window_days'], 7)
        self.assertEqual(summary['flares']['strongest']['class_type'], 'M1.2')
        self.assertEqual(summary['sep']['count'], 2)
        self.assertEqual(summary['geomagnetic']['g_level'], 2)
        self.assertEqual(summary['errors'], {})
        self.assertFalse(summary['sample'])
        self.assertEqual(len(summary['sources']), 3)
        self.assertEqual(summary['donki_url'], donki.DONKI_HOME_URL)

    def test_the_window_is_seven_days(self):
        summary, _ = self.build(GOOD)
        from datetime import date
        span = date.fromisoformat(summary['end_date']) - date.fromisoformat(summary['start_date'])
        self.assertEqual(span, timedelta(days=7))

    def test_no_key_in_the_summary(self):
        summary, _ = self.build(GOOD)
        blob = json.dumps(summary)
        self.assertNotIn(FAKE_NASA_KEY, blob)
        self.assertNotIn('api_key', blob)

    def test_all_empty_lists_is_valid_data(self):
        summary, _ = self.build({'FLR': [], 'SEP': [], 'GST': []})
        self.assertEqual(summary['flares']['count'], 0)
        self.assertEqual(summary['sep']['count'], 0)
        self.assertIsNone(summary['geomagnetic']['max_kp'])
        self.assertTrue(summary['flares']['available'])

    def test_flare_endpoint_404_fails_soft_and_the_rest_still_works(self):
        summary, _ = self.build({'FLR': http_error(404), 'SEP': SEP_EVENTS, 'GST': GST_EVENTS})
        self.assertFalse(summary['flares']['available'])
        self.assertTrue(summary['sep']['available'])
        self.assertEqual(summary['sep']['count'], 2)
        self.assertEqual(summary['geomagnetic']['max_kp'], 6.33)
        self.assertIn('FLR', summary['errors'])
        self.assertEqual(list(summary['errors']), ['FLR'])

    def test_each_single_failure_blanks_only_its_own_section(self):
        for failing, section in (('SEP', 'sep'), ('GST', 'geomagnetic')):
            routes = {**GOOD, failing: http_error(429)}
            with self.subTest(failing=failing):
                summary, _ = self.build(routes)
                self.assertFalse(summary[section]['available'])

    def test_all_three_failing_raises(self):
        with self.assertRaises(donki.DonkiError):
            self.build({'FLR': http_error(404), 'SEP': http_error(429), 'GST': TimeoutError('t')})

    def test_a_parser_crash_is_contained(self):
        with mock.patch.dict(donki.PARSERS, {'SEP': mock.Mock(side_effect=ValueError('boom'))}):
            summary, _ = self.build(GOOD)
        self.assertFalse(summary['sep']['available'])
        self.assertTrue(summary['flares']['available'])


# =========================================================================
# Refresh strategy, cache and snapshots
# =========================================================================

class GetSpaceWeatherTests(FakeKeyMixin, TestCase):
    def test_refresh_stores_a_snapshot_and_caches_it(self):
        patcher, fake = patch_net(GOOD)
        with patcher:
            first = donki.get_space_weather()
            second = donki.get_space_weather()
        self.assertEqual(first['state'], donki.STATE_LIVE)
        self.assertEqual(first['summary']['flares']['count'], 4)
        self.assertEqual(second, first)
        self.assertEqual(len(fake.calls), 3)                    # one per endpoint, then the cache
        row = SpaceWeatherSnapshot.objects.get()
        self.assertEqual(row.source, 'donki')
        self.assertEqual(row.payload['sep']['count'], 2)

    def test_a_fresh_snapshot_is_used_without_any_network_call(self):
        donki.save_snapshot(donki.sample_summary() | {'sample': False})
        cache.clear()
        patcher, fake = patch_net(GOOD)
        with patcher:
            view = donki.get_space_weather()
        self.assertEqual(view['state'], donki.STATE_LIVE)
        self.assertEqual(fake.calls, [])

    def old_snapshot(self, minutes=120, source='donki'):
        row = donki.save_snapshot(donki.sample_summary() | {'sample': source == 'sample'}, source)
        SpaceWeatherSnapshot.objects.filter(pk=row.pk).update(fetched_at=timezone.now() - timedelta(minutes=minutes))
        cache.clear()
        return row

    def test_api_down_falls_back_to_the_last_snapshot_labelled_cached(self):
        self.old_snapshot()
        for failure in (http_error(429), http_error(404), TimeoutError('slow')):
            cache.clear()
            with self.subTest(failure=failure):
                patcher, _ = patch_net({'FLR': failure, 'SEP': failure, 'GST': failure})
                with patcher:
                    view = donki.get_space_weather()
                self.assertEqual(view['state'], donki.STATE_CACHED)
                self.assertTrue(donki.describe(view)['status'].startswith('Cached from '))

    def test_no_data_ever_returns_none(self):
        patcher, _ = patch_net({'FLR': TimeoutError(), 'SEP': TimeoutError(), 'GST': TimeoutError()})
        with patcher:
            self.assertIsNone(donki.get_space_weather())
        self.assertFalse(SpaceWeatherSnapshot.objects.exists())

    def test_after_a_failure_nothing_is_retried_for_five_minutes(self):
        patcher, fake = patch_net({'FLR': TimeoutError(), 'SEP': TimeoutError(), 'GST': TimeoutError()})
        with patcher:
            donki.get_space_weather()
            donki.get_space_weather()
            donki.get_space_weather()
        self.assertEqual(len(fake.calls), 3)                    # only the first attempt

    def test_allow_refresh_false_never_touches_the_network(self):
        patcher, fake = patch_net(GOOD)
        with patcher:
            self.assertIsNone(donki.get_space_weather(allow_refresh=False))
            self.old_snapshot()
            view = donki.get_space_weather(allow_refresh=False)
        self.assertEqual(fake.calls, [])
        self.assertEqual(view['state'], donki.STATE_CACHED)

    def test_a_stale_snapshot_is_refreshed_when_allowed(self):
        self.old_snapshot()
        patcher, fake = patch_net(GOOD)
        with patcher:
            view = donki.get_space_weather()
        self.assertEqual(view['state'], donki.STATE_LIVE)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(SpaceWeatherSnapshot.objects.count(), 2)

    def test_a_sample_snapshot_is_labelled_sample_and_replaced_by_real_data(self):
        self.old_snapshot(minutes=1, source='sample')
        patcher, _ = patch_net({'FLR': TimeoutError(), 'SEP': TimeoutError(), 'GST': TimeoutError()})
        with patcher:
            offline = donki.get_space_weather()
        self.assertEqual(offline['state'], donki.STATE_SAMPLE)
        cache.clear()
        patcher, _ = patch_net(GOOD)
        with patcher:
            online = donki.get_space_weather()
        self.assertEqual(online['state'], donki.STATE_LIVE)
        self.assertEqual(online['source'], 'donki')

    def test_get_never_raises(self):
        with mock.patch('core.donki._model', side_effect=RuntimeError('db down')):
            self.assertIsNone(donki.get_space_weather())

    def test_old_snapshots_are_pruned(self):
        for _ in range(donki.KEEP_SNAPSHOTS + 5):
            donki.save_snapshot(donki.sample_summary(), 'sample')
        self.assertEqual(SpaceWeatherSnapshot.objects.count(), donki.KEEP_SNAPSHOTS)

    def test_sample_helper_only_stores_when_empty(self):
        self.assertTrue(donki.store_sample_snapshot_if_empty())
        self.assertFalse(donki.store_sample_snapshot_if_empty())
        row = SpaceWeatherSnapshot.objects.get()
        self.assertEqual(row.source, 'sample')
        self.assertTrue(row.payload['sample'])

    def test_sample_helper_never_replaces_real_data(self):
        stored_view()
        self.assertFalse(donki.store_sample_snapshot_if_empty())
        self.assertEqual(SpaceWeatherSnapshot.objects.get().source, 'donki')


# =========================================================================
# Words
# =========================================================================

class DescribeTests(FakeKeyMixin, TestCase):
    def test_live_card_text(self):
        shown = donki.describe(stored_view())
        self.assertEqual(shown['flare_main'], 'M1.2')
        self.assertIn('4 flares', shown['flare_sub'])
        self.assertIn('2 M-class', shown['flare_sub'])
        self.assertEqual(shown['sep_main'], '2')
        self.assertIn('13 Nov 09:30 UTC', shown['sep_sub'])
        self.assertEqual(shown['kp_main'], 'Kp 6.33 (G2)')
        self.assertIn('Moderate', shown['kp_sub'])
        self.assertTrue(shown['status'].startswith('Updated '))
        self.assertTrue(shown['status'].endswith('UTC'))
        self.assertEqual(shown['label'], donki.LABEL)
        self.assertIn('not a measurement of crew radiation dose', shown['label'])
        self.assertFalse(shown['is_sample'])

    def test_flares_unavailable_keeps_the_other_two(self):
        shown = donki.describe(stored_view({'FLR': http_error(404), 'SEP': SEP_EVENTS, 'GST': GST_EVENTS}))
        self.assertEqual(shown['flare_sub'], 'Flare data temporarily unavailable from NASA')
        self.assertEqual(shown['flare_main'], '--')
        self.assertEqual(shown['sep_main'], '2')
        self.assertEqual(shown['kp_main'], 'Kp 6.33 (G2)')

    def test_quiet_week(self):
        shown = donki.describe(stored_view({'FLR': [], 'SEP': [], 'GST': []}))
        self.assertEqual(shown['flare_main'], 'None')
        self.assertEqual(shown['sep_main'], '0')
        self.assertEqual(shown['kp_main'], 'None')

    def test_kp_below_g1_text(self):
        gst = [{'allKpIndex': [{'kpIndex': 4.33, 'observedTime': '2026-11-12T21:00Z'}], 'link': LINK.format(kind='GST', n=9)}]
        shown = donki.describe(stored_view({'FLR': [], 'SEP': [], 'GST': gst}))
        self.assertEqual(shown['kp_main'], 'Kp 4.33')
        self.assertIn('Below NOAA G1', shown['kp_sub'])

    def test_sample_label(self):
        view = {'summary': donki.sample_summary(), 'source': 'sample', 'fetched_at': timezone.now(),
                'state': donki.STATE_SAMPLE}
        shown = donki.describe(view)
        self.assertTrue(shown['is_sample'])
        self.assertIn('Sample data', shown['status'])


class BuildAnswerTests(FakeKeyMixin, TestCase):
    def test_answer_is_deterministic_and_labelled(self):
        answer = donki.build_answer(stored_view())
        self.assertEqual(answer['id'], 'space_weather')
        self.assertEqual(answer['answer'], (
            "In the last 7 days NASA DONKI recorded 4 solar flares, strongest M1.2 on 12 Nov 04:10 UTC; "
            "2 solar energetic particle events; the highest Kp index was 6.33 (NOAA G2, Moderate). "
            "This is space weather near Earth, not a measure of your dose."))
        self.assertEqual(answer['severity'], 'info')
        self.assertIn('NASA DONKI', answer['source'])
        self.assertIn('https://ccmc.gsfc.nasa.gov/tools/DONKI/', answer['source'])
        self.assertEqual(answer['disclaimer'], 'Not a medical device; does not diagnose.')
        self.assertEqual(answer['escalation'], '')

    def test_flares_unavailable_is_said_plainly(self):
        answer = donki.build_answer(stored_view({'FLR': http_error(404), 'SEP': SEP_EVENTS, 'GST': GST_EVENTS}))
        self.assertIn("solar flare feed is temporarily unavailable", answer['answer'])
        self.assertIn('2 solar energetic particle events', answer['answer'])
        self.assertIn('6.33', answer['answer'])

    def test_quiet_week(self):
        answer = donki.build_answer(stored_view({'FLR': [], 'SEP': [], 'GST': []}))
        self.assertIn('recorded no solar flares', answer['answer'])
        self.assertIn('0 solar energetic particle events', answer['answer'])
        self.assertIn('no geomagnetic storms', answer['answer'])

    def test_single_flare_and_event_are_singular(self):
        answer = donki.build_answer(stored_view(
            {'FLR': FLR_EVENTS[1:2], 'SEP': SEP_EVENTS[:1], 'GST': []}))
        self.assertIn('1 solar flare,', answer['answer'])
        self.assertIn('1 solar energetic particle event;', answer['answer'])

    def test_sample_and_cached_are_marked(self):
        sample = donki.build_answer({'summary': donki.sample_summary(), 'source': 'sample',
                                     'fetched_at': timezone.now(), 'state': donki.STATE_SAMPLE})
        self.assertTrue(sample['answer'].startswith('Sample data, not live NASA data.'))
        self.assertIn('Sample data', sample['source'])
        cached = donki.build_answer({**stored_view(), 'state': donki.STATE_CACHED})
        self.assertIn('Cached from', cached['answer'])


# =========================================================================
# Pages, JSON, Ask Pulse
# =========================================================================

DASHBOARD = '/'


class DashboardCardTests(FakeKeyMixin, AskViewTestCase):
    def test_card_with_live_data(self):
        stored_view()
        html = self.client.get(DASHBOARD).content.decode()
        self.assertIn('Space Weather (NASA DONKI)', html)
        self.assertIn('M1.2', html)
        self.assertIn('Kp 6.33 (G2)', html)
        self.assertIn('Context only', html)
        self.assertIn('Space weather near Earth (NASA DONKI). Context only; not a measurement of crew radiation dose.', html)
        self.assertIn('https://ccmc.gsfc.nasa.gov/tools/DONKI/', html)
        self.assertIn('https://www.swpc.noaa.gov/noaa-scales-explanation', html)
        self.assertNotIn('Sample data', html)

    def test_card_without_any_data_is_graceful(self):
        response = self.client.get(DASHBOARD)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('Space weather unavailable', html)
        self.assertIn('Space Weather (NASA DONKI)', html)

    def test_card_shows_sample_label(self):
        donki.save_snapshot(donki.sample_summary(), 'sample')
        html = self.client.get(DASHBOARD).content.decode()
        self.assertIn('Sample data', html)
        self.assertIn('not live NASA data', html)

    def test_card_shows_cached_label_when_nasa_is_down(self):
        row = donki.save_snapshot(donki.sample_summary() | {'sample': False}, 'donki')
        SpaceWeatherSnapshot.objects.filter(pk=row.pk).update(fetched_at=timezone.now() - timedelta(hours=3))
        cache.clear()
        patcher, _ = patch_net({'FLR': TimeoutError(), 'SEP': TimeoutError(), 'GST': TimeoutError()})
        with patcher, override_settings(SPACE_WEATHER_AUTO_REFRESH=True):
            html = self.client.get(DASHBOARD).content.decode()
        self.assertIn('Cached from ', html)

    def test_card_flares_unavailable_text(self):
        stored_view({'FLR': http_error(404), 'SEP': SEP_EVENTS, 'GST': GST_EVENTS})
        html = self.client.get(DASHBOARD).content.decode()
        self.assertIn('Flare data temporarily unavailable from NASA', html)
        self.assertIn('Kp 6.33 (G2)', html)

    def test_dashboard_refreshes_once_then_uses_the_cache(self):
        patcher, fake = patch_net(GOOD)
        with patcher, override_settings(SPACE_WEATHER_AUTO_REFRESH=True):
            first = self.client.get(DASHBOARD).content.decode()
            self.client.get(DASHBOARD)
        self.assertIn('M1.2', first)
        self.assertEqual(len(fake.calls), 3)

    def test_dashboard_makes_no_call_when_auto_refresh_is_off(self):
        patcher, fake = patch_net(GOOD)
        with patcher:
            self.assertEqual(self.client.get(DASHBOARD).status_code, 200)
        self.assertEqual(fake.calls, [])
        self.assertFalse(settings.SPACE_WEATHER_AUTO_REFRESH)

    def test_dashboard_survives_a_broken_card(self):
        stored_view()
        with mock.patch('core.views.donki.describe', side_effect=KeyError('x')):
            response = self.client.get(DASHBOARD)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Space weather unavailable', response.content.decode())

    def test_dashboard_survives_get_space_weather_failing_inside(self):
        with mock.patch('core.donki._model', side_effect=RuntimeError('db')):
            self.assertEqual(self.client.get(DASHBOARD).status_code, 200)


class JsonEndpointTests(FakeKeyMixin, AskViewTestCase):
    def test_requires_login(self):
        self.client.logout()
        response = self.client.get('/space-weather/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_with_data(self):
        stored_view()
        data = self.client.get('/space-weather/').json()
        self.assertTrue(data['available'])
        self.assertEqual(data['state'], 'live')
        self.assertEqual(data['source'], 'donki')
        self.assertEqual(data['summary']['flares']['strongest']['class_type'], 'M1.2')
        self.assertEqual(data['display']['kp_main'], 'Kp 6.33 (G2)')
        self.assertIn('not a measurement of crew radiation dose', data['label'])
        self.assertNotIn(FAKE_NASA_KEY, json.dumps(data))
        self.assertNotIn('api_key', json.dumps(data))

    def test_without_data(self):
        data = self.client.get('/space-weather/').json()
        self.assertEqual(data['available'], False)
        self.assertEqual(data['message'], 'Space weather unavailable')

    def test_get_only(self):
        self.assertEqual(self.client.post('/space-weather/').status_code, 405)


class AskPulseRadiationTests(FakeKeyMixin, AskViewTestCase):
    def test_radiation_answer_from_the_snapshot(self):
        stored_view()
        data = self.ask("What is the radiation level today?").json()
        self.assertFalse(data['out_of_scope'])
        self.assertEqual(data['matched_id'], 'space_weather')
        self.assertEqual(data['method'], 'keyword')
        answer = data['answer']
        self.assertIn('4 solar flares, strongest M1.2 on 12 Nov 04:10 UTC', answer['answer'])
        self.assertIn('2 solar energetic particle events', answer['answer'])
        self.assertIn('6.33 (NOAA G2, Moderate)', answer['answer'])
        self.assertIn('not a measure of your dose', answer['answer'])
        self.assertIn('NASA DONKI', answer['source'])
        self.assertEqual(answer['disclaimer'], 'Not a medical device; does not diagnose.')
        self.assertFalse(answer['ai_rephrased'])
        self.assertEqual(answer['ai_label'], '')

    def test_every_radiation_phrase_gets_the_answer_when_data_exists(self):
        stored_view()
        for phrase in ("any solar flare today", "space weather?", "is the sun dangerous today?", "cosmic radiation?"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.ask(phrase).json()['matched_id'], 'space_weather')

    def test_without_data_the_honest_reply_stays(self):
        data = self.ask("What is the radiation level today?").json()
        self.assertTrue(data['out_of_scope'])
        self.assertEqual(data['method'], 'keyword')
        self.assertIn(intent.NO_RADIATION_DATA_NOTE, data['message'])
        self.assertIn(intent.OUT_OF_SCOPE_MESSAGE, data['message'])

    def test_sample_data_is_announced_in_the_answer(self):
        donki.save_snapshot(donki.sample_summary(), 'sample')
        answer = self.ask("any solar flares?").json()['answer']
        self.assertTrue(answer['answer'].startswith('Sample data, not live NASA data.'))
        self.assertIn('Sample data', answer['source'])

    def test_flares_unavailable_answer(self):
        stored_view({'FLR': http_error(404), 'SEP': SEP_EVENTS, 'GST': GST_EVENTS})
        answer = self.ask("any solar flares?").json()['answer']
        self.assertIn('solar flare feed is temporarily unavailable', answer['answer'])

    def test_a_refresh_is_made_when_auto_refresh_is_on(self):
        patcher, fake = patch_net(GOOD)
        with patcher, override_settings(SPACE_WEATHER_AUTO_REFRESH=True):
            data = self.ask("any solar flares?").json()
        self.assertEqual(data['matched_id'], 'space_weather')
        self.assertEqual(len(fake.calls), 3)

    def test_nothing_about_the_astronauts_health_leaks_into_the_answer(self):
        stored_view()
        answer = self.ask("any solar flares?").json()['answer']
        self.assertEqual(answer['indicator'], None)
        self.assertEqual(answer['escalation'], '')
        self.assertNotIn('flight surgeon', answer['answer'].lower())

    # ---- the AI path
    def test_radiation_is_offered_to_the_ai_classifier_only_when_data_exists(self):
        question = "Are geomagnetic storms likely this week?"
        with override_settings(COMPANION_LLM_ENABLED=True), \
                mock.patch('core.views.classify_intent_with_llm', return_value=None) as classify:
            self.ask(question)
            without = classify.call_args.args
            stored_view()
            self.ask(question)
            with_data = classify.call_args.args
        self.assertNotIn('radiation', without[1])
        self.assertNotIn('radiation', without[2])
        self.assertIn('radiation', with_data[1])
        self.assertEqual(with_data[2]['radiation'], intent.RADIATION_INTENT_DESCRIPTION)
        self.assertEqual(tuple(with_data[1][:-1]), intent.INTENT_IDS)

    def test_ai_picking_radiation_without_data_is_out_of_scope(self):
        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', smart_urlopen('radiation')):
            data = self.ask("Are geomagnetic storms likely this week?").json()
        self.assertTrue(data['out_of_scope'])

    def test_ai_picking_radiation_with_data_gets_the_computed_answer(self):
        stored_view()
        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', smart_urlopen('radiation')):
            data = self.ask("Are geomagnetic storms likely this week?").json()
        self.assertFalse(data['out_of_scope'])
        self.assertEqual(data['matched_id'], 'space_weather')
        self.assertEqual(data['method'], 'ai')
        self.assertTrue(data['answer']['ai_rephrased'])               # reworded, 'Gently: ' prefix
        self.assertTrue(data['answer']['answer'].startswith('Gently: In the last 7 days'))
        self.assertIn('6.33', data['answer']['answer'])               # the numbers survived
        self.assertIn('NASA DONKI', data['answer']['source'])         # never rephrased
        self.assertEqual(data['answer']['disclaimer'], 'Not a medical device; does not diagnose.')

    def test_a_rewrite_that_adds_a_number_is_rejected(self):
        stored_view()

        def greedy(request, timeout=None):
            body = json.loads(request.data.decode('utf-8'))
            payload = json.loads(body['messages'][-1]['content'])
            content = json.dumps({'answers': [
                {'id': a['id'], 'text': a['text'] + ' Expect 999 flares tomorrow.'} for a in payload['answers']]})
            return FakeResponse(json.dumps(wrap(request.full_url, content)).encode('utf-8'))

        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', greedy):
            answer = self.ask("any solar flares?").json()['answer']
        self.assertFalse(answer['ai_rephrased'])
        self.assertNotIn('999', answer['answer'])
        self.assertTrue(answer['answer'].startswith('In the last 7 days'))

    def test_radiation_keyword_with_ai_on_does_not_call_the_classifier(self):
        stored_view()
        with override_settings(COMPANION_LLM_ENABLED=True), \
                mock.patch('core.views.classify_intent_with_llm') as classify, \
                mock.patch('core.views.rephrase_with_llm', side_effect=lambda answers, user=None: answers):
            data = self.ask("any solar flares?").json()
        classify.assert_not_called()
        self.assertEqual(data['matched_id'], 'space_weather')


# =========================================================================
# Management command and seeding
# =========================================================================

class CommandTests(FakeKeyMixin, TestCase):
    def run_command(self, *args):
        out = io.StringIO()
        call_command('fetch_space_weather', *args, stdout=out)
        return out.getvalue()

    def test_fetch_prints_the_summary_and_stores_a_snapshot(self):
        patcher, _ = patch_net(GOOD)
        with patcher:
            text = self.run_command()
        self.assertIn('Space weather snapshot stored.', text)
        self.assertIn('M1.2', text)
        self.assertIn('Kp 6.33 (G2)', text)
        self.assertNotIn(FAKE_NASA_KEY, text)
        self.assertEqual(SpaceWeatherSnapshot.objects.count(), 1)

    def test_fetch_reports_partial_data(self):
        patcher, _ = patch_net({'FLR': http_error(404), 'SEP': SEP_EVENTS, 'GST': GST_EVENTS})
        with patcher:
            text = self.run_command()
        self.assertIn('Partial data: FLR: HTTP 404', text)
        self.assertIn('Flare data temporarily unavailable from NASA', text)

    def test_fetch_failure_is_a_command_error_and_keeps_old_data(self):
        donki.save_snapshot(donki.sample_summary(), 'sample')
        patcher, _ = patch_net({'FLR': http_error(429), 'SEP': http_error(429), 'GST': http_error(429)})
        with patcher, self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual(SpaceWeatherSnapshot.objects.count(), 1)

    def test_sample_flag_stores_a_sample_without_network(self):
        patcher, fake = patch_net(GOOD)
        with patcher:
            text = self.run_command('--sample')
        self.assertEqual(fake.calls, [])
        self.assertIn('SAMPLE', text)
        self.assertEqual(SpaceWeatherSnapshot.objects.get().source, 'sample')

    def test_seed_demo_stores_a_labelled_sample_snapshot(self):
        call_command('seed_demo', stdout=io.StringIO())
        row = SpaceWeatherSnapshot.objects.get()
        self.assertEqual(row.source, 'sample')
        self.assertTrue(row.payload['sample'])


# =========================================================================
# Space weather is context only
# =========================================================================

class ScoreUnaffectedTests(FakeKeyMixin, TestCase):
    def test_health_score_and_statuses_ignore_space_weather(self):
        astronaut = AstronautProfile.objects.create(name='Score Tester', email='score@example.com', rank='Cmdr.')
        before = calculate_health_score(evaluate_astronaut_metrics(astronaut))
        statuses_before = {k: v['status'] for k, v in evaluate_astronaut_metrics(astronaut).items()}
        stored_view({'FLR': [{'classType': 'X9.9', 'peakTime': '2026-11-14T00:00Z'}], 'SEP': SEP_EVENTS * 5,
                     'GST': [{'allKpIndex': [{'kpIndex': 9.0, 'observedTime': '2026-11-14T00:00Z'}]}]})
        after = calculate_health_score(evaluate_astronaut_metrics(astronaut))
        statuses_after = {k: v['status'] for k, v in evaluate_astronaut_metrics(astronaut).items()}
        self.assertEqual(before, after)
        self.assertEqual(statuses_before, statuses_after)

    def test_health_rules_and_scoring_never_import_donki(self):
        core_dir = Path(__file__).resolve().parent
        for name in ('rules.py', 'services.py', 'companion.py', 'constants.py'):
            with self.subTest(file=name):
                self.assertNotIn('donki', (core_dir / name).read_text(encoding='utf-8').lower())


class ContextOnlyTests(FakeKeyMixin, AskViewTestCase):
    def test_dashboard_health_score_is_the_same_with_and_without_the_card(self):
        without = self.client.get(DASHBOARD).context['health_score']
        stored_view()
        with_card = self.client.get(DASHBOARD).context['health_score']
        self.assertEqual(without, with_card)

    def test_companion_chips_do_not_change(self):
        before = self.client.get('/companion/answers/').json()
        stored_view()
        after = self.client.get('/companion/answers/').json()
        self.assertEqual(before, after)
        self.assertNotIn('space_weather', [a['id'] for a in after['answers']])

    def test_radiation_is_still_not_an_intent_id(self):
        self.assertNotIn('radiation', intent.INTENT_IDS)
