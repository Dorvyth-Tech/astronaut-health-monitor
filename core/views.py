"""
Core views for Space-Pulse: Dashboard, Login, Logout, Profile, Ground Support.
"""

import json
import logging

from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.db.models import Count
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from core.models import AstronautProfile, Baseline, GroundMessage
from telemetry.models import Reading, CheckIn
from alerts.models import Alert
from core.constants import METRIC_CONFIG, METRICS
from core import intent
from core.companion import build_companion_answers, indicator_answer
from core.llm import rephrase_with_llm, classify_intent_with_llm
from core.services import (
    baseline_details,
    get_baselines,
    read_baseline_form,
    save_baseline_changes,
    evaluate_astronaut_metrics,
    detect_consecutive_trend,
    generate_sparkline_svg,
    calculate_health_score,
    calculate_risk_vectors,
    sync_alerts_for_astronaut,
)

logger = logging.getLogger(__name__)

# The shared password of the seeded demo accounts. Outside DEMO_MODE it is just
# the real password of those accounts; it is never a master key (see login_view).
DEMO_PASSWORD = "validAccess2026"


def get_active_astronaut(request):
    """The astronaut whose data this request is allowed to see.

    Normal mode: only the astronaut linked to the logged-in user (same email).
    DEMO_MODE: the crew switcher may pick any astronaut (kept in the session).
    Returns None when there is no such astronaut; views then go to the login page.
    """
    if not request.user.is_authenticated:
        return None

    if settings.DEMO_MODE:
        astro_id = request.session.get('active_astronaut_id')
        if astro_id:
            astro = AstronautProfile.objects.filter(id=astro_id).first()
            if astro:
                return astro

    if request.user.email:
        astro = AstronautProfile.objects.filter(email__iexact=request.user.email).first()
        if astro:
            return astro

    if settings.DEMO_MODE:
        # e.g. the admin account in a demo: fall back to the commander
        return (
            AstronautProfile.objects.filter(is_active_commander=True).first()
            or AstronautProfile.objects.first()
        )
    return None


@login_not_required
def login_view(request):
    """
    Cosmic glassmorphic login view.
    Authenticates crew credentials and routes to the requested path.
    This view and logout_view are the only ones that do not need a login.
    """
    next_url = request.GET.get('next') or request.POST.get('next') or reverse('core:dashboard')
    # Only follow ?next= to a page on this site.
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        next_url = reverse('core:dashboard')
    error_message = None

    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()
        password = request.POST.get('password', '').strip()

        # Find user or astronaut profile
        user = None
        user_match = User.objects.filter(email__iexact=email).first()
        if not user_match:
            user_match = User.objects.filter(username__iexact=email.split('@')[0]).first()

        if user_match:
            # Check the real password. In DEMO_MODE the shared demo password also works.
            if user_match.check_password(password) or (settings.DEMO_MODE and password == DEMO_PASSWORD):
                user = user_match

        # Fallback profile check for demo flexibility
        astro_match = AstronautProfile.objects.filter(email__iexact=email).first()
        if not user and settings.DEMO_MODE and astro_match and password == DEMO_PASSWORD:
            # If user record doesn't exist yet, create one (demo only)
            user, _ = User.objects.get_or_create(
                username=email.split('@')[0],
                email=email,
                defaults={'first_name': astro_match.rank, 'last_name': astro_match.name}
            )
            user.set_password("validAccess2026")
            user.save()

        # Find the astronaut linked to this user (same email).
        astro = None
        if user and user.email:
            astro = AstronautProfile.objects.filter(email__iexact=user.email).first()
        if user and not astro and settings.DEMO_MODE:
            astro = AstronautProfile.objects.filter(is_active_commander=True).first()

        if user and astro:
            login(request, user)
            if settings.DEMO_MODE:
                request.session['active_astronaut_id'] = astro.id

            # Route to their requested path
            return redirect(next_url)
        elif user:
            error_message = "This account is not linked to an astronaut profile."
        else:
            error_message = "Email or password not recognised."

    return render(request, 'login.html', {
        'error': error_message,
        'next_url': next_url,
        'submitted_email': request.POST.get('email', 'amara.reyes@mission.example'),
    })


