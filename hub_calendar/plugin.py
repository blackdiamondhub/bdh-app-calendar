from bdh_sdk import PluginSpec, Setting, SettingsPage, Tile
from bdh_sdk.plugins import CHOICE, INT, LIFECYCLE_ON_DEMAND, NETWORK_OUTBOUND

spec = PluginSpec(
    id="hub_calendar",
    app="hub_calendar",
    urls="hub_calendar.urls",
    url_prefix="calendar/",
    status="hub_calendar.homepage.status",
    tiles=(
        Tile(
            title="Calendar",
            url_name="hub_calendar",
            icon="hub_calendar/hub/icon.svg",
            section="lodge",
            subtitle="Family calendar and events",
            order=15,
        ),
    ),
    settings=(
        Setting("default_view", "Opens in", type=CHOICE, default="month",
                choices=(("month", "Month"), ("week", "Week"), ("day", "Day"),
                         ("agenda", "Agenda"))),
        Setting("theme", "Look", type=CHOICE, default="glass",
                choices=(("glass", "Glass"), ("paper", "Paper"), ("classic", "Classic")),
                help="Glass and Paper sit on a painting; Classic is the plain dark look."),
        Setting("week_start", "Week starts on", type=CHOICE, default="sun",
                choices=(("sun", "Sunday"), ("mon", "Monday"))),
        Setting("refresh", "Refresh feeds every", type=INT, default=15,
                minimum=5, maximum=1440, unit="min"),
        Setting("hub_color", "Colour of hub events", default="#3fbf7f",
                help="Events added on this hub, as a hex colour like #3fbf7f."),
    ),
    settings_pages=(
        SettingsPage("Calendar feeds", "hub_calendar_feeds",
                     subtitle="iCal links and calendars other apps share, shown on this hub",
                     icon="fas fa-rss"),
        SettingsPage("People", "hub_calendar_people",
                     subtitle="Who events can be tagged with", icon="fas fa-user-tag"),
    ),
    api_version=2,
    # Only its own settings; the stay comes with the hub's identity headers.
    permissions=(),
    # Fetches the iCal feeds (school, Google, iCloud).
    network=NETWORK_OUTBOUND,
    # Feeds are refreshed when the page or the hub's status hook asks; the
    # hourly wake keeps the tile and feeds current while nobody opens it.
    lifecycle=LIFECYCLE_ON_DEMAND,
    wake_every=3600,
    # Calendars other apps share (Settings > Apps > <app> > Sharing).
    reads=("text/calendar",),
    # A parsed feed takes ~15x its size (feeds are capped at 2 MB / 500 events)
    # and a refresh parses the new copy while the old one is cached.
    memory_mb=192,
)
