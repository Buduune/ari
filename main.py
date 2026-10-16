"""Backend for the invitation page.

Serves web/index.html and its photos, and emails her answer to you through
Gmail when she taps "Send it".

Environment variables
  GMAIL_ADDRESS       the Gmail account that sends the email (yours)
  GMAIL_APP_PASSWORD  a 16-character Google app password for that account
  MAIL_TO             optional: where the answer goes (defaults to GMAIL_ADDRESS)
  DRY_RUN=1           optional: print the email instead of sending it (for testing)
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
import threading
import time
import unicodedata
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html import escape
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

log = logging.getLogger("invite")
logging.basicConfig(level=logging.INFO)

WEB_DIR = Path(__file__).parent / "web"

app = FastAPI(title="invite", docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(RequestValidationError)
def _invalid_answer(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Say which fields were wrong, but never echo what was sent: odd characters in it
    # could otherwise break this very reply.
    fields = sorted({".".join(str(p) for p in e.get("loc", ())[1:]) or "body" for e in exc.errors()})
    return JSONResponse(status_code=422, content={"detail": "Invalid answer.", "fields": fields})


CLOCK = r"^(1[0-2]|[1-9])(:[0-5]\d)?(am|pm)$"  # how the page writes times: 5pm, 5:15pm, 12:30am

# Must match the DOORS titles in web/index.html (tests/test_page.py checks this).
DOOR_TITLES = {1: "The Stage", 2: "The Mind-Bender", 3: "Academic"}


class Reply(BaseModel):
    door: int = Field(ge=1, le=3)
    doorName: str = Field(min_length=1, max_length=80)
    plan: str = Field(min_length=1, max_length=1500)
    dateISO: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    timeText: str = Field(pattern=CLOCK)  # as the page writes it: 5pm, 5:15pm
    timeLabel: str = Field(default="", max_length=40)
    sunsetText: str | None = Field(default=None, pattern=CLOCK)
    note: str = Field(default="", max_length=600)
    noAttempts: int = Field(default=0, ge=0, le=99)
    caved: bool = False
    rid: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{6,64}$")  # one id per answer, against duplicates

    @model_validator(mode="before")
    @classmethod
    def _repair_broken_characters(cls, data: Any) -> Any:
        # Half of an emoji (a lone UTF-16 surrogate, e.g. from a cut-off paste) can't be put in an
        # email or a JSON reply. Swap it for U+FFFD instead of failing the whole answer.
        if isinstance(data, dict):
            return {k: v.encode("utf-16", "surrogatepass").decode("utf-16", "replace") if isinstance(v, str) else v
                    for k, v in data.items()}
        return data

    @model_validator(mode="after")
    def _door_name_matches_number(self) -> "Reply":
        if DOOR_TITLES.get(self.door) != self.doorName:
            raise ValueError("doorName doesn't match the door number")
        return self

    @field_validator("dateISO")
    @classmethod
    def _a_day_the_page_offers(cls, value: str) -> str:
        day = date.fromisoformat(value)  # raises ValueError for days like 2026-02-30
        # The page offers tomorrow to +120 days in Seattle; allow a day either side for time zones.
        today = datetime.now(timezone.utc).date()
        if not today - timedelta(days=1) <= day <= today + timedelta(days=125):
            raise ValueError("date is outside the days the invitation offers")
        return value


# A light brake so a stranger who finds the link can't flood your inbox: 8 sends per sender
# per 10 minutes, so someone else's flood can't block her reply, plus an overall cap so the
# Gmail account can't be used as a spam relay. (Best effort: serverless instances don't share memory.)
_recent: dict[str, list[float]] = {}
_recent_lock = threading.Lock()  # sync endpoints run in a thread pool


def _allow_send(sender: str, limit: int = 8, overall: int = 60, window_s: int = 600) -> bool:
    now = time.time()
    with _recent_lock:
        for key in list(_recent):
            _recent[key] = [t for t in _recent[key] if now - t < window_s]
            if not _recent[key]:
                del _recent[key]
        mine = _recent.get(sender, [])
        if len(mine) >= limit or sum(map(len, _recent.values())) >= overall:
            return False
        _recent.setdefault(sender, []).append(now)
        return True


# Answers already emailed (by their id), so a retry after a lost response doesn't email twice.
_sent_ids: "OrderedDict[str, float]" = OrderedDict()


def _already_sent(rid: str | None) -> bool:
    with _recent_lock:
        return rid is not None and rid in _sent_ids


def _remember_sent(rid: str | None) -> None:
    if rid is None:
        return
    with _recent_lock:
        _sent_ids[rid] = time.time()
        while len(_sent_ids) > 500:
            _sent_ids.popitem(last=False)


def _sender_of(request: Request) -> str:
    # On Vercel the platform sets these headers to the visitor's address.
    h = request.headers
    forwarded = h.get("x-forwarded-for", "").split(",")[0].strip()
    return h.get("x-real-ip") or forwarded or (request.client.host if request.client else "") or "unknown"


WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")


def date_text(day: date) -> str:
    """Saturday, October 24, 2026 — written from the date itself, not from what the page sent."""
    return f"{WEEKDAYS[day.weekday()]}, {MONTHS[day.month - 1]} {day.day}, {day.year}"


def _no_line(r: Reply) -> str:
    if r.noAttempts == 0:
        return "NO attempts on page 1: 0. Straight to YES."
    times = "time" if r.noAttempts == 1 else "times"
    line = f"NO attempts on page 1: {r.noAttempts} {times}."
    if r.caved:
        line += " Then NO turned into “ok fine… yes 🙈” and she pressed it."
    return line


def build_message(r: Reply, sender: str, to: str) -> EmailMessage:
    when = f"{date_text(date.fromisoformat(r.dateISO))} at {r.timeText}"
    if r.timeLabel and r.timeLabel != "Other time":
        when += f" ({r.timeLabel})"
    note = r.note.strip()

    text_lines = [
        "She said yes.",
        "",
        f"Door {r.door} — {r.doorName}",
        r.plan,
        "",
        "When",
        when,
    ]
    if r.sunsetText:
        text_lines.append(f"Sunset that day: {r.sunsetText}")
    text_lines += ["", "Her note", note or "No note.", "", _no_line(r)]
    text = "\n".join(text_lines) + "\n"

    sunset_html = (
        f'<p style="margin:4px 0 0;color:#5f5f5f;font-size:14px">Sunset that day: {escape(r.sunsetText)}</p>'
        if r.sunsetText
        else ""
    )
    note_html = escape(note).replace("\n", "<br>") if note else '<span style="color:#5f5f5f">No note.</span>'
    html = f"""\
