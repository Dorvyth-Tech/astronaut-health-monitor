from datetime import datetime
from django.conf import settings
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
    from core.views import get_active_astronaut
    from alerts.models import Alert
    from telemetry.models import Reading

    astronaut = None
    try:
        astronaut = get_active_astronaut(request)
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

    # True when any of this astronaut's readings is simulated: base.html then
    # shows the "Simulated data" banner (template contract: is_simulated).
    is_simulated_data = False
    if astronaut:
        try:
            is_simulated_data = Reading.objects.filter(astronaut=astronaut, source='simulated').exists()
        except Exception:
            pass

    from core.demo import READ_ONLY_MESSAGE, is_public_demo_user
    public_demo = is_public_demo_user(getattr(request, 'user', None))

    return {
        'demo_mode': settings.DEMO_MODE,
        # True only for /demo/ visitors: base.html shows the read-only banner.
        'public_demo': public_demo,
        'public_demo_message': READ_ONLY_MESSAGE,
        'is_simulated_data': is_simulated_data,
        'current_astronaut': astronaut,
        'greeting': get_greeting(),
        'unacknowledged_alerts_count': unacknowledged_alerts_count,
        'next_ground_pass': 'T-01:14:22',
        'comm_latency': '18m 42s',
        'orbital_sync': '99.8%',
    }
