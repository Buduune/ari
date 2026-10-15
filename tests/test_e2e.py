"""End-to-end: the whole invite in a phone-sized Chromium, against the real backend in DRY_RUN mode.

Needs:  pip install playwright  &&  python -m playwright install chromium
Skipped automatically when Playwright or its Chromium is not installed.
"""

import calendar
import os
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
    inert = lambda: page.evaluate("() => document.querySelector('#app').inert")
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


def test_door_one_only_allows_times_mopop_is_open(page):
    choose_door(page, 1)
    chips = page.locator(".chip")
    assert chips.nth(0).get_attribute("aria-checked") == "true"   # Afternoon (2pm) picked for her
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
    assert page.locator('.chip[aria-checked="true"]').count() == 0


def test_time_she_picked_herself_is_kept_when_she_switches_doors(page):
    choose_door(page, 2)
    page.locator('.chip[data-kind="evening"]').tap()
    page.locator("#change").tap()
    page.wait_for_selector("#p2.is-active")
    page.locator('.door[data-door="3"]').tap()
    page.wait_for_selector("#room.is-in", timeout=5000)
    page.locator("#choose").tap()
    page.wait_for_selector("#p3.is-active")
    assert page.locator('.chip[aria-checked="true"]').get_attribute("data-kind") == "evening"


def test_other_time_works_when_the_picker_only_fires_change(page):
    # iOS time wheels can report the value with a `change` event and no `input` event.
    choose_door(page, 2)
    pick_first_open_day(page)
    page.locator('.chip[data-kind="other"]').tap()
    page.eval_on_selector("#otherTime", "el => { el.value = '18:30'; el.dispatchEvent(new Event('change', {bubbles: true})); }")
    assert page.locator("#send").is_enabled()


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


def test_failed_send_keeps_her_on_page_three_with_a_message(page):
    page.route("**/api/reply", lambda route: route.fulfill(status=500, body="{}"))
    choose_door(page, 2)
    pick_first_open_day(page)
    page.locator('.chip[data-kind="evening"]').tap()
    page.locator("#send").tap()
    page.wait_for_function("() => document.querySelector('#sendStatus').textContent.length > 0", timeout=10000)
    assert page.locator("#sendStatus").inner_text() == "Didn’t send. Check your connection and try again."
    assert state(page, "page") == 3
    assert page.locator("#send").is_enabled()
