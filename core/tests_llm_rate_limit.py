"""Tests for the per-user live-LLM rate limit (core/llm.py).

No network: urllib.request.urlopen is replaced by a fake, and LLM env vars
are set explicitly so a real .env can't leak in.
"""

import json
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from core import intent, llm
from core.tests_llm import (
    FAKE_GROQ_KEY,
    FakeResponse,
    env,
    fake_urlopen,
    sample_answers,
    wrap,
)


def make_user(pk):
    user = mock.Mock()
    user.pk = pk
    user.is_authenticated = True
    return user


def tagged_answers(tag):
    """A fresh answer list whose cache key differs from sample_answers()."""
    return [
        {**answer, 'answer': f"{answer['answer']} [{tag}]"} if answer.get('answer') else answer
        for answer in sample_answers()
    ]


def intent_urlopen(intent_id='sleep'):
    """A urlopen replacement that classifies every request as intent_id."""
    requests = []

    def _open(request, timeout=None):
        requests.append((request, timeout))
        content = json.dumps({'intent': intent_id})
        return FakeResponse(json.dumps(wrap(request.full_url, content)).encode('utf-8'))

    _open.requests = requests
    return _open


class RateLimitTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_under_limit_calls_go_through(self):
        answers = sample_answers()
        opener = fake_urlopen()
        with override_settings(COMPANION_LLM_RATE_LIMIT=20, COMPANION_LLM_RATE_WINDOW=3600), \
                env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', opener):
            result = llm.rephrase_with_llm(answers, user=make_user(1))
        self.assertEqual(len(opener.requests), 1)
        self.assertTrue(any(a.get('ai_rephrased') for a in result))

    def test_call_over_the_limit_skips_llm_and_returns_rule_based_text(self):
        opener = fake_urlopen()
        user = make_user(1)
        with override_settings(COMPANION_LLM_RATE_LIMIT=2, COMPANION_LLM_RATE_WINDOW=3600), \
                env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', opener):
            first = tagged_answers('one')
            second = tagged_answers('two')
            third = tagged_answers('three')
            out1 = llm.rephrase_with_llm(first, user=user)
            out2 = llm.rephrase_with_llm(second, user=user)
            out3 = llm.rephrase_with_llm(third, user=user)
        self.assertEqual(len(opener.requests), 2)
        self.assertTrue(any(a.get('ai_rephrased') for a in out1))
        self.assertTrue(any(a.get('ai_rephrased') for a in out2))
        self.assertEqual(out3, third)
        self.assertFalse(any(a.get('ai_rephrased') for a in out3))

    def test_separate_users_have_separate_counters(self):
        opener = fake_urlopen()
        with override_settings(COMPANION_LLM_RATE_LIMIT=1, COMPANION_LLM_RATE_WINDOW=3600), \
                env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', opener):
            first = tagged_answers('user-1')
            other = tagged_answers('user-2')
            blocked = tagged_answers('user-1-again')
            out1 = llm.rephrase_with_llm(first, user=make_user(1))
            out2 = llm.rephrase_with_llm(other, user=make_user(2))
            out3 = llm.rephrase_with_llm(blocked, user=make_user(1))
        self.assertEqual(len(opener.requests), 2)
        self.assertTrue(any(a.get('ai_rephrased') for a in out1))
        self.assertTrue(any(a.get('ai_rephrased') for a in out2))
        self.assertEqual(out3, blocked)

    def test_cache_hits_do_not_count_against_the_limit(self):
        answers = sample_answers()
        opener = fake_urlopen()
        user = make_user(1)
        with override_settings(COMPANION_LLM_RATE_LIMIT=1, COMPANION_LLM_RATE_WINDOW=3600), \
                env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', opener):
            first = llm.rephrase_with_llm(answers, user=user)
            cached = llm.rephrase_with_llm(answers, user=user)
            later = llm.rephrase_with_llm(tagged_answers('fresh'), user=user)
        self.assertEqual(len(opener.requests), 1)
        self.assertTrue(any(a.get('ai_rephrased') for a in first))
        self.assertEqual(first, cached)
        self.assertEqual(later, tagged_answers('fresh'))
        self.assertFalse(any(a.get('ai_rephrased') for a in later))

    def test_anonymous_never_calls_llm(self):
        answers = sample_answers()
        opener = fake_urlopen()
        intent_opener = intent_urlopen()
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', opener):
            result = llm.rephrase_with_llm(answers, user=AnonymousUser())
        self.assertEqual(opener.requests, [])
        self.assertEqual(result, answers)
        self.assertFalse(any(a.get('ai_rephrased') for a in result))

        with env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', intent_opener):
            picked = llm.classify_intent_with_llm(
                "how did I sleep", intent.INTENT_IDS, intent.INTENT_DESCRIPTIONS,
                user=AnonymousUser(),
            )
        self.assertIsNone(picked)
        self.assertEqual(intent_opener.requests, [])

    def test_rephrase_and_classify_share_one_counter(self):
        rephrase_opener = fake_urlopen()
        classify_opener = intent_urlopen()
        user = make_user(1)
        with override_settings(COMPANION_LLM_RATE_LIMIT=1, COMPANION_LLM_RATE_WINDOW=3600), \
                env(GROQ_API_KEY=FAKE_GROQ_KEY):
            with mock.patch('core.llm.urllib.request.urlopen', rephrase_opener):
                out = llm.rephrase_with_llm(sample_answers(), user=user)
            with mock.patch('core.llm.urllib.request.urlopen', classify_opener):
                picked = llm.classify_intent_with_llm(
                    "how did I sleep", intent.INTENT_IDS, intent.INTENT_DESCRIPTIONS,
                    user=user,
                )
        self.assertEqual(len(rephrase_opener.requests), 1)
        self.assertTrue(any(a.get('ai_rephrased') for a in out))
        self.assertIsNone(picked)
        self.assertEqual(classify_opener.requests, [])