@login_not_required
def logout_view(request):
    """Logs out user and redirects to the login screen."""
    logout(request)
    if 'active_astronaut_id' in request.session:
        del request.session['active_astronaut_id']
    return redirect('core:login')


def companion_answers_for(astronaut, metric_evals, health_score):
    """The companion's answers for one astronaut.

    This is the one place that gathers the numbers from the database and hands them
    to core/companion.py (layer 1: fixed rules). If COMPANION_LLM_ENABLED is True,
    layer 2 (core/llm.py) may then rephrase the wording. It never changes the facts
    and falls back to the layer-1 answers on any problem.
    """
    rows = (
        Reading.objects.filter(astronaut=astronaut, metric__in=METRICS)
        .values('source')
        .annotate(n=Count('id'))
    )
    source_counts = {row['source']: row['n'] for row in rows}
    open_alerts = Alert.objects.filter(astronaut=astronaut, acknowledged=False).count()
    answers = build_companion_answers(metric_evals, health_score, source_counts, open_alerts)
    if settings.COMPANION_LLM_ENABLED:
        answers = rephrase_with_llm(answers)
    return answers


@require_GET
def companion_answers_view(request):
    """JSON with the companion's answers for the logged-in astronaut (login required).

    Only get_active_astronaut's astronaut is ever used, so nobody can ask about
    someone else's data. The dashboard shows the same answers in HTML.
    """
    astronaut = get_active_astronaut(request)
    if not astronaut:
        return JsonResponse({'error': 'No astronaut profile for this login.'}, status=403)

    metric_evals = evaluate_astronaut_metrics(astronaut)
    health_score = calculate_health_score(metric_evals)
    return JsonResponse({'answers': companion_answers_for(astronaut, metric_evals, health_score)})


def _ask_pulse_answer(intent_id, astronaut):
    """The computed companion answer for one intent id, or None if there is none.

    Always built by core/companion.py from the astronaut's own computed results.
    When the AI is on, rephrase_with_llm only rewords it (same safety checks as the chips).
    """
    metric_evals = evaluate_astronaut_metrics(astronaut)
    health_score = calculate_health_score(metric_evals)
    answers = companion_answers_for(astronaut, metric_evals, health_score)

    wanted = intent_id if intent_id in ('overall', 'what_to_do', 'data_source') else f'why_{intent_id}'
    for answer in answers:
        if answer['id'] == wanted:
            return answer

    # A nominal indicator has no chip, so build its answer from its own evaluation.
    if intent_id in intent.INDICATOR_LABELS and intent_id in metric_evals:
        simulated_note = answers[0].get('simulated_note', '') if answers else ''
        answer = indicator_answer(intent_id, metric_evals[intent_id], simulated_note)
        if settings.COMPANION_LLM_ENABLED:
            answer = rephrase_with_llm([answer])[0]
        return answer
    return None


def _ask_pulse_out_of_scope(method, note=''):
    message = f"{note} {intent.OUT_OF_SCOPE_MESSAGE}".strip()
    return JsonResponse({
        'matched_id': None,
        'method': method,
        'answer': None,
        'out_of_scope': True,
        'message': message,
        'supported_questions': list(intent.SUPPORTED_QUESTIONS),
    })


