from datetime import datetime
from django.utils import timezone


def get_greeting():
    """Returns morning, afternoon, or evening based on current hour."""
    hour = timezone.now().hour
    if 5 <= hour < 12:
        return "Good morning"
    elif 12 <= hour < 18:
        return "Good afternoon"
    else:
        return "Good evening"


def spacepulse_context(request):
    """
    Context processor to provide global HUD telemetry context
    to base templates and all views.
    """
    from core.models import AstronautProfile
    from alerts.models import Alert

    astronaut = None
    try:
        # Check session override first
        active_id = request.session.get('active_astronaut_id')
        if active_id:
            astronaut = AstronautProfile.objects.filter(id=active_id).first()

        # If not found and user logged in, match by user email
        if not astronaut and hasattr(request, 'user') and request.user.is_authenticated:
            astronaut = AstronautProfile.objects.filter(email=request.user.email).first()

        # Fallback to designated active commander
        if not astronaut:
            astronaut = AstronautProfile.objects.filter(is_active_commander=True).first()

        # Final fallback
        if not astronaut:
            astronaut = AstronautProfile.objects.first()
    except Exception:
        pass

    unacknowledged_alerts_count = 0
    if astronaut:
        try:
            unacknowledged_alerts_count = Alert.objects.filter(
                astronaut=astronaut,
                acknowledged=False
            ).count()
        except Exception:
            pass

    return {
        'current_astronaut': astronaut,
        'greeting': get_greeting(),
        'unacknowledged_alerts_count': unacknowledged_alerts_count,
        'next_ground_pass': 'T-01:14:22',
        'comm_latency': '18m 42s',
        'orbital_sync': '99.8%',
    }
