"""Backend: validation, the email she triggers, and how Gmail is used."""

import json
import re
import smtplib
from datetime import date, timedelta

import pytest

from conftest import DATE_TEXT, ROOT, make_payload

INDEX = (ROOT / "web" / "index.html").read_text(encoding="utf-8")


def body(msg, kind="plain"):
    return msg.get_body(preferencelist=(kind,)).get_content()


# --- serving the page -------------------------------------------------------

def test_page_is_served_at_root(api):
    r = api.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Would you" in r.text and "meet me?" in r.text


def test_every_photo_the_page_uses_is_served(api):
    paths = sorted(set(re.findall(r"img/[\w-]+\.jpg", INDEX)))
    assert len(paths) == 12
    for path in paths:
        r = api.get("/" + path)
        assert r.status_code == 200, path
        assert r.headers["content-type"] == "image/jpeg", path


def test_health_says_whether_email_is_set_up(api, monkeypatch):
    assert api.get("/api/health").json()["emailConfigured"] is False
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x" * 16)
    assert api.get("/api/health").json()["emailConfigured"] is True


def test_health_is_not_fooled_by_blank_settings(api, monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "   ")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", " ")
    assert api.get("/api/health").json()["emailConfigured"] is False


# --- sending ----------------------------------------------------------------