@require_POST
def companion_ask_view(request):
    """"Ask Pulse": a typed question -> one of the companion's computed answers.

    NOT a chatbot. The text is only classified into a fixed intent (keywords first,
    then, if the AI is on and nothing matched, the model picks from the same list and
    its choice is validated). The reply is always the computed answer for that intent,
    or a fixed out-of-scope message. Login and CSRF protected; only the logged-in
    user's astronaut is ever used. The question text is never logged.

    Accepts a form field or JSON: {"question": "..."}.
    """
    astronaut = get_active_astronaut(request)
    if not astronaut:
        return JsonResponse({'error': 'No astronaut profile for this login.'}, status=403)

    if request.content_type == 'application/json':
        try:
            body = json.loads(request.body or b'{}')
        except ValueError:
            return JsonResponse({'error': 'Send JSON like {"question": "..."}.'}, status=400)
        raw_question = body.get('question') if isinstance(body, dict) else None
    else:
        raw_question = request.POST.get('question')

    question, error = intent.clean_question(raw_question)
    if error:
        return JsonResponse({'error': error}, status=400)

    intent_id = intent.classify_by_keywords(question)
    method = intent.KEYWORD
    if intent_id is None and settings.COMPANION_LLM_ENABLED:
        picked = classify_intent_with_llm(question, intent.INTENT_IDS, intent.INTENT_DESCRIPTIONS)
        if picked:
            intent_id, method = picked[0], intent.AI

    if intent_id == 'radiation':
        logger.debug('ask_pulse intent=radiation method=%s', method)
        return _ask_pulse_out_of_scope(intent.KEYWORD, intent.NO_RADIATION_DATA_NOTE)
    if intent_id is None:
        logger.debug('ask_pulse intent=none')
        return _ask_pulse_out_of_scope(intent.NONE)

    answer = _ask_pulse_answer(intent_id, astronaut)
    if answer is None:
        return _ask_pulse_out_of_scope(intent.NONE)
    logger.debug('ask_pulse intent=%s method=%s', intent_id, method)
    public = {'ai_rephrased': False, 'ai_provider': '', **answer}
    # The label text comes from the server, like the one on the chip answers.
    public['ai_label'] = f"Rephrased by AI ({public['ai_provider']})" if public['ai_rephrased'] else ''
    return JsonResponse({
        'matched_id': answer['id'],
        'method': method,
        'answer': public,
        'out_of_scope': False,
        'message': '',
        'supported_questions': [],
    })