<!doctype html>
<html><body style="margin:0;padding:0;background:#ffffff">
<div style="max-width:560px;margin:0 auto;padding:28px 20px;font-family:Helvetica,Arial,sans-serif;color:#000000;line-height:1.45">
  <div style="width:48px;height:48px;background:#ffb000"></div>
  <h1 style="font-size:34px;line-height:1;letter-spacing:-1px;margin:22px 0 28px">She said yes.</h1>
  <p style="margin:0;font-size:13px;color:#5f5f5f">Door {r.door}</p>
  <p style="margin:2px 0 8px;font-size:22px;font-weight:bold">{escape(r.doorName)}</p>
  <p style="margin:0 0 26px;font-size:16px">{escape(r.plan)}</p>
  <div style="border-top:2px solid #000;padding-top:14px;margin-bottom:24px">
    <p style="margin:0;font-size:13px;color:#5f5f5f">When</p>
    <p style="margin:2px 0 0;font-size:18px;font-weight:bold">{escape(when)}</p>
    {sunset_html}
  </div>
  <div style="border-top:2px solid #000;padding-top:14px;margin-bottom:24px">
    <p style="margin:0;font-size:13px;color:#5f5f5f">Her note</p>
    <p style="margin:2px 0 0;font-size:16px">{note_html}</p>
  </div>
  <div style="border-top:1px solid #000;padding-top:12px">
    <p style="margin:0;font-size:14px">{escape(_no_line(r))}</p>
  </div>
