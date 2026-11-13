from django.db import models
from django.utils import timezone
from core.constants import METRIC_CHOICES, SEVERITY_CHOICES


class Alert(models.Model):
    """
    Automated or threshold-triggered health and safety signals.
    """
    astronaut = models.ForeignKey(
        'core.AstronautProfile',
        on_delete=models.CASCADE,
        related_name='alerts'
    )
    metric = models.CharField(max_length=30, choices=METRIC_CHOICES)
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default='nominal')
    title = models.CharField(max_length=200)
    message = models.TextField()
    mission_day = models.PositiveIntegerField(default=142)
    timestamp = models.DateTimeField(default=timezone.now)
    acknowledged = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Alert"
        verbose_name_plural = "Alerts"
        ordering = ['acknowledged', '-timestamp', '-id']

    def __str__(self):
        return f"[{self.severity.upper()}] {self.title} (Day {self.mission_day})"