def dashboard_view(request):
    """
    Approved deep-space HUD dashboard matching design/dashboard.html with live data.
    """
    astronaut = get_active_astronaut(request)
    if not astronaut:
        return redirect('core:login')

    # Recompute active signals and thresholds in SQLite
    sync_alerts_for_astronaut(astronaut)

    # Fetch personal baselines
    baselines = get_baselines(astronaut)

    # Evaluate all 7 metrics with the science rules (core/rules.py via services)
    metric_evals = evaluate_astronaut_metrics(astronaut, baselines)
    metric_cards = []
    consecutive_drop_metric = None
    consecutive_drop_streak = 0
    consecutive_trend_message = None

    for metric_key in METRICS:
        recent_reads = list(
            Reading.objects.filter(astronaut=astronaut, metric=metric_key)
            .order_by('-mission_day', '-timestamp')[:7]
        )
        chronological_reads = list(reversed(recent_reads))
        chronological_vals = [r.value for r in chronological_reads]
        latest_val = chronological_vals[-1] if chronological_vals else baselines.get(metric_key, 0.0)

        # Display baseline. The rules use their own (stored or computed) baseline
        # and fail closed when there is none; this one is only for showing.
        baseline_val = baselines.get(metric_key, METRIC_CONFIG.get(metric_key, {}).get('default_baseline', 0.0))
        eval_dict = metric_evals[metric_key]

        trend = detect_consecutive_trend(chronological_vals)
        sparkline_svg = generate_sparkline_svg(chronological_vals, width=70, height=20)

        # Track any significant consecutive drop/rise for headline summary
        if trend['flagged']:
            consecutive_drop_metric = eval_dict['display_label']
            consecutive_drop_streak = trend['streak_days']
            consecutive_trend_message = trend['message']

        # Determine direction indicator
        if trend['direction'] == 'up':
            dir_label = 'Up'
            dir_icon = 'arrow_upward'
            dir_class = {
                'nominal': 'text-[#22E3A0]',
                'no_data': 'text-on-surface-variant',
            }.get(eval_dict['status'], 'text-[#FFB020]')
        elif trend['direction'] == 'down':
            dir_label = 'Down'
            dir_icon = 'arrow_downward'
            dir_class = 'text-[#FFB020]' if eval_dict['status'] in ('caution', 'critical') else 'text-on-surface-variant'
        else:
            dir_label = 'Steady'
            dir_icon = ''
            dir_class = 'text-on-surface-variant'

        # Special formatting for display
        if metric_key == 'cabin_co2':
            formatted_val = f"{int(round(latest_val)):,}"
        elif metric_key in ('resting_hr', 'hrv', 'exercise'):
            formatted_val = f"{int(round(latest_val))}"
        elif metric_key == 'mood':
            formatted_val = f"{int(round(latest_val))}"
        else:
            formatted_val = f"{latest_val:.1f}"

        metric_cards.append({
            'key': metric_key,
            'label': eval_dict['display_label'],
            'unit': eval_dict['unit'],
            'value': latest_val,
            'formatted_value': formatted_val,
            'status': eval_dict['status'],
            'status_label': eval_dict['label'],
            'badge_bg': eval_dict['badge_bg'],
            'badge_text': eval_dict['badge_text'],
            'color_hex': eval_dict['color_hex'],
            'icon': eval_dict['icon'],
            'sparkline_points': sparkline_svg,
            'direction_label': dir_label,
            'direction_icon': dir_icon,
            'direction_class': dir_class,
            'is_caution': eval_dict['status'] == 'caution',
            'is_critical': eval_dict['status'] == 'critical',
            'is_no_data': eval_dict['status'] == 'no_data',
            'reason': eval_dict['reason'],
            'baseline': baseline_val,
        })

    # Overall Health Score calculation
    health_score = calculate_health_score(metric_evals)

    # Dynamic headline summary sentence (never hardcoded).
    # Based on the rule statuses first; the trend is only a mention.
    flagged = {
        'critical': [c['label'] for c in metric_cards if c['is_critical']],
        'caution': [c['label'] for c in metric_cards if c['is_caution']],
    }
    if flagged['critical']:
        headline_summary = f"Critical: {', '.join(flagged['critical'])} outside the personal baseline range. See alerts for steps."
    elif flagged['caution']:
        headline_summary = f"Caution: {', '.join(flagged['caution'])} drifting from the personal baseline."
    elif health_score['status'] == 'no_data':
        headline_summary = "Not enough baseline data yet to judge the crew's health score."
    elif consecutive_drop_metric and consecutive_drop_streak >= 3:
        headline_summary = f"Within baseline rules. {consecutive_drop_metric} {consecutive_trend_message}."
    else:
        headline_summary = "All tracked indicators are within the personal baseline range."

    # NASA HRP Risk Vectors
    risk_vectors = calculate_risk_vectors(metric_evals)

    # Active alerts feed (unacknowledged or active for today)
    active_alerts = Alert.objects.filter(
        astronaut=astronaut,
        acknowledged=False
    ).order_by('-timestamp')[:5]

    # Baseline for sleep in chart
    sleep_baseline = baselines.get('sleep', 7.2)

    # Companion panel. Chips are plain links (?ask=<id>), so they work without JavaScript.
    companion_answers = companion_answers_for(astronaut, metric_evals, health_score)
    selected_id = request.GET.get('ask')
    if selected_id not in [answer['id'] for answer in companion_answers]:
        selected_id = 'overall'
    for answer in companion_answers:
        answer['selected'] = answer['id'] == selected_id

    return render(request, 'core/dashboard.html', {
        'active_nav': 'dashboard',
        'current_astronaut': astronaut,
        'health_score': health_score,
        'headline_summary': headline_summary,
        'metric_cards': metric_cards,
        'risk_vectors': risk_vectors,
        'active_alerts': active_alerts,
        'companion_answers': companion_answers,
        'sleep_baseline': sleep_baseline,
        'next_ground_pass': 'T-01:14:22',
        'comm_latency': '18m 42s',
    })


