"""
Tests for the companion's AI rephrase layer (core/llm.py).

No network: urllib.request.urlopen is replaced by a fake in every test, and the
environment variables are set explicitly so a real .env can't leak in.
"""

import io
import json
import os
import socket
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core import companion, llm
from core.companion import build_companion_answers
from core.models import AstronautProfile
from core.tests_companion import make_evals

ENV_KEYS = ('COMPANION_LLM_PROVIDER', 'GROQ_API_KEY', 'GEMINI_API_KEY', 'GROQ_MODEL', 'GEMINI_MODEL')
FAKE_GROQ_KEY = 'test-groq-key'
FAKE_GEMINI_KEY = 'test-gemini-key'


def env(**values):
    """An environment with every LLM variable blank except the ones given."""
    merged = {key: '' for key in ENV_KEYS}
    merged.update(values)
    return mock.patch.dict(os.environ, merged)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _request_texts(request):
    """The {'answers': [...]} the app sent, for either provider's request shape."""
    body = json.loads(request.data.decode('utf-8'))
    if 'messages' in body:                                  # Groq
        user_text = body['messages'][-1]['content']
    else:                                                   # Gemini
        user_text = body['contents'][0]['parts'][0]['text']
    return json.loads(user_text)['answers']


def model_json(items, rewrite):
    return json.dumps({'answers': [{'id': i['id'], 'text': rewrite(i)} for i in items]})


def wrap(url, content):
    """Put the model's text in the shape each provider returns."""
    if 'groq.com' in url:
        return {'choices': [{'message': {'content': content}}]}
    return {'candidates': [{'content': {'parts': [{'text': content}]}}]}


def fake_urlopen(rewrite=lambda item: 'Gently: ' + item['text'], raw=None, fail_urls=()):
    """A urlopen replacement. Records every request in .requests."""
    requests = []

    def _open(request, timeout=None):
        requests.append((request, timeout))
        if any(part in request.full_url for part in fail_urls):
            raise OSError('provider down')
        content = raw if raw is not None else model_json(_request_texts(request), rewrite)
        return FakeResponse(json.dumps(wrap(request.full_url, content)).encode('utf-8'))

    _open.requests = requests
    return _open


def sample_answers():
    evals = make_evals(sleep='caution', hrv='critical')
    return build_companion_answers(
        evals, {'score': 60, 'label': 'Amber'}, {'simulated': 10, 'manual': 2}, open_alerts=2)


def by_id(answers):
    return {a['id']: a for a in answers}


class LLMTestCase(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)


# --- providers ---------------------------------------------------------------

