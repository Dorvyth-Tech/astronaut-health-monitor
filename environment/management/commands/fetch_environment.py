"""Management command to fetch and synchronize environment space-weather events."""

from django.core.management.base import BaseCommand
from environment.services import fetch_space_weather
from environment.models import SpaceWeatherEvent


class Command(BaseCommand):
    help = "Fetches and synchronizes recent NASA DONKI events for the environment monitor."

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=7,
            help='Number of days of space weather history to retrieve (default: 7)',
        )

    def handle(self, *args, **options):
        days = options.get('days', 7)
        self.stdout.write(f"Fetching environment space-weather events for the past {days} days...")

        events, api_available = fetch_space_weather(days=days)

        status_str = "NASA Live API" if api_available else "Local Cache / Baseline"
        self.stdout.write(
            self.style.SUCCESS(
                f"Synchronized {len(events)} Space Weather Events ({status_str})."
            )
        )

        for e in events[:5]:
            source = "SAMPLE DATA" if e.is_sample else "NASA DONKI"
            self.stdout.write(
                f" - [{source}] [{e.get_severity_display()}] {e.intensity} "
                f"{e.get_event_type_display()} at {e.start_time.strftime('%Y-%m-%d %H:%M')}"
            )
