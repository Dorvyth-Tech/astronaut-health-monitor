"""
Pulse chat endpoints (JSON). All are login-required (the project's LoginRequiredMiddleware),
CSRF-protected, and scoped to request.user and the active astronaut (core.views.get_active_astronaut).
Anything that belongs to another user is a 404, never a 403, so ids can't be probed.

  GET  history/                  messages of the current (or given) conversation
  POST send/                     {"message": "...", "conversation_id": optional}
  POST new/                      start a new conversation
  GET  memories/                 the user's saved notes
  POST memories/add/             {"text": "..."}   (explicit add; "remember that ..." in chat does the same)
  POST memories/<id>/delete/     forget one note

Message text is never logged.
"""

import json
import logging

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from companion import chat
from companion.models import CompanionMemory, Conversation, Message
from core import companion as rules_companion
from core import intent
from core.views import get_active_astronaut

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 50
NO_ASTRONAUT = {'error': 'No astronaut profile for this login.'}


def _json_body(request):
    """(dict, error response). The body must be a JSON object."""
    if request.content_type != 'application/json':
        return None, JsonResponse({'error': 'Send JSON.'}, status=400)
    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        return None, JsonResponse({'error': 'Send JSON like {"message": "..."}.'}, status=400)
    if not isinstance(body, dict):
        return None, JsonResponse({'error': 'Send a JSON object.'}, status=400)
    return body, None


def _links_for(message):
    """Existing pages Pulse points to. Built from the reply kind, never from model text."""
    ground = {'label': 'Open Ground Support', 'url': reverse('core:ground_support')}
    alerts = {'label': 'View alerts', 'url': reverse('alerts:list')}
    if message.kind == chat.ESCALATION:
        return [ground, alerts]
    if message.kind == chat.CHECKIN:
        return [{'label': 'Open the daily check-in', 'url': reverse('telemetry:check_in')}]
    if message.intent_id == 'medical':
        return [ground]
    if message.kind == chat.HEALTH and message.severity == rules_companion.CAUTION:
        return [alerts]
    return []


def _state_for(message):
    if message.role != Message.ROLE_PULSE:
        return 'idle'
    if message.kind == chat.ESCALATION:
        return 'emergency'
    if message.severity == rules_companion.CAUTION:
        return 'concern'
    return 'responding'


def serialize_message(message):
    data = {
        'id': message.pk,
        'role': message.role,
        'text': message.text,
        'created_at': message.created_at.isoformat(),
    }
    if message.role == Message.ROLE_PULSE:
        data.update({
            'kind': message.kind,
            'intent_id': message.intent_id,
            'severity': message.severity,
            'escalation': message.escalation,
            'details': message.details,
            'source': message.source,
            'simulated_note': message.simulated_note,
            'data_note': message.data_note,
            'context': message.context,
            'ai_label': f"Written by AI ({message.ai_provider})" if message.ai_provider else '',
            'disclaimer': rules_companion.DISCLAIMER,
            'links': _links_for(message),
            'state': _state_for(message),
        })
    return data


def serialize_memory(memory):
    return {'id': memory.pk, 'text': memory.text, 'created_at': memory.created_at.isoformat()}


def _current_conversation(request, astronaut, create=False):
    """The conversation named in the request (404 if not the user's) or the user's latest one."""
    raw_id = request.GET.get('conversation_id') if request.method == 'GET' else None
    return _lookup(request, astronaut, raw_id, create)


def _lookup(request, astronaut, raw_id, create):
    own = Conversation.objects.filter(user=request.user, astronaut=astronaut)
    if raw_id not in (None, ''):
        try:
            conversation_id = int(raw_id)
        except (TypeError, ValueError):
            conversation_id = -1
        return get_object_or_404(own, pk=conversation_id)
    conversation = own.first()
    if conversation is None and create:
        conversation = Conversation.objects.create(user=request.user, astronaut=astronaut)
    return conversation


@require_GET
def history_view(request):
    astronaut = get_active_astronaut(request)
    if not astronaut:
        return JsonResponse(NO_ASTRONAUT, status=403)
    conversation = _current_conversation(request, astronaut)
    if conversation is None:
        return JsonResponse({'conversation_id': None, 'messages': [], 'suggested_prompts': list(chat.SUGGESTED_PROMPTS)})
    recent = list(conversation.messages.order_by('-id')[:HISTORY_LIMIT])[::-1]
    return JsonResponse({
        'conversation_id': conversation.pk,
        'messages': [serialize_message(m) for m in recent],
        'suggested_prompts': list(chat.SUGGESTED_PROMPTS),
    })


@require_POST
def new_conversation_view(request):
    astronaut = get_active_astronaut(request)
    if not astronaut:
        return JsonResponse(NO_ASTRONAUT, status=403)
    conversation = Conversation.objects.create(user=request.user, astronaut=astronaut)
    return JsonResponse({'conversation_id': conversation.pk, 'messages': []}, status=201)


