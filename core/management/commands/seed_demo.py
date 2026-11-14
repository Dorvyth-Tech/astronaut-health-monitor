"""
Management command to seed reproducible demo data for Space-Pulse.
NASA Space Apps Challenge 2026.
"""

import math
import random
from datetime import timedelta
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from faker import Faker

from core.models import AstronautProfile, Baseline, GroundMessage
from telemetry.models import Reading, CheckIn
from alerts.models import Alert
from core import rules
from core.constants import METRIC_CONFIG, METRICS
from core.services import (
    evaluate_metric_status,
    readings_to_series,
    sync_alerts_for_astronaut,
)
from django.contrib.auth.models import User


# --- scenario "sleep_decline" (the 30-second demo story, day 142) -----------
# Cmdr. Reyes' last 7 nights (days 136-142). The baseline is ~7.2 h, so
# 6.0 / 5.9 / 5.8 are each more than 1 h below it on 3 nights in a row ending
# today: AMBER by the science table (Sleep row), no trend rule needed.
SCENARIO_SLEEP_NIGHTS = [7.2, 7.4, 7.3, 6.9, 6.0, 5.9, 5.8]
# A short resting-HR episode on days 128-130 (about +14-16% for 3 days = amber),
# back to normal afterwards. It becomes the older, already acknowledged alert.
SCENARIO_HR_EPISODE = {128: 66.0, 129: 67.0, 130: 67.0}
SCENARIO_NAMES = ('sleep_decline',)


