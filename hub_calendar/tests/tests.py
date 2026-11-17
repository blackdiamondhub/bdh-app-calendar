import json
from datetime import date, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hub_calendar import cal, homepage
from hub_calendar.models import Event, Feed, Person

TZ = {"timezone": "America/New_York"}

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
X-WR-TIMEZONE:America/New_York
BEGIN:VEVENT
UID:practice@test
DTSTART:20250901T220000Z
DTEND:20250901T233000Z
RRULE:FREQ=WEEKLY;COUNT=10
EXDATE:20250908T220000Z
SUMMARY:Practice
LOCATION:Field 2
END:VEVENT
BEGIN:VEVENT
UID:noschool@test
DTSTART;VALUE=DATE:20250903
DTEND;VALUE=DATE:20250905
SUMMARY:No school
END:VEVENT
END:VCALENDAR
"""


def L(s):
    return datetime.fromisoformat(s)


class _Viewer:
    guest = False

    def can_edit(self, event):
        return True


@override_settings(HUB_LOCATION=TZ)
class FeedTests(TestCase):
    def setUp(self):
        cal._parsed.clear()
        self.feed = Feed.objects.create(name="MIHS", url="https://x/ics", color="#4f8ff0", ics=ICS,
                                        fetched_at=timezone.now())

    def test_recurring_feed_events_in_local_time_with_exdate(self):
        evs = cal.feed_occurrences(self.feed, L("2025-09-01T00:00"), L("2025-09-29T00:00"))
        practice = [e["start"] for e in evs if e["title"] == "Practice"]
        # X-WR-TIMEZONE: 22:00 UTC is 6 pm in New York; Sep 8 was cancelled.
        self.assertEqual(practice, ["2025-09-01T18:00:00", "2025-09-15T18:00:00", "2025-09-22T18:00:00"])
        e = next(e for e in evs if e["title"] == "Practice")
        self.assertEqual((e["end"], e["location"], e["editable"], e["source"]),
                         ("2025-09-01T19:30:00", "Field 2", False, f"feed:{self.feed.id}"))

    def test_all_day_feed_event(self):
        evs = cal.feed_occurrences(self.feed, L("2025-09-04T00:00"), L("2025-09-05T00:00"))
        self.assertEqual([(e["title"], e["all_day"], e["start"], e["end"]) for e in evs],
                         [("No school", True, "2025-09-03T00:00:00", "2025-09-05T00:00:00")])

    def test_webcal_links_become_https(self):
        self.assertEqual(cal.normalize_url(" webcal://p01.icloud.com/x "), "https://p01.icloud.com/x")

    def test_failed_refresh_keeps_last_good_copy(self):
        with patch.object(cal, "fetch_ics", side_effect=OSError("timed out")):
            self.assertFalse(cal.refresh_feed(self.feed))
        self.feed.refresh_from_db()
        self.assertEqual((self.feed.ics, self.feed.last_error), (ICS, "timed out"))
        with patch.object(cal, "fetch_ics", return_value=ICS):
            self.assertTrue(cal.refresh_feed(self.feed))
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.last_error, "")

    def test_refresh_parses_once_and_reuses_it(self):
        real = cal.parse_ics
        with patch.object(cal, "fetch_ics", return_value=ICS), \
                patch.object(cal, "parse_ics", side_effect=real) as parse:
            self.assertTrue(cal.refresh_feed(self.feed))
            self.feed.refresh_from_db()
            self.assertTrue(cal.feed_occurrences(self.feed, L("2025-09-01T00:00"), L("2025-09-29T00:00")))
        self.assertEqual(parse.call_count, 1)

    def test_disabled_feeds_are_not_shown(self):
        self.feed.enabled = False
        self.feed.save()
        self.assertEqual(cal.occurrences(L("2025-09-01T00:00"), L("2025-09-29T00:00"), _Viewer()), [])


@override_settings(HUB_LOCATION=TZ)
class HubRecurrenceTests(TestCase):
    def ev(self, start, end, **kw):
        return Event.objects.create(title=kw.pop("title", "Piano"), start=cal.from_local(L(start)),
                                    end=cal.from_local(L(end)), **kw)

    def starts(self, a, b):
        return [o["start"] for o in cal.occurrences(L(a), L(b), _Viewer())]

    def test_weekly_keeps_wall_clock_time_across_dst(self):
        self.ev("2025-10-27T19:00", "2025-10-27T20:00", repeat="weekly", until=date(2025, 11, 10),
                exdates=["2025-11-03T19:00:00"])
        self.assertEqual(self.starts("2025-10-20T00:00", "2025-12-01T00:00"),
                         ["2025-10-27T19:00:00", "2025-11-10T19:00:00"])

    def test_all_day_event_on_dst_change_stays_one_day(self):
        self.ev("2025-11-02T00:00", "2025-11-03T00:00", all_day=True, title="Ski")
        [o] = cal.occurrences(L("2025-11-01T00:00"), L("2025-11-05T00:00"), _Viewer())
        self.assertEqual((o["start"], o["end"]), ("2025-11-02T00:00:00", "2025-11-03T00:00:00"))

    def test_weekdays_and_overlap_from_before_range(self):
        self.ev("2025-09-05T08:00", "2025-09-05T09:00", repeat="weekdays")  # a Friday
        self.ev("2025-09-07T23:00", "2025-09-08T01:00", title="Late")
        self.assertEqual(self.starts("2025-09-08T00:00", "2025-09-10T00:00"),
                         ["2025-09-07T23:00:00", "2025-09-08T08:00:00", "2025-09-09T08:00:00"])


@override_settings(HUB_LOCATION=TZ)
class ApiTests(TestCase):
    def setUp(self):
        p = patch.object(cal, "kick_refresh", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        self.url = reverse("hub_calendar_api_events")
        self.admin = get_user_model().objects.create_user("owner", password="x", is_staff=True)

    def post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

    def get_events(self, start="2025-09-01", end="2025-10-01"):
        r = self.client.get(self.url, {"start": start, "end": end})
        self.assertEqual(r.status_code, 200)
        return r.json()["events"]

    def weekly(self, **kw):
        body = {"title": "Piano", "start": "2025-09-01T16:00", "end": "2025-09-01T17:00", "repeat": "weekly"}
        body.update(kw)
        r = self.post(self.url, body)
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()["id"]

    def test_create_all_day_takes_inclusive_end(self):
        r = self.post(self.url, {"title": "Trip", "all_day": True, "start": "2025-09-05", "end": "2025-09-07"})
        self.assertEqual(r.status_code, 200)
        [e] = self.get_events()
        self.assertEqual((e["start"], e["end"], e["all_day"]), ("2025-09-05T00:00:00", "2025-09-08T00:00:00", True))

    def test_validation(self):
        self.assertEqual(self.post(self.url, {"title": " ", "start": "2025-09-01T10:00"}).status_code, 400)
        r = self.post(self.url, {"title": "x", "start": "2025-09-01T10:00", "end": "2025-09-01T09:00"})
        self.assertEqual(r.json()["error"], "The event ends before it starts.")
        self.assertEqual(self.client.get(self.url, {"start": "2025-01-01", "end": "2026-01-01"}).status_code, 400)

    def test_edit_this_event_only(self):
        pk = self.weekly()
        r = self.post(reverse("hub_calendar_api_event", args=[pk]),
                      {"title": "Piano recital", "start": "2025-09-08T18:00", "end": "2025-09-08T19:00",
                       "repeat": "weekly", "scope": "one", "occ": "2025-09-08T16:00:00"})
        self.assertEqual(r.status_code, 200)
        evs = [(e["start"], e["title"]) for e in self.get_events("2025-09-01", "2025-09-16")]
        self.assertEqual(evs, [("2025-09-01T16:00:00", "Piano"), ("2025-09-08T18:00:00", "Piano recital"),
                               ("2025-09-15T16:00:00", "Piano")])

    def test_edit_this_and_following(self):
        pk = self.weekly()
        self.post(reverse("hub_calendar_api_event", args=[pk]),
                  {"title": "Piano", "start": "2025-09-15T17:00", "end": "2025-09-15T18:00",
                   "repeat": "weekly", "scope": "following", "occ": "2025-09-15T16:00:00"})
        self.assertEqual([e["start"] for e in self.get_events("2025-09-01", "2025-09-23")],
                         ["2025-09-01T16:00:00", "2025-09-08T16:00:00", "2025-09-15T17:00:00", "2025-09-22T17:00:00"])
        self.assertEqual(Event.objects.get(pk=pk).until, date(2025, 9, 14))

    def test_edit_all_from_a_later_occurrence_moves_the_series(self):
        pk = self.weekly()
        self.post(reverse("hub_calendar_api_event", args=[pk]),
                  {"title": "Piano", "start": "2025-09-09T16:30", "end": "2025-09-09T17:30",
                   "repeat": "weekly", "scope": "all", "occ": "2025-09-08T16:00:00"})
        self.assertEqual([e["start"] for e in self.get_events("2025-09-01", "2025-09-16")],
                         ["2025-09-02T16:30:00", "2025-09-09T16:30:00"])

    def test_delete_scopes(self):
        pk = self.weekly(until="2025-09-29")
        url = reverse("hub_calendar_api_event_delete", args=[pk])
        self.post(url, {"scope": "one", "occ": "2025-09-08T16:00:00"})
        self.post(url, {"scope": "following", "occ": "2025-09-22T16:00:00"})
        self.assertEqual([e["start"] for e in self.get_events()], ["2025-09-01T16:00:00", "2025-09-15T16:00:00"])
        self.post(url, {"scope": "all"})
        self.assertFalse(Event.objects.exists())

    def guest(self, guest="Smiths"):
        stay = {"guest": guest, "arrival": timezone.make_aware(datetime(2025, 9, 1, 16)),
                "checkout": timezone.now() + timedelta(days=2), "owner": False}
        return patch.object(cal, "_current_stay", return_value=stay)

    def test_guests_only_change_their_own_events(self):
        owner = self.weekly()
        with self.guest():
            mine = self.post(self.url, {"title": "Hot tub", "start": "2025-09-02T20:00", "end": "2025-09-02T21:00"}).json()["id"]
            ev = Event.objects.get(pk=mine)
            self.assertTrue(ev.stay_key and ev.expires_at)
            evs = {e["title"]: e["editable"] for e in self.get_events("2025-09-01", "2025-09-03")}
            self.assertEqual(evs, {"Piano": False, "Hot tub": True})
            self.assertEqual(self.post(reverse("hub_calendar_api_event_delete", args=[owner]), {}).status_code, 403)
            self.assertEqual(self.post(reverse("hub_calendar_api_event", args=[owner]),
                                       {"title": "x", "start": "2025-09-01T10:00"}).status_code, 403)
            self.assertEqual(self.post(reverse("hub_calendar_api_event_delete", args=[mine]), {}).status_code, 200)
        with self.guest("Joneses"):
            other = self.post(self.url, {"title": "Ski", "start": "2025-09-03T09:00"}).json()["id"]
        with self.guest():
            self.assertEqual(self.post(reverse("hub_calendar_api_event_delete", args=[other]), {}).status_code, 403)
        # The owner can change anything added on the hub.
        self.client.force_login(self.admin)
        self.assertEqual(self.post(reverse("hub_calendar_api_event_delete", args=[other]), {}).status_code, 200)

    def test_guest_events_are_removed_after_checkout(self):
        e = Event.objects.create(title="Old", start=timezone.now(), end=timezone.now(), stay_key="abc",
                                 expires_at=timezone.now() - timedelta(minutes=1))
        Event.objects.create(title="Keep", start=timezone.now(), end=timezone.now())
        self.get_events()
        self.assertFalse(Event.objects.filter(pk=e.pk).exists())
        self.assertTrue(Event.objects.filter(title="Keep").exists())

    def test_page_renders(self):
        r = self.client.get(reverse("hub_calendar"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'id="cal-config"')
        with self.guest():
            self.assertContains(self.client.get(reverse("hub_calendar")), "removed after checkout")


@override_settings(HUB_LOCATION=TZ)
class SettingsPageTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user("owner", password="x", is_staff=True)

    def test_admin_only(self):
        self.assertEqual(self.client.get(reverse("hub_calendar_feeds")).status_code, 404)
        self.assertEqual(self.client.get(reverse("hub_calendar_people")).status_code, 404)

    def test_add_person_and_feed(self):
        self.client.force_login(self.admin)
        r = self.client.post(reverse("hub_calendar_people"), {"action": "add", "name": "Ava Slivinski", "color": "#e07bd0"})
        self.assertRedirects(r, reverse("hub_calendar_people"))
        ava = Person.objects.get()
        self.assertEqual((ava.initials, ava.color), ("AS", "#e07bd0"))
        with patch.object(cal, "fetch_ics", return_value=ICS) as fetch:
            r = self.client.post(reverse("hub_calendar_feeds"),
                                 {"action": "add", "name": "MIHS", "url": "webcal://example.org/mihs.ics",
                                  "people": [ava.id]})
        self.assertRedirects(r, reverse("hub_calendar_feeds"), fetch_redirect_response=False)
        fetch.assert_called_once_with("https://example.org/mihs.ics")
        feed = Feed.objects.get()
        self.assertEqual((feed.ics, list(feed.people.all())), (ICS, [ava]))
        page = self.client.get(reverse("hub_calendar_feeds"))
        self.assertContains(page, "Added MIHS.")
        self.assertContains(page, "Updated just now")
        self.assertContains(page, '<details class="hst-card hc-feed" open>')
        self.assertContains(page, 'id="hc-add" hidden')
        self.assertNotContains(self.client.get(reverse("hub_calendar_feeds")), "<details class=\"hst-card hc-feed\" open>")

    def test_rejected_new_feed_keeps_the_add_form_open(self):
        self.client.force_login(self.admin)
        Feed.objects.create(name="Old", url="https://example.org/old.ics", order=0)
        r = self.client.post(reverse("hub_calendar_feeds"), {"action": "add", "name": "Bad", "url": "ftp://example.org/x"})
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'id="hc-add" hidden')
        self.assertContains(r, 'id="hc-addbtn" hidden')

    def test_feed_that_fails_to_load_is_kept_with_error(self):
        self.client.force_login(self.admin)
        with patch.object(cal, "fetch_ics", side_effect=ValueError("that link didn't return an iCal calendar")):
            r = self.client.post(reverse("hub_calendar_feeds"),
                                 {"action": "add", "name": "Bad", "url": "https://example.org/x"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "couldn&#x27;t be loaded")
        self.assertTrue(Feed.objects.filter(name="Bad").exists())


@override_settings(HUB_LOCATION=TZ)
class StatusTests(TestCase):
    def test_tile_summarises_today_and_upcoming(self):
        now = cal.now_local().replace(second=0, microsecond=0)
        later = now + timedelta(hours=1)
        if later.date() != now.date():
            later = now
        Event.objects.create(title="Dinner", start=cal.from_local(later), end=cal.from_local(later + timedelta(hours=1)))
        tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
        Event.objects.create(title="Ski", start=cal.from_local(tomorrow), end=cal.from_local(tomorrow + timedelta(days=1)),
                             all_day=True)
        with patch.object(cal, "kick_refresh", return_value=False):
            s = homepage.status()
        self.assertEqual(s.text, "1 today")
        self.assertEqual([(r.title, r.dimmed) for r in s.rows], [("Dinner", False), ("Ski", True)])
        self.assertEqual(s.rows[1].detail, "Tomorrow · all day")

    def test_feed_rows_survive_the_hub_status_check(self):
        from bdh_sdk.hooks import status_value

        tomorrow = cal.now_local().date() + timedelta(days=1)
        feed = Feed.objects.create(name="School", url="https://example.org/s.ics", ics=(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:abc@google.com\r\n"
            f"DTSTART;VALUE=DATE:{tomorrow:%Y%m%d}\r\nSUMMARY:Field trip\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"), fetched_at=timezone.now())
        self.assertTrue(feed.ics)
        with patch.object(cal, "kick_refresh", return_value=False):
            value = status_value(homepage.status())
        self.assertEqual([r["title"] for r in value["rows"]], ["Field trip"])

    def test_empty(self):
        with patch.object(cal, "kick_refresh", return_value=False):
            self.assertEqual(homepage.status().text, "Nothing this week")


class SiteTzTests(TestCase):
    def test_follows_hub_location(self):
        from bdh_sdk.location import Location
        with patch.object(cal, "hub_location", return_value=Location(timezone="America/Vancouver")):
            self.assertEqual(str(cal.site_tz()), "America/Vancouver")

    @override_settings(TIME_ZONE="America/Toronto")
    def test_falls_back_to_django_time_zone(self):
        with patch.object(cal, "hub_location", return_value=None):
            self.assertEqual(str(cal.site_tz()), "America/Toronto")


class ContainerViewerTests(TestCase):
    """In its own container the hub's signed identity says who is looking."""

    def request(self, mode, stay_key):
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory
        req = RequestFactory().get("/calendar/")
        req.user = AnonymousUser()
        req.bdh_mode, req.bdh_stay_key = mode, stay_key
        return req

    def test_guest_owns_events_of_their_stay(self):
        with patch.object(cal, "_current_stay") as hub_stay:
            viewer = cal.Viewer(self.request("guest", "k1"))
        hub_stay.assert_not_called()
        self.assertTrue(viewer.guest)
        fields = viewer.new_event_fields()
        self.assertEqual(fields["stay_key"], "k1")
        self.assertGreater(fields["expires_at"], timezone.now() + timedelta(days=13))
        now = timezone.now()
        self.assertTrue(viewer.can_edit(Event(title="a", start=now, end=now, stay_key="k1")))
        self.assertFalse(viewer.can_edit(Event(title="b", start=now, end=now, stay_key="k2")))
        self.assertFalse(viewer.can_edit(Event(title="c", start=now, end=now)))

    def test_dashboard_and_owner_are_not_guests(self):
        for mode, key in (("dashboard", ""), ("owner", "k1"), ("guest", "")):
            viewer = cal.Viewer(self.request(mode, key))
            self.assertFalse(viewer.guest, mode)
            self.assertEqual(viewer.new_event_fields(), {"stay_key": "", "expires_at": None})
        self.assertEqual(cal.Viewer(self.request("owner", "k1")).current_stay_key, "k1")

    def test_other_stays_events_are_purged(self):
        now = timezone.now()
        later = now + timedelta(days=3)
        for title, key in (("Old stay", "k1"), ("This stay", "k2"), ("Hub", "")):
            Event.objects.create(title=title, start=now, end=now, stay_key=key,
                                 expires_at=later if key else None)
        cal.purge_expired()
        self.assertEqual(Event.objects.count(), 3)
        cal.purge_expired("k2")
        self.assertEqual(sorted(Event.objects.values_list("title", flat=True)), ["Hub", "This stay"])


