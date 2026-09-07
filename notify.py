"""
Email notifications over SMTP.

Exactly three things are ever sent: HIT, ERROR, HEARTBEAT. Credentials come
from environment variables (GitHub repo secrets in production) and are never
written to disk, logged, or committed.
"""

from __future__ import annotations

import html
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import List, Optional

from availability import site_label

SMTP_TIMEOUT_SECONDS = 45
SMTP_MAX_ATTEMPTS = 3


class NotificationError(Exception):
    """The email could not be delivered."""


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def config_problems() -> List[str]:
    """Missing-credential check, so a misconfigured repo fails loudly at start."""
    problems = []
    for name in ("SMTP_USERNAME", "SMTP_PASSWORD", "ALERT_EMAIL_TO"):
        if not _env(name):
            problems.append(f"missing required secret/env var {name}")
    return problems


def send_email(subject: str, text_body: str, html_body: Optional[str] = None) -> None:
    """Send one message, retrying transient SMTP failures."""
    problems = config_problems()
    if problems:
        raise NotificationError("; ".join(problems))

    host = _env("SMTP_HOST", "smtp.gmail.com")
    port = int(_env("SMTP_PORT", "465"))
    username = _env("SMTP_USERNAME")
    password = _env("SMTP_PASSWORD")
    mail_to = _env("ALERT_EMAIL_TO")
    mail_from = _env("ALERT_EMAIL_FROM", username)

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = mail_from
    message["To"] = mail_to
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    last_error: Optional[Exception] = None

    for attempt in range(1, SMTP_MAX_ATTEMPTS + 1):
        try:
            if port == 465:
                with smtplib.SMTP_SSL(
                    host, port, timeout=SMTP_TIMEOUT_SECONDS, context=context
                ) as server:
                    server.login(username, password)
                    server.send_message(message)
            else:
                with smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECONDS) as server:
                    server.ehlo()
                    server.starttls(context=context)
                    server.ehlo()
                    server.login(username, password)
                    server.send_message(message)
            return
        except smtplib.SMTPAuthenticationError as exc:
            # Bad app password will not fix itself; do not hammer the server.
            raise NotificationError(
                f"SMTP authentication rejected for {username} at {host}:{port} — "
                f"check the SMTP_PASSWORD secret (Gmail requires a 16-character "
                f"App Password, not your account password). {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - smtplib raises a wide family
            last_error = exc
            if attempt < SMTP_MAX_ATTEMPTS:
                import time

                time.sleep(2.0 * attempt)

    raise NotificationError(
        f"SMTP delivery to {host}:{port} failed after {SMTP_MAX_ATTEMPTS} attempts: "
        f"{type(last_error).__name__}: {last_error}"
    )


def _wrap_html(inner: str) -> str:
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,'
        'Arial,sans-serif;font-size:15px;line-height:1.5;color:#111;">'
        f"{inner}</div>"
    )


def send_hit(result, stay_label: str, link: str, park_name: str) -> None:
    """A campground newly meets the 2-night criteria."""
    site_lines = []
    for site in result.sites:
        nights = " + ".join(_night_label(n) for n in site.nights)
        tent_note = ""
        if site.tent_friendly is False:
            tent_note = f"  [NOT TENT — {site.category}]"
        elif site.category:
            tent_note = f"  [{site.category}]"
        site_lines.append(f"  {site_label(site.name)} — {nights}{tent_note}")

    if result.stay_kind == "single":
        arrangement = "ONE SITE covers both nights (single 2-night reservation)."
    else:
        arrangement = (
            "TWO DIFFERENT SITES — you must book them as two separate 1-night "
            "reservations and move camp between them."
        )

    flag = " [RV/NON-TENT]" if result.has_non_tent_site else ""
    subject = f"🏕️ BOOK NOW{flag} — {result.name} open {stay_label}"

    text_body = "\n".join(
        [
            f"{result.name} has 2-night availability for {stay_label}.",
            "",
            arrangement,
            "",
            "Site(s):",
            *site_lines,
            "",
            f"Park: {park_name}",
            f"Book: {link}",
            "",
            "Cancellations at these campgrounds are usually gone within minutes.",
        ]
    )

    site_html = "".join(
        "<li><b>{name}</b> — {nights}{note}</li>".format(
            name=html.escape(site_label(site.name)),
            nights=html.escape(" + ".join(_night_label(n) for n in site.nights)),
            note=(
                f' <span style="color:#b45309;">[not a tent site — '
                f"{html.escape(site.category or '')}]</span>"
                if site.tent_friendly is False
                else (
                    f' <span style="color:#666;">({html.escape(site.category)})</span>'
                    if site.category
                    else ""
                )
            ),
        )
        for site in result.sites
    )

    html_body = _wrap_html(
        f"<h2 style='margin:0 0 8px;'>{html.escape(result.name)}</h2>"
        f"<p style='margin:0 0 12px;color:#555;'>{html.escape(park_name)} &middot; "
        f"{html.escape(stay_label)}</p>"
        f"<p style='margin:0 0 12px;'><b>{html.escape(arrangement)}</b></p>"
        f"<ul style='margin:0 0 16px;padding-left:20px;'>{site_html}</ul>"
        f"<p style='margin:0 0 16px;'><a href='{html.escape(link)}' "
        "style='background:#0b7285;color:#fff;padding:10px 16px;border-radius:6px;"
        "text-decoration:none;display:inline-block;'>Book on ReserveCalifornia</a></p>"
        "<p style='margin:0;color:#777;font-size:13px;'>Cancellations at these "
        "campgrounds are usually gone within minutes.</p>"
    )

    send_email(subject, text_body, html_body)


