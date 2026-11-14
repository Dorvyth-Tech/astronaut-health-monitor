"""
Constants and configuration for Space-Pulse deep-space health monitoring.
"""

METRICS = [
    'resting_hr',
    'hrv',
    'sleep',
    'exercise',
    'body_temp',
    'mood',
    'cabin_co2',
]

# Exercise plan from the Science Lead table: 150 minutes of resistance exercise
# per week. The exercise rule compares each completed week against this number.
EXERCISE_WEEKLY_PLAN_MINUTES = 150

METRIC_CHOICES = [
    ('resting_hr', 'Resting Heart Rate'),
    ('hrv', 'Heart Rate Variability'),
    ('sleep', 'Sleep Duration'),
    ('exercise', 'Exercise Duration'),
    ('body_temp', 'Body Temperature'),
    ('mood', 'Mood'),
    ('mood_stress', 'Stress (self-report)'),
    ('cabin_co2', 'Cabin CO2 Concentration'),
]

SEVERITY_CHOICES = [
    ('nominal', 'Nominal'),
    ('caution', 'Caution'),
    ('critical', 'Critical'),
]

# Display metadata per metric (label, unit, icon, step) plus a DISPLAY-ONLY default
# baseline. Status thresholds no longer live here: they are baseline-relative
# rules in core/rules.py (Science Lead table).
METRIC_CONFIG = {
    'resting_hr': {
        'label': 'Resting HR',
        'unit': 'bpm',
        'default_baseline': 58.0,
        'icon': 'monitor_heart',
        'step': 1,
    },
    'hrv': {
        'label': 'HRV',
        'unit': 'ms',
        'default_baseline': 62.0,
        'icon': 'ecg',
        'step': 1,
    },
    'sleep': {
        'label': 'Sleep',
        'unit': 'h',
        'default_baseline': 7.2,
        'icon': 'bedtime',
        'step': 0.1,
    },
    'exercise': {
        'label': 'Exercise',
        'unit': 'min/d',
        'default_baseline': 60.0,
        'icon': 'fitness_center',
        'step': 1,
    },
    'body_temp': {
        'label': 'Body Temp',
        'unit': '°C',
        'default_baseline': 36.7,
        'icon': 'thermostat',
        'step': 0.1,
    },
    # Mood is one of the 7 scored indicators: the daily 1-5 score from the check-in.
    'mood': {
        'label': 'Mood',
        'unit': '/5',
        'default_baseline': 4.0,
        'icon': 'sentiment_satisfied',
        'step': 0.1,
    },
    # Stress (1-10) is NOT one of the 7 indicators (it has no science rule). It is
    # kept as a self-report reading for the ground-support page and history.
    'mood_stress': {
        'label': 'Stress',
        'unit': '/10',
        'default_baseline': 3.0,
        'icon': 'psychology',
        'step': 1,
    },
    'cabin_co2': {
        'label': 'Cabin CO2',
        'unit': 'ppm',
        'default_baseline': 2450.0,
        'icon': 'air',
        'step': 25,
    },
}
