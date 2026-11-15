"""
CSV import of readings (SRS FR-07 upload, FR-08 validation).

CSV format (a header row is required, columns can be in any order):

    timestamp,metric,value
    2026-11-10T07:30:00Z,sleep,7.1
    2026-11-10T08:00:00+00:00,resting_hr,58

- timestamp: ISO 8601, e.g. 2026-11-10T07:30:00Z, 2026-11-10T07:30:00+02:00 or just
  2026-11-10. A timestamp without a time zone is read as UTC.
- metric: one of the keys in core.constants.READING_PLAUSIBLE_RANGE.
- value: a plain number.

Every row is checked on its own. Good rows are saved as Reading(source='csv'), bad
rows are skipped and reported with their row number (the header is row 1) and the
reason. A file that is too big, has too many rows or lacks a required column is
refused as a whole (CsvFileError) and nothing is saved.
"""

import csv
import io
import math
from datetime import datetime, timedelta, timezone as dt_timezone

from django.db import transaction
from django.utils import timezone

from core.constants import READING_PLAUSIBLE_RANGE
from telemetry.models import Reading

MAX_FILE_BYTES = 1_000_000      # 1 MB
MAX_ROWS = 5000                 # data rows, not counting the header
REQUIRED_COLUMNS = ['timestamp', 'metric', 'value']
REJECTED_COLUMNS = ['row', 'reason', 'timestamp', 'metric', 'value']
MAX_SHOWN_CELL = 100            # long cells are cut when a rejected row is remembered


class CsvFileError(Exception):
    """The whole file is refused. The message is shown to the user as it is."""


# --- reading the file ------------------------------------------------------

def read_csv_text(upload):
    """Check the uploaded file itself and return its text."""
    name = (upload.name or '').lower()
    if not name.endswith('.csv'):
        raise CsvFileError("Please upload a .csv file.")
    if upload.size > MAX_FILE_BYTES:
        raise CsvFileError(f"The file is too large (limit {MAX_FILE_BYTES // 1000} KB).")
    try:
        return upload.read().decode('utf-8-sig')      # utf-8-sig also drops the Excel BOM
    except UnicodeDecodeError:
        raise CsvFileError("The file is not UTF-8 text. In Excel use 'CSV UTF-8' when saving.")


def parse_timestamp(text):
    """ISO text -> aware datetime in UTC, or None when it cannot be read."""
    text = text.strip()
    if text.endswith(('Z', 'z')):
        text = text[:-1] + '+00:00'
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_timezone.utc)
    return value.astimezone(dt_timezone.utc)


def check_row(cells, astronaut, now):
    """Validate one row. Returns (clean, None) or (None, reason).

    The first problem found is the one reported.
    """
    raw_timestamp = (cells.get('timestamp') or '').strip()
    raw_metric = (cells.get('metric') or '').strip().lower()
    raw_value = (cells.get('value') or '').strip()

    for name, text in (('timestamp', raw_timestamp), ('metric', raw_metric), ('value', raw_value)):
        if not text:
            return None, f"{name} is empty"

    when = parse_timestamp(raw_timestamp)
    if when is None:
        return None, f"timestamp '{raw_timestamp}' is not a valid ISO date/time (example: 2026-11-10T07:30:00Z)"
    if when > now:
        return None, "timestamp is in the future"

    # Mission day: the astronaut's current day is "today" (UTC); older dates count back.
    days_ago = (now.date() - when.date()).days
    mission_day = astronaut.current_mission_day - days_ago
    if mission_day < 1:
        return None, "timestamp is before mission day 1"

    if raw_metric not in READING_PLAUSIBLE_RANGE:
        known = ", ".join(READING_PLAUSIBLE_RANGE)
        return None, f"unknown metric '{raw_metric}' (use one of: {known})"

    try:
        value = float(raw_value)
    except ValueError:
        return None, f"value '{raw_value}' is not a number"
    if not math.isfinite(value):
        return None, f"value '{raw_value}' is not a usable number"

    low, high = READING_PLAUSIBLE_RANGE[raw_metric]
    if not low <= value <= high:
        return None, f"value {value:g} is outside the plausible range {low:g} to {high:g} for {raw_metric}"

    return {'metric': raw_metric, 'value': value, 'timestamp': when, 'mission_day': mission_day}, None