class ProviderTests(LLMTestCase):
    def test_groq_success(self):
        answers = sample_answers()
        opener = fake_urlopen()
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), mock.patch('core.llm.urllib.request.urlopen', opener):
            result = llm.rephrase_with_llm(answers)

        self.assertEqual(len(opener.requests), 1)
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, 'https://api.groq.com/openai/v1/chat/completions')
        self.assertEqual(request.get_header('Authorization'), f'Bearer {FAKE_GROQ_KEY}')
        self.assertLessEqual(timeout, 4.0)
        self.assertEqual(json.loads(request.data)['model'], llm.DEFAULT_GROQ_MODEL)

        rephrased = by_id(result)
        original = by_id(answers)
        self.assertTrue(rephrased['overall']['ai_rephrased'])
        self.assertEqual(rephrased['overall']['ai_provider'], 'Groq')
        self.assertEqual(rephrased['overall']['answer'], 'Gently: ' + original['overall']['answer'])
        self.assertEqual([a['id'] for a in result], [a['id'] for a in answers])

    def test_gemini_success(self):
        answers = sample_answers()
        opener = fake_urlopen()
        with env(COMPANION_LLM_PROVIDER='gemini', GEMINI_API_KEY=FAKE_GEMINI_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', opener):
            result = llm.rephrase_with_llm(answers)

        request, _ = opener.requests[0]
        self.assertIn('generativelanguage.googleapis.com', request.full_url)
        self.assertIn(llm.DEFAULT_GEMINI_MODEL, request.full_url)
        self.assertEqual(request.get_header('X-goog-api-key'), FAKE_GEMINI_KEY)
        self.assertNotIn(FAKE_GEMINI_KEY, request.full_url)     # key never in the URL
        self.assertEqual(by_id(result)['overall']['ai_provider'], 'Gemini')

    def test_model_env_override(self):
        opener = fake_urlopen()
        with env(GROQ_API_KEY=FAKE_GROQ_KEY, GROQ_MODEL='my-model'), \
                mock.patch('core.llm.urllib.request.urlopen', opener):
            llm.rephrase_with_llm(sample_answers())
        self.assertEqual(json.loads(opener.requests[0][0].data)['model'], 'my-model')

    def test_fallback_groq_to_gemini(self):
        answers = sample_answers()
        opener = fake_urlopen(fail_urls=('groq.com',))
        with env(GROQ_API_KEY=FAKE_GROQ_KEY, GEMINI_API_KEY=FAKE_GEMINI_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', opener):
            result = llm.rephrase_with_llm(answers)

        self.assertEqual(len(opener.requests), 2)
        self.assertIn('groq.com', opener.requests[0][0].full_url)
        self.assertIn('googleapis.com', opener.requests[1][0].full_url)
        self.assertEqual(by_id(result)['overall']['ai_provider'], 'Gemini')

    def test_gemini_primary_falls_back_to_groq(self):
        opener = fake_urlopen(fail_urls=('googleapis.com',))
        with env(COMPANION_LLM_PROVIDER='gemini', GROQ_API_KEY=FAKE_GROQ_KEY, GEMINI_API_KEY=FAKE_GEMINI_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', opener):
            result = llm.rephrase_with_llm(sample_answers())
        self.assertEqual(by_id(result)['overall']['ai_provider'], 'Groq')

    def test_only_other_provider_key_is_used_when_primary_key_missing(self):
        opener = fake_urlopen()
        with env(GEMINI_API_KEY=FAKE_GEMINI_KEY), mock.patch('core.llm.urllib.request.urlopen', opener):
            result = llm.rephrase_with_llm(sample_answers())
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(by_id(result)['overall']['ai_provider'], 'Gemini')

    def test_unknown_provider_name_means_groq(self):
        opener = fake_urlopen()
        with env(COMPANION_LLM_PROVIDER='nonsense', GROQ_API_KEY=FAKE_GROQ_KEY, GEMINI_API_KEY=FAKE_GEMINI_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', opener):
            llm.rephrase_with_llm(sample_answers())
        self.assertIn('groq.com', opener.requests[0][0].full_url)

    def test_markdown_fenced_json_is_accepted(self):
        answers = sample_answers()
        items = [{'id': 'overall', 'text': 'Calmly: ' + by_id(answers)['overall']['answer']}]
        raw = '```json\n' + json.dumps({'answers': [{'id': i['id'], 'text': i['text']} for i in items]}) + '\n```'
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', fake_urlopen(raw=raw)):
            result = llm.rephrase_with_llm(answers)
        self.assertTrue(by_id(result)['overall']['ai_rephrased'])


# --- failures return the original --------------------------------------------

class FallbackTests(LLMTestCase):
    def test_no_keys_returns_unchanged_without_calling_anything(self):
        answers = sample_answers()
        opener = fake_urlopen()
        with env(), mock.patch('core.llm.urllib.request.urlopen', opener):
            result = llm.rephrase_with_llm(answers)
        self.assertEqual(result, answers)
        self.assertEqual(opener.requests, [])

    def test_timeout_returns_unchanged(self):
        answers = sample_answers()
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', side_effect=socket.timeout('timed out')):
            result = llm.rephrase_with_llm(answers)
        self.assertEqual(result, answers)

    def test_http_error_returns_unchanged(self):
        import urllib.error
        error = urllib.error.HTTPError('https://x', 429, 'Too Many Requests', {}, None)
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', side_effect=error):
            result = llm.rephrase_with_llm(sample_answers())
        self.assertEqual(result, sample_answers())

    def test_malformed_json_returns_unchanged(self):
        answers = sample_answers()
        for raw in ('this is not json', '{"answers": "nope"}', '{"wrong": []}', ''):
            cache.clear()
            with env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                    mock.patch('core.llm.urllib.request.urlopen', fake_urlopen(raw=raw)):
                self.assertEqual(llm.rephrase_with_llm(answers), answers, raw)

    def test_garbage_http_body_returns_unchanged(self):
        answers = sample_answers()
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', lambda *a, **k: FakeResponse(b'<html>oops</html>')):
            self.assertEqual(llm.rephrase_with_llm(answers), answers)

    def test_input_list_is_not_modified(self):
        answers = sample_answers()
        snapshot = json.loads(json.dumps(answers))
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), mock.patch('core.llm.urllib.request.urlopen', fake_urlopen()):
            llm.rephrase_with_llm(answers)
        self.assertEqual(answers, snapshot)


# --- safety validator --------------------------------------------------------

class SafetyTests(LLMTestCase):
    def _run(self, rewrite, answers=None):
        answers = answers or sample_answers()
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', fake_urlopen(rewrite=rewrite)):
            return answers, llm.rephrase_with_llm(answers)

    def test_invented_number_is_rejected_per_answer(self):
        def rewrite(item):
            if item['id'] == 'overall':
                return item['text'] + ' Aim for 9 hours.'
            return 'Gently: ' + item['text']

        answers, result = self._run(rewrite)
        self.assertEqual(by_id(result)['overall'], by_id(answers)['overall'])
        self.assertNotIn('ai_rephrased', by_id(result)['overall'])
        self.assertTrue(by_id(result)['what_to_do']['ai_rephrased'])      # others still accepted

    def test_spelled_out_new_number_is_rejected(self):
        answers, result = self._run(lambda item: 'About three hours short. ' + item['text'])
        self.assertEqual(result, answers)

    def test_dropping_or_keeping_original_numbers_is_ok(self):
        answers = sample_answers()
        self.assertIn('60', by_id(answers)['overall']['answer'])
        self.assertTrue(llm.is_safe_rewrite('Your score is 60.', 'A calm 60 for you.'))
        self.assertTrue(llm.is_safe_rewrite('Your score is 60.', 'You are doing fine.'))
        self.assertFalse(llm.is_safe_rewrite('Your score is 60.', 'Your score is 61.'))

    def test_banned_diagnosis_term_is_rejected(self):
        for term in ('diagnosis', 'disorder', 'depression', 'disease', 'syndrome', 'prescribe', 'medication'):
            answers, result = self._run(lambda item, t=term: f'This could be a {t} sign.')
            self.assertEqual(result, answers, term)
            cache.clear()

    def test_banned_term_allowed_if_original_already_had_it(self):
        self.assertTrue(llm.is_safe_rewrite('Ask about your medication.', 'Do ask about your medication.'))
        self.assertFalse(llm.is_safe_rewrite('Ask about it.', 'Ask about your medication.'))

    def test_empty_or_non_string_or_runaway_rewrite_is_rejected(self):
        self.assertFalse(llm.is_safe_rewrite('Short.', ''))
        self.assertFalse(llm.is_safe_rewrite('Short.', '   '))
        self.assertFalse(llm.is_safe_rewrite('Short.', None))
        self.assertFalse(llm.is_safe_rewrite('Short.', ['x']))
        self.assertFalse(llm.is_safe_rewrite('Short.', 'word ' * 500))

    def test_fixed_fields_are_preserved_exactly(self):
        answers, result = self._run(lambda item: 'Gently: ' + item['text'])
        for before, after in zip(answers, result):
            for field in ('id', 'question', 'indicator', 'severity', 'source',
                          'simulated_note', 'disclaimer', 'escalation'):
                self.assertEqual(before[field], after[field], f"{before['id']}.{field}")
        critical = by_id(result)['why_hrv']
        self.assertEqual(critical['escalation'], companion.FLIGHT_SURGEON_LINE)
        self.assertEqual(critical['disclaimer'], companion.DISCLAIMER)
        self.assertEqual(critical['simulated_note'], companion.SIMULATED_NOTE)
        self.assertTrue(critical['ai_rephrased'])

    def test_model_never_sees_fixed_fields(self):
        opener = fake_urlopen()
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), mock.patch('core.llm.urllib.request.urlopen', opener):
            llm.rephrase_with_llm(sample_answers())
        sent = opener.requests[0][0].data.decode('utf-8')
        for secret in (companion.DISCLAIMER, companion.FLIGHT_SURGEON_LINE, companion.SIMULATED_NOTE,
                       companion.DEFAULT_SOURCE):
            self.assertNotIn(secret, sent)

    def test_data_source_answer_is_never_rephrased_or_sent(self):
        opener = fake_urlopen()
        answers = sample_answers()
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), mock.patch('core.llm.urllib.request.urlopen', opener):
            result = llm.rephrase_with_llm(answers)
        ids_sent = [i['id'] for i in _request_texts(opener.requests[0][0])]
        self.assertNotIn('data_source', ids_sent)
        self.assertEqual(by_id(result)['data_source'], by_id(answers)['data_source'])

    def test_model_cannot_rephrase_ids_it_was_not_given(self):
        raw = json.dumps({'answers': [{'id': 'data_source', 'text': 'Everything is official NASA data.'}]})
        answers = sample_answers()
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), mock.patch('core.llm.urllib.request.urlopen', fake_urlopen(raw=raw)):
            self.assertEqual(llm.rephrase_with_llm(answers), answers)