</div>
</body></html>
"""

    msg = EmailMessage()
    msg["Subject"] = "She said yes"
    msg["From"] = sender
    msg["To"] = to
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def send_gmail(msg: EmailMessage, user: str, app_password: str) -> None:
    context = ssl.create_default_context()
    smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=20)
    try:
        smtp.login(user, app_password)
        smtp.send_message(msg)
    finally:
        # Once Gmail has accepted the message, a bad goodbye must not turn it into a failure
        # (the page would then retry and you'd get the email twice).
        try:
            smtp.quit()
        except (smtplib.SMTPException, OSError):
            smtp.close()


def _clean(value: str) -> str:
    """Drops spaces and the invisible characters a copy-paste can bring along (non-breaking,
    zero-width…). Gmail addresses and app passwords never contain them, and smtplib can't send them."""
    return "".join(ch for ch in value if not ch.isspace() and unicodedata.category(ch) != "Cf")


def _email_settings() -> tuple[bool, str, str, str]:
    """(dry_run, sender, app password, recipient) from the environment, blanks treated as missing."""
    sender = _clean(os.environ.get("GMAIL_ADDRESS", ""))
    password = _clean(os.environ.get("GMAIL_APP_PASSWORD", ""))
    to = _clean(os.environ.get("MAIL_TO", "")) or sender
    return os.environ.get("DRY_RUN") == "1", sender, password, to


def _why_not_sent(exc: BaseException) -> str:
    """A short name for why Gmail didn't take the email (SMTPException is itself an OSError)."""
    if isinstance(exc, (smtplib.SMTPAuthenticationError, UnicodeError)):
        # Wrong address / app password, or 2-Step Verification off. UnicodeError: smtplib logs in
        # with ASCII only, so a password with other letters can never be the 16-letter app password.
        return "gmail_login"
    if isinstance(exc, smtplib.SMTPConnectError) or (
            isinstance(exc, OSError) and not isinstance(exc, smtplib.SMTPException)):
        return "gmail_unreachable"  # no connection: mail port blocked, no network, DNS, TLS
    return "gmail_failed"           # anything else; often temporary


def _refuse(status: int, reason: str, detail: str) -> JSONResponse:
    """An error reply with a short reason, which the page turns into a message that says what's wrong."""
    return JSONResponse(status_code=status, content={"detail": detail, "reason": reason})


@app.post("/api/reply", response_model=None)
def reply(r: Reply, request: Request) -> dict | JSONResponse:
    dry_run, sender, password, to = _email_settings()

    if not dry_run and not (sender and password):
        log.error("GMAIL_ADDRESS / GMAIL_APP_PASSWORD are not set")
        return _refuse(500, "not_configured", "Email is not set up yet.")
    if _already_sent(r.rid):
        return {"ok": True}
    if not _allow_send(_sender_of(request)):
        return _refuse(429, "busy", "Too many replies. Try again in a few minutes.")

    msg = build_message(r, sender or "dry-run@example.com", to or "dry-run@example.com")
    if dry_run:
        log.info("DRY_RUN: email not sent\n%s", msg.get_body(preferencelist=("plain",)).get_content())
        return {"ok": True, "dryRun": True}

    try:
        send_gmail(msg, sender, password)
    except (smtplib.SMTPException, OSError, UnicodeError) as exc:
        log.exception("Gmail send failed: %s", exc)
        return _refuse(502, _why_not_sent(exc), "Couldn't send the email.")
    _remember_sent(r.rid)
    return {"ok": True}


@app.get("/api/health")
def health() -> dict:
    dry_run, sender, password, _ = _email_settings()
    return {"ok": True, "emailConfigured": bool(sender and password), "dryRun": dry_run}


# The page and its photos. Declared after the API routes so /api/* always reaches the code above.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
