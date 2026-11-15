"""
Tests for the CSV upload (SRS FR-07 / FR-08): valid import, every rejection reason,
file limits, isolation between astronauts, source='csv' and the alert update.
"""

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from alerts.models import Alert
from core.models import AstronautProfile
from telemetry import csv_import
from telemetry.models import Reading

PASSWORD = "validAccess2026"
HEADER = "timestamp,metric,value\n"


def stamp(days_ago=1, minutes_ago=0):
    """An ISO timestamp (UTC) in the recent past."""
    when = timezone.now() - timedelta(days=days_ago, minutes=minutes_ago)
    return when.strftime('%Y-%m-%dT%H:%M:%SZ')


def make_crew_member(name, email, mission_day=142):
    astronaut = AstronautProfile.objects.create(
        name=name, email=email, rank="Cmdr.", habitat="HAB-01", current_mission_day=mission_day
    )
    user = User.objects.create_user(username=email.split('@')[0], email=email, password=PASSWORD)
    return astronaut, user


class CsvUploadBase(TestCase):
    def setUp(self):
        self.astro, self.user = make_crew_member("Amara Reyes", "amara@mission.example")
        self.client.force_login(self.user)

    def upload(self, text, name='readings.csv', **extra_fields):
        data = text.encode('utf-8') if isinstance(text, str) else text
        fields = {'csv_file': SimpleUploadedFile(name, data, content_type='text/csv')}
        fields.update(extra_fields)
        return self.client.post(reverse('telemetry:upload_csv'), fields)

    def saved(self, astronaut=None):
        return Reading.objects.filter(astronaut=astronaut or self.astro)