# --- cache -------------------------------------------------------------------

class CacheTests(LLMTestCase):
    def test_second_call_uses_cache(self):
        opener = fake_urlopen()
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), mock.patch('core.llm.urllib.request.urlopen', opener):
            first = llm.rephrase_with_llm(sample_answers())
            second = llm.rephrase_with_llm(sample_answers())
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(first, second)

    def test_different_answers_are_not_served_from_cache(self):
        opener = fake_urlopen()
        other = build_companion_answers(make_evals(), {'score': 100, 'label': 'Green'}, {'manual': 1}, 0)
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), mock.patch('core.llm.urllib.request.urlopen', opener):
            llm.rephrase_with_llm(sample_answers())
            llm.rephrase_with_llm(other)
        self.assertEqual(len(opener.requests), 2)

    def test_failure_is_cached_briefly_so_dashboard_is_not_slowed(self):
        opener = mock.Mock(side_effect=socket.timeout('timed out'))
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), mock.patch('core.llm.urllib.request.urlopen', opener):
            llm.rephrase_with_llm(sample_answers())
            llm.rephrase_with_llm(sample_answers())
        self.assertEqual(opener.call_count, 1)

    def test_cached_result_does_not_leak_between_callers(self):
        opener = fake_urlopen()
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), mock.patch('core.llm.urllib.request.urlopen', opener):
            first = llm.rephrase_with_llm(sample_answers())
            first[0]['selected'] = True          # the dashboard does this to its list
            second = llm.rephrase_with_llm(sample_answers())
        self.assertNotIn('selected', second[0])


