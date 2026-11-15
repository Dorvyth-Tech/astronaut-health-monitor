from django.contrib import admin
from .models import EnvironmentReading, EnvironmentAlert, SpaceWeatherEvent


@admin.register(EnvironmentReading)
class EnvironmentReadingAdmin(admin.ModelAdmin):
    list_display = ('location', 'temperature', 'pressure', 'co2_ppm', 'oxygen_percent', 'radiation_level', 'is_simulated', 'timestamp')
    list_filter = ('location', 'is_simulated')
    ordering = ('-timestamp',)


@admin.register(EnvironmentAlert)
class EnvironmentAlertAdmin(admin.ModelAdmin):
    list_display = ('message', 'severity', 'resolved', 'created_at')
    list_filter = ('severity', 'resolved')
    ordering = ('-created_at',)


@admin.register(SpaceWeatherEvent)
class SpaceWeatherEventAdmin(admin.ModelAdmin):
    list_display = ('external_id', 'event_type', 'intensity', 'severity', 'start_time')
    list_filter = ('event_type', 'severity')
    ordering = ('-start_time',)
