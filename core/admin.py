from django.contrib import admin
from core.models import AstronautProfile, Baseline, GroundMessage, SpaceWeatherSnapshot


@admin.register(AstronautProfile)
class AstronautProfileAdmin(admin.ModelAdmin):
    list_display = ('name', 'rank', 'habitat', 'current_mission_day', 'mission_total_days', 'is_active_commander')
    list_filter = ('habitat', 'is_active_commander')
    search_fields = ('name', 'rank')


@admin.register(Baseline)
class BaselineAdmin(admin.ModelAdmin):
    list_display = ('astronaut', 'metric', 'baseline_value')
    list_filter = ('metric', 'astronaut')


@admin.register(GroundMessage)
class GroundMessageAdmin(admin.ModelAdmin):
    list_display = ('sender', 'callsign', 'subject', 'mission_day', 'priority', 'timestamp', 'is_incoming')
    list_filter = ('priority', 'is_incoming')
    search_fields = ('subject', 'body', 'sender')


@admin.register(SpaceWeatherSnapshot)
class SpaceWeatherSnapshotAdmin(admin.ModelAdmin):
    list_display = ('source', 'fetched_at')
    list_filter = ('source',)
