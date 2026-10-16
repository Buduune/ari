"""End-to-end: the whole invite in a phone-sized Chromium, against the real backend in DRY_RUN mode.

Needs:  pip install playwright  &&  python -m playwright install chromium
Skipped automatically when Playwright or its Chromium is not installed.
"""

import calendar
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from conftest import ROOT

sync_api = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.e2e

PHONE = dict(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)


# --- fixtures -----------------------------------------------------------------

@pytest.fixture(scope="session")
def server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k not in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "MAIL_TO")}
    env["DRY_RUN"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("the local server did not start")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"Chromium for Playwright is not installed ({type(exc).__name__}). Run: python -m playwright install chromium")
        yield b
        b.close()


@pytest.fixture
def page(browser, server):
    ctx = browser.new_context(**PHONE)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(server + "/", wait_until="load")
    pg.wait_for_function("() => !!window.__invite")
    yield pg
    ctx.close()
    assert errors == [], f"JavaScript errors on the page: {errors}"


# --- helpers ------------------------------------------------------------------

def rect(pg, sel):
    return pg.eval_on_selector(sel, "e => { const r = e.getBoundingClientRect(); return {l: r.left, t: r.top, r: r.right, b: r.bottom, w: r.width, h: r.height}; }")


def tap_center(pg, sel):
    r = rect(pg, sel)
    pg.touchscreen.tap(r["l"] + r["w"] / 2, r["t"] + r["h"] / 2)


def state(pg, expr):
    return pg.evaluate(f"() => window.__invite.S.{expr}")


def to_doors(pg):
    pg.locator("#yes").tap()
    pg.wait_for_selector("#p2.is-active")


def solve_sheet(pg, n):
    p = pg.evaluate("n => { const p = window.__invite.S.puzzles[n]; return {sol: p.sol, cur: p.cur, given: p.given}; }", n)
    for i in range(16):
        if p["given"][i]:
            continue
        for _ in range((p["sol"][i] - p["cur"][i]) % 5):
            pg.locator(f'#grid .cell[data-i="{i}"]').tap()


def open_locked_door(pg, n):
    pg.locator(f'.door[data-door="{n}"]').tap()
    pg.wait_for_selector("#sheet.is-in")
    solve_sheet(pg, n)
    pg.wait_for_selector("#room.is-in", timeout=10000)


def close_room(pg):
    pg.locator("#roomClose").tap()
    pg.wait_for_selector("#room", state="hidden")


def choose_door(pg, n):
    """From page 1: open door n (solving if needed) and press Choose one."""
    to_doors(pg)
    open_locked_door(pg, n)
    pg.locator("#choose").tap()
    pg.wait_for_selector("#p3.is-active")


def pick_first_open_day(pg):
    pg.locator(".cal-day:not([disabled])").first.tap()


# --- the deployed page --------------------------------------------------------

def test_testing_hook_is_not_exposed_on_a_real_domain(browser, server):
    """window.__invite (used by these tests) holds the puzzle answers; it should only exist on localhost."""
    port = server.rsplit(":", 1)[1]
    other = browser.browser_type.launch(args=["--host-resolver-rules=MAP invite.test 127.0.0.1", "--no-proxy-server"])
    try:
        pg = other.new_page(**PHONE)
        pg.goto(f"http://invite.test:{port}/", wait_until="load")
        assert pg.locator("#q1").is_visible()
        assert pg.evaluate("() => typeof window.__invite") == "undefined"
    finally:
        other.close()


# --- page 1 -------------------------------------------------------------------

def test_yes_goes_straight_to_the_doors(page):
    to_doors(page)
    assert state(page, "noAttempts") == 0


def test_tapping_the_edge_of_yes_is_a_yes_not_a_no(page):
    # YES and NO sit 12px apart; a thumb on YES's right edge must not count as trying NO.
    yes = rect(page, "#yes")
    page.touchscreen.tap(yes["r"] - 4, yes["t"] + yes["h"] / 2)
    page.wait_for_selector("#p2.is-active", timeout=3000)
    assert state(page, "noAttempts") == 0


