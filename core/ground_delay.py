"""Earth-Mars communication delay figures, and the ground-panel context.

The one-way delay range comes from NASA (see core/services.py for the source
URLs): about 3 minutes at closest approach and up to about 22 minutes at the
farthest. It's a published range, not live telemetry, so the UI shows it as
a range. Pass countdowns and link quality have no real source here, so they
are labelled simulated rather than shown as if they were measured.
"""
from core.models import GroundMessage

ONE_WAY_MIN_MINUTES = 3
ONE_WAY_MAX_MINUTES = 22

COMM_LATENCY = f"{ONE_WAY_MIN_MINUTES}–{ONE_WAY_MAX_MINUTES} min"
ROUND_TRIP_DELAY = f"{ONE_WAY_MIN_MINUTES * 2}–{ONE_WAY_MAX_MINUTES * 2} min"
SIMULATED_NEXT_PASS = "T-01:14:22 (sim)"
SIMULATED_LINK_QUALITY = "99.8% (sim)"


def comm_context():
    """Header and ground-page values shared by every page."""
    return {
        'comm_latency': COMM_LATENCY,
        'round_trip_delay': ROUND_TRIP_DELAY,
        'next_ground_pass': SIMULATED_NEXT_PASS,
        'next_pass_countdown': SIMULATED_NEXT_PASS,
        'orbital_sync': SIMULATED_LINK_QUALITY,
        'signal_integrity': SIMULATED_LINK_QUALITY,
        'comm_one_way_min': ONE_WAY_MIN_MINUTES,
        'comm_one_way_max': ONE_WAY_MAX_MINUTES,
    }


def ground_panel_for(astronaut, active_alerts):
    """The dashboard's ground panel, filled from a packet that was really sent.

    Returns None (so the template drafts its own summary from the alerts)
    unless there's an active alert and an uplinked GroundMessage sent at or
    after the oldest one. An older packet is about something else.
    """
    alerts = list(active_alerts)
    if astronaut is None or not alerts:
        return None
    oldest = min(alert.timestamp for alert in alerts)
    packet = (
        GroundMessage.objects
        .filter(astronaut=astronaut, is_incoming=False, timestamp__gte=oldest)
        .order_by('-timestamp')
        .first()
    )
    if packet is None:
        return None
    return {
        'summary': [line for line in packet.body.splitlines() if line.strip()],
        'sent_at': packet.timestamp.isoformat(),
        'packet_id': f"GM-{packet.pk}",
        'one_way_min': ONE_WAY_MIN_MINUTES,
        'one_way_max': ONE_WAY_MAX_MINUTES,
    }


# The ground page draws its ring with r=68, so its circumference is 2*pi*68.
GROUND_RING_CIRCUMFERENCE = 427.26


def ground_ring(health):
    """Ground-page ring values from calculate_health_score's result.

    With no score ('--') the ring is empty, the same as on the dashboard.
    physio_reserve stays the team's experimental composite (the page labels
    it as not validated); it's taken from the same calculation, not typed in.
    """
    score = health['score']
    filled = score / 100.0 if isinstance(score, (int, float)) else 0.0
    reserve = str(health.get('physio_reserve', '')).rstrip('%')
    return {
        'health_score': score,
        'health_label': health['label'],
        'health_status': health['status'],
        'health_color': health['color'],
        'health_circumference': GROUND_RING_CIRCUMFERENCE,
        'health_dashoffset': round(GROUND_RING_CIRCUMFERENCE * (1 - filled), 2),
        'physio_reserve': reserve,
    }
