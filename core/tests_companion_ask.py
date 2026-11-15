"""
Tests for "Ask Pulse": a typed question -> one of the companion's computed answers.

No network: the model is mocked or urllib.request.urlopen is replaced by a fake in
every test, and the LLM environment variables are set explicitly so a real .env
can't leak in. The AI never writes an answer; these tests check it can only choose
from the fixed list of intents.
"""

import json
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core import companion, intent, llm
from core.constants import METRICS
from core.services import evaluate_astronaut_metrics as real_evaluate
from core.models import AstronautProfile
from core.tests_companion import make_evals
from core.tests_llm import FAKE_GROQ_KEY, FAKE_GEMINI_KEY, FakeResponse, env, wrap

ASK_URL = '/companion/ask/'


def model_reply(intent_value):
    return json.dumps({'intent': intent_value})


def user_payload(request):
    """The JSON object the app sent as the user message, for either provider's request shape."""
    body = json.loads(request.data.decode('utf-8'))
    if 'messages' in body:
        return json.loads(body['messages'][-1]['content'])
    return json.loads(body['contents'][0]['parts'][0]['text'])


def smart_urlopen(intent_value='none', fail_urls=()):
    """A urlopen fake that answers classification requests and rephrase requests.

    .payloads records what was sent, so a test can see exactly what reached the model.
    """
    payloads = []

    def _open(request, timeout=None):
        if any(part in request.full_url for part in fail_urls):
            raise OSError('provider down')
        payload = user_payload(request)
        payloads.append(payload)
        if 'question' in payload:
            content = model_reply(intent_value)
        else:
            content = json.dumps({'answers': [
                {'id': a['id'], 'text': 'Gently: ' + a['text']} for a in payload['answers']]})
        return FakeResponse(json.dumps(wrap(request.full_url, content)).encode('utf-8'))

    _open.payloads = payloads
    return _open


# --- keyword classifier -------------------------------------------------------

