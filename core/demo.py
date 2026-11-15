"""Public read-only demo for judges (Space Apps: the demo link must open without a login).

GET /demo/ signs the visitor in as a fresh, throwaway demo user that sees the
seeded commander's data. Each visitor gets their own user so chats and sessions
never mix between judges. Demo users:
  - have no usable password (nobody can log in as them through /login/),
  - can read every page, and use Pulse (ask and chat),
  - cannot change data: other POST/PUT/PATCH/DELETE requests are refused by
    ReadOnlyDemoMiddleware before the view runs,
  - share ONE live-LLM rate-limit bucket (core/llm.py), so opening /demo/ again
    does not buy more model calls.
Everything is OFF unless PUBLIC_DEMO=True.
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect

DEMO_USERNAME_PREFIX = "public-demo-"
READ_ONLY_MESSAGE = "This is the public read-only demo, so changes are not saved."

# Write requests a demo visitor may still make: asking Pulse, and the chat.
# Memory notes are not allowed (they are a write the next judge would not see anyway).
ALLOWED_WRITE_VIEWS = frozenset({
    "core:companion_ask",
    "core:logout",
    "companion:send",
    "companion:new",
})

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def public_demo_enabled() -> bool:
    return bool(getattr(settings, "PUBLIC_DEMO", False))


def is_public_demo_user(user) -> bool:
    """True for the throwaway demo users. Never true while PUBLIC_DEMO is off."""
    if not public_demo_enabled() or user is None:
        return False
    if not getattr(user, "is_authenticated", False):
        return False
    return (getattr(user, "username", "") or "").startswith(DEMO_USERNAME_PREFIX)


def demo_astronaut():
    """The astronaut every demo visitor sees: the seeded active commander."""
    from core.models import AstronautProfile

    return (
        AstronautProfile.objects.filter(is_active_commander=True).order_by("id").first()
        or AstronautProfile.objects.order_by("id").first()
    )


def create_demo_user():
    from django.contrib.auth.models import User

    user = User(
        username=f"{DEMO_USERNAME_PREFIX}{secrets.token_hex(6)}",
        first_name="Demo",
        last_name="Visitor",
        is_staff=False,
        is_superuser=False,
    )
    user.set_unusable_password()
    user.save()
    return user


def _wants_json(request) -> bool:
    accept = request.headers.get("Accept", "")
    return (
        "application/json" in accept
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or (request.content_type or "").startswith("application/json")
    )


class ReadOnlyDemoMiddleware:
    """Refuse write requests from demo users, except the allow-listed Pulse views."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.method in SAFE_METHODS:
            return None
        if not is_public_demo_user(getattr(request, "user", None)):
            return None
        match = getattr(request, "resolver_match", None)
        if match is not None and match.view_name in ALLOWED_WRITE_VIEWS:
            return None
        if _wants_json(request):
            return JsonResponse({"error": READ_ONLY_MESSAGE, "read_only_demo": True}, status=403)
        messages.info(request, READ_ONLY_MESSAGE)
        back = request.META.get("HTTP_REFERER")
        from django.utils.http import url_has_allowed_host_and_scheme

        if back and url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
            return redirect(back)
        return redirect("core:dashboard")
