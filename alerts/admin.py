from django.contrib import admin
from alerts.models import Alert


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ('title', 'astronaut', 'metric', 'severity', 'mission_day', 'acknowledged', 'timestamp')
    list_filter = ('severity', 'acknowledged', 'metric')
    search_fields = ('title', 'message', 'astronaut__name')
