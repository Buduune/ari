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
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

log = logging.getLogger("invite")
logging.basicConfig(level=logging.INFO)

WEB_DIR = Path(__file__).parent / "web"

app = FastAPI(title="invite", docs_url=None, redoc_url=None, openapi_url=None)


class Reply(BaseModel):
    door: int = Field(ge=1, le=3)
    doorName: str = Field(min_length=1, max_length=80)
    plan: str = Field(min_length=1, max_length=1500)
    dateISO: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    dateText: str = Field(min_length=1, max_length=80)
    timeText: str = Field(min_length=1, max_length=20)
    timeLabel: str = Field(default="", max_length=40)
    sunsetText: str | None = Field(default=None, max_length=20)
    note: str = Field(default="", max_length=600)
    noAttempts: int = Field(default=0, ge=0, le=99)
    caved: bool = False
    website: str = Field(default="", max_length=200)  # honeypot: people never fill this


# A light brake so a stranger who finds the link can't flood your inbox.
# (Best effort: serverless instances don't share memory.)
_recent: list[float] = []


def _allow_send(limit: int = 8, window_s: int = 600) -> bool:
    now = time.time()
    _recent[:] = [t for t in _recent if now - t < window_s]
    if len(_recent) >= limit:
        return False
    _recent.append(now)
    return True


def _no_line(r: Reply) -> str:
    if r.noAttempts == 0:
        return "NO attempts on page 1: 0. Straight to YES."
    times = "time" if r.noAttempts == 1 else "times"
    line = f"NO attempts on page 1: {r.noAttempts} {times}."
    if r.caved:
        line += " Then NO turned into “ok fine… yes 🙈” and she pressed it."
    return line


def build_message(r: Reply, sender: str, to: str) -> EmailMessage:
    when = f"{r.dateText} at {r.timeText}"
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
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=20) as smtp:
        smtp.login(user, app_password.replace(" ", ""))
        smtp.send_message(msg)


@app.post("/api/reply")
def reply(r: Reply) -> dict:
    if r.website:  # filled by a bot, not by her
        return {"ok": True}

    dry_run = os.environ.get("DRY_RUN") == "1"
    sender = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    to = os.environ.get("MAIL_TO", "").strip() or sender

    if not dry_run and not (sender and password):
        log.error("GMAIL_ADDRESS / GMAIL_APP_PASSWORD are not set")
        raise HTTPException(status_code=500, detail="Email is not set up yet.")
    if not _allow_send():
        raise HTTPException(status_code=429, detail="Too many replies. Try again in a few minutes.")

    msg = build_message(r, sender or "dry-run@example.com", to or "dry-run@example.com")
    if dry_run:
        log.info("DRY_RUN: email not sent\n%s", msg.get_body(preferencelist=("plain",)).get_content())
        return {"ok": True, "dryRun": True}

    try:
        send_gmail(msg, sender, password)
    except (smtplib.SMTPException, OSError) as exc:
        log.exception("Gmail send failed: %s", exc)
        raise HTTPException(status_code=502, detail="Couldn't send the email.") from exc
    return {"ok": True}


@app.get("/api/health")
def health() -> dict:
    configured = bool(os.environ.get("GMAIL_ADDRESS") and os.environ.get("GMAIL_APP_PASSWORD"))
    return {"ok": True, "emailConfigured": configured, "dryRun": os.environ.get("DRY_RUN") == "1"}


# The page and its photos. Declared after the API routes so /api/* always reaches the code above.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
