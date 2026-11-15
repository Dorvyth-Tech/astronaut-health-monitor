"""
SPACE-PULSE space-weather card: a small client for NASA DONKI.

WHAT THIS IS: a labelled feed of space weather NEAR EARTH (solar flares, solar
energetic particle events and geomagnetic storms). It is context only. It is NOT a
measurement of crew radiation dose and it is NOT part of any health status or score
(nothing in core/rules.py or core/services.py reads this module).

Endpoints (https://api.nasa.gov/DONKI/<name>?startDate=..&endDate=..&api_key=..):
  FLR  solar flares                  classType is a NOAA letter class: A, B, C, M, X
  SEP  solar energetic particles     we only count events and keep the latest time
  GST  geomagnetic storms            allKpIndex[].kpIndex -> NOAA G-scale
Every event has a 'link' to its DONKI page; those links are kept.

NOAA G-scale (published by NOAA SWPC, https://www.swpc.noaa.gov/noaa-scales-explanation):
  Kp 5 = G1 Minor, 6 = G2 Moderate, 7 = G3 Strong, 8 = G4 Severe, 9 = G5 Extreme.
  DONKI reports thirds (5.33, 6.67), which are floored to the whole Kp (NOAA's "5-" is
  4.67, which is below 5). We invent no thresholds of our own.

Refresh strategy (the dashboard must never wait on NASA):
  1. cache (30 min)                     -> 'live'
  2. newest snapshot row < 30 min old   -> 'live'   (other workers' fetch, restart-proof)
  3. one bounded refresh (3 requests in parallel, 5 s timeout), then stored as a snapshot
  4. if that fails: newest snapshot of any age -> 'cached' (or 'sample')
  5. nothing at all                     -> None ("Space weather unavailable")
  After a failed refresh no new attempt is made for 5 minutes. `manage.py
  fetch_space_weather` does the same refresh by hand (cron or before the demo).

The NASA key comes from NASA_API_KEY (env or .env) and falls back to DEMO_KEY
(30 requests/hour/IP). The key is never logged, stored or put in an error message.
FLR is currently broken on NASA's gateway (HTTP 404); it fails soft: the other two
sections still work and the flare part says it is temporarily unavailable.
"""

import json
import logging
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as dt_timezone

from decouple import config
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

API_BASE = 'https://api.nasa.gov/DONKI/'
DONKI_HOME_URL = 'https://ccmc.gsfc.nasa.gov/tools/DONKI/'
NOAA_SCALES_URL = 'https://www.swpc.noaa.gov/noaa-scales-explanation'
USER_AGENT = 'SPACE-PULSE/1.0 (NASA Space Apps Accra 2026; space-weather context card)'

WINDOW_DAYS = 7
TIMEOUT_SECONDS = 5
CACHE_SECONDS = 30 * 60          # a fetched summary is "live" for 30 minutes
RETRY_AFTER_FAILURE_SECONDS = 5 * 60
KEEP_SNAPSHOTS = 50
CACHE_KEY = 'space_weather:summary'
COOLDOWN_KEY = 'space_weather:cooldown'

SOURCE_DONKI = 'donki'
SOURCE_SAMPLE = 'sample'

STATE_LIVE = 'live'
STATE_CACHED = 'cached'
STATE_SAMPLE = 'sample'

LABEL = "Space weather near Earth (NASA DONKI). Context only; not a measurement of crew radiation dose."
FLARES_UNAVAILABLE = "Flare data temporarily unavailable from NASA"
SAMPLE_LABEL = "Sample data"

ENDPOINTS = ('FLR', 'SEP', 'GST')

# NOAA flare letter classes, weakest to strongest.
FLARE_LETTERS = ('A', 'B', 'C', 'M', 'X')
_FLARE_RE = re.compile(r'^\s*([ABCMX])\s*(\d+(?:\.\d+)?)?\s*$', re.IGNORECASE)

# NOAA G-scale names (published).
G_NAMES = {1: 'Minor', 2: 'Moderate', 3: 'Strong', 4: 'Severe', 5: 'Extreme'}


class DonkiError(Exception):
    """A DONKI request failed. The text never contains the API key."""


# =========================================================================
# Pure helpers
# =========================================================================