def test_dry_run_answers_ok_without_gmail(api, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    r = api.post("/api/reply", json=make_payload())
    assert r.status_code == 200
    assert r.json() == {"ok": True, "dryRun": True}


def test_missing_email_settings_is_an_error_not_a_fake_success(api):
    r = api.post("/api/reply", json=make_payload())
    assert r.status_code == 500


def test_sends_through_gmail_over_ssl(api, gmail):
    r = api.post("/api/reply", json=make_payload())
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert gmail.host == ("smtp.gmail.com", 465)
    assert gmail.logins == [("me@example.com", "abcdefghijklmnop")]  # spaces in the app password are dropped
    assert len(gmail.sent) == 1
    msg = gmail.sent[0]
    assert msg["Subject"] == "She said yes"
    assert msg["From"] == "me@example.com"
    assert msg["To"] == "me@example.com"


def test_mail_to_overrides_the_recipient(api, gmail, monkeypatch):
    monkeypatch.setenv("MAIL_TO", "elsewhere@example.com")
    api.post("/api/reply", json=make_payload())
    assert gmail.sent[0]["To"] == "elsewhere@example.com"


def test_email_has_door_plan_time_note_and_no_count(api, gmail):
    api.post("/api/reply", json=make_payload(noAttempts=3))
    text = body(gmail.sent[0])
    assert "Door 3 — Academic" in text
    assert "Japanese haiku" in text
    assert f"{DATE_TEXT} at 5pm (Golden hour)" in text
    assert "Sunset that day: 6:06pm" in text
    assert "No peanuts. I'll bring a scarf." in text
    assert "NO attempts on page 1: 3 times." in text


def test_date_in_the_email_comes_from_the_date_itself(api, gmail):
    api.post("/api/reply", json=make_payload(dateText="Funday, Smarch 32, 1999"))
    text = body(gmail.sent[0])
    assert "Smarch" not in text
    assert f"{DATE_TEXT} at 5pm" in text


@pytest.mark.parametrize("attempts,caved,expected", [
    (0, False, "NO attempts on page 1: 0. Straight to YES."),
    (1, False, "NO attempts on page 1: 1 time."),
    (5, True, "Then NO turned into “ok fine… yes 🙈” and she pressed it."),
])
def test_no_attempt_wording(api, gmail, attempts, caved, expected):
    api.post("/api/reply", json=make_payload(noAttempts=attempts, caved=caved))
    assert expected in body(gmail.sent[0])


def test_empty_note_reads_no_note(api, gmail):
    api.post("/api/reply", json=make_payload(note="   "))
    assert "No note." in body(gmail.sent[0])


def test_html_part_escapes_what_she_typed(api, gmail):
    api.post("/api/reply", json=make_payload(note="<script>alert(1)</script> & <b>x</b>"))
    html = body(gmail.sent[0], "html")
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;x&lt;/b&gt;" in html


def test_half_an_emoji_in_the_note_still_sends(api, gmail):
    # A lone surrogate (half of an emoji, e.g. after a cut-off paste) must not crash the email.
    raw = make_payload(note="ok 🙂")
    body_json = json.dumps(raw).replace("\\ud83d\\ude42", "\\ud83d")  # keep only the first half
    r = api.post("/api/reply", content=body_json, headers={"Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    assert len(gmail.sent) == 1
    assert "ok \ufffd" in body(gmail.sent[0])   # the broken half becomes a replacement character


def test_rejected_input_is_never_echoed_back(api, gmail):
    # Error replies don't repeat what was sent, so odd input can't crash them into a 500.
    raw = json.dumps(make_payload(note=["x \U0001F642"])).replace("\\ud83d\\ude42", "\\ud83d")
    r = api.post("/api/reply", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert "x " not in r.text
    assert gmail.sent == []


def test_other_time_label_is_not_repeated(api, gmail):
    api.post("/api/reply", json=make_payload(timeText="1:15pm", timeLabel="Other time", sunsetText=None))
    text = body(gmail.sent[0])
    assert "at 1:15pm\n" in text
    assert "Sunset that day" not in text


@pytest.mark.parametrize("bad", [
    {"door": 0},
    {"door": 4},
    {"dateISO": "tomorrow"},
    {"dateISO": "2026-1-5"},
    {"note": "x" * 601},
    {"doorName": ""},
    {"noAttempts": -1},
])
def test_rejects_invalid_answers(api, gmail, bad):
    r = api.post("/api/reply", json=make_payload(**bad))
    assert r.status_code == 422
    assert gmail.sent == []


@pytest.mark.parametrize("bad_time", ["25pm", "7", "19:00", "5:7pm", "noon", "<b>5pm</b>"])
def test_rejects_times_the_page_never_sends(api, gmail, bad_time):
    assert api.post("/api/reply", json=make_payload(timeText=bad_time)).status_code == 422


def test_sunset_time_is_checked_like_the_main_time(api, gmail):
    assert api.post("/api/reply", json=make_payload(sunsetText="<b>late</b>")).status_code == 422
    assert api.post("/api/reply", json=make_payload(sunsetText="6:06pm")).status_code == 200
    assert api.post("/api/reply", json=make_payload(sunsetText=None)).status_code == 200


@pytest.mark.parametrize("good_time", ["5pm", "5:15pm", "12:30am", "10am"])
def test_accepts_times_as_the_page_writes_them(api, gmail, good_time):
    assert api.post("/api/reply", json=make_payload(timeText=good_time)).status_code == 200


def test_rejects_a_door_name_that_does_not_match_its_number(api, gmail):
    assert api.post("/api/reply", json=make_payload(door=1, doorName="Academic")).status_code == 422
    assert api.post("/api/reply", json=make_payload(door=1, doorName="The Stage")).status_code == 200


@pytest.mark.parametrize("days_from_now", [-30, -3, 200])
def test_rejects_dates_the_page_never_offers(api, gmail, days_from_now):
    # The page offers tomorrow up to 120 days out (a day of slack either way for time zones).
    day = (date.today() + timedelta(days=days_from_now)).isoformat()
    assert api.post("/api/reply", json=make_payload(dateISO=day)).status_code == 422


@pytest.mark.parametrize("days_from_now", [1, 60, 120])
def test_accepts_dates_the_page_offers(api, gmail, days_from_now):
    day = (date.today() + timedelta(days=days_from_now)).isoformat()
    assert api.post("/api/reply", json=make_payload(dateISO=day)).status_code == 200


@pytest.mark.parametrize("day", ["2026-02-30", "2026-13-01", "2026-00-10"])
def test_rejects_dates_that_do_not_exist(api, gmail, day):
    r = api.post("/api/reply", json=make_payload(dateISO=day))
    assert r.status_code == 422
    assert gmail.sent == []


def test_rejects_missing_fields(api, gmail):
    payload = make_payload()
    del payload["timeText"]
    assert api.post("/api/reply", json=payload).status_code == 422


def test_gmail_failure_is_reported(api, gmail):
    gmail.fail = smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
    r = api.post("/api/reply", json=make_payload())
    assert r.status_code == 502


def test_odd_goodbye_after_gmail_took_the_email_still_counts_as_sent(api, gmail):
    # Reporting failure here would make the page retry and send a second email.
    gmail.quit_reply = 451
    r = api.post("/api/reply", json=make_payload())
    assert r.status_code == 200
    assert len(gmail.sent) == 1


def test_the_same_answer_is_emailed_only_once(api, gmail):
    first = api.post("/api/reply", json=make_payload(rid="a1b2c3d4-e5f6"))
    again = api.post("/api/reply", json=make_payload(rid="a1b2c3d4-e5f6"))
    assert first.status_code == again.status_code == 200
    assert len(gmail.sent) == 1


def test_a_failed_send_can_be_retried_with_the_same_id(api, gmail):
    gmail.fail = smtplib.SMTPServerDisconnected("dropped")
    assert api.post("/api/reply", json=make_payload(rid="retry-me")).status_code == 502
    gmail.fail = None
    assert api.post("/api/reply", json=make_payload(rid="retry-me")).status_code == 200
    assert len(gmail.sent) == 1


def test_someone_elses_flood_does_not_block_her(api, gmail):
    spammer, her = {"X-Forwarded-For": "203.0.113.7"}, {"X-Forwarded-For": "198.51.100.23"}
    codes = [api.post("/api/reply", json=make_payload(), headers=spammer).status_code for _ in range(9)]
    assert codes[-1] == 429
    assert api.post("/api/reply", json=make_payload(), headers=her).status_code == 200


def test_rate_limit_stops_a_flood(api, gmail):
    codes = [api.post("/api/reply", json=make_payload()).status_code for _ in range(10)]
    assert codes == [200] * 8 + [429] * 2
    assert len(gmail.sent) == 8
