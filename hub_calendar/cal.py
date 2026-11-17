"""Calendar logic: who is looking, feed refresh, and expanding events into a date range.

Times are handled as *local wall-clock* times in the hub's time zone
(Settings > General, via ``bdh_sdk.location``); the database stores aware UTC datetimes.
"""
import hashlib
import logging
import re
import threading
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from bdh_sdk.auth import is_admin
from bdh_sdk.location import location as hub_location
from bdh_sdk.settings import app_setting
from dateutil import rrule
from django.core.cache import cache
from django.db import close_old_connections
from django.utils import timezone

from .models import Event, Feed

log = logging.getLogger(__name__)

APP = "hub_calendar"
GUEST_EVENT_TTL = timedelta(days=14)  # when the stay has no checkout time
MAX_ICS_BYTES = 2 * 1024 * 1024  # kept and parsed; a parsed feed takes ~15x its size
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024  # streamed through trim_events, never held whole
MAX_EVENTS = 500  # VEVENTs kept per feed
MAX_DESCRIPTION = 4000
MAX_LOCATION = 300
MAX_PRICE = 100
MAX_SCHEDULE = 30
MAX_SCHEDULE_LABEL = 120
MAX_LINK = 2000
FETCH_TIMEOUT = 15
APP_FEED_REFRESH = timedelta(minutes=2)  # app feeds are cheap to check (if_revision)
SHARE_PREFIX = "bdh-share:"
REFRESH_LOCK = "hub_calendar:refreshing"

# Parsed feeds, shared by request threads and the refresh thread. Parses are
# serialized so a large feed is never held in memory more than once.
_parsed = {}  # feed id -> (fetched_at, calendar)
_parse_lock = threading.Lock()


# ---- time ---------------------------------------------------------------

def site_tz():
    where = hub_location()
    name = (where.timezone if where else "") or timezone.get_default_timezone_name()
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def now_local():
    return timezone.now().astimezone(site_tz()).replace(tzinfo=None)


def to_local(dt):
    """Aware datetime -> naive local wall-clock time."""
    return dt.astimezone(site_tz()).replace(tzinfo=None)


def from_local(naive):
    """Naive local wall-clock time -> aware datetime."""
    return naive.replace(tzinfo=site_tz())


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# ---- who is looking ------------------------------------------------------

def _current_stay(request):
    """The stay in progress, from the hub itself (running in-process only)."""
    try:
        from hub.modes import current_stay
    except ImportError:  # running outside the hub
        return None
    try:
        return current_stay(request)
    except Exception:
        log.exception("stay lookup failed")
        return None


def stay_key(stay):
    """Same id the hub exposes as ``hub_stay_key``."""
    arrival = stay["arrival"].isoformat() if stay.get("arrival") else ""
    return hashlib.sha256(f"{stay['guest']}|{arrival}".encode()).hexdigest()[:12]


class Viewer:
    """Who is using the calendar and what they may change.

    Guests (a stay is in progress and nobody is signed in) only change the
    events they added during their stay; those events are deleted at checkout.
    Everyone else changes any event added on the hub. Feed events are read-only.

    In its own container the app learns this from the hub's signed identity
    (``request.bdh_mode`` / ``request.bdh_stay_key``), which carries the stay's
    key but not its guest name or dates (:attr:`stay` is then None).
    """

    def __init__(self, request):
        self.admin = is_admin(request)
        self.stay = None
        if hasattr(request, "bdh_mode"):
            key = getattr(request, "bdh_stay_key", "") or ""
            self.current_stay_key = key
            self.guest = request.bdh_mode == "guest" and bool(key)
            self.stay_key = key if self.guest else ""
            return
        stay = None
        if not request.user.is_authenticated:
            stay = _current_stay(request)
            if stay and stay.get("owner"):
                stay = None
        self.stay = stay
        self.guest = stay is not None
        self.stay_key = stay_key(stay) if stay else ""
        self.current_stay_key = ""

    def can_edit(self, event):
        if not self.guest:
            return True
        return bool(event.stay_key) and event.stay_key == self.stay_key

    def new_event_fields(self):
        """Ownership fields for an event created now."""
        if not self.guest:
            return {"stay_key": "", "expires_at": None}
        checkout = self.stay.get("checkout") if self.stay else None
        if checkout is not None and timezone.is_naive(checkout):
            checkout = from_local(checkout)
        return {"stay_key": self.stay_key,
                "expires_at": checkout or timezone.now() + GUEST_EVENT_TTL}