@override_settings(HUB_LOCATION=TZ)
class BrokerTests(TestCase):
    """The app with ``bdh_sdk.rpc`` pointed at a fake hub broker."""

    def setUp(self):
        from bdh_sdk import settings, storage
        from bdh_sdk.testing import FakeBroker

        saved = (settings._store, storage._backend)

        def restore():
            settings.set_store(saved[0])
            storage.set_state_backend(saved[1])
        self.broker = FakeBroker(grants=("settings.*", "state.*"), install=True)
        self.broker.start()
        self.addCleanup(restore)
        self.addCleanup(self.broker.stop)

    def test_page_reads_settings_from_the_hub(self):
        self.broker.settings.update({("hub_calendar", "default_view"): "agenda",
                                     ("hub_calendar", "week_start"): "mon"})
        r = self.client.get(reverse("hub_calendar"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.context["config"]["view"], r.context["config"]["weekStart"]), ("agenda", 1))
        self.assertIn({"app": "hub_calendar", "key": "default_view"}, self.broker.ops("settings.get"))


def vevent(uid, start, *extra, rrule=False):
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTART:{start}T180000Z", f"SUMMARY:{uid}"]
    if rrule:
        lines.append("RRULE:FREQ=WEEKLY")
    return "\r\n".join(lines + list(extra) + ["END:VEVENT", ""])