def test_a_long_press_on_no_counts_once(page):
    # On touch, the press itself makes NO run. The click that follows when the finger lifts
    # (here after 350ms, past the double-count guard) must not count a second time.
    page.eval_on_selector("#no", """el => {
        const r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
        el.dispatchEvent(new PointerEvent('pointerdown', {pointerType: 'touch', clientX: x, clientY: y, bubbles: true, cancelable: true}));
    }""")
    page.wait_for_timeout(350)
    page.eval_on_selector("#no", "el => el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, detail: 1}))")
    page.wait_for_timeout(100)
    assert state(page, "noAttempts") == 1


def test_mouse_over_yes_does_not_scare_no(browser, server):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    try:
        pg = ctx.new_page()
        pg.goto(server + "/", wait_until="load")
        yes = rect(pg, "#yes")
        pg.mouse.move(yes["l"] + 10, yes["t"] + 10)
        pg.mouse.move(yes["r"] - 3, yes["t"] + yes["h"] / 2, steps=8)
        pg.wait_for_timeout(300)
        assert pg.evaluate("() => window.__invite.S.noAttempts") == 0
        no = rect(pg, "#no")
        pg.mouse.move(no["l"] + 5, no["t"] + no["h"] / 2, steps=6)   # actually going for NO
        pg.wait_for_timeout(300)
        assert pg.evaluate("() => window.__invite.S.noAttempts") == 1
    finally:
        ctx.close()


def test_no_runs_away_shrinks_and_gives_in_after_five(page):
    widths, yes_widths = [rect(page, "#no")["w"]], [rect(page, "#yes")["w"]]
    for _ in range(5):
        tap_center(page, "#no")
        page.wait_for_timeout(950)
        no, yes = rect(page, "#no"), rect(page, "#yes")
        assert no["l"] >= 0 and no["t"] >= 0 and no["r"] <= 390 and no["b"] <= 844, no
        assert no["r"] <= yes["l"] or no["l"] >= yes["r"] or no["b"] <= yes["t"] or no["t"] >= yes["b"], (no, yes)
        assert state(page, "page") == 1
        widths.append(no["w"])
        yes_widths.append(yes["w"])
    assert widths[1] < widths[0] and widths[4] < widths[1]   # NO shrinks while it runs
    assert yes_widths[-1] > yes_widths[0] * 1.5              # YES grows
    assert page.locator("#no").inner_text() == "ok fine… yes 🙈"
    assert state(page, "noAttempts") == 5 and state(page, "caved") is True
    tap_center(page, "#no")
    page.wait_for_selector("#p2.is-active")
    assert state(page, "pressedCaved") is True               # the email will say she pressed it


# --- page 2 -------------------------------------------------------------------

def test_a_locked_door_asks_for_the_puzzle(page):
    to_doors(page)
    page.locator('.door[data-door="1"]').tap()
    page.wait_for_selector("#sheet.is-in")
    assert page.locator("#sheetDoor").inner_text() == "Door 1"
    givens = page.locator("#grid .cell.is-given").count()
    assert givens == 8


def test_wrong_square_is_flagged_and_hint_fills_a_right_one(page):
    to_doors(page)
    page.locator('.door[data-door="2"]').tap()
    page.wait_for_selector("#sheet.is-in")
    p = page.evaluate("() => { const p = window.__invite.S.puzzles[2]; return {sol: p.sol, given: p.given}; }")
    i = next(k for k in range(16) if not p["given"][k])
    wrong = 1 if p["sol"][i] != 1 else 2
    for _ in range(wrong):
        page.locator(f'#grid .cell[data-i="{i}"]').tap()
    page.locator("#check").tap()
    assert page.locator("#sheetMsg").inner_text() == "Not quite. One square is off."
    assert "is-wrong" in page.locator(f'#grid .cell[data-i="{i}"]').get_attribute("class")
    page.locator("#hint").tap()
    assert page.locator("#grid .cell.is-hint").count() == 1
    hinted = page.evaluate("() => { const p = window.__invite.S.puzzles[2]; return p.hint.map((h, i) => h ? p.cur[i] === p.sol[i] : true).every(Boolean); }")
    assert hinted


