"""
Tests for Pulse chat (companion app): ownership, login, memory controls, validation,
escalation, small-talk fallback and safety checks, and "health facts come from
core/companion.py, never from the model".

No network: urllib.request.urlopen is replaced in every test (a loud failure by default),
the LLM environment variables are blank unless a test sets them, and
COMPANION_LLM_ENABLED is already forced off while running tests (settings.py).
"""

import json
import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from companion import chat
from companion.models import CompanionMemory, Conversation, Message
from core import companion as rules_companion
from core.constants import METRICS
from core.models import AstronautProfile
from core.services import evaluate_astronaut_metrics as real_evaluate
from core.tests_companion import make_evals
from core.tests_companion_ask import smart_urlopen
from core.tests_llm import FAKE_GROQ_KEY, FakeResponse, env, wrap
from telemetry.models import Reading

SEND = '/companion/chat/send/'
HISTORY = '/companion/chat/history/'
NEW = '/companion/chat/new/'
MEMORIES = '/companion/chat/memories/'
MEMORY_ADD = '/companion/chat/memories/add/'


def memory_delete_url(pk):
    return f'/companion/chat/memories/{pk}/delete/'


def forced_evals(**statuses):
    """Patch target for core.views.evaluate_astronaut_metrics: real evaluation with forced statuses."""
    def fake(*args, **kwargs):
        real = real_evaluate(*args, **kwargs)
        forced = make_evals(**statuses)
        return {**real, **{key: {**real[key], **forced[key]} for key in METRICS}}
    return fake


def no_network(request, timeout=None):
    raise AssertionError('Unexpected network call in a Pulse chat test')


def persona_urlopen(reply_text, record=None):
    """A urlopen fake that answers the small-talk persona request with {"reply": reply_text}."""
    def _open(request, timeout=None):
        body = json.loads(request.data.decode('utf-8'))
        if record is not None:
            record.append(body)
        content = json.dumps({'reply': reply_text})
        return FakeResponse(json.dumps(wrap(request.full_url, content)).encode('utf-8'))
    return _open


class ChatTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user_a = User.objects.create_user(username='a', email='a@example.com', password='pw-12345-ok')
        cls.user_b = User.objects.create_user(username='b', email='b@example.com', password='pw-12345-ok')
        cls.astro_a = AstronautProfile.objects.create(name='Alpha', email='a@example.com', rank='Cmdr.')
        cls.astro_b = AstronautProfile.objects.create(name='Bravo', email='b@example.com', rank='Dr.')

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        for patcher in (env(), mock.patch('core.llm.urllib.request.urlopen', no_network)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client_a = Client()
        self.client_a.force_login(self.user_a)
        self.client_b = Client()
        self.client_b.force_login(self.user_b)

    def say(self, text, client=None, **extra):
        return (client or self.client_a).post(
            SEND, data=json.dumps({'message': text, **extra}), content_type='application/json')

    def patch_evals(self, **statuses):
        patcher = mock.patch('core.views.evaluate_astronaut_metrics', side_effect=forced_evals(**statuses))
        patcher.start()
        self.addCleanup(patcher.stop)

    def add_reading(self, metric='sleep', value=7.0, age=timedelta(0), source='manual'):
        return Reading.objects.create(
            astronaut=self.astro_a, metric=metric, value=value, mission_day=10,
            timestamp=timezone.now() - age, source=source)


# --- login, CSRF, ownership ---------------------------------------------------------------

class AccessTests(ChatTestCase):
    def test_every_endpoint_needs_login(self):
        anon = Client()
        calls = [
            anon.get(HISTORY), anon.post(SEND, '{}', content_type='application/json'), anon.post(NEW),
            anon.get(MEMORIES), anon.post(MEMORY_ADD, '{}', content_type='application/json'),
            anon.post(memory_delete_url(1)),
        ]
        for response in calls:
            self.assertEqual(response.status_code, 302)
            self.assertIn(reverse('core:login'), response['Location'])
        self.assertEqual(Message.objects.count(), 0)

    def test_user_without_astronaut_profile_gets_403(self):
        loner = User.objects.create_user(username='loner', email='loner@example.com', password='pw-12345-ok')
        client = Client()
        client.force_login(loner)
        self.assertEqual(client.get(HISTORY).status_code, 403)
        self.assertEqual(client.post(SEND, json.dumps({'message': 'hi'}), content_type='application/json').status_code, 403)
        self.assertEqual(client.get(MEMORIES).status_code, 403)

    def test_post_needs_csrf_token(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user_a)
        response = strict.post(SEND, json.dumps({'message': 'hi'}), content_type='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Message.objects.count(), 0)

    def test_wrong_http_method_is_405(self):
        self.assertEqual(self.client_a.get(SEND).status_code, 405)
        self.assertEqual(self.client_a.post(HISTORY).status_code, 405)


class OwnershipTests(ChatTestCase):
    def test_user_a_cannot_read_user_bs_conversation(self):
        self.say('hello there', client=self.client_b)
        conversation_b = Conversation.objects.get(user=self.user_b)
        response = self.client_a.get(HISTORY, {'conversation_id': conversation_b.pk})
        self.assertEqual(response.status_code, 404)

    def test_user_a_cannot_post_into_user_bs_conversation(self):
        self.say('hello there', client=self.client_b)
        conversation_b = Conversation.objects.get(user=self.user_b)
        before = Message.objects.filter(conversation=conversation_b).count()
        response = self.say('sneaky', conversation_id=conversation_b.pk)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Message.objects.filter(conversation=conversation_b).count(), before)

    def test_history_only_returns_own_messages(self):
        self.say('my secret plan', client=self.client_b)
        self.say('hello from a')
        data = self.client_a.get(HISTORY).json()
        texts = [m['text'] for m in data['messages']]
        self.assertIn('hello from a', texts)
        self.assertNotIn('my secret plan', texts)

    def test_bad_conversation_id_is_404(self):
        self.assertEqual(self.client_a.get(HISTORY, {'conversation_id': 'abc'}).status_code, 404)
        self.assertEqual(self.client_a.get(HISTORY, {'conversation_id': 99999}).status_code, 404)

    def test_conversation_is_scoped_to_the_active_astronaut(self):
        other = Conversation.objects.create(user=self.user_a, astronaut=self.astro_b)
        self.assertEqual(self.client_a.get(HISTORY, {'conversation_id': other.pk}).status_code, 404)

    def test_user_a_cannot_delete_user_bs_memory(self):
        memory = CompanionMemory.objects.create(user=self.user_b, text='B likes tea')
        response = self.client_a.post(memory_delete_url(memory.pk))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(CompanionMemory.objects.filter(pk=memory.pk).exists())

    def test_user_a_cannot_list_user_bs_memories(self):
        CompanionMemory.objects.create(user=self.user_b, text='B likes tea')
        self.assertEqual(self.client_a.get(MEMORIES).json()['memories'], [])

    def test_new_conversation_starts_empty_and_becomes_current(self):
        self.say('hello there')
        first = Conversation.objects.get(user=self.user_a)
        response = self.client_a.post(NEW)
        self.assertEqual(response.status_code, 201)
        new_id = response.json()['conversation_id']
        self.assertNotEqual(new_id, first.pk)
        data = self.client_a.get(HISTORY).json()
        self.assertEqual(data['conversation_id'], new_id)
        self.assertEqual(data['messages'], [])
        # the old conversation is still readable by its owner
        old = self.client_a.get(HISTORY, {'conversation_id': first.pk}).json()
        self.assertEqual(len(old['messages']), 2)

    def test_history_with_no_conversation_is_empty(self):
        data = self.client_a.get(HISTORY).json()
        self.assertIsNone(data['conversation_id'])
        self.assertEqual(data['messages'], [])
        self.assertEqual(len(data['suggested_prompts']), 4)


# --- validation ---------------------------------------------------------------------------------

class ValidationTests(ChatTestCase):
    def test_empty_message_is_400(self):
        for text in ('', '   ', None, 5):
            with self.subTest(text=text):
                response = self.client_a.post(SEND, json.dumps({'message': text}), content_type='application/json')
                self.assertEqual(response.status_code, 400)
        self.assertEqual(Message.objects.count(), 0)

    def test_too_long_message_is_400_and_300_is_fine(self):
        self.assertEqual(self.say('x' * 301).status_code, 400)
        self.assertEqual(self.say('x' * 300).status_code, 200)

    def test_non_json_body_is_400(self):
        response = self.client_a.post(SEND, 'message=hi', content_type='application/x-www-form-urlencoded')
        self.assertEqual(response.status_code, 400)
        response = self.client_a.post(SEND, '{not json', content_type='application/json')
        self.assertEqual(response.status_code, 400)
        response = self.client_a.post(SEND, '[1, 2]', content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Message.objects.count(), 0)

    def test_reply_is_saved_with_the_message(self):
        data = self.say('hello').json()
        self.assertEqual(data['user_message']['role'], 'user')
        self.assertEqual(data['reply']['role'], 'pulse')
        self.assertEqual(Message.objects.filter(conversation_id=data['conversation_id']).count(), 2)


# --- health answers come from core/companion.py ---------------------------------------------------

class HealthAnswerTests(ChatTestCase):
    def test_suggested_prompts_get_computed_answers(self):
        self.patch_evals(sleep='caution')
        expected = {
            "How am I doing?": 'overall',
            "Explain my sleep trend": 'why_sleep',
            "Review the cabin environment": 'status_cabin_co2',
        }
        for prompt, intent_id in expected.items():
            with self.subTest(prompt=prompt):
                reply = self.say(prompt).json()['reply']
                self.assertEqual(reply['kind'], 'health')
                self.assertEqual(reply['intent_id'], intent_id)
                self.assertEqual(reply['ai_label'], '')
                self.assertEqual(reply['disclaimer'], rules_companion.DISCLAIMER)

    def test_text_is_exactly_the_rule_based_answer(self):
        self.patch_evals(sleep='caution')
        reply = self.say('Explain my sleep trend').json()['reply']
        self.assertIn("Sleep reason text for caution.", reply['text'])
        self.assertEqual(reply['severity'], 'caution')
        self.assertEqual(reply['source'], rules_companion.source_for('sleep'))
        self.assertEqual([link['url'] for link in reply['links']], [reverse('alerts:list')])

    def test_model_is_never_used_for_health_answers_when_off(self):
        self.patch_evals(sleep='caution')
        with mock.patch('companion.chat._llm_smalltalk') as smalltalk:
            self.say('How is my sleep?')
        smalltalk.assert_not_called()

    def test_health_answer_is_not_replaced_by_model_text(self):
        # Even with the model on, a health question gets the rule-based answer. The model is
        # only used to rephrase (core/llm.py, validated) or to pick an intent, never to write facts.
        self.patch_evals(sleep='caution')
        opener = smart_urlopen('none')
        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', opener):
            reply = self.say('How is my sleep?').json()['reply']
        self.assertTrue(reply['text'].endswith("Sleep reason text for caution. That's enough for a caution, so it's worth keeping an eye on."))
        self.assertEqual(reply['ai_label'], 'Written by AI (Groq)')     # rephrase prefix only
        self.assertTrue(reply['text'].startswith('Gently: '))

    def test_unsafe_model_rephrase_is_dropped(self):
        self.patch_evals(sleep='caution')

        def bad(request, timeout=None):
            payload = json.loads(json.loads(request.data.decode())['messages'][-1]['content'])
            content = json.dumps({'answers': [{'id': a['id'], 'text': 'You have a 99 percent chance of illness'} for a in payload['answers']]})
            return FakeResponse(json.dumps(wrap(request.full_url, content)).encode())

        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', bad):
            reply = self.say('How is my sleep?').json()['reply']
        self.assertIn('Sleep reason text for caution.', reply['text'])
        self.assertEqual(reply['ai_label'], '')

    def test_no_data_is_labelled_not_normal(self):
        self.patch_evals(sleep='no_data')
        reply = self.say('How is my sleep?').json()['reply']
        self.assertEqual(reply['severity'], 'no_data')
        self.assertIn('No readings yet', reply['data_note'])
        self.assertNotIn('within your baseline', reply['text'])

    def test_stale_data_is_labelled(self):
        self.patch_evals()
        self.add_reading('sleep', age=timedelta(days=10))
        reply = self.say('How is my sleep?').json()['reply']
        self.assertIn('out of date', reply['data_note'])
        self.assertIn('not the same as normal', reply['data_note'])

    def test_recent_data_has_no_stale_note(self):
        self.patch_evals()
        self.add_reading('sleep', age=timedelta(hours=2))
        self.assertEqual(self.say('How is my sleep?').json()['reply']['data_note'], '')

    def test_simulated_label_is_kept(self):
        self.patch_evals(sleep='caution')
        self.add_reading('sleep', source='simulated')
        reply = self.say('How is my sleep?').json()['reply']
        self.assertEqual(reply['simulated_note'], rules_companion.SIMULATED_NOTE)

    def test_cabin_answer_adds_space_weather_as_context_only(self):
        self.patch_evals()
        view = {'state': 'live'}
        with mock.patch('companion.chat.get_space_weather', return_value=view), \
                mock.patch('companion.chat.donki.build_answer', return_value={'answer': 'Quiet Sun.'}):
            reply = self.say('Review the cabin environment').json()['reply']
        self.assertEqual(reply['context'], 'Context only: Quiet Sun.')
        self.assertNotIn('Quiet Sun', reply['text'])

    def test_cabin_answer_without_space_weather_has_no_context(self):
        self.patch_evals()
        reply = self.say('Review the cabin environment').json()['reply']
        self.assertEqual(reply['context'], '')

    def test_radiation_without_data_is_honest(self):
        self.patch_evals()
        reply = self.say('any solar flares lately?').json()['reply']
        self.assertEqual(reply['text'], "I don't have any radiation or space-weather data yet.")

    def test_checkin_prompt_links_to_the_existing_checkin_page(self):
        reply = self.say('Start a check-in').json()['reply']
        self.assertEqual(reply['kind'], 'checkin')
        self.assertEqual([link['url'] for link in reply['links']], [reverse('telemetry:check_in')])

    def test_checkin_rejects_non_requests(self):
        self.assertFalse(chat.CHECKIN_RX.search('How is my sleep?'))


# --- escalation --------------------------------------------------------------------------------------

class EscalationTests(ChatTestCase):
    URGENT = ["I have chest pain", "I can't breathe", "I cannot breathe properly", "this is an emergency",
              "I want to hurt myself", "there is smoke in the cabin", "having trouble breathing"]

    def test_urgent_phrases_get_the_fixed_pointer_and_nothing_else(self):
        for phrase in self.URGENT:
            with self.subTest(phrase=phrase), mock.patch('companion.chat._llm_smalltalk') as smalltalk:
                reply = self.say(phrase).json()['reply']
                smalltalk.assert_not_called()
                self.assertEqual(reply['kind'], 'escalation')
                self.assertEqual(reply['text'], chat.URGENT_TEXT)
                self.assertEqual(reply['escalation'], rules_companion.FLIGHT_SURGEON_LINE)
                self.assertEqual(reply['state'], 'emergency')
                urls = [link['url'] for link in reply['links']]
                self.assertEqual(urls, [reverse('core:ground_support'), reverse('alerts:list')])

    def test_urgent_phrase_never_reaches_the_model(self):
        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', no_network):
            reply = self.say("I can't breathe").json()['reply']
        self.assertEqual(reply['text'], chat.URGENT_TEXT)

    def test_urgent_phrase_beats_remember_and_is_not_stored(self):
        reply = self.say('remember that I have chest pain').json()['reply']
        self.assertEqual(reply['kind'], 'escalation')
        self.assertEqual(CompanionMemory.objects.count(), 0)

    def test_critical_answer_escalates(self):
        self.patch_evals(hrv='critical')
        data = self.say('How is my HRV?').json()
        reply = data['reply']
        self.assertEqual(reply['kind'], 'escalation')
        self.assertEqual(reply['text'], chat.CRITICAL_TEXT)
        self.assertEqual(reply['severity'], 'critical')
        self.assertEqual(reply['escalation'], rules_companion.FLIGHT_SURGEON_LINE)
        self.assertIn('HRV reason text for critical.', reply['details'])    # computed facts stay visible
        self.assertEqual([link['url'] for link in reply['links']],
                         [reverse('core:ground_support'), reverse('alerts:list')])

    def test_overall_with_a_critical_indicator_escalates(self):
        self.patch_evals(hrv='critical')
        self.assertEqual(self.say('How am I doing?').json()['reply']['kind'], 'escalation')

    def test_medical_advice_requests_point_to_ground_support(self):
        reply = self.say('What medication should I take for my headache?').json()['reply']
        self.assertEqual(reply['text'], chat.MEDICAL_TEXT)
        self.assertEqual([link['url'] for link in reply['links']], [reverse('core:ground_support')])


# --- small talk ---------------------------------------------------------------------------------------

class SmallTalkFallbackTests(ChatTestCase):
    def test_llm_off_uses_fixed_replies_without_network(self):
        cases = {
            'hello': chat.GREETING_TEXT,
            'Good morning': chat.GREETING_TEXT,
            'I feel lonely': chat.LONELY_TEXT,
            'thanks!': chat.THANKS_TEXT,
            'tell me something interesting': None,
            'blah blah space cheese': chat.GENERIC_TEXT,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                reply = self.say(text).json()['reply']
                self.assertEqual(reply['kind'], 'fixed')
                self.assertEqual(reply['ai_label'], '')
                if expected is None:
                    self.assertIn(reply['text'], chat.FUN_FACTS)
                else:
                    self.assertEqual(reply['text'], expected)

    def test_loneliness_suggests_people(self):
        self.assertIn('crewmate', chat.LONELY_TEXT)
        self.assertIn('Ground Support', chat.LONELY_TEXT)

    def test_i_feel_lonely_is_not_a_mood_indicator_answer(self):
        reply = self.say('I feel lonely').json()['reply']
        self.assertEqual(reply['intent_id'], '')
        self.assertNotEqual(reply['kind'], 'health')

    def test_how_am_i_feeling_is_still_a_health_question(self):
        self.patch_evals()
        self.assertEqual(self.say('how am I feeling today?').json()['reply']['intent_id'], 'status_mood')

    def test_generic_fallback_lists_what_pulse_can_do(self):
        self.assertIn('sleep trend', chat.GENERIC_TEXT)
        self.assertIn('cabin environment', chat.GENERIC_TEXT)
        self.assertIn('check-in', chat.GENERIC_TEXT)

    def test_out_of_scope_requests_get_a_polite_fixed_reply(self):
        for text in ('write me a poem about Mars', 'write python code for a sorting function'):
            with self.subTest(text=text), mock.patch('companion.chat._llm_smalltalk') as smalltalk:
                reply = self.say(text).json()['reply']
                smalltalk.assert_not_called()
                self.assertEqual(reply['text'], chat.OUT_OF_SCOPE_TEXT)

    def test_fixed_small_talk_texts_contain_no_digits(self):
        for text in (chat.GREETING_TEXT, chat.LONELY_TEXT, chat.THANKS_TEXT, chat.GENERIC_TEXT, *chat.FUN_FACTS):
            with self.subTest(text=text):
                self.assertFalse(re.search(r'\d', text))


class SmallTalkModelTests(ChatTestCase):
    def talk(self, text, reply_text, record=None, **extra):
        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', persona_urlopen(reply_text, record)):
            return self.say(text, **extra).json()['reply']

    def test_safe_model_reply_is_used_and_labelled(self):
        record = []
        reply = self.talk('I feel lonely', 'That sounds hard. Is there a crewmate you could sit with?', record)
        self.assertEqual(reply['kind'], 'smalltalk')
        self.assertEqual(reply['text'], 'That sounds hard. Is there a crewmate you could sit with?')
        self.assertEqual(reply['ai_label'], 'Written by AI (Groq)')
        self.assertEqual(reply['state'], 'responding')
        system = record[0]['messages'][0]['content']
        self.assertIn('You are an AI', system)
        self.assertIn('never give medical facts', system)

    def test_model_gets_only_the_message_notes_and_recent_chat(self):
        self.patch_evals(hrv='critical')
        self.add_reading('sleep', value=4.2)
        CompanionMemory.objects.create(user=self.user_a, text='I like chess')
        CompanionMemory.objects.create(user=self.user_b, text='B-only secret')
        record = []
        self.talk('hello', 'Hello there. How is your day going?', record)
        sent = json.loads(record[0]['messages'][-1]['content'])
        self.assertEqual(set(sent), {'message', 'notes', 'recent_chat'})
        self.assertEqual(sent['notes'], ['I like chess'])
        blob = json.dumps(record)
        for forbidden in ('4.2', 'B-only secret', 'critical', 'HRV', FAKE_GROQ_KEY.upper()):
            self.assertNotIn(forbidden, blob.replace(FAKE_GROQ_KEY, ''))

    def test_recent_chat_excludes_health_answers(self):
        self.patch_evals(sleep='caution')
        self.say('How is my sleep?')
        record = []
        self.talk('hello', 'Hello there.', record)
        sent = json.loads(record[0]['messages'][-1]['content'])
        texts = ' '.join(turn['text'] for turn in sent['recent_chat'])
        self.assertIn('How is my sleep?', texts)            # the user's own words
        self.assertNotIn('reason text', texts)              # never Pulse's health answer

    def test_reply_with_numbers_is_rejected_for_the_fixed_one(self):
        reply = self.talk('hello', 'Hello! You slept 6 hours, nice.')
        self.assertEqual(reply['kind'], 'fixed')
        self.assertEqual(reply['text'], chat.GREETING_TEXT)
        self.assertEqual(reply['ai_label'], '')

    def test_reply_with_diagnosis_words_is_rejected(self):
        for bad in ('It sounds like depression to me.', 'That may be an anxiety problem.',
                    'I would diagnose that as stress.', 'Try some medication for that.'):
            with self.subTest(bad=bad):
                cache.clear()
                reply = self.talk('I feel lonely', bad)
                self.assertEqual(reply['text'], chat.LONELY_TEXT)
                self.assertEqual(reply['kind'], 'fixed')

    def test_reply_claiming_human_feelings_is_rejected(self):
        reply = self.talk('I feel lonely', 'I feel lonely too, I miss home.')
        self.assertEqual(reply['text'], chat.LONELY_TEXT)

    def test_too_long_or_wrong_shape_reply_is_rejected(self):
        self.assertEqual(self.talk('hello', 'Hello. ' * 100)['text'], chat.GREETING_TEXT)

        def bad_shape(request, timeout=None):
            return FakeResponse(json.dumps(wrap(request.full_url, 'not json at all')).encode())

        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', bad_shape):
            self.assertEqual(self.say('hello').json()['reply']['text'], chat.GREETING_TEXT)

    def test_provider_failure_falls_back_and_is_not_retried_for_a_minute(self):
        calls = []

        def down(request, timeout=None):
            calls.append(request.full_url)
            raise OSError('down')

        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', down):
            self.assertEqual(self.say('hello').json()['reply']['text'], chat.GREETING_TEXT)
            self.assertEqual(self.say('hello again').json()['reply']['text'], chat.GREETING_TEXT)
        self.assertEqual(len(calls), 1)

    def test_no_key_means_no_call(self):
        with override_settings(COMPANION_LLM_ENABLED=True), mock.patch('core.llm.urllib.request.urlopen', no_network):
            self.assertEqual(self.say('hello').json()['reply']['kind'], 'fixed')

    def test_model_is_asked_through_the_shared_llm_helpers(self):
        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('companion.chat.llm._query_providers', return_value=('groq', 'Hello. Good to see you.')) as query:
            reply = self.say('hello').json()['reply']
        query.assert_called_once()
        self.assertEqual(reply['ai_label'], 'Written by AI (Groq)')


class SafetyCheckUnitTests(SimpleTestCase):
    def test_accepts_short_plain_text(self):
        self.assertTrue(chat.is_safe_smalltalk("That sounds like a lot. What would help right now?"))

    def test_rejects(self):
        for bad in ('', '   ', None, 5, 'It is 5 degrees', 'Take two of them', 'It might be a disorder',
                    'Visit https://example.com', 'x' * 400, 'As a human I agree', 'I love talking with you',
                    'Here is {json}', 'See <b>this</b>', 'Your symptoms are mild'):
            with self.subTest(bad=bad):
                self.assertFalse(chat.is_safe_smalltalk(bad))


# --- memory ---------------------------------------------------------------------------------------

class MemoryTests(ChatTestCase):
    def test_add_list_delete(self):
        response = self.client_a.post(MEMORY_ADD, json.dumps({'text': '  I like chess  '}), content_type='application/json')
        self.assertEqual(response.status_code, 201)
        memory_id = response.json()['memory']['id']
        listed = self.client_a.get(MEMORIES).json()['memories']
        self.assertEqual([m['text'] for m in listed], ['I like chess'])
        self.assertEqual(self.client_a.post(memory_delete_url(memory_id)).status_code, 200)
        self.assertEqual(self.client_a.get(MEMORIES).json()['memories'], [])
        self.assertEqual(self.client_a.post(memory_delete_url(memory_id)).status_code, 404)

    def test_remember_that_in_chat_saves_a_memory(self):
        data = self.say('Remember that I prefer short answers').json()
        self.assertEqual(data['reply']['kind'], 'memory')
        self.assertEqual(data['reply']['text'], "Okay, I'll remember: I prefer short answers")
        self.assertEqual(list(CompanionMemory.objects.values_list('user_id', 'text')),
                         [(self.user_a.pk, 'I prefer short answers')])
        self.assertEqual(data['memory']['text'], 'I prefer short answers')

    def test_memories_are_only_created_when_explicitly_asked(self):
        messages = [
            'hello', 'I feel lonely', 'I like chess', 'my favourite colour is blue', 'How is my sleep?',
            'Do you remember my name?', 'I want you to know I miss my dog', 'tell me something interesting',
        ]
        for text in messages:
            self.say(text)
        self.assertEqual(CompanionMemory.objects.count(), 0)

    def test_health_answers_and_small_talk_do_not_write_memories_even_with_the_model_on(self):
        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', persona_urlopen('Nice to meet you.')):
            self.say('hello, I like chess')
        self.assertEqual(CompanionMemory.objects.count(), 0)

    def test_memory_text_validation(self):
        for body in ({'text': ''}, {'text': '   '}, {'text': 5}, {'text': 'x' * 201}, {}):
            with self.subTest(body=body):
                response = self.client_a.post(MEMORY_ADD, json.dumps(body), content_type='application/json')
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client_a.post(MEMORY_ADD, 'text=hi', content_type='application/x-www-form-urlencoded').status_code, 400)
        self.assertEqual(CompanionMemory.objects.count(), 0)

    def test_long_remember_request_is_refused_politely(self):
        data = self.say('remember that ' + 'x' * 250).json()
        self.assertEqual(data['reply']['kind'], 'memory')
        self.assertIn('shorten', data['reply']['text'])
        self.assertEqual(CompanionMemory.objects.count(), 0)

    def test_memory_limit(self):
        for i in range(chat.MAX_MEMORIES):
            CompanionMemory.objects.create(user=self.user_a, text=f'note {i}')
        response = self.client_a.post(MEMORY_ADD, json.dumps({'text': 'one more'}), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Delete', self.say('remember that one more').json()['reply']['text'])
        self.assertEqual(CompanionMemory.objects.filter(user=self.user_a).count(), chat.MAX_MEMORIES)

    def test_memory_is_per_user(self):
        self.client_a.post(MEMORY_ADD, json.dumps({'text': 'A note'}), content_type='application/json')
        self.assertEqual(self.client_b.get(MEMORIES).json()['memories'], [])
        self.assertEqual(CompanionMemory.objects.filter(user=self.user_a).count(), 1)

    def test_recall_lists_only_own_memories(self):
        CompanionMemory.objects.create(user=self.user_a, text='I like chess')
        CompanionMemory.objects.create(user=self.user_b, text='B-only')
        text = self.say('what do you remember about me?').json()['reply']['text']
        self.assertIn('I like chess', text)
        self.assertNotIn('B-only', text)

    def test_memory_is_used_as_small_talk_context(self):
        CompanionMemory.objects.create(user=self.user_a, text='I like chess')
        record = []
        with override_settings(COMPANION_LLM_ENABLED=True), env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('core.llm.urllib.request.urlopen', persona_urlopen('Hello there.', record)):
            self.say('hello')
        self.assertIn('I like chess', record[0]['messages'][-1]['content'])


# --- dashboard ----------------------------------------------------------------------------------------

class DashboardPanelTests(ChatTestCase):
    def test_dashboard_has_the_chat_panel_and_keeps_ask_pulse(self):
        html = self.client_a.get(reverse('core:dashboard')).content.decode()
        for needle in ('id="pulse-chat"', 'Chat with Pulse', 'What Pulse remembers', 'New conversation',
                       'Pulse is thinking…', 'aria-live="polite"', 'Not a medical device; does not diagnose.',
                       'How am I doing?', 'Explain my sleep trend', 'Review the cabin environment', 'Start a check-in',
                       'id="companion-ask-form"', 'data-companion-chip'):
            self.assertIn(needle, html)
        self.assertNotIn('innerHTML', html.split('id="pulse-chat"', 1)[1].split('</script>', 1)[0])

    def test_the_name_is_pulse_everywhere_in_the_new_files(self):
        import pathlib
        base = pathlib.Path(__file__).resolve().parent.parent
        banned = re.compile('bay' + 'max|arte' + 'mis', re.IGNORECASE)
        for path in (base / 'companion', base / 'templates' / 'core' / '_pulse_chat.html'):
            files = [path] if path.is_file() else [p for p in path.rglob('*') if p.suffix in ('.py', '.html')]
            for file in files:
                if file.name == 'tests_chat.py':
                    continue
                self.assertIsNone(banned.search(file.read_text(encoding='utf-8')), str(file))


# --- shared per-user LLM rate limit (core/llm.py _allow_live_llm) -------------------------


class RateLimitTests(ChatTestCase):
    def test_small_talk_falls_back_to_fixed_reply_when_over_the_limit(self):
        with override_settings(COMPANION_LLM_ENABLED=True, COMPANION_LLM_RATE_LIMIT=1), \
                env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('companion.chat.llm._query_providers', return_value=('groq', 'Hello. Good to see you.')) as query:
            first = self.say('hello').json()['reply']
            second = self.say('hello again').json()['reply']
        self.assertEqual(first['kind'], 'smalltalk')
        self.assertEqual(second['kind'], 'fixed')
        query.assert_called_once()

    def test_the_limit_is_per_user(self):
        with override_settings(COMPANION_LLM_ENABLED=True, COMPANION_LLM_RATE_LIMIT=1), \
                env(GROQ_API_KEY=FAKE_GROQ_KEY), \
                mock.patch('companion.chat.llm._query_providers', return_value=('groq', 'Hello. Good to see you.')) as query:
            self.say('hello')
            other = self.say('hello', client=self.client_b).json()['reply']
        self.assertEqual(other['kind'], 'smalltalk')
        self.assertEqual(query.call_count, 2)

    def test_the_view_passes_the_logged_in_user(self):
        with mock.patch('companion.views.chat.build_reply', wraps=chat.build_reply) as build:
            self.say('hello')
        self.assertEqual(build.call_args.kwargs['user'], self.user_a)
