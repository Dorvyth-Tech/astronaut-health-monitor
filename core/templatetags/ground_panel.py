"""'Ground is ~20 minutes away' panel (alert detail page and dashboard).

An inclusion tag, so the panel works on any page without touching a view:

    {% load ground_panel %}
    {% ground_delay_panel alert=alert %}            {# alert detail #}
    {% ground_delay_panel alerts=active_alerts link=True %}   {# dashboard #}

Everything is built from data the app already has: the verbatim Science Lead alert
wording (core.alert_texts) and the alert's own latest reading and baseline. If the backend
later supplies a `ground_panel` dict in the template context, its keys win over the
fallbacks (see ground_panel_overrides below). Nothing is saved or sent from here: the
"sent" time and the countdown are a simulated uplink kept in the browser.
"""

from django import template

from core.alert_texts import INDICATOR_TITLES, DISPLAY_STATE, parts_for, state_from_severity
from core.constants import METRIC_CONFIG

register = template.Library()

# NASA: one-way light time between Earth and Mars is roughly 3 to 22 minutes,
# depending on where the two planets are in their orbits.
ONE_WAY_MIN_MINUTES = 3
ONE_WAY_MAX_MINUTES = 22

_SEVERITY_RANK = {'critical': 2, 'caution': 1}

STEP_LABELS = (
    ('what', 'What changed', 'troubleshoot'),
    ('why', 'Why it matters', 'psychology'),
    ('action', 'Do this now', 'playlist_add_check'),
    ('escalate', 'When to escalate', 'fmd_bad'),
)


def _pick_alert(alert, alerts):
    """The alert to build the panel for: the one given, else the worst unacknowledged one."""
    if alert is not None:
        return alert if parts_for(alert.metric, alert.severity) else None
    best = None
    for candidate in alerts or []:
        if getattr(candidate, 'acknowledged', False):
            continue
        rank = _SEVERITY_RANK.get(candidate.severity, 0)
        if not rank or not parts_for(candidate.metric, candidate.severity):
            continue
        if best is None or rank > _SEVERITY_RANK[best.severity]:
            best = candidate
    return best


def _latest_vs_baseline(alert):
    """'5.2 h vs baseline 7.1 h' from stored readings, or '' when there is no data."""
    try:
        from telemetry.models import Reading
        from core.services import get_baselines

        astronaut = alert.astronaut
        reading = (
            Reading.objects.filter(astronaut=astronaut, metric=alert.metric)
            .order_by('-mission_day', '-timestamp')
            .first()
        )
        if reading is None:
            return ''
        unit = METRIC_CONFIG.get(alert.metric, {}).get('unit', '')
        text = f"{reading.value:g} {unit}".strip()
        baseline = get_baselines(astronaut).get(alert.metric)
        if baseline is not None:
            text += f" vs baseline {baseline:g} {unit}".rstrip()
        return text
    except Exception:
        return ''


def _draft_packet(alert, parts, simulated):
    astronaut = alert.astronaut
    indicator = INDICATOR_TITLES.get(alert.metric, alert.metric)
    state = DISPLAY_STATE[state_from_severity(alert.severity)]
    lines = [
        f"To: Flight surgeon (ground)",
        f"Re: {indicator}, {state.lower()} flag, mission day {alert.mission_day}",
        f"Crew: {astronaut.rank} {astronaut.name}",
        f"Flag: rule-based status from the Science Lead table. This is not a diagnosis.",
        f"Observed: {parts['what']}",
    ]
    latest = _latest_vs_baseline(alert)
    if latest:
        lines.append(f"Latest: {latest}")
    lines.append(f"Planned on-board step: {parts['action']}")
    lines.append(
        "Data: simulated crew data (prototype)." if simulated else "Data: crew telemetry."
    )
    return lines


@register.inclusion_tag('partials/ground_delay_panel.html', takes_context=True)
def ground_delay_panel(context, alert=None, alerts=None, link=False):
    chosen = _pick_alert(alert, alerts)
    if chosen is None:
        return {'panel': None}

    parts = parts_for(chosen.metric, chosen.severity)
    simulated = bool(context.get('is_simulated_data') or context.get('public_demo'))

    # Optional backend override. Every key is optional; each has the fallback described here.
    #   summary      list of lines (or one string)  -> auto-drafted lines above
    #   sent_at      ISO-8601 string                -> '' (not sent: "Queue packet" starts it)
    #   packet_id    string                         -> not shown
    #   one_way_min  minutes, number                -> 3
    #   one_way_max  minutes, number                -> 22
    override = context.get('ground_panel') or {}

    summary = override.get('summary') or _draft_packet(chosen, parts, simulated)
    if isinstance(summary, str):
        summary = summary.splitlines()

    one_way_min = override.get('one_way_min', ONE_WAY_MIN_MINUTES)
    one_way_max = override.get('one_way_max', ONE_WAY_MAX_MINUTES)

    return {
        'panel': {
            'alert': chosen,
            'severity': chosen.severity,
            'indicator': INDICATOR_TITLES.get(chosen.metric, chosen.metric),
            'steps': [
                {'key': key, 'label': label, 'icon': icon, 'text': parts[key]}
                for key, label, icon in STEP_LABELS
            ],
            'summary': summary,
            'packet_id': override.get('packet_id', ''),
            'sent_at': override.get('sent_at', ''),
            'one_way_min': one_way_min,
            'one_way_max': one_way_max,
            'round_trip_min': one_way_min * 2,
            'round_trip_max': one_way_max * 2,
            'simulated': simulated,
        },
        'comm_latency': context.get('comm_latency', ''),
        'show_link': link,
    }