class KeywordTests(SimpleTestCase):
    CASES = {
        'overall': ["How am I doing?", "What's my overall status?", "what is my health score",
                    "Give me a summary", "am I ok?"],
        'what_to_do': ["What should I do?", "any advice for me", "What are my next steps?",
                       "what to do now"],
        'data_source': ["Where does this data come from?", "what is the data source",
                        "is this real data or simulated?", "where does my sleep data come from",
                        "was this uploaded from a CSV", "tell me about my data"],
        'sleep': ["Am I sleeping enough?", "how is my sleep", "I feel tired", "did I rest well",
                  "any insomnia?", "how long did I sleep"],
        'mood': ["How is my mood?", "how am I feeling today", "am I stressed", "stress levels?"],
        'exercise': ["Did I exercise enough?", "how is my workout", "what about training",
                     "resistance sessions this week"],
        'resting_hr': ["How is my resting heart rate?", "what's my pulse", "heart rate ok?",
                       "my resting HR", "bpm?"],
        'hrv': ["How is my HRV?", "heart rate variability please"],
        'body_temp': ["How is my body temperature?", "do I have a fever", "temp check"],
        'cabin_co2': ["How is the cabin CO2?", "carbon dioxide level", "air quality in here",
                      "cabin air"],
    }

    def test_every_intent_has_keyword_routes(self):
        for expected, phrases in self.CASES.items():
            for phrase in phrases:
                with self.subTest(expected=expected, phrase=phrase):
                    self.assertEqual(intent.classify_by_keywords(phrase), expected)

    def test_case_insensitive(self):
        self.assertEqual(intent.classify_by_keywords("AM I SLEEPING ENOUGH?"), 'sleep')
        self.assertEqual(intent.classify_by_keywords("hOw Is My hRv"), 'hrv')

    def test_hrv_is_not_mistaken_for_resting_hr(self):
        self.assertEqual(intent.classify_by_keywords("heart rate variability"), 'hrv')

    def test_indicator_beats_general_words(self):
        # "status" and "what should I do" are general; the named indicator wins.
        self.assertEqual(intent.classify_by_keywords("what is the status of my sleep"), 'sleep')
        self.assertEqual(intent.classify_by_keywords("what should I do about my mood"), 'mood')

    def test_first_mentioned_indicator_wins(self):
        self.assertEqual(intent.classify_by_keywords("mood and sleep"), 'mood')
        self.assertEqual(intent.classify_by_keywords("sleep and mood"), 'sleep')

    def test_strong_data_source_phrase_beats_indicator(self):
        self.assertEqual(intent.classify_by_keywords("where does my heart rate come from"), 'data_source')

    def test_radiation_words_are_recognised_but_not_an_intent(self):
        for phrase in ("What is the radiation level?", "any solar flare today", "space weather?",
                       "DONKI alerts", "is the sun dangerous"):
            with self.subTest(phrase=phrase):
                self.assertEqual(intent.classify_by_keywords(phrase), 'radiation')
        self.assertNotIn('radiation', intent.INTENT_IDS)

    def test_no_match(self):
        for phrase in ("write me a poem", "what is the capital of France", "hello", "tell me a joke"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(intent.classify_by_keywords(phrase))

    def test_no_substring_false_positives(self):
        # "air" inside "fairly" and "rest" inside "interesting" are not the topics.
        self.assertIsNone(intent.classify_by_keywords("that is fairly interesting"))

    def test_every_keyword_result_is_an_allowed_intent(self):
        for phrases in self.CASES.values():
            for phrase in phrases:
                self.assertIn(intent.classify_by_keywords(phrase), intent.INTENT_IDS)

    def test_supported_questions_cover_every_intent(self):
        self.assertEqual(len(intent.SUPPORTED_QUESTIONS), 3 + len(intent.INDICATOR_LABELS))
        self.assertEqual(set(intent.INTENT_IDS),
                         {'overall', 'what_to_do', 'data_source', *intent.INDICATOR_LABELS})


class CleanQuestionTests(SimpleTestCase):
    def test_strips_text(self):
        self.assertEqual(intent.clean_question("  how am I?  "), ("how am I?", ''))

    def test_empty_and_blank_and_wrong_type(self):
        for raw in ('', '   \n\t', None, 5, ['sleep']):
            with self.subTest(raw=raw):
                self.assertEqual(intent.clean_question(raw), ('', intent.EMPTY_MESSAGE))

    def test_length_limit(self):
        self.assertEqual(intent.clean_question('a' * 300)[1], '')
        self.assertEqual(intent.clean_question('a' * 301), ('', intent.TOO_LONG_MESSAGE))
        self.assertEqual(intent.clean_question(' ' * 50 + 'a' * 300 + ' ' * 50)[1], '')   # measured after strip


# --- nominal indicator answer ----------------------------------------------------

class IndicatorAnswerTests(SimpleTestCase):
    def test_non_nominal_is_the_why_answer(self):
        evals = make_evals(sleep='caution')
        answer = companion.indicator_answer('sleep', evals['sleep'], '')
        self.assertEqual(answer['id'], 'why_sleep')

    def test_nominal_has_its_own_id_rule_sentence_and_standard_fields(self):
        evals = make_evals()
        answer = companion.indicator_answer('exercise', evals['exercise'], companion.SIMULATED_NOTE)
        self.assertEqual(answer['id'], 'status_exercise')
        self.assertEqual(answer['severity'], 'nominal')
        self.assertIn(evals['exercise']['reason'], answer['answer'])
        self.assertEqual(answer['source'], companion.RULE_SOURCES['exercise'])
        self.assertEqual(answer['simulated_note'], companion.SIMULATED_NOTE)
        self.assertEqual(answer['disclaimer'], companion.DISCLAIMER)
        self.assertEqual(answer['escalation'], '')

    def test_nominal_mood_keeps_the_mood_note(self):
        answer = companion.indicator_answer('mood', make_evals()['mood'], '')
        self.assertIn(companion.MOOD_NOTE, answer['answer'])


# --- LLM classifier (core/llm.py) --------------------------------------------------

class LLMClassifierTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def classify(self, question, opener, **keys):
        with env(**keys), mock.patch('core.llm.urllib.request.urlopen', opener):
            return llm.classify_intent_with_llm(question, intent.INTENT_IDS, intent.INTENT_DESCRIPTIONS)

    def test_success_returns_allowed_id_and_provider(self):
        opener = smart_urlopen('sleep')
        result = self.classify("Any reason to be concerned about how I slept?", opener, GROQ_API_KEY=FAKE_GROQ_KEY)
        self.assertEqual(result, ('sleep', 'Groq'))

    def test_request_contains_only_the_question_and_the_allowed_list(self):
        opener = smart_urlopen('overall')
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), mock.patch('core.llm.urllib.request.urlopen', opener):
            llm.classify_intent_with_llm("is something wrong", intent.INTENT_IDS, intent.INTENT_DESCRIPTIONS)
        self.assertEqual(len(opener.payloads), 1)
        payload = opener.payloads[0]
        self.assertEqual(set(payload), {'question', 'allowed'})
        self.assertEqual(payload['question'], "is something wrong")
        self.assertEqual(list(payload['allowed']), list(intent.INTENT_IDS))

    def test_system_prompt_restricts_to_the_list_and_does_not_ask_for_answers(self):
        self.assertIn('allowed', llm.INTENT_SYSTEM_PROMPT)
        self.assertIn('none', llm.INTENT_SYSTEM_PROMPT)
        self.assertIn('Do not answer the question', llm.INTENT_SYSTEM_PROMPT)

    def test_groq_request_uses_the_intent_prompt_and_stays_in_budget(self):
        seen = []

        def opener(request, timeout=None):
            seen.append((request, timeout))
            return FakeResponse(json.dumps(wrap(request.full_url, model_reply('mood'))).encode('utf-8'))

        self.classify("how do I feel", opener, GROQ_API_KEY=FAKE_GROQ_KEY)
        request, timeout = seen[0]
        body = json.loads(request.data)
        self.assertEqual(body['messages'][0]['content'], llm.INTENT_SYSTEM_PROMPT)
        self.assertLessEqual(timeout, 4.0)
        self.assertNotIn(FAKE_GROQ_KEY, request.full_url)

    def test_none_means_none(self):
        self.assertIsNone(self.classify("write me a poem", smart_urlopen('none'), GROQ_API_KEY=FAKE_GROQ_KEY))

    def test_id_outside_the_allowed_list_is_rejected(self):
        for bad in ('radiation', 'delete_everything', 'why_sleep', 'SLEEP', '', 'sleep; DROP TABLE'):
            cache.clear()
            with self.subTest(bad=bad):
                self.assertIsNone(self.classify("anything", smart_urlopen(bad), GROQ_API_KEY=FAKE_GROQ_KEY))

    def test_non_string_or_malformed_model_output_is_rejected(self):
        for raw in ('{"intent": ["sleep"]}', '{"intent": 5}', '{"answer": "sleep"}', 'sleep', '', '[]'):
            cache.clear()
            opener = mock.Mock(side_effect=lambda request, timeout=None, raw=raw: FakeResponse(
                json.dumps(wrap(request.full_url, raw)).encode('utf-8')))
            with self.subTest(raw=raw):
                self.assertIsNone(self.classify("anything", opener, GROQ_API_KEY=FAKE_GROQ_KEY))

    def test_fenced_json_is_accepted(self):
        opener = mock.Mock(side_effect=lambda request, timeout=None: FakeResponse(json.dumps(
            wrap(request.full_url, '```json\n{"intent": "hrv"}\n```')).encode('utf-8')))
        self.assertEqual(self.classify("anything", opener, GROQ_API_KEY=FAKE_GROQ_KEY), ('hrv', 'Groq'))

    def test_provider_failure_returns_none(self):
        opener = smart_urlopen('sleep', fail_urls=('groq.com',))
        self.assertIsNone(self.classify("anything", opener, GROQ_API_KEY=FAKE_GROQ_KEY))

    def test_falls_back_to_gemini_when_groq_fails(self):
        opener = smart_urlopen('mood', fail_urls=('groq.com',))
        result = self.classify("anything", opener, GROQ_API_KEY=FAKE_GROQ_KEY, GEMINI_API_KEY=FAKE_GEMINI_KEY)
        self.assertEqual(result, ('mood', 'Gemini'))

    def test_no_key_means_no_call(self):
        opener = mock.Mock()
        self.assertIsNone(self.classify("anything", opener))
        opener.assert_not_called()

    def test_timeout_returns_none(self):
        opener = mock.Mock(side_effect=TimeoutError('timed out'))
        self.assertIsNone(self.classify("anything", opener, GROQ_API_KEY=FAKE_GROQ_KEY))

    def test_empty_question_makes_no_call(self):
        opener = mock.Mock()
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), mock.patch('core.llm.urllib.request.urlopen', opener):
            self.assertIsNone(llm.classify_intent_with_llm('   ', intent.INTENT_IDS))
        opener.assert_not_called()

    def test_result_is_cached_for_the_same_question(self):
        opener = smart_urlopen('sleep')
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), mock.patch('core.llm.urllib.request.urlopen', opener):
            first = llm.classify_intent_with_llm("How did I slept", intent.INTENT_IDS)
            second = llm.classify_intent_with_llm("how did i slept", intent.INTENT_IDS)
        self.assertEqual(first, second)
        self.assertEqual(len(opener.payloads), 1)

    def test_over_long_question_is_cut_before_sending(self):
        opener = smart_urlopen('none')
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), mock.patch('core.llm.urllib.request.urlopen', opener):
            llm.classify_intent_with_llm('x' * 5000, intent.INTENT_IDS)
        self.assertEqual(len(opener.payloads[0]['question']), llm.INTENT_MAX_QUESTION_CHARS)

    def test_rephrase_still_works_after_the_refactor(self):
        answers = companion.build_companion_answers(
            make_evals(sleep='caution'), {'score': 60, 'label': 'Amber'}, {'manual': 1}, 0)
        with env(GROQ_API_KEY=FAKE_GROQ_KEY), mock.patch('core.llm.urllib.request.urlopen', smart_urlopen()):
            result = llm.rephrase_with_llm(answers)
        self.assertTrue(result[0]['answer'].startswith('Gently: '))
        self.assertTrue(result[0]['ai_rephrased'])