def test_solving_one_door_unlocks_all_three(page):
    to_doors(page)
    open_locked_door(page, 2)
    assert page.locator("#roomTitle").text_content() == "The Mind-Bender"
    close_room(page)
    assert page.locator(".door-state").all_inner_texts() == ["Unlocked", "Open", "Unlocked"]
    page.locator('.door[data-door="1"]').tap()
    page.wait_for_selector("#room.is-in", timeout=5000)
    assert page.locator("#sheet").is_hidden()
    assert page.locator("#roomTitle").text_content() == "The Stage"


def test_page_behind_a_dialog_is_out_of_reach(page):
    # Keyboard and screen-reader users shouldn't wander behind the puzzle or the room.
    to_doors(page)
    def inert():
        # `inert` for current browsers; aria-hidden too, since iOS 15.0–15.4 ignore `inert`
        return page.evaluate("() => { const a = document.querySelector('#app'); "
                             "return a.inert && a.getAttribute('aria-hidden') === 'true' ? true "
                             ": (!a.inert && !a.hasAttribute('aria-hidden') ? false : 'mixed'); }")
    assert inert() is False
    page.locator('.door[data-door="1"]').tap()
    page.wait_for_selector("#sheet.is-in")
    assert inert() is True
    page.locator("#sheet .x").tap()
    page.wait_for_selector("#sheet", state="hidden")
    assert inert() is False
    open_locked_door(page, 1)
    assert inert() is True
    close_room(page)
    assert inert() is False


def test_reopening_the_puzzle_right_after_closing_it_works(page):
    # Esc closes it and puts focus back on the door; Enter right away reopens it
    # before the old close timer fires. That timer must not hide the new sheet.
    to_doors(page)
    page.locator('.door[data-door="1"]').tap()
    page.wait_for_selector("#sheet.is-in")
    page.keyboard.press("Escape")
    page.wait_for_timeout(80)
    page.keyboard.press("Enter")
    page.wait_for_timeout(700)
    assert page.locator("#sheet").is_visible()
    page.keyboard.press("Escape")
    page.wait_for_selector("#sheet", state="hidden")
    assert page.evaluate("() => document.querySelector('#app').inert") is False


def test_reopening_a_room_right_after_closing_it_works(page):
    to_doors(page)
    open_locked_door(page, 1)
    page.keyboard.press("Escape")
    page.wait_for_timeout(80)
    page.keyboard.press("Enter")
    page.wait_for_timeout(700)
    assert page.locator("#room").is_visible()
    assert page.locator("#roomTitle").count() == 1
    page.keyboard.press("Escape")
    page.wait_for_selector("#room", state="hidden")
    assert page.evaluate("() => document.querySelector('#app').inert") is False


@pytest.mark.parametrize("dialog", ["sheet", "room"])
def test_focus_stays_on_the_door_after_a_quick_close(page, dialog):
    to_doors(page)
    if dialog == "sheet":
        page.locator('.door[data-door="1"]').tap()
        page.wait_for_selector("#sheet.is-in")
    else:
        open_locked_door(page, 1)
    page.keyboard.press("Escape")            # before the dialog's own focus timer has fired
    page.wait_for_timeout(500)               # let that timer fire
    focused = page.evaluate("() => document.activeElement.closest('.door')?.dataset.door")
    assert focused == "1"


def test_leaving_the_doors_mid_animation_cancels_the_room(page):
    to_doors(page)
    page.locator('.door[data-door="2"]').tap()
    page.wait_for_selector("#sheet.is-in")
    solve_sheet(page, 2)
    page.wait_for_selector("#sheet", state="hidden", timeout=5000)   # door is about to swing
    page.go_back()
    page.wait_for_selector("#p1.is-active", timeout=3000)
    page.wait_for_timeout(2500)                                      # longer than the swing
    assert page.locator("#room").is_hidden()
    assert page.evaluate("() => document.querySelector('#app').inert") is False