def parse_flare_class(class_type):
    """'M1.2' -> (3, 1.2) so flares sort X > M > C > B > A, then by number. None if unreadable."""
    match = _FLARE_RE.match(class_type) if isinstance(class_type, str) else None
    if not match:
        return None
    letter = match.group(1).upper()
    number = float(match.group(2)) if match.group(2) else 0.0
    return FLARE_LETTERS.index(letter), number


def kp_to_g_level(kp):
    """NOAA G-scale: Kp 5 -> 1 ... Kp 9 -> 5. None when below G1 (or not a number)."""
    try:
        kp = float(kp)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(kp):
        return None
    whole = int(kp + 1e-9)
    if whole < 5:
        return None
    return min(whole, 9) - 4


def parse_time(value):
    """DONKI time such as '2026-10-31T21:00Z' -> aware UTC datetime, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    for fmt in ('%Y-%m-%dT%H:%MZ', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=dt_timezone.utc)
        except ValueError:
            continue
    return None


def _event_link(event):
    link = event.get('link') if isinstance(event, dict) else None
    return link if isinstance(link, str) and link.startswith('https://') else ''


def _latest_time_event(events, time_keys):
    """(time_string, event) of the event with the newest parseable time, or (None, None)."""
    best = None
    for event in events:
        if not isinstance(event, dict):
            continue
        for key in time_keys:
            when = parse_time(event.get(key))
            if when and (best is None or when > best[0]):
                best = (when, event.get(key), event)
                break
    return (best[1], best[2]) if best else (None, None)


# =========================================================================
# Parsing: one function per endpoint, each takes the JSON list DONKI returned
# =========================================================================

def summarize_flares(events):
    by_class = {letter: 0 for letter in reversed(FLARE_LETTERS)}   # X, M, C, B, A
    strongest = None        # (rank, event)
    count = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        rank = parse_flare_class(event.get('classType'))
        if rank is None:
            continue
        count += 1
        by_class[FLARE_LETTERS[rank[0]]] += 1
        if strongest is None or rank > strongest[0]:
            strongest = (rank, event)
    result = {'available': True, 'count': count, 'by_class': by_class, 'strongest': None}
    if strongest:
        event = strongest[1]
        result['strongest'] = {
            'class_type': event['classType'].strip().upper(),
            'peak_time': event.get('peakTime') or event.get('beginTime') or '',
            'link': _event_link(event),
        }
    return result


def summarize_sep(events):
    events = [e for e in events if isinstance(e, dict)]
    latest_time, latest = _latest_time_event(events, ('eventTime',))
    return {
        'available': True,
        'count': len(events),
        'latest_time': latest_time or '',
        'latest_link': _event_link(latest) if latest else '',
    }


def summarize_gst(events):
    events = [e for e in events if isinstance(e, dict)]
    best = None             # (kp, observedTime, link)
    for event in events:
        readings = event.get('allKpIndex')
        for reading in readings if isinstance(readings, list) else []:
            try:
                kp = float(reading.get('kpIndex'))
            except (AttributeError, TypeError, ValueError):
                continue
            if best is None or kp > best[0]:
                best = (kp, reading.get('observedTime') or event.get('startTime') or '', _event_link(event))
    result = {'available': True, 'storm_count': len(events), 'max_kp': None, 'g_level': None,
              'kp_time': '', 'link': ''}
    if best:
        result.update(max_kp=best[0], g_level=kp_to_g_level(best[0]), kp_time=best[1], link=best[2])
    return result


PARSERS = {'FLR': summarize_flares, 'SEP': summarize_sep, 'GST': summarize_gst}


# =========================================================================
# Network
# =========================================================================

def api_key():
    """NASA_API_KEY from the environment/.env, or the public DEMO_KEY."""
    return (config('NASA_API_KEY', default='') or '').strip() or 'DEMO_KEY'


def public_url(endpoint, start, end):
    """The request URL WITHOUT the key: safe to store, show and cite."""
    query = urllib.parse.urlencode({'startDate': start, 'endDate': end})
    return f'{API_BASE}{endpoint}?{query}'


def fetch_endpoint(endpoint, start, end, timeout=TIMEOUT_SECONDS):
    """GET one DONKI endpoint and return the parsed JSON list. Raises DonkiError."""
    query = urllib.parse.urlencode({'startDate': start, 'endDate': end, 'api_key': api_key()})
    request = urllib.request.Request(
        f'{API_BASE}{endpoint}?{query}',
        headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise DonkiError(f'{endpoint}: HTTP {exc.code}') from None
    except (TimeoutError, OSError) as exc:      # URLError and socket.timeout are OSErrors
        raise DonkiError(f'{endpoint}: {type(exc).__name__}') from None
    try:
        data = json.loads(body)
    except ValueError:
        raise DonkiError(f'{endpoint}: not valid JSON') from None
    if not isinstance(data, list):
        raise DonkiError(f'{endpoint}: unexpected response shape')
    return data


def build_summary(now=None):
    """Fetch FLR, SEP and GST in parallel and return a summary dict.

    A failing endpoint only blanks its own section. If all three fail, raises DonkiError.
    """
    now = now or timezone.now()
    end = now.astimezone(dt_timezone.utc).date()
    start = end - timedelta(days=WINDOW_DAYS)
    start_s, end_s = start.isoformat(), end.isoformat()

    def one(endpoint):
        try:
            return endpoint, PARSERS[endpoint](fetch_endpoint(endpoint, start_s, end_s)), ''
        except DonkiError as exc:
            return endpoint, None, str(exc)
        except Exception as exc:                # a parser bug must not break the page either
            return endpoint, None, f'{endpoint}: {type(exc).__name__}'

    with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as pool:
        results = list(pool.map(one, ENDPOINTS))

    sections, errors = {}, {}
    for endpoint, section, error in results:
        sections[endpoint] = section
        if error:
            errors[endpoint] = error
    if all(section is None for section in sections.values()):
        raise DonkiError('; '.join(errors.values()))

    unavailable = {'available': False}
    return {
        'sample': False,
        'window_days': WINDOW_DAYS,
        'start_date': start_s,
        'end_date': end_s,
        'flares': sections['FLR'] or {**unavailable, 'count': 0, 'by_class': {}, 'strongest': None},
        'sep': sections['SEP'] or {**unavailable, 'count': 0, 'latest_time': '', 'latest_link': ''},
        'geomagnetic': sections['GST'] or {**unavailable, 'storm_count': 0, 'max_kp': None,
                                           'g_level': None, 'kp_time': '', 'link': ''},
        'errors': errors,
        'fetched_at': now.astimezone(dt_timezone.utc).isoformat(),
        'sources': [public_url(e, start_s, end_s) for e in ENDPOINTS],
        'donki_url': DONKI_HOME_URL,
        'noaa_scales_url': NOAA_SCALES_URL,
    }


def sample_summary(now=None):
    """A clearly labelled invented sample, for an offline demo. Never presented as live."""
    now = now or timezone.now()
    end = now.astimezone(dt_timezone.utc).date()
    start = end - timedelta(days=WINDOW_DAYS)
    stamp = (now - timedelta(days=2)).astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
    return {
        'sample': True,
        'window_days': WINDOW_DAYS,
        'start_date': start.isoformat(),
        'end_date': end.isoformat(),
        'flares': {'available': True, 'count': 4, 'by_class': {'X': 0, 'M': 1, 'C': 3, 'B': 0, 'A': 0},
                   'strongest': {'class_type': 'M1.2', 'peak_time': stamp, 'link': ''}},
        'sep': {'available': True, 'count': 0, 'latest_time': '', 'latest_link': ''},
        'geomagnetic': {'available': True, 'storm_count': 1, 'max_kp': 5.0, 'g_level': 1,
                        'kp_time': stamp, 'link': '', },
        'errors': {},
        'fetched_at': now.astimezone(dt_timezone.utc).isoformat(),
        'sources': [],
        'donki_url': DONKI_HOME_URL,
        'noaa_scales_url': NOAA_SCALES_URL,
    }


# =========================================================================
# Storage, cache and the refresh strategy
# =========================================================================

def _model():
    from core.models import SpaceWeatherSnapshot      # imported late: keeps parsing importable without Django apps
    return SpaceWeatherSnapshot


def save_snapshot(summary, source=SOURCE_DONKI):
    model = _model()
    row = model.objects.create(source=source, payload=summary, fetched_at=timezone.now())
    stale = model.objects.order_by('-fetched_at', '-id').values_list('id', flat=True)[KEEP_SNAPSHOTS:]
    if stale:
        model.objects.filter(id__in=list(stale)).delete()
    return row


def store_sample_snapshot_if_empty():
    """Used by seed_demo: store a labelled sample only when there is no snapshot at all."""
    if _model().objects.exists():
        return False
    save_snapshot(sample_summary(), SOURCE_SAMPLE)
    return True


def _view(summary, source, fetched_at, state):
    return {'summary': summary, 'source': source, 'fetched_at': fetched_at, 'state': state}


def _snapshot_view(row, fresh_for=None):
    if row.source == SOURCE_SAMPLE:
        state = STATE_SAMPLE
    elif fresh_for is not None and timezone.now() - row.fetched_at < fresh_for:
        state = STATE_LIVE
    else:
        state = STATE_CACHED
    return _view(row.payload, row.source, row.fetched_at, state)


def refresh_space_weather():
    """One bounded refresh. Returns the new view, or None if NASA could not be reached."""
    try:
        summary = build_summary()
    except DonkiError as exc:
        logger.warning('DONKI refresh failed: %s', exc)
        cache.set(COOLDOWN_KEY, True, RETRY_AFTER_FAILURE_SECONDS)
        return None
    row = save_snapshot(summary)
    view = _view(summary, SOURCE_DONKI, row.fetched_at, STATE_LIVE)
    cache.set(CACHE_KEY, view, CACHE_SECONDS)
    cache.delete(COOLDOWN_KEY)
    return view


def get_space_weather(allow_refresh=True):
    """The space weather to show, as {'summary','source','fetched_at','state'} or None.

    Never raises. Blocks at most one refresh (about TIMEOUT_SECONDS) and only when the
    data is more than 30 minutes old and no refresh failed in the last 5 minutes.
    """
    try:
        cached = cache.get(CACHE_KEY)
        if cached:
            return cached
        latest = _model().objects.order_by('-fetched_at', '-id').first()
        fresh = timedelta(seconds=CACHE_SECONDS)
        if latest and latest.source == SOURCE_DONKI and timezone.now() - latest.fetched_at < fresh:
            view = _snapshot_view(latest, fresh)
            cache.set(CACHE_KEY, view, CACHE_SECONDS)
            return view
        if allow_refresh and not cache.get(COOLDOWN_KEY):
            view = refresh_space_weather()
            if view:
                return view
        return _snapshot_view(latest) if latest else None
    except Exception as exc:
        logger.warning('Space weather unavailable: %s', type(exc).__name__)
        return None


# =========================================================================
# Words for the card, the JSON and Ask Pulse
# =========================================================================

def format_time(value):
    """DONKI string or datetime -> '12 Nov 04:10 UTC' (Accra is on UTC all year)."""
    when = parse_time(value) if isinstance(value, str) else value
    if not when:
        return ''
    return when.astimezone(dt_timezone.utc).strftime('%d %b %H:%M UTC').lstrip('0')


def _kp_text(kp):
    return f'{kp:g}'


def describe(view):
    """Plain strings for templates and the JSON endpoint. Pure; no database."""
    summary, state = view['summary'], view['state']
    flares, sep, gst = summary['flares'], summary['sep'], summary['geomagnetic']
    when = format_time(view['fetched_at'])

    if state == STATE_SAMPLE:
        status = f'{SAMPLE_LABEL}: not live NASA data'
    elif state == STATE_CACHED:
        status = f'Cached from {when} (NASA could not be reached just now)'
    else:
        status = f'Updated {when}'

    if not flares['available']:
        flare_main, flare_sub = '--', FLARES_UNAVAILABLE
    elif not flares['count']:
        flare_main, flare_sub = 'None', f"No solar flares recorded in the last {summary['window_days']} days"
    else:
        top = flares['strongest']
        flare_main = top['class_type']
        parts = [f'{n} {letter}-class' for letter, n in flares['by_class'].items() if n]
        flare_sub = f"{flares['count']} flares ({', '.join(parts)}). Strongest peaked {format_time(top['peak_time'])}"

    if not sep['available']:
        sep_main, sep_sub = '--', 'SEP data temporarily unavailable from NASA'
    else:
        sep_main = str(sep['count'])
        sep_sub = (f"Latest {format_time(sep['latest_time'])}" if sep['count'] and sep['latest_time']
                   else f"No events in the last {summary['window_days']} days")

    if not gst['available']:
        kp_main, kp_sub = '--', 'Kp data temporarily unavailable from NASA'
    elif gst['max_kp'] is None:
        kp_main, kp_sub = 'None', f"No geomagnetic storms in the last {summary['window_days']} days"
    else:
        level = gst['g_level']
        kp_main = f"Kp {_kp_text(gst['max_kp'])}"
        if level:
            kp_main += f" (G{level})"
            kp_sub = f"NOAA G{level} {G_NAMES[level]} geomagnetic storm level, observed {format_time(gst['kp_time'])}"
        else:
            kp_sub = f"Below NOAA G1, observed {format_time(gst['kp_time'])}"

    return {
        'state': state,
        'is_sample': state == STATE_SAMPLE,
        'is_cached': state == STATE_CACHED,
        'status': status,
        'label': LABEL,
        'window_days': summary['window_days'],
        'flare_main': flare_main, 'flare_sub': flare_sub, 'flare_available': flares['available'],
        'sep_main': sep_main, 'sep_sub': sep_sub, 'sep_available': sep['available'],
        'kp_main': kp_main, 'kp_sub': kp_sub, 'kp_available': gst['available'],
        'donki_url': summary.get('donki_url') or DONKI_HOME_URL,
        'noaa_scales_url': summary.get('noaa_scales_url') or NOAA_SCALES_URL,
        'sources': summary.get('sources', []),
    }


def build_answer(view):
    """The Ask Pulse answer (companion answer shape) built only from the stored summary."""
    from core import companion         # late import: companion imports rules, not donki

    summary, state = view['summary'], view['state']
    flares, sep, gst = summary['flares'], summary['sep'], summary['geomagnetic']
    days = summary['window_days']
    parts = []

    if not flares['available']:
        parts.append("NASA's solar flare feed is temporarily unavailable, so I can't report flares")
    elif not flares['count']:
        parts.append(f"In the last {days} days NASA DONKI recorded no solar flares")
    else:
        top = flares['strongest']
        noun = 'solar flare' if flares['count'] == 1 else 'solar flares'
        parts.append(f"In the last {days} days NASA DONKI recorded {flares['count']} {noun}, "
                     f"strongest {top['class_type']} on {format_time(top['peak_time'])}")

    if sep['available']:
        noun = 'solar energetic particle event' if sep['count'] == 1 else 'solar energetic particle events'
        parts.append(f"{sep['count']} {noun}")
    else:
        parts.append("solar energetic particle data is unavailable")

    if not gst['available']:
        parts.append("geomagnetic data is unavailable")
    elif gst['max_kp'] is None:
        parts.append("no geomagnetic storms")
    elif gst['g_level']:
        parts.append(f"the highest Kp index was {_kp_text(gst['max_kp'])} (NOAA G{gst['g_level']}, "
                     f"{G_NAMES[gst['g_level']]})")
    else:
        parts.append(f"the highest Kp index was {_kp_text(gst['max_kp'])} (below NOAA G1)")

    text = '; '.join(parts) + '. This is space weather near Earth, not a measure of your dose.'
    if state == STATE_SAMPLE:
        text = f"Sample data, not live NASA data. {text}"
    elif state == STATE_CACHED:
        text = f"{text} (Cached from {format_time(view['fetched_at'])}.)"

    source = f"NASA DONKI ({summary.get('donki_url') or DONKI_HOME_URL}); NOAA scales ({NOAA_SCALES_URL})"
    if state == STATE_SAMPLE:
        source = "Sample data for offline demo; real source is NASA DONKI (" + DONKI_HOME_URL + ")"
    return companion._make_answer(
        'space_weather', "What is the space weather?", text, companion.INFO, source=source)