# --- the endpoint ------------------------------------------------------------------

class AskViewTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='ask', email='ask@example.com', password='pw-12345-ok')
        cls.astronaut = AstronautProfile.objects.create(
            name='Ask Tester', email='ask@example.com', rank='Cmdr.', habitat='HAB-01')

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client.force_login(self.user)
        # sleep caution, HRV critical, the rest nominal. The real evaluation is kept underneath
        # so the dashboard page (which needs units, icons, ...) still renders.
        def fake_evaluate(*args, **kwargs):
            real = real_evaluate(*args, **kwargs)
            forced = make_evals(sleep='caution', hrv='critical')
            return {**real, **{key: {**real[key], **forced[key]} for key in METRICS}}

        patcher = mock.patch('core.views.evaluate_astronaut_metrics', side_effect=fake_evaluate)
        self.evaluate = patcher.start()
        self.addCleanup(patcher.stop)

    def ask(self, question, client=None, **extra):
        return (client or self.client).post(
            ASK_URL, data=json.dumps({'question': question}), content_type='application/json', **extra)


class KeywordRoutingTests(AskViewTestCase):
    def test_each_intent_routes_to_its_answer(self):
        expected = {
            "How am I doing?": 'overall',
            "What should I do?": 'what_to_do',
            "Where does this data come from?": 'data_source',
            "am I sleeping enough?": 'why_sleep',           # caution -> the chip answer
            "how is my HRV": 'why_hrv',                      # critical -> the chip answer
            "How is my mood?": 'status_mood',                # nominal -> built on demand
            "did I exercise enough": 'status_exercise',
            "resting heart rate?": 'status_resting_hr',
            "body temperature": 'status_body_temp',
            "cabin CO2": 'status_cabin_co2',
        }
        for question, matched in expected.items():
            with self.subTest(question=question):
                response = self.ask(question)
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual(data['matched_id'], matched)
                self.assertEqual(data['method'], 'keyword')
                self.assertFalse(data['out_of_scope'])
                self.assertEqual(data['answer']['id'], matched)

    def test_answer_has_the_same_shape_as_the_companion_answers(self):
        data = self.ask("am I sleeping enough?").json()
        chip = {a['id']: a for a in self.client.get(reverse('core:companion_answers')).json()['answers']}
        self.assertEqual(data['answer']['answer'], chip['why_sleep']['answer'])
        for key in ('id', 'question', 'answer', 'indicator', 'severity', 'source',
                    'simulated_note', 'disclaimer', 'escalation'):
            self.assertEqual(data['answer'][key], chip['why_sleep'][key])
        self.assertFalse(data['answer']['ai_rephrased'])
        self.assertEqual(data['answer']['ai_provider'], '')
        self.assertEqual(data['answer']['ai_label'], '')

    def test_source_and_disclaimer_are_kept(self):
        for question in ("how is my exercise", "how is my sleep", "how am I doing", "where does the data come from"):
            with self.subTest(question=question):
                answer = self.ask(question).json()['answer']
                self.assertEqual(answer['disclaimer'], companion.DISCLAIMER)
                self.assertTrue(answer['source'])
        exercise = self.ask("how is my exercise").json()['answer']
        self.assertEqual(exercise['source'], companion.RULE_SOURCES['exercise'])

    def test_critical_answer_has_the_flight_surgeon_line_and_others_do_not(self):
        critical = self.ask("how is my HRV").json()['answer']
        self.assertEqual(critical['severity'], 'critical')
        self.assertEqual(critical['escalation'], companion.FLIGHT_SURGEON_LINE)
        overall = self.ask("how am I doing").json()['answer']        # worst is critical
        self.assertEqual(overall['escalation'], companion.FLIGHT_SURGEON_LINE)
        caution = self.ask("how is my sleep").json()['answer']
        self.assertEqual(caution['escalation'], '')
        nominal = self.ask("how is my mood").json()['answer']
        self.assertEqual(nominal['escalation'], '')

    def test_form_post_is_accepted_too(self):
        response = self.client.post(ASK_URL, {'question': 'am I sleeping enough?'})
        self.assertEqual(response.json()['matched_id'], 'why_sleep')

    def test_uses_the_logged_in_users_astronaut(self):
        other_user = User.objects.create_user(username='other', email='other@example.com', password='pw-12345-ok')
        AstronautProfile.objects.create(name='Other', email='other@example.com', rank='Dr.', habitat='HAB-02')
        self.ask("how am I doing")
        self.assertTrue(self.evaluate.called)
        self.assertEqual(self.evaluate.call_args.args[0], self.astronaut)
        self.assertNotEqual(self.evaluate.call_args.args[0].email, other_user.email)

    def test_no_astronaut_for_login_is_403(self):
        stranger = User.objects.create_user(username='nobody', email='nobody@example.com', password='pw-12345-ok')
        self.client.force_login(stranger)
        self.assertEqual(self.ask("how am I doing").status_code, 403)

    def test_does_not_log_the_question_at_info(self):
        with self.assertNoLogs('core.views', level='INFO'):
            self.ask("am I sleeping enough? my secret is purple-elephant")