class ValidImportTests(CsvUploadBase):
    def test_page_loads_and_is_linked_from_the_nav(self):
        resp = self.client.get(reverse('telemetry:upload_csv'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'timestamp,metric,value')
        dashboard = self.client.get(reverse('core:dashboard'))
        self.assertContains(dashboard, reverse('telemetry:upload_csv'))

    def test_valid_file_imports_every_row_as_source_csv(self):
        resp = self.upload(HEADER + f"{stamp(2)},sleep,7.1\n{stamp(2)},resting_hr,58\n{stamp(1)},hrv,64.5\n")
        result = resp.context['result']
        self.assertEqual((result['total'], result['imported'], result['rejected']), (3, 3, []))
        self.assertEqual(self.saved().count(), 3)
        self.assertEqual(set(self.saved().values_list('source', flat=True)), {'csv'})
        self.assertContains(resp, '3 of 3 rows imported')

    def test_mission_day_is_worked_out_from_the_date(self):
        self.upload(HEADER + f"{stamp(0, 1)},sleep,7.0\n{stamp(3)},resting_hr,58\n")
        self.assertEqual(self.saved().get(metric='sleep').mission_day, 142)
        self.assertEqual(self.saved().get(metric='resting_hr').mission_day, 139)

    def test_formats_that_are_accepted(self):
        day = (timezone.now() - timedelta(days=3)).date().isoformat()      # a recent date, e.g. 2026-11-12
        text = ("\ufeffValue, Timestamp ,METRIC,extra\n"                 # BOM, other order/case, extra column
                f"7.1,{day}T07:30:00+02:00,Sleep,ignored\n"               # offset and upper-case metric
                f"58,{day},resting_hr,\n"                                 # date only: midnight UTC
                f"4,{day}T08:00:00,mood,\n")                              # no zone: UTC
        resp = self.upload(text)
        self.assertEqual(resp.context['result']['rejected'], [])
        sleep = self.saved().get(metric='sleep')
        self.assertEqual(sleep.timestamp.hour, 5)                          # 07:30+02:00 is 05:30 UTC
        self.assertEqual(self.saved().count(), 3)

    def test_limits_of_the_plausible_range_are_allowed(self):
        resp = self.upload(HEADER + f"{stamp(2)},sleep,0\n{stamp(2)},mood,5\n{stamp(2)},mood_stress,10\n")
        self.assertEqual(resp.context['result']['imported'], 3)

    def test_blank_lines_are_ignored(self):
        resp = self.upload(HEADER + f"\n{stamp(2)},sleep,7\n\n")
        self.assertEqual(resp.context['result']['imported'], 1)

    def test_sample_template_downloads_and_imports_cleanly(self):
        resp = self.client.get(reverse('telemetry:csv_template'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/csv', resp['Content-Type'])
        text = resp.content.decode()
        self.assertTrue(text.startswith('timestamp,metric,value'))
        result = self.upload(text).context['result']
        self.assertEqual(result['rejected'], [])
        self.assertGreater(result['imported'], 0)


class RejectedRowTests(CsvUploadBase):
    def assert_rejected(self, row_text, reason_part):
        """Upload one bad row (it is row 2) and check the report."""
        resp = self.upload(HEADER + row_text + "\n")
        result = resp.context['result']
        self.assertEqual(result['imported'], 0, row_text)
        self.assertEqual(len(result['rejected']), 1, row_text)
        item = result['rejected'][0]
        self.assertEqual(item['row'], 2)
        self.assertIn(reason_part, item['reason'], row_text)
        self.assertEqual(self.saved().count(), 0)
        return resp

    def test_empty_cells(self):
        self.assert_rejected(f",sleep,7", "timestamp is empty")
        self.assert_rejected(f"{stamp()},,7", "metric is empty")
        self.assert_rejected(f"{stamp()},sleep,", "value is empty")

    def test_bad_timestamps(self):
        self.assert_rejected("yesterday,sleep,7", "not a valid ISO")
        self.assert_rejected("05/01/2026,sleep,7", "not a valid ISO")

    def test_future_timestamp(self):
        future = (timezone.now() + timedelta(days=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.assert_rejected(f"{future},sleep,7", "in the future")

    def test_timestamp_before_mission_day_one(self):
        self.astro.current_mission_day = 5
        self.astro.save()
        self.assert_rejected(f"{stamp(10)},sleep,7", "before mission day 1")

    def test_unknown_metric(self):
        resp = self.assert_rejected(f"{stamp()},blood_pressure,120", "unknown metric 'blood_pressure'")
        self.assertContains(resp, 'resting_hr')            # the message lists the allowed keys

    def test_value_not_a_number(self):
        self.assert_rejected(f"{stamp()},sleep,lots", "is not a number")
        self.assert_rejected(f"{stamp()},sleep,nan", "not a usable number")
        self.assert_rejected(f"{stamp()},sleep,inf", "not a usable number")

    def test_value_outside_the_plausible_range(self):
        self.assert_rejected(f"{stamp()},sleep,70", "outside the plausible range")
        self.assert_rejected(f"{stamp()},resting_hr,-5", "outside the plausible range")
        self.assert_rejected(f"{stamp()},body_temp,98.6", "outside the plausible range")     # Fahrenheit
        self.assert_rejected(f"{stamp()},mood,6", "outside the plausible range")

    def test_duplicate_of_an_already_saved_reading(self):
        when = stamp(2)
        self.upload(HEADER + f"{when},sleep,7.0\n")
        resp = self.upload(HEADER + f"{when},sleep,7.0\n")
        result = resp.context['result']
        self.assertEqual(result['imported'], 0)
        self.assertIn('already saved', result['rejected'][0]['reason'])
        self.assertEqual(self.saved().count(), 1)

    def test_duplicate_inside_the_same_file(self):
        when = stamp(2)
        resp = self.upload(HEADER + f"{when},sleep,7.0\n{when},sleep,6.0\n{when},hrv,60\n")
        result = resp.context['result']
        self.assertEqual(result['imported'], 2)             # same moment but another metric is fine
        self.assertEqual(result['rejected'][0]['row'], 3)
        self.assertIn('duplicate of row 2', result['rejected'][0]['reason'])
        self.assertEqual(self.saved().get(metric='sleep').value, 7.0)       # the first one wins

    def test_good_rows_are_saved_and_bad_rows_reported_with_row_numbers(self):
        text = HEADER + (
            f"{stamp(2)},sleep,7.1\n"         # row 2 ok
            f"{stamp(2)},sleep,abc\n"         # row 3 bad
            f"{stamp(2)},hrv,64\n"            # row 4 ok
            f"nope,hrv,64\n"                  # row 5 bad
        )
        resp = self.upload(text)
        result = resp.context['result']
        self.assertEqual((result['total'], result['imported']), (4, 2))
        self.assertEqual([item['row'] for item in result['rejected']], [3, 5])
        self.assertContains(resp, 'Rejected rows (2)')
        self.assertContains(resp, 'is not a number')
        self.assertEqual(self.saved().count(), 2)

    def test_rejected_report_downloads_as_csv(self):
        self.upload(HEADER + f"{stamp(2)},sleep,abc\n=HYPERLINK(1),sleep,7\n")
        resp = self.client.get(reverse('telemetry:csv_rejected'))
        self.assertEqual(resp.status_code, 200)
        lines = resp.content.decode().splitlines()
        self.assertEqual(lines[0], 'row,reason,timestamp,metric,value')
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[1].startswith('2,'))
        self.assertIn("'=HYPERLINK(1)", lines[2])           # spreadsheet formulas are defused

    def test_download_without_an_upload_goes_back_to_the_page(self):
        resp = self.client.get(reverse('telemetry:csv_rejected'))
        self.assertRedirects(resp, reverse('telemetry:upload_csv'))


class WholeFileRefusedTests(CsvUploadBase):
    def assert_refused(self, resp, message_part):
        self.assertIsNone(resp.context['result'])
        self.assertContains(resp, message_part)
        self.assertEqual(Reading.objects.count(), 0)

    def test_missing_required_column(self):
        resp = self.upload(f"timestamp,value\n{stamp()},7\n")
        self.assert_refused(resp, 'Missing required column(s): metric')

    def test_empty_file(self):
        self.assert_refused(self.upload(""), 'The file is empty')

    def test_header_only_file_imports_nothing_without_error(self):
        resp = self.upload(HEADER)
        self.assertEqual(resp.context['result']['total'], 0)

    def test_no_file_chosen(self):
        resp = self.client.post(reverse('telemetry:upload_csv'), {})
        self.assertContains(resp, 'Choose a CSV file first')

    def test_wrong_file_type(self):
        self.assert_refused(self.upload(HEADER, name='readings.txt'), 'Please upload a .csv file')

    def test_not_utf8_text(self):
        self.assert_refused(self.upload(b'\xff\xfe\x00\x01', name='x.csv'), 'not UTF-8')

    def test_file_size_cap(self):
        with mock.patch.object(csv_import, 'MAX_FILE_BYTES', 60):
            resp = self.upload(HEADER + f"{stamp()},sleep,7\n" * 5)
        self.assert_refused(resp, 'too large')

    def test_row_cap_saves_nothing(self):
        with mock.patch.object(csv_import, 'MAX_ROWS', 3):
            resp = self.upload(HEADER + "".join(f"{stamp(2, i)},sleep,7\n" for i in range(4)))
        self.assert_refused(resp, 'Too many rows')

    def test_row_cap_allows_exactly_the_limit(self):
        with mock.patch.object(csv_import, 'MAX_ROWS', 3):
            resp = self.upload(HEADER + "".join(f"{stamp(2, i)},sleep,7\n" for i in range(3)))
        self.assertEqual(resp.context['result']['imported'], 3)


class IsolationAndLoginTests(CsvUploadBase):
    def setUp(self):
        super().setUp()
        self.other, self.other_user = make_crew_member("Ben Okoye", "ben@mission.example")

    def test_anonymous_users_are_sent_to_login(self):
        self.client.logout()
        for name in ('telemetry:upload_csv', 'telemetry:csv_template', 'telemetry:csv_rejected'):
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 302, name)
            self.assertTrue(resp.headers['Location'].startswith(reverse('core:login')), name)
        resp = self.upload(HEADER + f"{stamp(2)},sleep,7\n")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Reading.objects.count(), 0)

    @override_settings(DEMO_MODE=False)
    def test_rows_go_to_my_astronaut_even_if_the_form_names_another(self):
        self.upload(HEADER + f"{stamp(2)},sleep,7\n",
                    astronaut=self.other.id, astronaut_id=self.other.id, email=self.other.email)
        self.assertEqual(self.saved(self.astro).count(), 1)
        self.assertEqual(self.saved(self.other).count(), 0)

    @override_settings(DEMO_MODE=False)
    def test_switch_parameter_cannot_redirect_an_upload_outside_demo_mode(self):
        self.client.get(reverse('core:profile') + f'?switch={self.other.id}')
        self.upload(HEADER + f"{stamp(2)},sleep,7\n")
        self.assertEqual(self.saved(self.astro).count(), 1)
        self.assertEqual(self.saved(self.other).count(), 0)

    @override_settings(DEMO_MODE=True)
    def test_in_demo_mode_the_active_astronaut_receives_the_upload(self):
        self.client.get(reverse('core:profile') + f'?switch={self.other.id}')
        self.upload(HEADER + f"{stamp(2)},sleep,7\n")
        self.assertEqual(self.saved(self.other).count(), 1)
        self.assertEqual(self.saved(self.astro).count(), 0)

    def test_duplicates_are_only_checked_against_my_own_readings(self):
        when = stamp(2)
        Reading.objects.create(astronaut=self.other, metric='sleep', value=7.0, mission_day=140,
                               timestamp=csv_import.parse_timestamp(when))
        resp = self.upload(HEADER + f"{when},sleep,7.0\n")
        self.assertEqual(resp.context['result']['imported'], 1)


class AlertSyncTests(CsvUploadBase):
    def test_import_updates_the_alerts_like_a_manual_reading(self):
        # Baseline: 14 early nights of 7.2 h. Then three short nights arrive by CSV.
        for day in range(1, 15):
            Reading.objects.create(astronaut=self.astro, metric='sleep', value=7.2,
                                   mission_day=day, source='manual')
        self.assertFalse(Alert.objects.filter(astronaut=self.astro).exists())

        text = HEADER + (f"{stamp(2, 1)},sleep,5.8\n{stamp(1, 1)},sleep,5.8\n{stamp(0, 1)},sleep,5.8\n")
        resp = self.upload(text)

        self.assertEqual(resp.context['result']['imported'], 3)
        alert = Alert.objects.get(astronaut=self.astro, metric='sleep', acknowledged=False)
        self.assertEqual(alert.severity, 'caution')

    def test_nothing_imported_means_no_alert_change(self):
        with mock.patch('telemetry.views.sync_alerts_for_astronaut') as sync:
            self.upload(HEADER + f"{stamp()},sleep,abc\n")
        sync.assert_not_called()
        with mock.patch('telemetry.views.sync_alerts_for_astronaut') as sync:
            self.upload(HEADER + f"{stamp(2)},sleep,7\n")
        sync.assert_called_once_with(self.astro)
