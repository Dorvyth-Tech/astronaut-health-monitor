"""
Constants and configuration for Space-Pulse deep-space health monitoring.
"""

METRICS = [
    'resting_hr',
    'hrv',
    'sleep',
    'exercise',
    'body_temp',
    'mood_stress',
    'cabin_co2',
]

METRIC_CHOICES = [
    ('resting_hr', 'Resting Heart Rate'),
    ('hrv', 'Heart Rate Variability'),
    ('sleep', 'Sleep Duration'),
    ('exercise', 'Exercise Duration'),
    ('body_temp', 'Body Temperature'),
    ('mood_stress', 'Mood & Stress'),
    ('cabin_co2', 'Cabin CO2 Concentration'),
]

SEVERITY_CHOICES = [
    ('nominal', 'Nominal'),
    ('caution', 'Caution'),
    ('critical', 'Critical'),
]

# Baseline defaults and thresholds per NASA HRP prototype requirements
METRIC_CONFIG = {
    'resting_hr': {
        'label': 'Resting HR',
        'unit': 'bpm',
        'default_baseline': 58.0,
        'caution_high': 75.0,
        'critical_high': 95.0,
        'caution_low': 45.0,
        'icon': 'monitor_heart',
        'step': 1,
    },
    'hrv': {
        'label': 'HRV',
        'unit': 'ms',
        'default_baseline': 62.0,
        'caution_low': 45.0,
        'critical_low': 35.0,
        'icon': 'ecg',
        'step': 1,
    },
    'sleep': {
        'label': 'Sleep',
        'unit': 'h',
        'default_baseline': 7.2,
        'caution_low': 6.0,
        'critical_low': 4.5,
        'icon': 'bedtime',
        'step': 0.1,
    },
    'exercise': {
        'label': 'Exercise',
        'unit': 'min/d',
        'default_baseline': 60.0,
        'caution_low': 40.0,
        'critical_low': 20.0,
        'icon': 'fitness_center',
        'step': 1,
    },
    'body_temp': {
        'label': 'Body Temp',
        'unit': '°C',
        'default_baseline': 36.7,
        'caution_low': 36.0,
        'caution_high': 37.5,
        'critical_high': 38.5,
        'critical_low': 35.0,
        'icon': 'thermostat',
        'step': 0.1,
    },
    'mood_stress': {
        'label': 'Mood & Stress',
        'unit': '/10',
        'default_baseline': 3.0,  # stress level (1-10)
        'caution_high': 6.0,
        'critical_high': 8.0,
        'icon': 'sentiment_satisfied',
        'step': 1,
    },
    'cabin_co2': {
        'label': 'Cabin CO2',
        'unit': 'ppm',
        'default_baseline': 2450.0,
        'caution_high': 3000.0,
        'critical_high': 5000.0,
        'icon': 'air',
        'step': 25,
    },
}