class OutOfScopeTests(AskViewTestCase):
    def assert_out_of_scope(self, response, method='none'):
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['out_of_scope'])
        self.assertIsNone(data['matched_id'])
        self.assertIsNone(data['answer'])
        self.assertEqual(data['method'], method)
        self.assertIn(intent.OUT_OF_SCOPE_MESSAGE, data['message'])
        self.assertEqual(data['supported_questions'], list(intent.SUPPORTED_QUESTIONS))
        return data

    def test_unknown_question_with_ai_off_gets_the_fixed_reply(self):
        with override_settings(COMPANION_LLM_ENABLED=False), \
                mock.patch('core.views.classify_intent_with_llm') as classify:
            data = self.assert_out_of_scope(self.ask("write me a poem"))
        classify.assert_not_called()
        self.assertEqual(data['message'], intent.OUT_OF_SCOPE_MESSAGE)
        self.assertIn("How am I doing?", data['supported_questions'])

    def test_radiation_gets_an_honest_no_data_reply_without_calling_the_ai(self):
        with override_settings(COMPANION_LLM_ENABLED=True), \
                mock.patch('core.views.classify_intent_with_llm') as classify:
            data = self.assert_out_of_scope(self.ask("is the sun dangerous today?"), method='keyword')
        classify.assert_not_called()
        self.assertIn(intent.NO_RADIATION_DATA_NOTE, data['message'])

    def test_ai_none_is_out_of_scope(self):
        with override_settings(COMPANION_LLM_ENABLED=True), \
                mock.patch('core.views.classify_intent_with_llm', return_value=None) as classify:
            self.assert_out_of_scope(self.ask("tell me a joke"))
        classify.assert_called_once()

    def test_ai_failure_is_out_of_scope(self):
        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', side_effect=TimeoutError('timed out')):
            self.assert_out_of_scope(self.ask("tell me a joke"))

    def test_ai_returning_an_id_outside_the_list_is_out_of_scope(self):
        for bad in ('radiation', 'delete_everything', 'why_sleep', 'status_mood'):
            cache.clear()
            with self.subTest(bad=bad), override_settings(COMPANION_LLM_ENABLED=True), \
                    env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                    mock.patch('core.llm.urllib.request.urlopen', smart_urlopen(bad)):
                self.assert_out_of_scope(self.ask("tell me a joke"))

    def test_ai_none_answer_through_the_real_classifier_is_out_of_scope(self):
        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', smart_urlopen('none')):
            self.assert_out_of_scope(self.ask("write me a poem"))