def vcal(*events):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n" + "".join(events) + "END:VCALENDAR\r\n"


POSTER = vcal(vevent(
    "fest", "20250913",
    "DESCRIPTION:Live music on the village stage.\\nBring a chair\\, it's free.",
    "LOCATION:Village Day Plaza",
    "URL:https://sunpeaksresort.com/events/fest",
    "IMAGE;VALUE=URI:bdh-share:hero",
    "X-BDH-PRICE:Free",
    "X-BDH-SCHEDULE:11:00\u201312:00|Kids' parade",
    "X-BDH-SCHEDULE:13:30|Headliner",
    "X-BDH-SCHEDULE:nonsense",
))


class TrimTests(TestCase):
    def test_keeps_repeating_and_upcoming_events_then_newest_past(self):
        evs = [vevent("weekly", "20200106", rrule=True), vevent("old", "20200301"),
               vevent("older", "20190301"), vevent("soon", "20250910"), vevent("recent", "20250801")]
        text = cal.trim_events(vcal(*evs), limit=3, today=date(2025, 9, 1))
        self.assertEqual([u for u in ("weekly", "old", "older", "soon", "recent") if f"UID:{u}\r" in text],
                         ["weekly", "soon", "recent"])
        self.assertTrue(text.startswith("BEGIN:VCALENDAR") and text.endswith("END:VCALENDAR\r\n"))

    def test_streamed_download_is_trimmed_before_it_is_kept(self):
        body = vcal(*[vevent(f"e{i}", "20990101") for i in range(cal.MAX_EVENTS + 20)]).encode()
        chunks = [body[i:i + 1000] for i in range(0, len(body), 1000)]
        text = cal.trim_events(cal._lines(chunks))
        self.assertEqual(text.count("BEGIN:VEVENT"), cal.MAX_EVENTS)
        cal.parse_ics(text)
        with self.assertRaises(ValueError):
            list(cal._lines(chunks, limit=len(body) - 1))
        with self.assertRaisesMessage(ValueError, "didn't return an iCal"):
            list(cal._lines([b"<html>" + b"x" * 5000 + b"\n"]))


