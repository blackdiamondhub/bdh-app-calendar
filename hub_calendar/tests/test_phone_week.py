from datetime import date, timedelta

from django.test import override_settings, tag
from django.urls import reverse
from selenium.webdriver.support.ui import WebDriverWait

from tests.selenium_helpers import SeleniumTestCase, wait_for_paint

# Synthetic pointer drag on a day column: arguments are column index, dx, dy.
DRAG = """
const col = document.querySelectorAll('.xbody .xcol')[arguments[0]], r = col.getBoundingClientRect();
const x = r.left + r.width / 2, y = r.top + r.height / 2, o = {pointerId: 7, bubbles: true, isPrimary: true, pointerType: 'touch'};
col.dispatchEvent(new PointerEvent('pointerdown', Object.assign({clientX: x, clientY: y}, o)));
for (let i = 1; i <= 8; i++)
  col.dispatchEvent(new PointerEvent('pointermove', Object.assign({clientX: x + arguments[1] * i / 8, clientY: y + arguments[2] * i / 8}, o)));
col.dispatchEvent(new PointerEvent('pointerup', Object.assign({clientX: x + arguments[1], clientY: y + arguments[2]}, o)));
"""
DAYS = "return [...document.querySelectorAll('.xbody .xcol')].map(c => c.dataset.day)"


def shift(key, n):
    return (date.fromisoformat(key) + timedelta(days=n)).isoformat()


@tag("selenium")
@override_settings(HUB_LOCATION={"timezone": "America/New_York"})
class PhoneWeekTest(SeleniumTestCase):
    """Week view on a phone-width screen: a few day columns, swiped sideways a day at a time."""

    def setUp(self):
        super().setUp()
        b = self.browser
        b.set_window_size(414, 896)
        self.addCleanup(b.set_window_size, 1920, 1080)
        b.get(self.live_server_url + reverse("hub_calendar"))
        b.execute_script("localStorage.setItem('hubcal.view', 'week')")
        b.refresh()
        wait_for_paint(b)
        WebDriverWait(b, 10).until(lambda d: d.execute_script(DAYS))
        self.start = b.execute_script(DAYS)

    def days(self):
        return self.browser.execute_script(DAYS)

    def test_shows_three_days_from_today_with_a_week_strip(self):
        b = self.browser
        self.assertEqual(len(self.start), 3)
        self.assertEqual(self.start[0], b.execute_script("return document.querySelector('.xpd.today').dataset.pday"))
        strip = b.execute_script("return [...document.querySelectorAll('.xpd')].map(e => [e.dataset.pday, e.classList.contains('vis')])")
        self.assertEqual([k for k, vis in strip if vis], [k for k, _ in strip if k in self.start])
        b.execute_script("document.getElementById('c-next').click()")
        WebDriverWait(b, 5).until(lambda d: self.days()[0] == shift(self.start[0], 3))

    def test_sideways_swipe_moves_one_day_and_vertical_drag_scrolls_the_column(self):
        b = self.browser
        b.execute_script(DRAG, 1, -120, 12)
        WebDriverWait(b, 5).until(lambda d: self.days()[0] == shift(self.start[0], 1))
        b.execute_script(DRAG, 1, 120, -10)
        WebDriverWait(b, 5).until(lambda d: self.days() == self.start)
        b.execute_script(DRAG, 1, 14, 160)
        self.assertEqual(self.days(), self.start)
        self.assertEqual(b.execute_script("return [...document.querySelectorAll('.xbody .xcol')].map(c => c.classList.contains('dty'))"),
                         [False, True, False])

    def test_tapping_a_strip_day_brings_it_into_view(self):
        b = self.browser
        hidden = b.execute_script("const e = document.querySelector('.xpd:not(.vis)'); e.click(); return e.dataset.pday")
        WebDriverWait(b, 5).until(lambda d: hidden in self.days())