class AIClassificationTests(AskViewTestCase):
    def test_ai_classification_success_returns_the_computed_answer(self):
        opener = smart_urlopen('sleep')
        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', opener):
            data = self.ask("Any reason to be concerned about my body today?").json()
        self.assertEqual(data['method'], 'ai')
        self.assertEqual(data['matched_id'], 'why_sleep')
        self.assertFalse(data['out_of_scope'])
        # The model only chose the intent; the text is the computed answer (here also reworded).
        questions = [p for p in opener.payloads if 'question' in p]
        self.assertEqual(len(questions), 1)

    def test_the_model_never_sees_readings_or_other_answers_in_the_classification(self):
        opener = smart_urlopen('overall')
        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', opener):
            self.ask("should I worry about anything?")
        classification = [p for p in opener.payloads if 'question' in p][0]
        self.assertEqual(set(classification), {'question', 'allowed'})
        self.assertNotIn('reason text', json.dumps(classification))

    def test_ai_nominal_indicator_is_built_and_reworded(self):
        opener = smart_urlopen('mood')
        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', opener):
            data = self.ask("how is my head space").json()
        self.assertEqual(data['method'], 'ai')
        self.assertEqual(data['matched_id'], 'status_mood')
        self.assertTrue(data['answer']['ai_rephrased'])
        self.assertTrue(data['answer']['answer'].startswith('Gently: '))
        self.assertEqual(data['answer']['disclaimer'], companion.DISCLAIMER)

    def test_keyword_match_does_not_call_the_classifier(self):
        with override_settings(COMPANION_LLM_ENABLED=True), \
                mock.patch('core.views.classify_intent_with_llm') as classify, \
                mock.patch('core.views.rephrase_with_llm', side_effect=lambda answers, user=None: answers):
            data = self.ask("am I sleeping enough?").json()
        classify.assert_not_called()
        self.assertEqual(data['method'], 'keyword')

    def test_keyword_answer_is_rephrased_with_all_other_fields_untouched(self):
        plain = self.ask("how is my HRV").json()['answer']
        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', smart_urlopen()):
            answer = self.ask("how is my HRV").json()['answer']
        self.assertTrue(answer['ai_rephrased'])
        self.assertEqual(answer['ai_provider'], 'Groq')
        self.assertEqual(answer['ai_label'], 'Rephrased by AI (Groq)')
        self.assertEqual(answer['answer'], 'Gently: ' + plain['answer'])
        for key in ('source', 'simulated_note', 'disclaimer', 'escalation', 'severity', 'id'):
            self.assertEqual(answer[key], plain[key])
        self.assertEqual(answer['escalation'], companion.FLIGHT_SURGEON_LINE)

    def test_data_source_answer_is_never_reworded(self):
        opener = smart_urlopen()
        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', opener):
            answer = self.ask("where does this data come from").json()['answer']
        self.assertFalse(answer['ai_rephrased'])
        self.assertNotIn('Gently', answer['answer'])

    def test_provider_down_falls_back_to_the_rule_based_answer(self):
        plain = self.ask("how is my sleep").json()['answer']
        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', side_effect=TimeoutError('timed out')):
            answer = self.ask("how is my sleep").json()['answer']
        self.assertEqual(answer, plain)

    def test_unsafe_rewrite_is_dropped(self):
        def invent(request, timeout=None):
            payload = user_payload(request)
            content = json.dumps({'answers': [
                {'id': a['id'], 'text': a['text'] + ' This could be depression, about 99 percent.'}
                for a in payload['answers']]})
            return FakeResponse(json.dumps(wrap(request.full_url, content)).encode('utf-8'))

        plain = self.ask("how is my sleep").json()['answer']
        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', invent):
            answer = self.ask("how is my sleep").json()['answer']
        self.assertEqual(answer['answer'], plain['answer'])
        self.assertFalse(answer['ai_rephrased'])