class ExtrasTests(TestCase):
    def event(self, *props, source="url", attachments=None):
        feed = Feed(name="F", url="https://x", source=source, attachments=attachments or {})
        [comp] = cal.parse_ics(vcal(vevent("e", "20250913", *props))).walk("VEVENT")
        return cal.event_extras(comp, feed, "https://hub.example/")

    def test_schedule_rows(self):
        rows = cal.parse_schedule(["9:05-10:00|Warm up", "11:00\u201412:00|  Race  ", "13:00|Awards",
                                   "25:00|Bad hour", "10:00", "|No time", "10:00|", "noon|Lunch"])
        self.assertEqual(rows, [{"start": "09:05", "end": "10:00", "label": "Warm up"},
                                {"start": "11:00", "end": "12:00", "label": "Race"},
                                {"start": "13:00", "end": "", "label": "Awards"}])
        many = cal.parse_schedule([f"10:00|{'x' * 500}"] * 50)
        self.assertEqual((len(many), len(many[0]["label"])), (cal.MAX_SCHEDULE, cal.MAX_SCHEDULE_LABEL))

    def test_bounds_and_links(self):
        x = self.event("DESCRIPTION:" + "d" * 5000, "URL:javascript:alert(1)",
                       "IMAGE;VALUE=URI:http://insecure.example/a.jpg", "X-BDH-PRICE:$25\\n adults")
        self.assertEqual((len(x["notes"]), x["url"], x["image"], x["price"]),
                         (cal.MAX_DESCRIPTION, "", "", "$25 adults"))
        x = self.event("URL:http://example.org/e", "IMAGE;VALUE=URI:https://img.example/a.jpg")
        self.assertEqual((x["url"], x["image"]), ("http://example.org/e", "https://img.example/a.jpg"))

    def test_share_images_only_resolve_for_app_feeds(self):
        att = {"hero": "/apps/_share/abc"}
        self.assertEqual(self.event("IMAGE;VALUE=URI:bdh-share:hero", attachments=att)["image"], "")
        self.assertEqual(self.event("IMAGE;VALUE=URI:bdh-share:hero", source="app", attachments=att)["image"],
                         "https://hub.example/apps/_share/abc")
        self.assertEqual(self.event("IMAGE;VALUE=URI:bdh-share:missing", source="app", attachments=att)["image"], "")

    @override_settings(HUB_LOCATION=TZ)
    def test_feed_event_carries_poster_fields(self):
        feed = Feed.objects.create(name="Sun Peaks", source="app", provider="events", share_name="events",
                                   ics=POSTER, fetched_at=timezone.now(), attachments={"hero": "/apps/_share/h"})
        [e] = cal.feed_occurrences(feed, L("2025-09-13T00:00"), L("2025-09-14T00:00"), "https://hub")
        self.assertEqual(e["notes"], "Live music on the village stage.\nBring a chair, it's free.")
        self.assertEqual((e["location"], e["price"], e["image"], e["url"]),
                         ("Village Day Plaza", "Free", "https://hub/apps/_share/h",
                          "https://sunpeaksresort.com/events/fest"))
        self.assertEqual([r["label"] for r in e["schedule"]], ["Kids' parade", "Headliner"])


