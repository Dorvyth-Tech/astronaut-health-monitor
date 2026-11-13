from django.contrib import admin
from telemetry.models import Reading, CheckIn


@admin.register(Reading)
class ReadingAdmin(admin.ModelAdmin):
    list_display = ('astronaut', 'metric', 'value', 'mission_day', 'source', 'timestamp')
    list_filter = ('metric', 'source', 'astronaut')
    search_fields = ('astronaut__name', 'metric')


@admin.register(CheckIn)
class CheckInAdmin(admin.ModelAdmin):
    list_display = ('astronaut', 'mission_day', 'mood', 'stress', 'sleep_hours', 'exercise_minutes', 'timestamp')
    list_filter = ('astronaut', 'mission_day')
