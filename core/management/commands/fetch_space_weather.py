"""
Refresh the space-weather snapshot from NASA DONKI (FLR, SEP, GST; last 7 days).

  python manage.py fetch_space_weather            fetch now and store a snapshot
  python manage.py fetch_space_weather --sample   store a labelled SAMPLE snapshot (offline demo)

Run it by hand before a demo or from cron. The dashboard also refreshes by itself
(at most once per 30 minutes), so this is optional. The NASA key is NASA_API_KEY
(falls back to DEMO_KEY) and is never printed.
"""

from django.core.management.base import BaseCommand, CommandError

from core import donki


class Command(BaseCommand):
    help = 'Fetch the last 7 days of NASA DONKI space weather and store it as the latest snapshot.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--sample', action='store_true',
            help="Store an invented, clearly labelled SAMPLE snapshot (source='sample') instead of calling NASA.",
        )

    def handle(self, *args, **options):
        if options['sample']:
            donki.save_snapshot(donki.sample_summary(), donki.SOURCE_SAMPLE)
            self.stdout.write(self.style.WARNING("Stored a SAMPLE space-weather snapshot (source='sample')."))
            return

        view = donki.refresh_space_weather()
        if view is None:
            raise CommandError(
                'Could not reach NASA DONKI (rate limit, outage or no network). '
                'The last stored snapshot, if any, stays in use.'
            )
        shown = donki.describe(view)
        self.stdout.write(self.style.SUCCESS('Space weather snapshot stored.'))
        self.stdout.write(f"Flares:  {shown['flare_main']}  ({shown['flare_sub']})")
        self.stdout.write(f"SEP:     {shown['sep_main']}  ({shown['sep_sub']})")
        self.stdout.write(f"Kp:      {shown['kp_main']}  ({shown['kp_sub']})")
        self.stdout.write(shown['status'])
        for error in view['summary'].get('errors', {}).values():
            self.stdout.write(self.style.WARNING(f'Partial data: {error}'))
