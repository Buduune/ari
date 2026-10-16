import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main  # noqa: E402

ENV_KEYS = ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "MAIL_TO", "DRY_RUN")


# A date the page would offer: ten days from now.
DATE = date.today() + timedelta(days=10)
DATE_TEXT = f"{DATE:%A}, {DATE:%B} {DATE.day}, {DATE.year}"


def make_payload(**overrides):
    """A valid answer, the way the page sends it."""
    payload = {
        "door": 3,
        "doorName": "Academic",
        "plan": "University of Washington campus tour, talking about Russian literature, Japanese haiku, writing poems together.",
        "dateISO": DATE.isoformat(),
        "timeText": "5pm",
        "timeLabel": "Golden hour",
        "sunsetText": "6:06pm",
        "note": "No peanuts. I'll bring a scarf.",
        "noAttempts": 2,
        "caved": False,
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
    main._sent_ids.clear()
    return TestClient(main.app)


@pytest.fixture
def gmail(api, monkeypatch):
    """Replaces Gmail with a fake SMTP server and records what would be sent.

    Depends on `api` so the email settings below are applied after the reset."""
    box = SimpleNamespace(sent=[], logins=[], host=None, timeout=None, fail=None, quit_reply=221)

    class FakeSMTP:
        """Behaves like smtplib.SMTP_SSL, including what its `with` block does on exit."""

        def __init__(self, host, port, context=None, timeout=None):
            box.host = (host, port)
            box.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            # smtplib sends QUIT here and raises if the reply isn't 221
            try:
                self.quit()
            finally:
                self.close()
            return False

        def quit(self):
            if box.quit_reply != 221:
                raise main.smtplib.SMTPResponseException(box.quit_reply, b"odd goodbye")

        def close(self):
            pass

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