def purge_expired(current_stay_key=""):
    """Delete events guests added once their stay is over.

    ``current_stay_key`` is the stay in progress as the hub reports it: when the
    checkout time isn't known, guest events of any other stay are over too.
    """
    Event.objects.filter(expires_at__isnull=False, expires_at__lte=timezone.now()).delete()
    if current_stay_key:
        Event.objects.exclude(stay_key="").exclude(stay_key=current_stay_key).delete()


# ---- feeds ----------------------------------------------------------------

def normalize_url(url):
    url = (url or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    return url


def parse_ics(text):
    import icalendar
    import x_wr_timezone

    cal = icalendar.Calendar.from_ical(text)
    return x_wr_timezone.to_standard(cal)


def _lines(chunks, limit=MAX_DOWNLOAD_BYTES):
    """Byte chunks -> decoded lines (with endings), at most ``limit`` bytes in all."""
    size, buf, head = 0, b"", ""
    for chunk in chunks:
        size += len(chunk)
        if size > limit:
            raise ValueError(f"calendar is larger than {limit // (1024 * 1024)} MB")
        buf += chunk
        *done, buf = buf.split(b"\n")
        for raw in done:
            # iCal is UTF-8 by spec; requests would guess Latin-1 for text/calendar.
            line = raw.decode("utf-8", errors="replace") + "\n"
            if head is not None:
                head += line
                if "BEGIN:VCALENDAR" in head.upper():
                    head = None
                elif len(head) > 4096:
                    raise ValueError("that link didn't return an iCal calendar")
            yield line
    if buf:
        yield buf.decode("utf-8", errors="replace")


def fetch_ics(url):
    """Download a feed, keeping only :data:`MAX_EVENTS` events (see :func:`trim_events`)."""
    import requests

    r = requests.get(url, timeout=FETCH_TIMEOUT, stream=True,
                     headers={"User-Agent": "BlackDiamondHub-Calendar/1.0"})
    r.raise_for_status()
    text = trim_events(_lines(r.iter_content(65536)))
    if "BEGIN:VCALENDAR" not in text[:4096].upper():
        raise ValueError("that link didn't return an iCal calendar")
    if len(text) > MAX_ICS_BYTES:
        raise ValueError("calendar is larger than 2 MB, even with only its next 500 events")
    return text


class ShareGone(Exception):
    """The app share was revoked or removed: drop the cached copy."""


def fetch_share(feed):
    """``(text, revision, attachments)`` of an app feed, or None when unchanged."""
    try:
        from bdh_sdk import shares
    except ImportError:
        raise ValueError("this hub's SDK doesn't support app shares")
    try:
        opened = shares.open_share(feed.provider, feed.share_name,
                                   if_revision=feed.share_revision if feed.ics else None)
    except shares.SharesError as exc:
        if exc.code == "forbidden":
            raise ShareGone("this calendar is no longer shared with Calendar")
        if exc.code == "not_found":
            raise ShareGone("the app isn't publishing this calendar (yet)")
        raise ValueError(f"the app share couldn't be read ({exc.code})")
    if opened is None:
        return None
    if len(opened.data) > MAX_ICS_BYTES:
        raise ValueError("calendar is larger than 2 MB")
    text = opened.data.decode("utf-8", errors="replace")
    if "BEGIN:VCALENDAR" not in text[:4096].upper():
        raise ValueError("the app didn't share an iCal calendar")
    attachments = {str(k): str(v) for k, v in (opened.attachments or {}).items()
                   if isinstance(v, str) and v.startswith("/")}
    return text, int(opened.revision), attachments


_DATE_RE = re.compile(r"^(DTEND|DTSTART)[^:]*:(\d{8})", re.I)


def trim_events(lines, limit=MAX_EVENTS, today=None):
    """Keep at most ``limit`` VEVENTs: repeating and upcoming ones (from 60
    days ago on) first, in feed order, then the most recent past ones.

    ``lines`` is the feed's text or an iterable of its lines; an iterable is
    consumed as it comes, so a big feed is never held (or parsed) whole.
    """
    import heapq

    if isinstance(lines, str):
        if lines.upper().count("BEGIN:VEVENT") <= limit:
            return lines
        lines = lines.splitlines(keepends=True)
    cutoff = ((today or date.today()) - timedelta(days=60)).strftime("%Y%m%d")
    head, tail = [], []  # everything outside VEVENTs, before / after the first one
    first, past = [], []  # (seq, text); past is a min-heap of (last date, seq, text)
    block, seq = None, 0
    for line in lines:
        upper = line[:16].upper()
        if block is None:
            if upper.startswith("BEGIN:VEVENT"):
                block, recurring, last = [line], False, ""
            else:
                (tail if seq else head).append(line)
            continue
        block.append(line)
        if upper.startswith(("RRULE", "RDATE")):
            recurring = True
        elif upper.startswith(("DTSTART", "DTEND")):
            m = _DATE_RE.match(line)
            if m and m.group(2) > last:
                last = m.group(2)
        elif upper.startswith("END:VEVENT"):
            seq += 1
            text = "".join(block)
            block = None
            if recurring or last >= cutoff:
                if len(first) < limit:
                    first.append((seq, text))
            elif len(past) < limit:
                heapq.heappush(past, (last, seq, text))
            else:
                heapq.heappushpop(past, (last, seq, text))
    room = limit - len(first)
    newest = heapq.nlargest(room, past) if room > 0 else []
    kept = sorted(first + [(s, t) for _, s, t in newest])
    return "".join(head) + "".join(t for _, t in kept) + "".join(tail)


def refresh_feed(feed):
    """Download a feed. On failure the last good copy is kept."""
    feed.last_attempt = timezone.now()
    try:
        if feed.source == "app":
            got = fetch_share(feed)
            if got is None:  # not modified
                feed.last_error = ""
                feed.save(update_fields=["last_attempt", "last_error"])
                return True
            text, revision, attachments = got
        else:
            text = fetch_ics(feed.url)
        text = trim_events(text)
        with _parse_lock:
            # A big feed parses to ~30 MB; drop the old copy first so two never coexist.
            _parsed.pop(feed.id, None)
            parsed = parse_ics(text)
    except ShareGone as exc:
        _parsed.pop(feed.id, None)
        feed.ics, feed.fetched_at, feed.share_revision, feed.attachments = "", None, 0, {}
        feed.last_error = str(exc)[:300]
        feed.save(update_fields=["ics", "fetched_at", "share_revision", "attachments",
                                 "last_attempt", "last_error"])
        log.warning("calendar feed %s: %s", feed.name, feed.last_error)
        return False
    except Exception as exc:
        feed.last_error = (str(exc) or exc.__class__.__name__)[:300]
        feed.save(update_fields=["last_attempt", "last_error"])
        log.warning("calendar feed %s failed: %s", feed.name, feed.last_error)
        return False
    feed.ics = text
    feed.fetched_at = feed.last_attempt
    feed.last_error = ""
    fields = ["ics", "fetched_at", "last_attempt", "last_error"]
    if feed.source == "app":
        feed.share_revision, feed.attachments = revision, attachments
        fields += ["share_revision", "attachments"]
    feed.save(update_fields=fields)
    _parsed[feed.id] = (feed.fetched_at, parsed)
    return True


def refresh_minutes():
    try:
        return max(5, int(app_setting(APP, "refresh", 15)))
    except (TypeError, ValueError):
        return 15


def stale_feeds():
    now = timezone.now()
    cutoff = now - timedelta(minutes=refresh_minutes())
    app_cutoff = now - APP_FEED_REFRESH
    return [f for f in Feed.objects.filter(enabled=True)
            if f.last_attempt is None
            or f.last_attempt < (app_cutoff if f.source == "app" else cutoff)]


def _refresh_worker(ids):
    try:
        for feed in Feed.objects.filter(id__in=ids):
            refresh_feed(feed)
    except Exception:
        log.exception("calendar refresh failed")
    finally:
        cache.delete(REFRESH_LOCK)
        close_old_connections()


def kick_refresh():
    """Refresh stale feeds in the background; returns at once."""
    ids = [f.id for f in stale_feeds()]
    if not ids or not cache.add(REFRESH_LOCK, 1, 300):
        return False
    threading.Thread(target=_refresh_worker, args=(ids,), daemon=True,
                     name="hub-calendar-refresh").start()
    return True


def _feed_calendar(feed):
    hit = _parsed.get(feed.id)
    if hit and hit[0] == feed.fetched_at:
        return hit[1]
    with _parse_lock:
        hit = _parsed.get(feed.id)
        if hit and hit[0] == feed.fetched_at:
            return hit[1]
        _parsed.pop(feed.id, None)
        try:
            cal = parse_ics(trim_events(feed.ics))
        except Exception:
            log.exception("calendar feed %s cached copy is unreadable", feed.name)
            return None
        _parsed[feed.id] = (feed.fetched_at, cal)
        return cal


def _local(value, tz):
    """iCal DTSTART/DTEND value -> (naive local datetime, is_date)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(tz).replace(tzinfo=None)
        return value, False
    return datetime.combine(value, time()), True


def _values(comp, name):
    """Every value of a (possibly repeated) property, as stripped strings."""
    raw = comp.get(name)
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for item in items:
        if isinstance(item, (list, tuple)):
            out.extend(str(x).strip() for x in item)
        else:
            out.append(str(item).strip())
    if name.startswith("X-"):
        # icalendar hands X- properties back raw; undo RFC 5545 TEXT escaping.
        out = [_TEXT_ESCAPE.sub(_unescape, v) for v in out]
    return [v for v in out if v]


_TEXT_ESCAPE = re.compile(r"\\([nN,;\\])")


def _unescape(m):
    c = m.group(1)
    return "\n" if c in "nN" else c


def _one_line(value, limit):
    return " ".join(value.split())[:limit]


def _web_link(value, https_only=False):
    value = value.strip()
    if len(value) > MAX_LINK or any(c.isspace() for c in value):
        return ""
    ok = ("https://",) if https_only else ("https://", "http://")
    return value if value.lower().startswith(ok) else ""


def share_image(ref, feed, hub_origin):
    """``bdh-share:<key>`` -> the hub URL of that attachment (app feeds only)."""
    if feed.source != "app" or not ref.startswith(SHARE_PREFIX):
        return ""
    try:
        from bdh_sdk import shares
        opened = shares.Opened(revision=feed.share_revision, type="text/calendar", title="",
                               data=b"", attachments=dict(feed.attachments or {}))
        return shares.resolve(ref, opened, hub_origin) or ""
    except (ImportError, AttributeError, TypeError):
        path = (feed.attachments or {}).get(ref[len(SHARE_PREFIX):])
        return (hub_origin or "").rstrip("/") + path if path else ""


_TIME = r"([01]?\d|2[0-3]):([0-5]\d)"
_SCHEDULE_RE = re.compile(rf"^{_TIME}(?:\s*[-\u2013\u2014]\s*{_TIME})?$")


def parse_schedule(values):
    """X-BDH-SCHEDULE values -> ``[{"start", "end", "label"}]``.

    Each value is ``HH:MM|Label`` or ``HH:MM–HH:MM|Label`` (24-hour time; the
    dash may be an en dash, em dash or hyphen). Anything else is skipped.
    """
    rows = []
    for value in values:
        if len(rows) >= MAX_SCHEDULE:
            break
        when, sep, label = value.partition("|")
        label = _one_line(label, MAX_SCHEDULE_LABEL)
        m = _SCHEDULE_RE.match(when.strip())
        if not sep or not label or not m:
            continue
        rows.append({
            "start": f"{int(m.group(1)):02d}:{m.group(2)}",
            "end": f"{int(m.group(3)):02d}:{m.group(4)}" if m.group(3) else "",
            "label": label,
        })
    return rows


def event_extras(comp, feed, hub_origin=""):
    """The bounded poster fields of a VEVENT: url, image, notes, location, price, schedule."""
    url = next((u for u in map(_web_link, _values(comp, "URL")) if u), "")
    image = ""
    for ref in _values(comp, "IMAGE"):
        image = share_image(ref, feed, hub_origin) if ref.startswith(SHARE_PREFIX) \
            else _web_link(ref, https_only=True)
        if image:
            break
    description = "\n".join(_values(comp, "DESCRIPTION"))
    price = next(iter(_values(comp, "X-BDH-PRICE")), "")
    return {
        "url": url,
        "image": image,
        "notes": description.replace("\r\n", "\n")[:MAX_DESCRIPTION],
        "location": _one_line(" ".join(_values(comp, "LOCATION")), MAX_LOCATION),
        "price": _one_line(price, MAX_PRICE),
        "schedule": parse_schedule(_values(comp, "X-BDH-SCHEDULE")),
    }


def feed_occurrences(feed, start, end, hub_origin=""):
    """Occurrences of a feed overlapping [start, end) (naive local datetimes)."""
    import recurring_ical_events

    cal = _feed_calendar(feed) if feed.ics else None
    if cal is None:
        return []
    tz = site_tz()
    people = [p.id for p in feed.people.all()]
    out = []
    try:
        comps = recurring_ical_events.of(cal, skip_bad_series=True).between(
            start.date() - timedelta(days=1), end.date() + timedelta(days=1))
    except Exception:
        log.exception("calendar feed %s could not be expanded", feed.name)
        return []
    for comp in comps:
        if comp.name != "VEVENT" or "DTSTART" not in comp:
            continue
        s, all_day = _local(comp["DTSTART"].dt, tz)
        if "DTEND" in comp:
            e, _ = _local(comp["DTEND"].dt, tz)
        elif "DURATION" in comp:
            e = s + comp["DURATION"].dt
        else:
            e = s + (timedelta(days=1) if all_day else timedelta())
        if e < s:
            e = s
        if not (s < end and (e > start or (e == s and s >= start))):
            continue
        uid = str(comp.get("UID", "")) or f"{s}"
        try:
            extras = event_extras(comp, feed, hub_origin)
        except Exception:
            log.exception("calendar feed %s: bad event extras", feed.name)
            extras = {"url": "", "image": "", "notes": "", "location": "", "price": "", "schedule": []}
        out.append({
            "id": f"f{feed.id}:{uid}:{iso(s)}",
            "source": f"feed:{feed.id}",
            "title": _one_line(str(comp.get("SUMMARY", "")), 200) or "(no title)",
            "start": iso(s), "end": iso(e), "all_day": all_day,
            **extras,
            "color": feed.color, "people": people,
            "editable": False, "repeat": "", "feed": feed.name,
        })
    return out


# ---- hub events -------------------------------------------------------------

_FREQ = {
    "daily": dict(freq=rrule.DAILY),
    "weekdays": dict(freq=rrule.DAILY, byweekday=(rrule.MO, rrule.TU, rrule.WE, rrule.TH, rrule.FR)),
    "weekly": dict(freq=rrule.WEEKLY),
    "biweekly": dict(freq=rrule.WEEKLY, interval=2),
    "monthly": dict(freq=rrule.MONTHLY),
    "yearly": dict(freq=rrule.YEARLY),
}


def wall_duration(event):
    """Length on the clock, so an all-day event stays a whole day across DST."""
    return to_local(event.end) - to_local(event.start)


def occurrence_starts(event, start, end):
    """Local start times of ``event`` whose occurrence overlaps [start, end).

    Repeats follow wall-clock time, so a 7 pm event stays at 7 pm across DST.
    """
    s = to_local(event.start)
    dur = wall_duration(event)
    if not event.repeat:
        return [s] if s < end and s + dur > start or (dur == timedelta() and start <= s < end) else []
    kw = dict(_FREQ[event.repeat])
    if event.until:
        kw["until"] = datetime.combine(event.until, time(23, 59, 59))
    rule = rrule.rrule(dtstart=s, **kw)
    skip = set(event.exdates or [])
    return [o for o in rule.between(start - dur, end, inc=True)
            if iso(o) not in skip and o < end and (o + dur > start or (dur == timedelta() and o >= start))]


def hub_occurrences(start, end, viewer, color):
    out = []
    events = (Event.objects.filter(start__lt=from_local(end))
              .prefetch_related("people"))
    for ev in events:
        if not ev.repeat and ev.end <= from_local(start) and ev.end != ev.start:
            continue
        people = [p.id for p in ev.people.all()]
        dur = wall_duration(ev)
        for o in occurrence_starts(ev, start, end):
            out.append({
                "id": f"h{ev.id}:{iso(o)}",
                "source": "hub", "event": ev.id, "occ": iso(o),
                "title": ev.title, "start": iso(o), "end": iso(o + dur),
                "all_day": ev.all_day, "location": ev.location, "notes": ev.notes,
                "color": color, "people": people,
                "editable": viewer.can_edit(ev), "guest": bool(ev.stay_key),
                "repeat": ev.repeat, "until": ev.until.isoformat() if ev.until else "",
                "series_start": iso(to_local(ev.start)),
            })
    return out


def hub_color():
    c = str(app_setting(APP, "hub_color", "#3fbf7f") or "").strip()
    return c if len(c) == 7 and c.startswith("#") else "#3fbf7f"


def occurrences(start, end, viewer, feeds=None, hub_origin=""):
    """All events overlapping [start, end), sorted by start.

    ``hub_origin`` (``request.bdh_hub_origin``) makes images apps share loadable.
    """
    feeds = list(Feed.objects.filter(enabled=True).prefetch_related("people")) if feeds is None else feeds
    out = hub_occurrences(start, end, viewer, hub_color())
    for feed in feeds:
        out.extend(feed_occurrences(feed, start, end, hub_origin))
    out.sort(key=lambda o: (o["start"][:10], not o["all_day"], o["start"], o["title"]))
    return out
