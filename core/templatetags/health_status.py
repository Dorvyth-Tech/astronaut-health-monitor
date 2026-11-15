"""Status pill, ring colour and headline for the Ground Support summary card.

    {% load health_status %}
    {% health_status health_status health_label health_color as hs %}

Inputs are the context keys `health_status`, `health_label` and `health_color` (the same
values the dashboard ring uses, from core.services.calculate_health_score). Any of them can
be missing or odd: the result then falls back to a "No data" state, never a fake "Nominal".
The headline is a fixed sentence per status. It makes no trend claim and gives no diagnosis.
"""

import re

from django import template

register = template.Library()

_HEX = re.compile(r'^#[0-9a-fA-F]{3,8}$')

# status -> pill background, pill text, dot background, ring colour, default label, headline
# (pill classes match core.services.SEVERITY_STYLE, the dashboard's badges)
STATUS_STYLE = {
    'nominal': {
        'pill': 'bg-[rgba(34,227,160,0.15)] text-[#22E3A0]',
        'dot': 'bg-[#22E3A0]',
        'color': '#22E3A0',
        'label': 'Nominal',
        'headline': 'Overall health score is in the nominal range.',
    },
    'caution': {
        'pill': 'bg-[rgba(255,176,32,0.2)] text-[#FFB020]',
        'dot': 'bg-[#FFB020]',
        'color': '#FFB020',
        'label': 'Caution',
        'headline': 'Overall health score is in the caution range. See alerts for steps.',
    },
    'critical': {
        'pill': 'bg-error-container/60 text-error',
        'dot': 'bg-error',
        'color': '#ffb4ab',
        'label': 'Critical',
        'headline': 'Overall health score is in the critical range. See alerts for steps.',
    },
    'no_data': {
        'pill': 'bg-surface-container-highest text-outline',
        'dot': 'bg-outline',
        'color': '#8E9AAF',
        'label': 'No data',
        'headline': 'Not enough baseline data yet to show an overall health score.',
    },
}


@register.simple_tag
def health_status(status=None, label=None, color=None):
    key = status if status in STATUS_STYLE else 'no_data'
    style = STATUS_STYLE[key]
    return {
        'status': key,
        # Show the backend label for a known status; a missing one falls back to the default.
        'label': (str(label).strip() if key != 'no_data' and label else '') or style['label'],
        'pill': style['pill'],
        'dot': style['dot'],
        'color': color if isinstance(color, str) and _HEX.match(color) and key != 'no_data' else style['color'],
        'headline': style['headline'],
        'pulse': key != 'no_data',
    }
