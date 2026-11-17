# bdh-app-calendar

A family calendar app for [Black Diamond Hub](https://github.com/blackdiamondlodge/BlackDiamondHub).
It merges any number of iCal subscriptions (school calendars, Google, iCloud …)
with events added right on the hub.

Since 2.0 it runs in its own container (hub_api 2): it reads its settings and
the current stay from the hub, and needs outbound network access for the feeds.

## Features

- **Month, week and agenda views.** The week view covers the full 24 hours;
  it scrolls vertically with bouncing arrows instead of scrollbars.
- **Calendar feeds.** Subscribe to iCal/webcal links. Feeds refresh in the
  background (every 15 minutes by default) and the last good copy is kept if
  a refresh fails. Feed events are read-only.
- **Calendars from other apps.** Apps on the hub can share a calendar
  (`text/calendar`) with Calendar; add it under *Add feed → From an app*. An
  admin grants the share in Settings → Apps → *app* → Sharing. Shared feeds
  are checked every couple of minutes and only re-read when the app publishes
  a new revision. If the share is revoked the feed shows an error instead of
  events; pausing a feed in Calendar doesn't revoke anything.
- **Event posters.** Events with a photo, link, description or schedule open
  as a poster: the photo full-bleed with a frosted panel holding the time,
  place, price, schedule, description and a QR code for the event's link.
- **Events added on the hub,** including repeating events (daily, weekdays,
  weekly, every 2 weeks, monthly, yearly) with "this event / this and
  following / all events" editing.
- **People.** Tag events and feeds with people and filter the calendar by
  person or by calendar. Colours are per feed.
- **Guests.** During a stay, guests can add events, but can only change or
  delete events they added. Their events are removed after checkout.
- **Homepage tile** showing today's count and the next few events.

## Settings (Settings → Calendar)

| Setting | |
|---|---|
| Calendar feeds | Add, colour, tag, pause, refresh or remove iCal subscriptions |
| People | The people events can be tagged with |
| Default view | Month, week or agenda |
| Week starts on | Sunday or Monday |
| Refresh feeds every | Minutes between feed refreshes (minimum 5) |
| Colour for hub events | Colour of events added on the hub |

Every hub has its own feeds, so a guest hub can stay free of personal calendars.

### Feed limits

A feed may be at most 2 MB; only the 500 most relevant events are kept
(repeating events, and events from the last 60 days onwards, first).

### Event details

Calendar reads these VEVENT properties from every feed:

| Property | Shown as |
|---|---|
| `DESCRIPTION` | Plain text (newlines kept), up to 4000 characters |
| `LOCATION` | 📍 line, up to 300 characters |
| `URL` | QR code ("Scan for details"); `http(s)` links only |
| `IMAGE;VALUE=URI` | Poster photo. `https://` only, or `bdh-share:<key>` for an attachment of a calendar shared by an app |
| `X-BDH-PRICE` | 🎟️ line: free text, one line, up to 100 characters, e.g. `X-BDH-PRICE:$25 adults · free under 12` |
| `X-BDH-SCHEDULE` | Schedule rows (see below) |

`X-BDH-SCHEDULE` may appear up to 30 times per event (in order), one row
each. A value is `HH:MM|Label` or `HH:MM–HH:MM|Label`: 24-hour local times,
the range separated by an en dash, em dash or hyphen, then `|` and a one-line
label (up to 120 characters). Remember iCal escaping (`\,` and `\;`) in
labels. Malformed rows are skipped:

```
X-BDH-SCHEDULE:09:00–10:30|First Chair · Sunburst Express
X-BDH-SCHEDULE:10:00|Harry Bear & cinnamon buns
X-BDH-SCHEDULE:15:00-18:00|Après & Ales at Masa's
```

### Finding feed links

- **Google Calendar:** Settings → *your calendar* → Integrate calendar →
  **Secret address in iCal format**.
- **iCloud (iPhone):** Calendar app → ⓘ next to the calendar → **Public
  Calendar** → Share Link. `webcal://` links are accepted.
- **Mercer Island School District (Finalsite):**
  `https://www.mercerislandschools.org/fs/calendar-manager/events.ics?calendar_ids[]=<ID>`
  where the ID is 32 (MIHS), 14 (IMS), 30 (district), 15 (MIHS athletics),
  26 (Lakeridge), 25 (Island Park), 27 (Northwood), 28 (West Mercer).

## Changelog

- **2.1.0** — Calendars shared by other apps (*From an app* feeds, bdh-sdk
  0.35 shares); event posters with photo, schedule, price and QR code;
  `URL`, `IMAGE`, `X-BDH-PRICE` and `X-BDH-SCHEDULE` support; feeds capped at
  2 MB / 500 events to keep memory low.

## Development

Tests run inside a hub checkout:

```
pip install -e /path/to/bdh-app-calendar
python manage.py test hub_calendar.tests --settings=BlackDiamondHub.settings_test
```