@override_settings(HUB_LOCATION=TZ)
class AppFeedTests(TestCase):
    """Calendars other apps share with Calendar, via a fake hub broker."""

    def setUp(self):
        import base64

        from bdh_sdk import settings, storage
        from bdh_sdk.testing import FakeBroker

        cal._parsed.clear()
        saved = (settings._store, storage._backend)

        def restore():
            settings.set_store(saved[0])
            storage.set_state_backend(saved[1])
        self.b64 = lambda data: base64.b64encode(data).decode()
        self.broker = FakeBroker(install=True, app_id="hub_calendar")
        self.broker.start()
        self.addCleanup(restore)
        self.addCleanup(self.broker.stop)
        self.broker.set_manifest_shares("events", [
            {"name": "events", "type": "text/calendar", "title": "Sun Peaks events"},
            {"name": "photos", "type": "image/jpeg", "title": "Photos"}])
        self.broker.set_manifest_shares("tides", [
            {"name": "tides", "type": "text/calendar", "title": "Tides"}])
        self.admin = get_user_model().objects.create_user("owner", password="x", is_staff=True)

    def publish(self, ics=POSTER):
        self.broker.app_id = "events"
        try:
            img = self.broker._share("shares.put_blob", {"mime": "image/jpeg", "data_b64": self.b64(b"jpeg")})
            main = self.broker._share("shares.put_blob", {"mime": "text/calendar", "data_b64": self.b64(ics.encode())})
            self.broker._share("shares.publish", {"name": "events", "main": main["sha256"],
                                                  "attachments": {"hero": img["sha256"]}})
            return img["sha256"]
        finally:
            self.broker.app_id = "hub_calendar"

    def feed(self):
        return Feed.objects.create(name="Sun Peaks", source="app", provider="events", share_name="events")

    def week(self, feed, origin="https://hub.local"):
        return cal.occurrences(L("2025-09-08T00:00"), L("2025-09-15T00:00"), _Viewer(), [feed], origin)

    def test_granted_share_shows_events_and_reuses_the_cache(self):
        self.broker.grant_share("hub_calendar", "events", "events")
        img = self.publish()
        feed = self.feed()
        self.assertTrue(cal.refresh_feed(feed))
        feed.refresh_from_db()
        self.assertEqual((feed.share_revision, feed.last_error), (1, ""))
        [e] = self.week(feed)
        self.assertEqual((e["title"], e["image"]), ("fest", f"https://hub.local/apps/_share/{img}"))
        with patch.object(cal, "parse_ics") as parse:
            self.assertTrue(cal.refresh_feed(feed))
            self.assertEqual(len(self.week(feed)), 1)
        parse.assert_not_called()
        self.assertEqual(self.broker.ops("shares.open")[-1]["if_revision"], 1)

    def test_revoked_share_is_an_error_not_a_crash(self):
        self.broker.grant_share("hub_calendar", "events", "events")
        self.publish()
        feed = self.feed()
        self.assertTrue(cal.refresh_feed(feed))
        self.broker.revoke_share("hub_calendar", "events", "events")
        self.assertFalse(cal.refresh_feed(feed))
        feed.refresh_from_db()
        self.assertEqual((feed.ics, feed.share_revision), ("", 0))
        self.assertIn("no longer shared", feed.last_error)
        self.assertEqual(self.week(feed), [])
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("hub_calendar_feeds")), "no longer shared")

    def test_add_form_lists_only_granted_calendar_shares(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse("hub_calendar_feeds"))
        self.assertContains(page, "No apps are sharing calendars with Calendar yet.")
        self.broker.grant_share("hub_calendar", "events", "events")
        self.broker.grant_share("hub_calendar", "events", "photos")
        page = self.client.get(reverse("hub_calendar_feeds"))
        self.assertContains(page, '<option value="events/events" data-title="Sun Peaks events">'
                                  'Sun Peaks events — from Events</option>', html=False)
        self.assertNotContains(page, "events/photos")
        self.assertNotContains(page, "tides/tides")
        self.assertNotContains(page, "No apps are sharing")

    def test_shared_feeds_named_by_app_name_not_id(self):
        self.client.force_login(self.admin)
        listed = [{"provider": "sunpeaks_events", "provider_title": "Sun Peaks Events", "name": "events",
                   "title": "Sun Peaks events", "type": "text/calendar", "revision": 1, "updated": None},
                  {"provider": "lodge_tides", "name": "tides", "title": "Tides", "type": "text/calendar",
                   "revision": 1, "updated": None}]
        with patch("bdh_sdk.shares.list_shares", return_value=listed):
            page = self.client.get(reverse("hub_calendar_feeds"))
        self.assertContains(page, "Sun Peaks events — from Sun Peaks Events")
        self.assertContains(page, "Tides — from Lodge Tides")
        self.assertNotContains(page, "from sunpeaks_events")

    def test_add_app_feed(self):
        self.client.force_login(self.admin)
        url = reverse("hub_calendar_feeds")
        r = self.client.post(url, {"action": "add", "name": "Tides", "source": "app", "share": "tides/tides"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Feed.objects.exists())
        self.broker.grant_share("hub_calendar", "events", "events")
        self.publish()
        r = self.client.post(url, {"action": "add", "name": "Village", "source": "app", "share": "events/events",
                                   "url": "https://ignored.example/x"})
        self.assertRedirects(r, url, fetch_redirect_response=False)
        feed = Feed.objects.get()
        self.assertEqual((feed.source, feed.provider, feed.share_name, feed.url, feed.share_revision),
                         ("app", "events", "events", "", 1))
        self.assertContains(self.client.get(url), "Sun Peaks events — from Events")


@override_settings(HUB_LOCATION=TZ)
class PosterTests(TestCase):
    def test_qr_code(self):
        url = reverse("hub_calendar_api_qr")
        r = self.client.get(url, {"u": "https://sunpeaksresort.com/events/fest"})
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "image/svg+xml"))
        self.assertIn(b"<svg", r.content)
        self.assertEqual(self.client.get(url, {"u": "javascript:alert(1)"}).status_code, 400)

    def test_page_has_escaped_poster(self):
        r = self.client.get(reverse("hub_calendar"))
        self.assertContains(r, 'id="poster"')
        self.assertContains(r, "Scan for details")
        self.assertContains(r, "esc(e.notes)")

    def test_event_text_reaches_the_page_as_json_data(self):
        Feed.objects.create(name="X", ics=vcal(vevent("e", "20250913", "DESCRIPTION:<img src=x onerror=alert(1)>")),
                            url="https://x", fetched_at=timezone.now())
        with patch.object(cal, "kick_refresh", return_value=False):
            r = self.client.get(reverse("hub_calendar_api_events"), {"start": "2025-09-13", "end": "2025-09-14"})
        self.assertEqual(r["Content-Type"], "application/json")
        self.assertEqual(r.json()["events"][0]["notes"], "<img src=x onerror=alert(1)>")