def profile_view(request):
    """
    Astronaut Profile & Personal Baselines View.
    Displays astronaut bio-dossier, crew roster, and editable physiological baselines.
    Handles POST submissions to update baselines and recalibrate active alerts.
    """
    # Crew switcher: DEMO_MODE only. Otherwise ?switch= is ignored.
    switch_id = request.GET.get('switch') if settings.DEMO_MODE else None
    if switch_id:
        target_astro = AstronautProfile.objects.filter(id=switch_id).first()
        if target_astro:
            request.session['active_astronaut_id'] = target_astro.id
            return redirect('core:profile')

    astronaut = get_active_astronaut(request)
    if not astronaut:
        return redirect('core:login')

    success_message = None
    error_message = None

    if request.method == 'POST':
        try:
            # Check the baseline fields first, so nothing is saved if one is invalid.
            baseline_changes, baseline_errors = read_baseline_form(request.POST, baseline_details(astronaut))
            if baseline_errors:
                raise ValueError(" ".join(baseline_errors))

            # Update astronaut profile attributes
            name_val = request.POST.get('name', '').strip()
            rank_val = request.POST.get('rank', '').strip()
            habitat_val = request.POST.get('habitat', '').strip()
            role_val = request.POST.get('role_description', '').strip()

            if name_val:
                astronaut.name = name_val
            if rank_val:
                astronaut.rank = rank_val
            if habitat_val:
                astronaut.habitat = habitat_val
            if role_val:
                astronaut.role_description = role_val
            astronaut.save()

            # Only changed baselines are saved, and they become labelled manual overrides.
            save_baseline_changes(astronaut, baseline_changes, request.user)

            # Recompute active signals and thresholds in SQLite
            sync_alerts_for_astronaut(astronaut)
            success_message = "Clinical baselines and astronaut bio-file updated successfully in SQLite."
        except Exception as e:
            error_message = f"Error saving baselines: {str(e)}"

    # Fetch updated baselines
    details = baseline_details(astronaut)

    # Clinical rationale annotations
    clinical_notes = {
        'sleep': {
            'rationale': 'Restorative sleep target for neurobehavioral vigilance and psychomotor speed in microgravity.',
            'reference': '7.0 — 8.5 h',
        },
        'resting_hr': {
            'rationale': 'Basal autonomic cardiovascular setpoint recorded during resting circadian periods.',
            'reference': '50 — 70 bpm',
        },
        'hrv': {
            'rationale': 'RMSSD parasympathetic tone metric reflecting stress resilience and autonomic recovery.',
            'reference': '50 — 85 ms',
        },
        'exercise': {
            'rationale': 'Active microgravity countermeasure protocol to prevent musculoskeletal deconditioning.',
            'reference': '60 — 90 min/d',
        },
        'body_temp': {
            'rationale': 'Core thermal balance baseline for monitoring spaceflight circadian disruption and infection.',
            'reference': '36.4 — 37.2 °C',
        },
        'mood': {
            'rationale': 'Daily self-reported mood (1-5). The rule watches for drops below your own baseline.',
            'reference': '1 (lowest) — 5 (highest)',
        },
        'cabin_co2': {
            'rationale': 'Atmospheric life support setpoint for closed habitat environmental control loop.',
            'reference': '1,800 — 2,800 ppm',
        },
    }

    baseline_items = []
    for m_key in METRICS:
        cfg = METRIC_CONFIG.get(m_key, {})
        detail = details.get(m_key, {})
        val = detail.get('value', cfg.get('default_baseline', 0.0))
        note = clinical_notes.get(m_key, {})

        baseline_items.append({
            'key': m_key,
            'label': cfg.get('label', m_key),
            'unit': cfg.get('unit', ''),
            'icon': cfg.get('icon', 'insights'),
            'step': cfg.get('step', 1),
            'value': val,
            'kind': detail.get('kind', 'none'),     # manual / computed / stored / none
            'by': detail.get('by'),
            'at': detail.get('at'),
            'rationale': note.get('rationale', ''),
            'reference': note.get('reference', ''),
        })

    # The crew list is only shown (and only loaded) in DEMO_MODE.
    all_crew = AstronautProfile.objects.all().order_by('id') if settings.DEMO_MODE else AstronautProfile.objects.none()

    return render(request, 'core/profile.html', {
        'active_nav': 'profile',
        'current_astronaut': astronaut,
        'baseline_items': baseline_items,
        'all_crew': all_crew,
        'success_message': success_message,
        'error_message': error_message,
    })