def test_back_while_a_dialog_fades_out_still_goes_back(page):
    to_doors(page)
    page.locator('.door[data-door="1"]').tap()
    page.wait_for_selector("#sheet.is-in")
    page.locator("#sheet .x").tap()
    page.go_back()                                       # right away, while the sheet is fading
    page.wait_for_selector("#p1.is-active", timeout=3000)


def test_double_tap_on_a_door_does_not_reset_the_room(page):
    to_doors(page)
    open_locked_door(page, 2)
    close_room(page)
    door = page.locator('.door[data-door="1"]')
    door.tap()
    page.wait_for_timeout(120)
    door.tap()                                            # impatient second tap while it swings
    page.wait_for_selector("#room.is-in", timeout=5000)
    page.locator("#galNext").tap()
    page.wait_for_function("() => document.querySelector('#galCount').textContent === '2/4'")
    page.wait_for_timeout(1600)                           # longer than the swing
    assert page.locator("#galCount").inner_text() == "2/4"


def test_room_shows_photos_first_then_the_plan_and_the_site_link(page):
    to_doors(page)
    open_locked_door(page, 1)
    order = page.eval_on_selector("#roomIn", "e => [...e.children].map(c => c.className)")
    assert order[:3] == ["room-tag", "gallery", "room-body"]
    assert page.locator(".shot-img").first.get_attribute("src") == "img/d1-1.jpg"
    link = page.locator(".room-actions a")
    assert link.get_attribute("href") == "https://www.mopop.org/"
    assert link.get_attribute("target") == "_blank"
    close_room(page)
    page.locator('.door[data-door="3"]').tap()
    page.wait_for_selector("#room.is-in", timeout=5000)
    assert page.locator(".room-actions a").count() == 0   # no website for Door 3


def test_gallery_moves_to_the_next_photo(page):
    to_doors(page)
    open_locked_door(page, 3)
    page.locator("#galNext").tap()
    page.wait_for_function("() => document.querySelector('#galCount').textContent === '2/4'")
    assert page.locator("#galCap").inner_text() == "Suzzallo reading room"


def test_keyboard_focus_ring_shows_on_the_dark_room(page):
    to_doors(page)
    open_locked_door(page, 2)
    page.wait_for_function("() => document.activeElement.id === 'roomClose'")
    page.keyboard.press("Tab")
    color = page.evaluate("() => getComputedStyle(document.activeElement).outlineColor")
    r, g, b = (int(v) for v in re.findall(r"\d+", color)[:3])
    assert min(r, g, b) > 180, color        # light ring on the #1c1c1c room


def test_small_controls_are_big_enough_to_tap(page):
    to_doors(page)
    open_locked_door(page, 1)
    for sel in ("#roomClose", "#galPrev", "#galNext"):
        r = rect(page, sel)
        assert r["w"] >= 44 and r["h"] >= 44, (sel, r)


def test_back_button_closes_the_room_and_stays_on_the_doors(page):
    to_doors(page)
    open_locked_door(page, 2)
    page.go_back()
    page.wait_for_selector("#room", state="hidden")
    assert state(page, "page") == 2


# --- page 3 -------------------------------------------------------------------

def test_dates_before_tomorrow_cannot_be_picked(page):
    choose_door(page, 2)
    today = page.evaluate("""() => {
        const s = new Intl.DateTimeFormat('en-CA', {timeZone: 'America/Los_Angeles', year: 'numeric', month: '2-digit', day: '2-digit'}).format(new Date());
        return s.split('-').map(Number); }""")
    days = page.locator(".cal-day")
    month_shown = page.locator("#calMonth").inner_text()
    if month_shown.startswith(calendar.month_name[today[1]]):
        for d in range(1, today[2] + 1):
            assert days.nth(d - 1).is_disabled(), d


