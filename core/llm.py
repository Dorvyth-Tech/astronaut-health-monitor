"""
SPACE-PULSE companion, layer 2: optional AI rephrase.

Layer 1 (core/companion.py) builds the answers from computed results. This module
can ask a language model to say the SAME answer text in a warmer, calmer voice.
The model never decides anything:

- It only receives each answer's id, severity and text (never raw readings,
  never other people's data).
- Only the main 'answer' text can change. source, simulated_note, disclaimer and
  escalation ('Contact your flight surgeon now.') are never sent and never touched.
- 'data_source' is not rephrased at all: it carries the "demo estimates, not
  official NASA limits" caveat word for word.
- A rewrite is thrown away (that answer keeps its layer-1 text) if it contains a
  number the original did not, or a diagnosis-style word the original did not.
- Anything that goes wrong (no key, timeout, HTTP error, bad JSON) returns the
  answers unchanged. rephrase_with_llm() never raises.

Standard library only (urllib + json). Turned on with COMPANION_LLM_ENABLED=True,
see core.views.companion_answers_for and .env.example.

Settings (environment variables or .env):
  COMPANION_LLM_PROVIDER  groq (default) or gemini. The other one is the fallback
                          when its key exists.
  GROQ_API_KEY, GEMINI_API_KEY
  GROQ_MODEL, GEMINI_MODEL   optional model overrides
"""

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request

from decouple import config
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Sources: console.groq.com/docs/models (production model list) and
# ai.google.dev/gemini-api/docs/deprecations (recommended for new projects).
DEFAULT_GROQ_MODEL = 'openai/gpt-oss-20b'
DEFAULT_GEMINI_MODEL = 'gemini-3.5-flash-lite'

GROQ_URL = 'https://api.groq.com/openai/v1/chat/completions'
GEMINI_URL = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'

PROVIDER_LABELS = {'groq': 'Groq', 'gemini': 'Gemini'}
DEFAULT_PROVIDER = 'groq'

TOTAL_TIMEOUT_SECONDS = 4.0     # all provider attempts for one rephrase share this
CACHE_SECONDS = 300             # a good rephrase is reused for 5 minutes
FAILURE_CACHE_SECONDS = 60      # after a failure, don't retry for a minute
CACHE_PREFIX = 'companion_llm:'

# These answers are never sent to the model.
NEVER_REPHRASE = {'data_source'}

# Words that imply a diagnosis. Allowed only if the original text already used them.
BANNED_TERMS = (
    'diagnos', 'disorder', 'depress', 'disease', 'syndrome', 'prescri', 'medicat',
    'anxiety', 'therapy', 'infection', 'cancer', 'illness',
)

# Spelled-out numbers can sneak in a new quantity. ('one' is left out: too common.)
NUMBER_WORDS = (
    'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten',
    'eleven', 'twelve', 'twenty', 'thirty', 'fifty', 'hundred', 'thousand',
    'half', 'double', 'triple',
)

SYSTEM_PROMPT = (
    "You are the voice of Pulse Companion, a calm, warm health companion for astronauts. "
    "You will receive a JSON object with a list of answers, each with an id, a severity and a text. "
    "Rewrite each text in a warm, calm, concise, human voice, in at most two short sentences. "
    "Strict rules: keep every fact exactly as given. Do NOT add any number, threshold, date, "
    "diagnosis, cause, medical advice or recommendation that is not already in that text. "
    "Do not remove a number that is in the text. Do not make a caution or critical text sound "
    "less serious, and do not make a nominal text sound worrying. "
    "Do not use words like diagnosis, disorder, disease, medication or depression unless the "
    "original text uses them. "
    "Return ONLY strict JSON in exactly this shape and nothing else: "
    '{"answers": [{"id": "<same id>", "text": "<rewritten text>"}]}'
)


# =========================================================================
# Safety validator
# =========================================================================

_NUMBER_RE = re.compile(r'\d+(?:[.,]\d+)?')


def _numbers_in(text):
    return {n.replace(',', '.') for n in _NUMBER_RE.findall(text)}


def _words_in(text):
    return set(re.findall(r"[a-z]+", text.lower()))


def is_safe_rewrite(original, rewrite):
    """True only if the rewrite is plain text that adds no number or diagnosis word."""
    if not isinstance(rewrite, str):
        return False
    rewrite = rewrite.strip()
    if not rewrite or len(rewrite) > 2 * len(original) + 200:
        return False
    if not _numbers_in(rewrite) <= _numbers_in(original):
        return False
    lowered_new, lowered_old = rewrite.lower(), original.lower()
    for term in BANNED_TERMS:
        if term in lowered_new and term not in lowered_old:
            return False
    new_words, old_words = _words_in(rewrite), _words_in(original)
    for word in NUMBER_WORDS:
        if word in new_words and word not in old_words:
            return False
    return True


