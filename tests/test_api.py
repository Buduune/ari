"""Backend: validation, the email she triggers, and how Gmail is used."""

import re
import smtplib

import pytest

from conftest import ROOT, make_payload

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
    assert "Saturday, October 24, 2026 at 5pm (Golden hour)" in text
    assert "Sunset that day: 6:06pm" in text
    assert "No peanuts. I'll bring a scarf." in text
    assert "NO attempts on page 1: 3 times." in text


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


@pytest.mark.parametrize("day", ["2026-02-30", "2026-13-01", "2026-00-10"])
def test_rejects_dates_that_do_not_exist(api, gmail, day):
    r = api.post("/api/reply", json=make_payload(dateISO=day))
    assert r.status_code == 422
    assert gmail.sent == []


def test_rejects_missing_fields(api, gmail):
    payload = make_payload()
    del payload["timeText"]
    assert api.post("/api/reply", json=payload).status_code == 422


def test_honeypot_is_ignored_quietly(api, gmail):
    r = api.post("/api/reply", json=make_payload(website="http://spam.example"))
    assert r.status_code == 200
    assert gmail.sent == []


def test_gmail_failure_is_reported(api, gmail):
    gmail.fail = smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
    r = api.post("/api/reply", json=make_payload())
    assert r.status_code == 502


def test_rate_limit_stops_a_flood(api, gmail):
    codes = [api.post("/api/reply", json=make_payload()).status_code for _ in range(10)]
    assert codes == [200] * 8 + [429] * 2
    assert len(gmail.sent) == 8