class Command(BaseCommand):
    help = (
        'Seeds database with reproducible demo telemetry for Cmdr. Reyes and crew. '
        'Use --scenario=sleep_decline for the demo story computed by the science rules. '
        'WARNING: this DELETES all existing missions, readings, alerts and users first.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--scenario',
            choices=SCENARIO_NAMES,
            default=None,
            help='sleep_decline: day 142, a 3-night sleep decline (amber), hourly cabin CO2, '
                 'baselines computed from days 1-14, alerts produced by the rules.',
        )

    def handle(self, *args, **options):
        scenario = options.get('scenario')
        # call_command(scenario=...) skips argparse's choices check, and nothing
        # must be deleted for a typo, so validate before the flush below.
        if scenario and scenario not in SCENARIO_NAMES:
            raise CommandError(f"Unknown scenario {scenario!r}. Choose from: {', '.join(SCENARIO_NAMES)}.")
        if scenario:
            self.stdout.write(self.style.WARNING(f"Scenario: {scenario}"))
        self.stdout.write(self.style.WARNING("Flushing existing mission telemetry..."))
        Alert.objects.all().delete()
        CheckIn.objects.all().delete()
        Reading.objects.all().delete()
        Baseline.objects.all().delete()
        GroundMessage.objects.all().delete()
        AstronautProfile.objects.all().delete()

        # Seed random and Faker for 100% reproducible data
        random.seed(42)
        Faker.seed(42)
        fake = Faker()

        self.stdout.write(self.style.MIGRATE_HEADING("Creating Astronaut Crew Profiles..."))

        # 1. Commander Amara Reyes (Primary User)
        cmdr = AstronautProfile.objects.create(
            name="Amara Reyes",
            email="amara.reyes@mission.example",
            rank="Cmdr.",
            mission_name="Deep-Space Transit Mars Transit Hab-1",
            mission_total_days=540,
            current_mission_day=142,
            habitat="HAB-01",
            is_active_commander=True,
            role_description="Mission Commander & Lead Astrobiologist"
        )

        # 3 Other Crew Members
        crew_members = [
            AstronautProfile.objects.create(
                name="Leo Vance",
                email="flight.surgeon@jsc.nasa.gov",
                rank="Dr.",
                mission_name="Deep-Space Transit Mars Transit Hab-1",
                mission_total_days=540,
                current_mission_day=142,
                habitat="HAB-01",
                is_active_commander=False,
                role_description="Flight Surgeon & Medical Officer"
            ),
            AstronautProfile.objects.create(
                name="Maya Chen",
                email="maya.chen@mission.example",
                rank="Lt.",
                mission_name="Deep-Space Transit Mars Transit Hab-1",
                mission_total_days=540,
                current_mission_day=142,
                habitat="HAB-01",
                is_active_commander=False,
                role_description="Life Support & Systems Engineer"
            ),
            AstronautProfile.objects.create(
                name="Alex Mercer",
                email="alex.mercer@mission.example",
                rank="Dr.",
                mission_name="Deep-Space Transit Mars Transit Hab-1",
                mission_total_days=540,
                current_mission_day=142,
                habitat="HAB-01",
                is_active_commander=False,
                role_description="Orbital Navigator & EVA Specialist"
            ),
        ]

        all_astronauts = [cmdr] + crew_members

        # Create or update corresponding Django Users for login
        User.objects.all().delete()
        for astro in all_astronauts:
            username = astro.email.split('@')[0]
            user = User.objects.create_user(
                username=username,
                email=astro.email,
                password="validAccess2026",
                first_name=astro.rank,
                last_name=astro.name
            )
            if astro.is_active_commander:
                user.is_staff = True
                user.is_superuser = True
                user.save()

        # Personal baselines for each astronaut
        baseline_templates = {
            cmdr.id: {
                'resting_hr': 58.0,
                'hrv': 62.0,
                'sleep': 7.2,
                'exercise': 60.0,
                'body_temp': 36.7,
                'mood_stress': 3.0,
                'cabin_co2': 2300.0,
            },
            crew_members[0].id: {
                'resting_hr': 62.0,
                'hrv': 58.0,
                'sleep': 7.5,
                'exercise': 60.0,
                'body_temp': 36.6,
                'mood_stress': 2.5,
                'cabin_co2': 2300.0,
            },
            crew_members[1].id: {
                'resting_hr': 55.0,
                'hrv': 68.0,
                'sleep': 7.0,
                'exercise': 65.0,
                'body_temp': 36.8,
                'mood_stress': 3.2,
                'cabin_co2': 2300.0,
            },
            crew_members[2].id: {
                'resting_hr': 60.0,
                'hrv': 64.0,
                'sleep': 7.4,
                'exercise': 55.0,
                'body_temp': 36.7,
                'mood_stress': 3.0,
                'cabin_co2': 2300.0,
            },
        }

        self.stdout.write("Setting baseline biometric parameters...")
        for astro in all_astronauts:
            b_vals = baseline_templates[astro.id]
            for metric_key, val in b_vals.items():
                Baseline.objects.create(
                    astronaut=astro,
                    metric=metric_key,
                    baseline_value=val
                )

        # Generate 142 days of telemetry
        self.stdout.write("Generating 142 days of bio-telemetry and habitability readings...")
        base_time = timezone.now() - timedelta(days=142)

        readings_batch = []
        checkins_batch = []

        # Exact approved values for Cmdr. Reyes last 7 days (Days 136 to 142)
        # Matches design reference sparklines and charts
        cmdr_last_7 = {
            'resting_hr': [56.0, 57.0, 56.0, 57.0, 58.0, 57.0, 58.0],
            'hrv': [67.0, 65.0, 66.0, 64.0, 63.0, 63.0, 62.0],
            'sleep': [7.2, 7.4, 7.1, 7.3, 6.9, 6.3, 5.8],  # Declined 4 days in a row!
            'exercise': [62.0, 58.0, 60.0, 55.0, 59.0, 56.0, 57.0],
            'body_temp': [36.7, 36.7, 36.7, 36.7, 36.7, 36.7, 36.7],
            'mood_stress': [3.0, 3.0, 4.0, 3.0, 4.0, 3.0, 3.0],  # Stress score
            'cabin_co2': [2300.0, 2350.0, 2400.0, 2380.0, 2420.0, 2440.0, 2450.0],
        }

        if scenario == 'sleep_decline':
            cmdr_last_7['sleep'] = SCENARIO_SLEEP_NIGHTS

        for astro in all_astronauts:
            b_vals = baseline_templates[astro.id]

            for day in range(1, 143):
                day_timestamp = base_time + timedelta(days=day, hours=7, minutes=45)

                # Determine metrics for this day
                is_cmdr_last_7 = (astro == cmdr and day >= 136)
                last_7_idx = day - 136 if is_cmdr_last_7 else None

                # 1. Sleep
                if is_cmdr_last_7:
                    sleep_val = cmdr_last_7['sleep'][last_7_idx]
                else:
                    sleep_val = round(b_vals['sleep'] + random.uniform(-0.6, 0.6), 1)

                # 2. Resting HR
                if is_cmdr_last_7:
                    rhr_val = cmdr_last_7['resting_hr'][last_7_idx]
                else:
                    rhr_val = round(b_vals['resting_hr'] + random.uniform(-3, 3), 1)

                if scenario and astro == cmdr and day in SCENARIO_HR_EPISODE:
                    rhr_val = SCENARIO_HR_EPISODE[day]

                # 3. HRV
                if is_cmdr_last_7:
                    hrv_val = cmdr_last_7['hrv'][last_7_idx]
                else:
                    hrv_val = round(b_vals['hrv'] + random.uniform(-4, 4), 1)

                # 4. Exercise
                if is_cmdr_last_7:
                    ex_val = cmdr_last_7['exercise'][last_7_idx]
                else:
                    ex_val = round(b_vals['exercise'] + random.uniform(-8, 8), 0)

                # 5. Body Temp
                if is_cmdr_last_7:
                    temp_val = cmdr_last_7['body_temp'][last_7_idx]
                else:
                    temp_val = round(b_vals['body_temp'] + random.uniform(-0.15, 0.15), 1)

                # 6. Mood & Stress
                if is_cmdr_last_7:
                    stress_val = cmdr_last_7['mood_stress'][last_7_idx]
                else:
                    stress_val = round(b_vals['mood_stress'] + random.uniform(-1, 1), 0)
                stress_val = max(1.0, min(10.0, stress_val))
                mood_val = 5 - int(round(stress_val / 2.5))
                mood_val = max(1, min(5, mood_val))

                # 7. Cabin CO2
                if is_cmdr_last_7:
                    co2_val = cmdr_last_7['cabin_co2'][last_7_idx]
                else:
                    co2_val = round(b_vals['cabin_co2'] + random.uniform(-80, 80), 0)

                # Create readings for the 7 metrics
                day_metrics = [
                    ('resting_hr', rhr_val),
                    ('hrv', hrv_val),
                    ('sleep', sleep_val),
                    ('exercise', ex_val),
                    ('body_temp', temp_val),
                    ('mood_stress', stress_val),
                    ('cabin_co2', co2_val),
                ]

                for m_key, m_val in day_metrics:
                    readings_batch.append(
                        Reading(
                            astronaut=astro,
                            metric=m_key,
                            value=m_val,
                            mission_day=day,
                            timestamp=day_timestamp,
                            source='sensor' if m_key not in ('mood_stress',) else 'manual'
                        )
                    )

                # Check-in entry once per day
                notes_sample = ""
                if day == 142:
                    notes_sample = "Heavy workload during airlock maintenance. Feeling fatigued despite nominal vitals."
                elif day % 14 == 0:
                    notes_sample = "Bi-weekly EVA simulation conducted. Ergometer cardio completed."

                checkins_batch.append(
                    CheckIn(
                        astronaut=astro,
                        mission_day=day,
                        mood=mood_val,
                        stress=int(round(stress_val)),
                        sleep_hours=sleep_val,
                        exercise_minutes=int(round(ex_val)),
                        resting_hr=rhr_val,
                        hrv=hrv_val,
                        body_temp=temp_val,
                        notes=notes_sample,
                        timestamp=day_timestamp
                    )
                )

        Reading.objects.bulk_create(readings_batch)
        CheckIn.objects.bulk_create(checkins_batch)
        self.stdout.write(f"Created {len(readings_batch)} readings and {len(checkins_batch)} check-ins.")

        if scenario == 'sleep_decline':
            self.apply_sleep_decline(cmdr, all_astronauts)

        # Ground Support Messages
        self.stdout.write("Populating Ground Support communication packets...")
        if scenario == 'sleep_decline':
            surgeon_body = (
                "Good morning Amara. Flight Surgeon console notes three consecutive nights more than 1 h "
                "below your 7.2 h baseline (6.0 h, 5.9 h, 5.8 h). Recommend a 45-minute light-schedule offset, "
                "dimming cabin ambient luminaire spectrum to 2200K 90 minutes prior to sleep period."
            )
        else:
            surgeon_body = (
                "Good morning Amara. Flight Surgeon console notes a 4-consecutive-day downward sleep duration "
                "trend (7.3h down to 5.8h). Recommend a 45-minute light-schedule offset, dimming cabin ambient "
                "luminaire spectrum to 2200K 90 minutes prior to sleep period."
            )
        GroundMessage.objects.create(
            astronaut=cmdr,
            sender="Flight Surgeon (GND-SURG-2)",
            callsign="HOU-SURG-2",
            subject="Bio-Telemetry Review: Transit Day 142 Sleep Deficit",
            body=surgeon_body,
            timestamp=timezone.now() - timedelta(hours=1, minutes=15),
            mission_day=142,
            is_incoming=True,
            priority="Priority",
            flight_surgeon_ack="Acknowledged (GND-SURG-2)"
        )

        GroundMessage.objects.create(
            astronaut=cmdr,
            sender="Mission Control Houston",
            callsign="HOU-CAPCOM",
            subject="MCC-2 Mid-Course Correction Confirmation",
            body="Space-Pulse telemetry sync confirms Mid-Course Correction burn #2 completed with 100% nominal delta-V. Mars transit corridor deviation is under 0.002%. Excellent work crew.",
            timestamp=timezone.now() - timedelta(hours=3, minutes=30),
            mission_day=142,
            is_incoming=True,
            priority="Routine",
            flight_surgeon_ack="Acknowledged (GND-NAV)"
        )

        GroundMessage.objects.create(
            astronaut=cmdr,
            sender="Life Support Engineering",
            callsign="ECLSS-GND",
            subject="Cabin Atmosphere Telemetry Acknowledgment",
            body="HAB-01 carbon dioxide levels steady at 2,450 ppm. Secondary amine bed regeneration sequence scheduled for Day 145. No crew intervention required.",
            timestamp=timezone.now() - timedelta(hours=6, minutes=45),
            mission_day=142,
            is_incoming=True,
            priority="Routine",
            flight_surgeon_ack="Acknowledged (ECLSS-MON)"
        )

        GroundMessage.objects.create(
            astronaut=cmdr,
            sender="Cmdr. Amara Reyes",
            callsign="HAB-01-CMDR",
            subject="Crew Readiness & EVA Airlock Check",
            body="Houston, crew has completed morning vitals sync. EVA airlock prep on track for 12:00 UTC. Dr. Vance has reviewed hydration logs.",
            timestamp=timezone.now() - timedelta(hours=7, minutes=10),
            mission_day=142,
            is_incoming=False,
            priority="Routine",
            flight_surgeon_ack="Transmitted Uplink"
        )

        if scenario:
            # Alerts come from the rules (apply_sleep_decline), not from hand-written rows.
            pass
        else:
            # Active Alerts
            self.stdout.write("Configuring active and historical signals...")
            # 1. Caution Alert: Sleep trending down
            Alert.objects.create(
                astronaut=cmdr,
                metric="sleep",
                severity="caution",
                title="Sleep is trending down",
                message="5.8 h today, down from 7.3 h four days ago. Your baseline is 7.2 h.",
                mission_day=142,
                timestamp=timezone.now() - timedelta(minutes=45),
                acknowledged=False
            )

            # 2. Nominal Alert / Signal: Cabin CO2 steady
            Alert.objects.create(
                astronaut=cmdr,
                metric="cabin_co2",
                severity="nominal",
                title="Cabin CO2 steady",
                message="2,450 ppm, within range and 150 ppm above your baseline.",
                mission_day=142,
                timestamp=timezone.now() - timedelta(hours=1, minutes=15),
                acknowledged=False
            )

            # 3. Past acknowledged alert for historical audit testing
            Alert.objects.create(
                astronaut=cmdr,
                metric="resting_hr",
                severity="caution",
                title="Resting HR elevation during high-workload EVA",
                message="Resting heart rate reached 76 bpm following 4-hour airlock depressurization drill.",
                mission_day=130,
                timestamp=timezone.now() - timedelta(days=12),
                acknowledged=True
            )

        self.stdout.write(self.style.SUCCESS(
            "Successfully seeded Space-Pulse database!\n"
            f"- 4 Crew Profiles (Active: Cmdr. {cmdr.name})\n"
            f"- {Baseline.objects.count()} Personal Baselines\n"
            f"- {Reading.objects.count()} Telemetry Readings\n"
            f"- {CheckIn.objects.count()} Check-In records\n"
            f"- {GroundMessage.objects.count()} Ground comm packets\n"
            f"- {Alert.objects.count()} Alerts ({Alert.objects.filter(acknowledged=False).count()} active unacknowledged)"
        ))

    def apply_sleep_decline(self, cmdr, all_astronauts):
        """Turns the freshly generated data into the demo story.

        1. Baselines are computed from days 1-14 (the science definition),
           not copied from a template. mood_stress keeps its template value.
        2. Cabin CO2 gets 48 hourly readings (shared habitat air, so every crew
           member gets the same series). The old daily CO2 rows for days 141-142
           are replaced, because the CO2 rules are hourly.
        3. One older, acknowledged alert, written from the rule result.
        4. The active alerts are produced by sync_alerts_for_astronaut.
        """
        # 1. Baselines from days 1-14
        for astro in all_astronauts:
            for metric in METRICS:
                if metric == 'mood_stress':
                    continue
                early = list(
                    Reading.objects.filter(astronaut=astro, metric=metric, mission_day__lte=rules.BASELINE_DAYS)
                    .values_list('mission_day', 'value')
                )
                baseline = rules.compute_baseline(early)
                if baseline is None:
                    raise CommandError(f"Baseline for {astro.name}/{metric}: days 1-14 are not all present.")
                Baseline.objects.update_or_create(
                    astronaut=astro, metric=metric, defaults={'baseline_value': round(baseline, 1)}
                )

        # 2. Hourly cabin CO2 for the last 48 hours (ends at 2,450 ppm, as on the HUD)
        Reading.objects.filter(metric='cabin_co2', mission_day__gte=141).delete()
        end = timezone.now().replace(minute=45, second=0, microsecond=0)
        co2_rows = []
        for hours_ago in range(47, -1, -1):          # oldest first
            ppm = 2400 + 70 * math.sin(2 * math.pi * (47 - hours_ago) / 24) + random.uniform(-30, 30)
            if 12 <= hours_ago <= 14:                # a mild scrubber-regeneration bump, stays under 3,500
                ppm += 600
            if hours_ago == 0:
                ppm = 2450.0
            for astro in all_astronauts:
                co2_rows.append(Reading(
                    astronaut=astro,
                    metric='cabin_co2',
                    value=round(ppm, 0),
                    mission_day=142 if hours_ago < 24 else 141,
                    timestamp=end - timedelta(hours=hours_ago),
                    source='sensor',
                ))
        Reading.objects.bulk_create(co2_rows)

        # 3. Older acknowledged alert, from the rule result on the data up to day 130
        hr_rows = list(
            Reading.objects.filter(astronaut=cmdr, metric='resting_hr', mission_day__lte=130)
            .order_by('-mission_day', '-timestamp')
            .values_list('mission_day', 'timestamp', 'value')[:30]
        )
        hr_rows.reverse()
        hr_baseline = Baseline.objects.get(astronaut=cmdr, metric='resting_hr').baseline_value
        result = evaluate_metric_status('resting_hr', readings_to_series('resting_hr', hr_rows), hr_baseline)
        if result['status'] != 'caution':
            raise CommandError(f"Scenario expects an amber heart-rate episode on day 130, got {result['status']}.")
        Alert.objects.create(
            astronaut=cmdr,
            metric='resting_hr',
            severity='caution',
            title=f"{result['display_label']} caution",
            message=f"{result['reason']} Your baseline is {hr_baseline:.1f} {result['unit']}.",
            mission_day=130,
            timestamp=hr_rows[-1][1],
            acknowledged=True,
        )

        # 4. Active alerts from the rules
        for astro in all_astronauts:
            sync_alerts_for_astronaut(astro)
