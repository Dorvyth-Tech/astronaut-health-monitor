"""Clickable body map for the dashboard (hand-made SVG, see partials/body_figure.html).

    {% load body_figure %}
    {% body_figure metric_cards %}

Each region links to the dashboard indicator card (id="metric-<key>") and is coloured by
that card's existing status (nominal / caution / critical / no_data). A region that maps to
two indicators takes the worse status and links to the first one; the list beside the
figure links to every indicator. Display only: it never changes a status.
"""

from django import template

register = template.Library()

# status -> (label shown to the user, rank for "worst of")
_STATUS = {
    'critical': ('Critical', 3),
    'caution': ('Caution', 2),
    'nominal': ('Nominal', 1),
    'no_data': ('No data', 0),
}

# (region id, label, indicator keys in link order)
REGIONS = (
    ('head', 'Head', ('sleep', 'mood')),
    ('chest', 'Chest', ('resting_hr', 'hrv')),
    ('limbs', 'Arms and legs', ('exercise',)),
    ('skin', 'Skin', ('body_temp',)),
)


def _status_of(card):
    status = card.get('status') if card else None
    return status if status in _STATUS else 'no_data'


@register.inclusion_tag('partials/body_figure.html')
def body_figure(metric_cards):
    by_key = {card['key']: card for card in (metric_cards or [])}
    regions = []
    for region_id, label, keys in REGIONS:
        cards = [by_key[k] for k in keys if k in by_key]
        if not cards:
            continue
        status = max((_status_of(c) for c in cards), key=lambda s: _STATUS[s][1])
        regions.append({
            'id': region_id,
            'label': label,
            'status': status,
            'status_label': _STATUS[status][0],
            'first_key': cards[0]['key'],
            'cards': [
                {
                    'key': c['key'],
                    'label': c['label'],
                    'value': f"{c['formatted_value']} {c['unit']}".strip(),
                    'status': _status_of(c),
                    'status_label': _STATUS[_status_of(c)][0],
                    'has_data': _status_of(c) != 'no_data',
                }
                for c in cards
            ],
            'indicators': ' and '.join(c['label'] for c in cards),
        })
    return {'regions': regions, 'by_id': {r['id']: r for r in regions}}