def send_error(summary: str, detail: str, consecutive_failures: int) -> None:
    subject = f"⚠️ Campsite scanner ERROR — {summary}"
    text_body = "\n".join(
        [
            "The ReserveCalifornia scanner run failed.",
            "",
            f"Summary: {summary}",
            f"Consecutive failing runs: {consecutive_failures}",
            "",
            "Detail:",
            detail,
            "",
            "Repeat errors are throttled; the heartbeat email reports overall health.",
        ]
    )
    html_body = _wrap_html(
        f"<h2 style='margin:0 0 8px;color:#b91c1c;'>Scanner error</h2>"
        f"<p style='margin:0 0 4px;'><b>{html.escape(summary)}</b></p>"
        f"<p style='margin:0 0 12px;color:#555;'>Consecutive failing runs: "
        f"{consecutive_failures}</p>"
        "<pre style='background:#f4f4f5;padding:12px;border-radius:6px;"
        "white-space:pre-wrap;font-size:13px;'>"
        f"{html.escape(detail)}</pre>"
    )
    send_email(subject, text_body, html_body)


def send_heartbeat(
    last_run: str,
    polls: int,
    campground_count: int,
    stay_label: str,
    failures: int,
    open_now: List[str],
) -> None:
    subject = "✅ Campsite scanner heartbeat — running normally"
    open_line = ", ".join(open_now) if open_now else "none"
    text_body = "\n".join(
        [
            "The ReserveCalifornia scanner is running normally.",
            "",
            f"Last run (UTC):            {last_run}",
            f"Polls since last heartbeat: {polls}",
            f"Campgrounds watched:        {campground_count}",
            f"Target stay:                {stay_label}",
            f"Failed runs in that window: {failures}",
            f"Currently open (no new alert): {open_line}",
            "",
            "If you stop receiving this weekly email, the scanner has stopped.",
        ]
    )
    html_body = _wrap_html(
        "<h2 style='margin:0 0 12px;'>Scanner heartbeat</h2>"
        "<table style='border-collapse:collapse;font-size:14px;'>"
        f"<tr><td style='padding:3px 12px 3px 0;color:#555;'>Last run (UTC)</td>"
        f"<td><b>{html.escape(last_run)}</b></td></tr>"
        f"<tr><td style='padding:3px 12px 3px 0;color:#555;'>Polls since last heartbeat</td>"
        f"<td><b>{polls}</b></td></tr>"
        f"<tr><td style='padding:3px 12px 3px 0;color:#555;'>Campgrounds watched</td>"
        f"<td><b>{campground_count}</b></td></tr>"
        f"<tr><td style='padding:3px 12px 3px 0;color:#555;'>Target stay</td>"
        f"<td><b>{html.escape(stay_label)}</b></td></tr>"
        f"<tr><td style='padding:3px 12px 3px 0;color:#555;'>Failed runs</td>"
        f"<td><b>{failures}</b></td></tr>"
        f"<tr><td style='padding:3px 12px 3px 0;color:#555;'>Currently open</td>"
        f"<td><b>{html.escape(open_line)}</b></td></tr>"
        "</table>"
        "<p style='margin:14px 0 0;color:#777;font-size:13px;'>If you stop receiving "
        "this weekly email, the scanner has stopped.</p>"
    )
    send_email(subject, text_body, html_body)


def _night_label(iso_date: str) -> str:
    import datetime

    try:
        day = datetime.date.fromisoformat(iso_date)
    except ValueError:
        return iso_date
    return f"{day.strftime('%A')} {day.month}/{day.day} night"
