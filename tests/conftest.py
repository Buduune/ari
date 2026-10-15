import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main  # noqa: E402

ENV_KEYS = ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "MAIL_TO", "DRY_RUN")


def make_payload(**overrides):
    """A valid answer, the way the page sends it."""
    payload = {
        "door": 3,
        "doorName": "Academic",
        "plan": "University of Washington campus tour, talking about Russian literature, Japanese haiku, writing poems together.",
        "dateISO": "2026-10-24",
        "dateText": "Saturday, October 24, 2026",
        "timeText": "5pm",
        "timeLabel": "Golden hour",
        "sunsetText": "6:06pm",
        "note": "No peanuts. I'll bring a scarf.",
        "noAttempts": 2,
        "caved": False,
        "website": "",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def api(monkeypatch):
    """A test client with no email settings and an empty rate limiter."""
    from fastapi.testclient import TestClient

    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    main._recent.clear()
    return TestClient(main.app)


@pytest.fixture
def gmail(api, monkeypatch):
    """Replaces Gmail with a fake SMTP server and records what would be sent.

    Depends on `api` so the email settings below are applied after the reset."""
    box = SimpleNamespace(sent=[], logins=[], host=None, timeout=None, fail=None)

    class FakeSMTP:
        def __init__(self, host, port, context=None, timeout=None):
            box.host = (host, port)
            box.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, password):
            if box.fail:
                raise box.fail
            box.logins.append((user, password))

        def send_message(self, msg):
            box.sent.append(msg)

    monkeypatch.setattr(main.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    return box