def short(text):
    return (text or '').strip()[:MAX_SHOWN_CELL]


# --- the import ------------------------------------------------------------

def import_readings_csv(astronaut, upload, now=None):
    """Validate and save an uploaded CSV for ONE astronaut.

    Returns {'total': rows read, 'imported': rows saved, 'rejected': [...]}, where each
    rejected item is {'row', 'reason', 'timestamp', 'metric', 'value'}.
    Raises CsvFileError when the whole file is refused.
    """
    now = now or timezone.now()
    text = read_csv_text(upload)

    reader = csv.DictReader(io.StringIO(text))
    try:
        header = reader.fieldnames
        if not header:
            raise CsvFileError("The file is empty.")
        reader.fieldnames = [(name or '').strip().lower() for name in header]
        missing = [name for name in REQUIRED_COLUMNS if name not in reader.fieldnames]
        if missing:
            raise CsvFileError(
                f"Missing required column(s): {', '.join(missing)}. "
                f"The first row must be: {','.join(REQUIRED_COLUMNS)}"
            )

        good = []          # (row number, clean values)
        rejected = []
        total = 0
        for cells in reader:
            total += 1
            if total > MAX_ROWS:
                raise CsvFileError(f"Too many rows (limit {MAX_ROWS}). Split the file and upload it in parts.")
            clean, reason = check_row(cells, astronaut, now)
            if reason:
                rejected.append(rejected_item(reader.line_num, reason, cells))
            else:
                good.append((reader.line_num, clean, cells))
    except csv.Error as error:
        raise CsvFileError(f"The file could not be read as CSV ({error}).")

    # Duplicates: the same metric at the same moment, in this file or already saved.
    existing = set()
    if good:
        times = [clean['timestamp'] for _row, clean, _cells in good]
        existing = set(
            Reading.objects.filter(astronaut=astronaut, timestamp__gte=min(times), timestamp__lte=max(times))
            .values_list('metric', 'timestamp')
        )
    seen_in_file = {}
    to_save = []
    for row_number, clean, cells in good:
        key = (clean['metric'], clean['timestamp'])
        if key in existing:
            rejected.append(rejected_item(row_number, "duplicate: this reading is already saved", cells))
        elif key in seen_in_file:
            rejected.append(rejected_item(
                row_number, f"duplicate of row {seen_in_file[key]} in this file", cells))
        else:
            seen_in_file[key] = row_number
            to_save.append(Reading(astronaut=astronaut, source='csv', **clean))

    with transaction.atomic():
        Reading.objects.bulk_create(to_save)

    rejected.sort(key=lambda item: item['row'])
    return {'total': total, 'imported': len(to_save), 'rejected': rejected}


def rejected_item(row_number, reason, cells):
    return {
        'row': row_number,
        'reason': reason,
        'timestamp': short(cells.get('timestamp')),
        'metric': short(cells.get('metric')),
        'value': short(cells.get('value')),
    }


# --- CSV files we hand out -------------------------------------------------

def safe_cell(value):
    """Stop spreadsheets from running a cell that starts with = + - or @ as a formula."""
    text = str(value)
    return "'" + text if text[:1] in ('=', '+', '-', '@') else text


def rejected_rows_to_csv(rejected):
    """The rejected-row report as CSV text (row, reason, and the original cells)."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(REJECTED_COLUMNS)
    for item in rejected:
        writer.writerow([safe_cell(item[column]) for column in REJECTED_COLUMNS])
    return out.getvalue()


def sample_csv_text(now=None):
    """A small template with valid example rows from the last few days."""
    now = (now or timezone.now()).replace(minute=0, second=0, microsecond=0)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(REQUIRED_COLUMNS)
    for days_ago, metric, value in ((2, 'sleep', 7.1), (2, 'resting_hr', 58),
                                    (1, 'sleep', 6.8), (1, 'hrv', 64), (1, 'body_temp', 36.7)):
        when = (now - timedelta(days=days_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')
        writer.writerow([when, metric, value])
    return out.getvalue()