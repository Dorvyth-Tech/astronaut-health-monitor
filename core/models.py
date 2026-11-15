from django.conf import settings
from django.db import models
from core.constants import METRIC_CHOICES


class AstronautProfile(models.Model):
    """
    Represents an astronaut on the deep-space mission.
    """
    name = models.CharField(max_length=120)
    email = models.EmailField(blank=True, default='')
    rank = models.CharField(max_length=80, default="Commander")
    mission_name = models.CharField(
        max_length=150,
        default="Deep-Space Transit Mars Transit Hab-1"
    )
    mission_total_days = models.PositiveIntegerField(default=540)
    current_mission_day = models.PositiveIntegerField(default=142)
    habitat = models.CharField(max_length=80, default="HAB-01")
    is_active_commander = models.BooleanField(
        default=False,
        help_text="Designates the current active crew view on the local terminal"
    )
    role_description = models.CharField(
        max_length=150,
        blank=True,
        default="Commander & Lead Astrobiologist"
    )

    class Meta:
        verbose_name = "Astronaut Profile"
        verbose_name_plural = "Astronaut Profiles"

    def __str__(self):
        return f"{self.rank} {self.name}"

    @property
    def mission_progress_pct(self):
        """Calculate transit progress percentage."""
        if not self.mission_total_days:
            return 0
        return int(round((self.current_mission_day / self.mission_total_days) * 100))

    @property
    def full_title(self):
        return f"{self.rank} {self.name}"


class Baseline(models.Model):
    """
    Personal baseline value for an astronaut on a specific biometric/environmental metric.
    """
    astronaut = models.ForeignKey(
        AstronautProfile,
        on_delete=models.CASCADE,
        related_name='baselines'
    )
    metric = models.CharField(max_length=30, choices=METRIC_CHOICES)
    baseline_value = models.FloatField()

    # By default a baseline is COMPUTED from mission days 1-14 (core/services.py).
    # Someone may type their own value instead. That is allowed, but it is always
    # flagged here and shown as "Manual override" in the profile.
    is_manual_override = models.BooleanField(default=False)
    overridden_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )
    overridden_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Baseline"
        verbose_name_plural = "Baselines"
        unique_together = ('astronaut', 'metric')

    def __str__(self):
        return f"{self.astronaut.name} - {self.metric}: {self.baseline_value}"


class GroundMessage(models.Model):
    """
    Ground support communications and telemetry packets.
    """
    PRIORITY_CHOICES = [
        ('Routine', 'Routine'),
        ('Priority', 'Priority'),
        ('Critical', 'Critical'),
    ]

    astronaut = models.ForeignKey(
        AstronautProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ground_messages'
    )
    sender = models.CharField(max_length=120, default="Houston CapCom")
    callsign = models.CharField(max_length=40, default="HOU-CAPCOM")
    subject = models.CharField(max_length=200)
    body = models.TextField()
    timestamp = models.DateTimeField()
    mission_day = models.PositiveIntegerField(default=142)
    is_incoming = models.BooleanField(default=True, help_text="True if ground to hab, False if uplinked")
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='Routine')
    flight_surgeon_ack = models.CharField(
        max_length=80,
        blank=True,
        default="Acknowledged (GND-SURG-2)"
    )

    class Meta:
        verbose_name = "Ground Message"
        verbose_name_plural = "Ground Messages"
        ordering = ['-timestamp']

    def __str__(self):
        return f"[Day {self.mission_day}] {self.sender}: {self.subject}"