def ground_support_view(request):
    """
    Approved Ground Support Briefing & DSN Telemetry View matching design/ground_support.html.
    Displays JSC Flight Surgeon brief, SVG HealthScoreRing (82/100), 2x2 grid of 7-day
    biometric vector charts, autonomous signals, simulated privacy mode, export controls,
    and interactive uplink messaging.
    """
    astronaut = get_active_astronaut(request)
    if not astronaut:
        return redirect('core:login')

    success_msg = None
    if request.method == 'POST':
        recipient = request.POST.get('recipient', 'Flight Surgeon (GND-SURG-2)').strip()
        priority = request.POST.get('priority', 'Routine').strip()
        subject = request.POST.get('subject', '').strip()
        body = request.POST.get('body', '').strip()

        if subject and body:
            callsign = "ARES-CMDR" if "Commander" in astronaut.rank else f"ARES-{astronaut.rank[:4].upper()}"
            GroundMessage.objects.create(
                astronaut=astronaut,
                sender=astronaut.full_title,
                callsign=callsign,
                subject=subject,
                body=body,
                timestamp=timezone.now(),
                mission_day=astronaut.current_mission_day,
                is_incoming=False,
                priority=priority,
                flight_surgeon_ack="Queued for DSN Uplink (Pass 0412)"
            )
            success_msg = f"Transmission packet logged and staged for DSN uplink pass. Estimated ground receipt in 18m 42s."

    # Baselines
    baselines = get_baselines(astronaut)

    # Recent 7 readings per metric for vector cards
    def get_recent_7(metric_name, default_vals):
        reads = list(
            Reading.objects.filter(astronaut=astronaut, metric=metric_name)
            .order_by('-mission_day', '-timestamp')[:7]
        )
        if len(reads) >= 4:
            return [r.value for r in reversed(reads)]
        return default_vals

    sleep_vals = get_recent_7('sleep', [7.2, 7.4, 7.1, 7.3, 6.9, 6.3, 5.8])
    hrv_vals = get_recent_7('hrv', [67, 66, 63, 64, 65, 63, 62])
    rhr_vals = get_recent_7('resting_hr', [56, 56, 57, 58, 57, 58, 58])
    stress_vals = get_recent_7('mood_stress', [3, 3, 4, 3, 4, 3, 3])

    sleep_latest = sleep_vals[-1] if sleep_vals else 5.8
    sleep_base = baselines.get('sleep', 7.2)
    sleep_delta = round(sleep_latest - sleep_base, 1)

    hrv_latest = int(round(hrv_vals[-1])) if hrv_vals else 62
    hrv_base = int(round(baselines.get('hrv', 66.0)))

    rhr_latest = int(round(rhr_vals[-1])) if rhr_vals else 58
    rhr_base = int(round(baselines.get('resting_hr', 56.0)))

    stress_latest = int(round(stress_vals[-1])) if stress_vals else 3
    stress_base = int(round(baselines.get('mood_stress', 3.0)))

    # CO2 latest reading
    co2_reading = Reading.objects.filter(astronaut=astronaut, metric='cabin_co2').order_by('-mission_day', '-timestamp').first()
    co2_val = int(round(co2_reading.value)) if co2_reading else 2450

    # Filter messages
    filter_type = request.GET.get('filter', 'all').lower()
    messages_query = GroundMessage.objects.filter(astronaut=astronaut).order_by('-timestamp')

    if filter_type == 'surgeon':
        messages_query = messages_query.filter(sender__icontains='Surgeon')
    elif filter_type == 'incoming':
        messages_query = messages_query.filter(is_incoming=True)
    elif filter_type == 'uplink':
        messages_query = messages_query.filter(is_incoming=False)

    return render(request, 'core/ground_support.html', {
        'active_nav': 'ground_support',
        'current_astronaut': astronaut,
        'health_score': 82,
        'health_circumference': 427.25,
        'health_dashoffset': 76.9,
        'physio_reserve': 88.4,
        'flight_surgeon_name': "Dr. Marcus Vance",
        'flight_surgeon_console': "Ground Console JSC-MED-1",
        'sleep_latest': f"{sleep_latest:.1f}",
        'sleep_base': f"{sleep_base:.1f}",
        'sleep_delta': f"{sleep_delta:+.1f}",
        'hrv_latest': hrv_latest,
        'hrv_base': hrv_base,
        'rhr_latest': rhr_latest,
        'rhr_base': rhr_base,
        'stress_latest': stress_latest,
        'stress_base': stress_base,
        'co2_latest': f"{co2_val:,}",
        'ground_messages': messages_query,
        'selected_filter': filter_type,
        'success_message': success_msg,
        'comm_latency': '18m 42s',
        'round_trip_delay': '37m 24s',
        'next_pass_countdown': 'T-01:14:22',
        'signal_integrity': '99.8%',
        'dsn_station': 'Goldstone DSS-14 (70m)',
    })