# --- the hook in the views and the template ----------------------------------

class HookTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='ai', email='ai@example.com', password='pw-12345-ok')
        cls.astronaut = AstronautProfile.objects.create(
            name='AI Tester', email='ai@example.com', rank='Cmdr.', habitat='HAB-01')

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client.force_login(self.user)

    def test_disabled_flag_never_calls_the_llm(self):
        with override_settings(COMPANION_LLM_ENABLED=False), \
                mock.patch('core.views.rephrase_with_llm') as rephrase:
            response = self.client.get(reverse('core:companion_answers'))
        self.assertEqual(response.status_code, 200)
        rephrase.assert_not_called()

    def test_tests_never_enable_it_from_the_environment(self):
        from django.conf import settings
        self.assertFalse(settings.COMPANION_LLM_ENABLED)

    def test_enabled_flag_calls_the_llm_and_uses_its_answers(self):
        def fake(answers):
            return [{**a, 'ai_rephrased': True, 'ai_provider': 'Groq'} for a in answers]

        with override_settings(COMPANION_LLM_ENABLED=True), \
                mock.patch('core.views.rephrase_with_llm', side_effect=fake) as rephrase:
            response = self.client.get(reverse('core:companion_answers'))
        rephrase.assert_called_once()
        answers = response.json()['answers']
        self.assertTrue(all(a['ai_rephrased'] for a in answers))

    def test_dashboard_shows_label_only_when_rephrased(self):
        with override_settings(COMPANION_LLM_ENABLED=False):
            plain = self.client.get(reverse('core:dashboard')).content.decode()
        self.assertNotIn('Rephrased by AI', plain)

        def fake(answers):
            return [{**a, 'ai_rephrased': True, 'ai_provider': 'Groq'} for a in answers]

        with override_settings(COMPANION_LLM_ENABLED=True), \
                mock.patch('core.views.rephrase_with_llm', side_effect=fake):
            page = self.client.get(reverse('core:dashboard')).content.decode()
        self.assertIn('Rephrased by AI (Groq)', page)
        self.assertIn(companion.DISCLAIMER, page)

    def test_enabled_but_provider_down_still_serves_layer_one(self):
        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', side_effect=socket.timeout('timed out')):
            response = self.client.get(reverse('core:companion_answers'))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(any(a.get('ai_rephrased') for a in response.json()['answers']))