def _recent_turns(conversation):
    """The last few turns the model may see: the user's words and Pulse's small-talk replies only."""
    turns = []
    rows = conversation.messages.order_by('-id')[:10]
    for m in reversed(list(rows)):
        if m.role == Message.ROLE_USER:
            turns.append({'from': 'user', 'text': m.text})
        elif m.kind == chat.SMALLTALK:
            turns.append({'from': 'pulse', 'text': m.text})
    return turns[-chat.RECENT_TURNS:]


@require_POST
def send_view(request):
    astronaut = get_active_astronaut(request)
    if not astronaut:
        return JsonResponse(NO_ASTRONAUT, status=403)
    body, error = _json_body(request)
    if error:
        return error
    question, problem = intent.clean_question(body.get('message'))
    if problem:
        return JsonResponse({'error': problem}, status=400)

    conversation = _lookup(request, astronaut, body.get('conversation_id'), create=True)
    memories = list(CompanionMemory.objects.filter(user=request.user).values_list('text', flat=True))
    turn_index = conversation.messages.filter(role=Message.ROLE_USER).count()

    memory_text = None if chat.is_urgent(question) else chat.memory_request(question)
    try:
        if memory_text is not None:
            reply, new_memory = _remember_reply(request.user, memory_text)
        else:
            new_memory = None
            reply = chat.build_reply(question, astronaut, memories, _recent_turns(conversation), turn_index)
    except Exception as exc:    # a bug must not look like an answer; the text is never logged
        logger.warning('Pulse chat reply failed: %s', type(exc).__name__)
        return JsonResponse({'error': 'Pulse could not answer just now. Please try again.'}, status=500)

    with transaction.atomic():
        user_message = Message.objects.create(conversation=conversation, role=Message.ROLE_USER, text=question)
        pulse_message = Message.objects.create(conversation=conversation, role=Message.ROLE_PULSE, **reply)
        conversation.save(update_fields=['updated_at'])
    payload = {
        'conversation_id': conversation.pk,
        'user_message': serialize_message(user_message),
        'reply': serialize_message(pulse_message),
    }
    if new_memory is not None:
        payload['memory'] = serialize_memory(new_memory)
    return JsonResponse(payload)


def _remember_reply(user, text):
    """Store a note the user explicitly asked for. Returns (reply dict, memory or None)."""
    if len(text) > chat.MAX_MEMORY_CHARS:
        return chat._reply(
            f"That's a bit long for me to keep. Please shorten it to {chat.MAX_MEMORY_CHARS} characters or fewer.",
            chat.MEMORY), None
    if CompanionMemory.objects.filter(user=user).count() >= chat.MAX_MEMORIES:
        return chat._reply(
            "I'm already keeping as many notes as I can. Delete one under \"What Pulse remembers\" first.",
            chat.MEMORY), None
    memory = CompanionMemory.objects.create(user=user, text=text)
    return chat._reply(f"Okay, I'll remember: {text}", chat.MEMORY), memory


@require_GET
def memories_view(request):
    astronaut = get_active_astronaut(request)
    if not astronaut:
        return JsonResponse(NO_ASTRONAUT, status=403)
    memories = CompanionMemory.objects.filter(user=request.user)
    return JsonResponse({'memories': [serialize_memory(m) for m in memories], 'limit': chat.MAX_MEMORIES})


@require_POST
def memory_add_view(request):
    """Explicit add (the "Remember" form). Nothing else in the app creates memories."""
    astronaut = get_active_astronaut(request)
    if not astronaut:
        return JsonResponse(NO_ASTRONAUT, status=403)
    body, error = _json_body(request)
    if error:
        return error
    raw = body.get('text')
    text = raw.strip() if isinstance(raw, str) else ''
    if not text:
        return JsonResponse({'error': 'Type what you want me to remember.'}, status=400)
    if len(text) > chat.MAX_MEMORY_CHARS:
        return JsonResponse({'error': f'Please keep it under {chat.MAX_MEMORY_CHARS} characters.'}, status=400)
    if chat.is_urgent(text):
        return JsonResponse({'error': "That sounds urgent. Please contact your flight surgeon through Ground Support."}, status=400)
    if CompanionMemory.objects.filter(user=request.user).count() >= chat.MAX_MEMORIES:
        return JsonResponse({'error': f'I can keep {chat.MAX_MEMORIES} notes. Delete one first.'}, status=400)
    memory = CompanionMemory.objects.create(user=request.user, text=text)
    return JsonResponse({'memory': serialize_memory(memory)}, status=201)


@require_POST
def memory_delete_view(request, memory_id):
    astronaut = get_active_astronaut(request)
    if not astronaut:
        return JsonResponse(NO_ASTRONAUT, status=403)
    memory = get_object_or_404(CompanionMemory, pk=memory_id, user=request.user)
    memory.delete()
    return JsonResponse({'deleted': True})