def test_keyboard_focus_stays_put_after_picking_a_date_or_time(page):
    choose_door(page, 2)
    day = page.locator(".cal-day:not([disabled])").first
    d = day.get_attribute("data-d")
    day.focus()
    page.keyboard.press("Enter")
    assert page.evaluate("() => document.activeElement.dataset.d") == d
    chip = page.locator('.chip[data-kind="evening"]')
    chip.focus()
    page.keyboard.press("Space")
    assert page.evaluate("() => document.activeElement.dataset.kind") == "evening"
    assert chip.get_attribute("aria-pressed") == "true"


def test_door_one_only_allows_times_mopop_is_open(page):
    choose_door(page, 1)
    chips = page.locator(".chip")
    assert chips.nth(0).get_attribute("aria-pressed") == "true"   # Afternoon (2pm) picked for her
    assert chips.nth(1).is_disabled() and chips.nth(2).is_disabled()
    assert page.locator("#timeNote").inner_text() == "MoPOP closes at 5."
    pick_first_open_day(page)
    page.locator('.chip[data-kind="other"]').tap()
    page.locator("#otherTime").fill("16:30")
    assert page.locator("#send").is_disabled()
    assert "start by 3pm" in page.locator("#timeNote").inner_text()
    page.locator("#otherTime").fill("13:15")
    assert page.locator("#send").is_enabled()


def test_time_picked_for_her_does_not_follow_her_to_another_door(page):
    choose_door(page, 1)                                 # Door 1 pre-selects Afternoon (2pm)
    page.locator("#change").tap()
    page.wait_for_selector("#p2.is-active")
    page.locator('.door[data-door="3"]').tap()           # already unlocked, opens straight away
    page.wait_for_selector("#room.is-in", timeout=5000)
    page.locator("#choose").tap()
    page.wait_for_selector("#p3.is-active")
    assert page.locator('.chip[aria-pressed="true"]').count() == 0


def test_time_she_picked_herself_is_kept_when_she_switches_doors(page):
    choose_door(page, 2)
    page.locator('.chip[data-kind="evening"]').tap()
    page.locator("#change").tap()
    page.wait_for_selector("#p2.is-active")
    page.locator('.door[data-door="3"]').tap()
    page.wait_for_selector("#room.is-in", timeout=5000)
    page.locator("#choose").tap()
    page.wait_for_selector("#p3.is-active")
    assert page.locator('.chip[aria-pressed="true"]').get_attribute("data-kind") == "evening"


def test_other_time_works_when_the_picker_only_fires_change(page):
    # iOS time wheels can report the value with a `change` event and no `input` event.
    choose_door(page, 2)
    pick_first_open_day(page)
    page.locator('.chip[data-kind="other"]').tap()
    page.eval_on_selector("#otherTime", "el => { el.value = '18:30'; el.dispatchEvent(new Event('change', {bubbles: true})); }")
    assert page.locator("#send").is_enabled()


def test_golden_hour_shows_a_time_only_once_there_is_a_date(page):
    choose_door(page, 3)
    golden = page.locator('.chip[data-kind="golden"]')
    assert golden.inner_text() == "Golden hour"
    pick_first_open_day(page)
    assert re.fullmatch(r"Golden hour \(\d{1,2}(:\d\d)?pm\)", golden.inner_text())


def test_door_two_says_the_latest_start(page):
    choose_door(page, 2)
    pick_first_open_day(page)
    page.locator('.chip[data-kind="other"]').tap()
    page.locator("#otherTime").fill("20:30")
    assert page.locator("#send").is_disabled()
    assert "start by 8pm" in page.locator("#timeNote").inner_text()


def test_door_three_suggests_golden_hour(page):
    choose_door(page, 3)
    golden = page.locator('.chip[data-kind="golden"]')
    assert "is-suggested" in golden.get_attribute("class")
    assert page.locator("#timeNote").inner_text().startswith("Sunset’s the whole point of this one 🌅")
    assert page.locator("#send").is_disabled()
    pick_first_open_day(page)
    assert page.locator("#send").is_disabled()          # still needs a time
    golden.tap()
    assert page.locator("#send").is_enabled()
    assert "Sunset that day:" in page.locator("#timeNote").inner_text()