class InputValidationTests(AskViewTestCase):
    def test_empty_and_blank_are_rejected(self):
        for question in ('', '   ', '\n'):
            with self.subTest(question=question):
                response = self.ask(question)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['error'], intent.EMPTY_MESSAGE)

    def test_missing_field_wrong_type_and_bad_json_are_rejected(self):
        self.assertEqual(self.client.post(ASK_URL, {}).status_code, 400)
        self.assertEqual(self.ask(None).status_code, 400)
        self.assertEqual(self.ask(['sleep']).status_code, 400)
        bad = self.client.post(ASK_URL, data='{not json', content_type='application/json')
        self.assertEqual(bad.status_code, 400)
        not_object = self.client.post(ASK_URL, data='["sleep"]', content_type='application/json')
        self.assertEqual(not_object.status_code, 400)

    def test_over_300_characters_is_rejected_with_a_friendly_message(self):
        response = self.ask('sleep ' + 'x' * 295)       # 301 characters
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], intent.TOO_LONG_MESSAGE)

    def test_exactly_300_characters_is_accepted(self):
        response = self.ask('sleep ' + 'x' * 294)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['matched_id'], 'why_sleep')

    def test_too_long_question_never_reaches_the_ai(self):
        with override_settings(COMPANION_LLM_ENABLED=True), \
                mock.patch('core.views.classify_intent_with_llm') as classify:
            self.assertEqual(self.ask('x' * 400).status_code, 400)
        classify.assert_not_called()


