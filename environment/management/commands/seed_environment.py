"""
Management command to seed 24 hours of realistic environmental telemetry across
Earth, Orbit, Moon, and Mars.
"""

from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from faker import Faker

from environment.models import EnvironmentReading, EnvironmentAlert
from environment.services import (
    SIMULATION_PROFILES,
    check_thresholds,
    fetch_space_weather,
)


class Command(BaseCommand):
    help = "Seeds 24 hours of environmental telemetry and habitat readings for Earth, Orbit, Moon, and Mars."

    def add_arguments(self, parser):
        parser.add_argument(
            '--clean',
            action='store_true',
            help='Delete existing Environment readings before seeding',
        )

    def handle(self, *args, **options):
        fake = Faker()
        Faker.seed(2026)

        if options.get('clean', True):
            self.stdout.write("Clearing existing environment telemetry records...")
            EnvironmentAlert.objects.all().delete()
            EnvironmentReading.objects.all().delete()

        now = timezone.now()
        hours = 24
        total_created = 0

        # Base seed trajectories
        locations = ['earth', 'orbit', 'moon', 'mars']

        self.stdout.write("Generating 24-hour longitudinal atmospheric curves...")

        for loc in locations:
            # Starting values
            if loc == 'earth':
                temp = 22.0
                pressure = 101.3
                humidity = 55.0
                o2 = 20.95
                co2 = 415.0
                rad = 0.13
                is_sim = False
            else:
                p = SIMULATION_PROFILES[loc]
                temp = p['temp']['target']
                pressure = p['pressure']['target']
                humidity = p['humidity']['target']
                o2 = p['o2']['target']
                co2 = p['co2']['target']
                rad = p['rad']['target']
                is_sim = True

            for h in range(hours, -1, -1):
                timestamp = now - timedelta(hours=h)

                if loc == 'earth':
                    # Earth diurnal variation (warmer afternoon, cooler night)
                    diurnal = 3.5 * ((12 - abs(timestamp.hour - 14)) / 12)
                    current_temp = round(21.0 + diurnal + fake.pyfloat(min_value=-0.5, max_value=0.5), 1)
                    current_press = round(101.3 + fake.pyfloat(min_value=-0.2, max_value=0.2), 1)
                    current_hum = round(55.0 - (diurnal * 2) + fake.pyfloat(min_value=-1.0, max_value=1.0), 1)
                    current_o2 = 20.95
                    current_co2 = round(418.0 + fake.pyfloat(min_value=-5.0, max_value=5.0), 0)
                    current_rad = round(0.14 + fake.pyfloat(min_value=-0.02, max_value=0.02), 2)
                else:
                    prof = SIMULATION_PROFILES[loc]
                    # Walk gently
                    temp += fake.pyfloat(min_value=-prof['temp']['step'], max_value=prof['temp']['step'])
                    temp = max(prof['temp']['min'], min(prof['temp']['max'], temp))

                    pressure += fake.pyfloat(min_value=-prof['pressure']['step'], max_value=prof['pressure']['step'])
                    pressure = max(prof['pressure']['min'], min(prof['pressure']['max'], pressure))

                    humidity += fake.pyfloat(min_value=-prof['humidity']['step'], max_value=prof['humidity']['step'])
                    humidity = max(prof['humidity']['min'], min(prof['humidity']['max'], humidity))

                    o2 += fake.pyfloat(min_value=-prof['o2']['step'], max_value=prof['o2']['step'])
                    o2 = max(prof['o2']['min'], min(prof['o2']['max'], o2))

                    co2 += fake.pyfloat(min_value=-prof['co2']['step'], max_value=prof['co2']['step'])
                    co2 = max(prof['co2']['min'], min(prof['co2']['max'], co2))

                    rad += fake.pyfloat(min_value=-prof['rad']['step'], max_value=prof['rad']['step'])
                    rad = max(prof['rad']['min'], min(prof['rad']['max'], rad))

                    current_temp = round(temp, 1)
                    current_press = round(pressure, 1)
                    current_hum = round(humidity, 1)
                    current_o2 = round(o2, 1)
                    current_co2 = round(co2, 0)
                    current_rad = round(rad, 2)

                reading = EnvironmentReading.objects.create(
                    location=loc,
                    temperature=current_temp,
                    pressure=current_press,
                    humidity=current_hum,
                    oxygen_percent=current_o2,
                    co2_ppm=current_co2,
                    radiation_level=current_rad,
                    is_simulated=is_sim,
                    timestamp=timestamp,
                )
                total_created += 1

                # Check thresholds on recent readings so sample active alerts exist
                if h <= 4:
                    check_thresholds(reading)

        alert_count = EnvironmentAlert.objects.count()

        # Seed NASA DONKI space weather events
        self.stdout.write("Fetching & synchronizing NASA DONKI space weather events...")
        sw_events, _ = fetch_space_weather(days=7)
        sample_count = sum(event.is_sample for event in sw_events)

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully seeded {total_created} environmental readings, {alert_count} active safety alerts, and "
                f"{len(sw_events)} space weather events ({sample_count} SAMPLE DATA) across 4 locations!"
            )
        )