def test_send_posts_her_answer_and_shows_the_last_page(page):
    choose_door(page, 3)
    pick_first_open_day(page)
    page.locator('.chip[data-kind="golden"]').tap()
    page.locator("#note").fill("No peanuts.")
    with page.expect_request("**/api/reply") as req:
        page.locator("#send").tap()
    sent = req.value.post_data_json
    assert sent["door"] == 3 and sent["doorName"] == "Academic"
    assert sent["timeLabel"] == "Golden hour" and sent["sunsetText"]
    assert sent["note"] == "No peanuts." and sent["noAttempts"] == 0 and sent["caved"] is False
    page.wait_for_selector("#p4.is-active")
    see = page.locator("#see").inner_text()
    assert see.startswith("See you on ") and f" at {sent['timeText']}." in see


def test_last_page_shows_the_date_that_was_sent(page):
    choose_door(page, 2)
    days = page.locator(".cal-day:not([disabled])")
    days.nth(0).tap()
    sent_label = days.nth(0).get_attribute("aria-label")          # e.g. "Friday, October 16"
    page.locator('.chip[data-kind="evening"]').tap()
    page.evaluate("() => { const f = window.fetch; window.fetch = (...a) => new Promise(r => setTimeout(r, 1500)).then(() => f(...a)); }")
    page.locator("#send").tap()
    page.wait_for_timeout(300)
    days.nth(3).tap()                                              # another day, while it says "Sending…"
    page.wait_for_selector("#p4.is-active", timeout=10000)
    assert page.locator("#see").inner_text() == f"See you on {sent_label} at 7pm."


def test_email_says_which_button_she_really_pressed(page):
    for _ in range(5):
        tap_center(page, "#no")
        page.wait_for_timeout(400)
    page.wait_for_timeout(600)
    assert page.locator("#no").inner_text() == "ok fine… yes 🙈"
    page.locator("#yes").tap()                            # the real YES, not the one NO turned into
    page.wait_for_selector("#p2.is-active")
    open_locked_door(page, 2)
    page.locator("#choose").tap()
    page.wait_for_selector("#p3.is-active")
    pick_first_open_day(page)
    page.locator('.chip[data-kind="evening"]').tap()
    with page.expect_request("**/api/reply") as req:
        page.locator("#send").tap()
    sent = req.value.post_data_json
    assert sent["noAttempts"] == 5
    assert sent["caved"] is False


def _send_with(page, handler):
    calls = []
    page.route("**/api/reply", lambda route: (calls.append(1), handler(route)))
    choose_door(page, 2)
    pick_first_open_day(page)
    page.locator('.chip[data-kind="evening"]').tap()
    page.locator("#send").tap()
    page.wait_for_function("() => document.querySelector('#sendStatus').textContent.length > 0", timeout=10000)
    page.wait_for_timeout(300)
    return calls


@pytest.mark.parametrize("status,message", [
    (500, "Didn’t send. Try again in a minute."),
    (502, "Didn’t send. Try again in a minute."),
    (429, "Didn’t send. Too many tries, wait a few minutes."),
])
def test_server_error_is_not_retried_and_says_so(page, status, message):
    calls = _send_with(page, lambda route: route.fulfill(status=status, body="{}"))
    assert len(calls) == 1                                 # retrying a server error could email twice
    assert page.locator("#sendStatus").inner_text() == message
    assert state(page, "page") == 3
    assert page.locator("#send").is_enabled()


def test_lost_connection_is_retried_once_then_explained(page):
    calls = _send_with(page, lambda route: route.abort("internetdisconnected"))
    assert len(calls) == 2
    assert page.locator("#sendStatus").inner_text() == "Didn’t send. Check your connection and try again."
    assert state(page, "page") == 3
    assert page.locator("#send").is_enabled()
