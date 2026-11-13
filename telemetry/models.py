from django.db import models
from django.utils import timezone
from core.constants import METRIC_CHOICES


class Reading(models.Model):
    """
    Biometric or habitat telemetry reading for an astronaut.
    """
    SOURCE_CHOICES = [
        ('sensor', 'Sensor'),
        ('manual', 'Manual'),
    ]

    astronaut = models.ForeignKey(
        'core.AstronautProfile',
        on_delete=models.CASCADE,
        related_name='readings'
    )
    metric = models.CharField(max_length=30, choices=METRIC_CHOICES, db_index=True)
    value = models.FloatField()
    mission_day = models.PositiveIntegerField(db_index=True)
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='sensor')

    class Meta:
        verbose_name = "Reading"
        verbose_name_plural = "Readings"
        ordering = ['mission_day', 'timestamp']
        indexes = [
            models.Index(fields=['astronaut', 'metric', 'mission_day']),
        ]

    def __str__(self):
        return f"[D{self.mission_day}] {self.astronaut.name} - {self.metric}: {self.value}"


class CheckIn(models.Model):
    """
    Daily self-reported evaluation logged by the astronaut.
    """
    astronaut = models.ForeignKey(
        'core.AstronautProfile',
        on_delete=models.CASCADE,
        related_name='checkins'
    )
    mission_day = models.PositiveIntegerField(db_index=True)
    mood = models.PositiveSmallIntegerField(help_text="Mood score from 1 (lowest) to 5 (highest)")
    stress = models.PositiveSmallIntegerField(help_text="Stress score from 1 (lowest) to 10 (highest)")
    sleep_hours = models.FloatField(help_text="Hours slept")
    exercise_minutes = models.PositiveIntegerField(help_text="Minutes of physical exercise")

    # Optional clinical measurements at check-in
    resting_hr = models.FloatField(null=True, blank=True, help_text="Resting heart rate in bpm")
    hrv = models.FloatField(null=True, blank=True, help_text="Heart rate variability in ms")
    body_temp = models.FloatField(null=True, blank=True, help_text="Body temperature in °C")

    notes = models.TextField(blank=True, default='')
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Check-In"
        verbose_name_plural = "Check-Ins"
        ordering = ['-mission_day', '-timestamp']

    def __str__(self):
        return f"Check-In [Day {self.mission_day}] - {self.astronaut.name}"