class SecurityTests(AskViewTestCase):
    def test_login_is_required(self):
        anonymous = Client()
        response = anonymous.post(ASK_URL, data=json.dumps({'question': 'how am I'}), content_type='application/json')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(ASK_URL).status_code, 405)

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        body = json.dumps({'question': 'am I sleeping enough?'})

        rejected = client.post(ASK_URL, data=body, content_type='application/json')
        self.assertEqual(rejected.status_code, 403)

        page = client.get(reverse('core:dashboard'))                  # sets the csrftoken cookie
        token = page.cookies['csrftoken'].value
        accepted = client.post(ASK_URL, data=body, content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()['matched_id'], 'why_sleep')

    def test_csrf_is_enforced_for_form_posts_too(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(ASK_URL, {'question': 'sleep'}).status_code, 403)


class DashboardCardTests(AskViewTestCase):
    def test_card_has_the_ask_form_with_csrf_token_and_url(self):
        page = self.client.get(reverse('core:dashboard')).content.decode()
        self.assertIn('id="companion-ask-form"', page)
        self.assertIn(ASK_URL, page)
        self.assertIn('maxlength="300"', page)
        form = page[page.index('id="companion-ask-form"'):]
        self.assertIn('csrfmiddlewaretoken', form[:form.index('</form>')])

    def test_page_does_not_claim_ai_when_nothing_was_rephrased(self):
        page = self.client.get(reverse('core:dashboard')).content.decode()
        self.assertNotIn('Rephrased by AI', page)

    def test_existing_chips_and_answers_are_still_there(self):
        page = self.client.get(reverse('core:dashboard')).content.decode()
        self.assertIn('data-companion-chip="overall"', page)
        self.assertIn('data-companion-answer="why_sleep"', page)
        self.assertIn(companion.DISCLAIMER, page)
