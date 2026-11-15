"""
Pulse chat storage: conversations, their messages, and the few things a user
has explicitly asked Pulse to remember.

Every row belongs to ONE user (and, for conversations, one astronaut). The views
in companion/views.py always filter by request.user, so nobody can read or delete
someone else's chat or memory.
"""

from django.conf import settings
from django.db import models


class Conversation(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='pulse_conversations')
    astronaut = models.ForeignKey(
        'core.AstronautProfile', on_delete=models.CASCADE, related_name='pulse_conversations')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return f"Conversation {self.pk} ({self.user_id})"


class Message(models.Model):
    ROLE_USER = 'user'
    ROLE_PULSE = 'pulse'
    ROLE_CHOICES = [(ROLE_USER, 'User'), (ROLE_PULSE, 'Pulse')]

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    text = models.TextField()

    # What kind of reply this was (see companion/chat.py): health, escalation, smalltalk,
    # fixed, checkin, memory. Empty for the user's own messages.
    kind = models.CharField(max_length=20, blank=True, default='')
    intent_id = models.CharField(max_length=40, blank=True, default='')
    severity = models.CharField(max_length=20, blank=True, default='')
    escalation = models.CharField(max_length=200, blank=True, default='')
    # Computed answer text kept next to a fixed escalation message (from core/companion.py).
    details = models.TextField(blank=True, default='')
    source = models.CharField(max_length=300, blank=True, default='')
    simulated_note = models.TextField(blank=True, default='')
    # "No readings yet" / "latest reading is old" notes, and context-only lines (space weather).
    data_note = models.TextField(blank=True, default='')
    context = models.TextField(blank=True, default='')
    # 'Groq' / 'Gemini' when a model wrote or reworded the text, else ''.
    ai_provider = models.CharField(max_length=20, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.role} message {self.pk}"


class CompanionMemory(models.Model):
    """One short note the user explicitly asked Pulse to keep. Their own words."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='pulse_memories')
    text = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        verbose_name_plural = 'companion memories'

    def __str__(self):
        return f"Memory {self.pk} ({self.user_id})"