# =========================================================================
# Provider calls
# =========================================================================

def _post_json(url, payload, headers, timeout):
    """POST JSON, return the parsed JSON response. Raises on any problem."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'User-Agent': 'SpacePulse/1.0', **headers},
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def _call_groq(user_text, api_key, model, timeout):
    body = _post_json(
        GROQ_URL,
        {
            'model': model,
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': user_text},
            ],
            'temperature': 0.3,
            'max_tokens': 1200,
            'response_format': {'type': 'json_object'},
        },
        {'Authorization': f'Bearer {api_key}'},
        timeout,
    )
    return body['choices'][0]['message']['content']


def _call_gemini(user_text, api_key, model, timeout):
    # The key goes in a header, not the URL, so it can't end up in a log line.
    # No temperature: newer Gemini models ignore or reject sampling parameters.
    body = _post_json(
        GEMINI_URL.format(model=model),
        {
            'systemInstruction': {'parts': [{'text': SYSTEM_PROMPT}]},
            'contents': [{'role': 'user', 'parts': [{'text': user_text}]}],
            'generationConfig': {'responseMimeType': 'application/json'},
        },
        {'x-goog-api-key': api_key},
        timeout,
    )
    return ''.join(part.get('text', '') for part in body['candidates'][0]['content']['parts'])


def _provider_order():
    """[(name, key, model, call_function)] for providers that have a key, primary first."""
    primary = str(config('COMPANION_LLM_PROVIDER', default=DEFAULT_PROVIDER)).strip().lower()
    if primary not in PROVIDER_LABELS:
        primary = DEFAULT_PROVIDER
    other = 'gemini' if primary == 'groq' else 'groq'
    providers = {
        'groq': (config('GROQ_API_KEY', default='').strip(),
                 config('GROQ_MODEL', default='').strip() or DEFAULT_GROQ_MODEL, _call_groq),
        'gemini': (config('GEMINI_API_KEY', default='').strip(),
                   config('GEMINI_MODEL', default='').strip() or DEFAULT_GEMINI_MODEL, _call_gemini),
    }
    return [(name, *providers[name]) for name in (primary, other) if providers[name][0]]


def _parse_texts(raw):
    """Model output -> {id: text}. Raises if it is not the agreed JSON shape."""
    raw = raw.strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw)
    data = json.loads(raw)
    return {str(item['id']): item['text'] for item in data['answers']}


# =========================================================================
# Public API
# =========================================================================

def _cache_key(items, order):
    blob = json.dumps([items, [name for name, *_ in order]], sort_keys=True)
    return CACHE_PREFIX + hashlib.sha256(blob.encode('utf-8')).hexdigest()


def _ask_providers(items, order):
    """Try each provider within the shared time budget. Returns (name, {id: text}) or None."""
    user_text = json.dumps({'answers': items})
    deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS
    for name, key, model, call in order:
        remaining = deadline - time.monotonic()
        if remaining <= 0.2:
            break
        try:
            return name, _parse_texts(call(user_text, key, model, remaining))
        except Exception as exc:    # never let the AI break the dashboard
            logger.warning('Companion LLM (%s) failed: %s', name, type(exc).__name__)
    return None


def rephrase_with_llm(answers):
    """Return the answers with warmer wording where the model's rewrite is safe.

    Takes the list from core.companion.build_companion_answers and returns a new
    list of the same length and order. Answers that were rephrased get
    ai_rephrased=True and ai_provider='Groq'/'Gemini'; all others are untouched.
    On any failure the original answers are returned unchanged.
    """
    try:
        items = [
            {'id': a['id'], 'severity': a['severity'], 'text': a['answer']}
            for a in answers if a['id'] not in NEVER_REPHRASE and a.get('answer')
        ]
        order = _provider_order()
        if not items or not order:
            return answers

        key = _cache_key(items, order)
        cached = cache.get(key)
        if cached is None:
            result = _ask_providers(items, order)
            cached = result or {}
            cache.set(key, cached, CACHE_SECONDS if result else FAILURE_CACHE_SECONDS)
        if not cached:
            return answers

        provider, texts = cached
        label = PROVIDER_LABELS[provider]
        out = []
        for answer in answers:
            new_text = texts.get(answer['id'])
            if answer['id'] in NEVER_REPHRASE or not is_safe_rewrite(answer['answer'], new_text):
                out.append(answer)
                continue
            out.append({**answer, 'answer': new_text.strip(), 'ai_rephrased': True, 'ai_provider': label})
        return out
    except Exception as exc:
        logger.warning('Companion LLM rephrase skipped: %s', type(exc).__name__)
        return answers