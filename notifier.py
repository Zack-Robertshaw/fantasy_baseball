"""
Email notifications for scheduled fantasy lineup optimizations.
"""

from __future__ import annotations

import html
import logging
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _result_change_count(entry: dict) -> int:
    result = entry.get("result") or {}
    summary = result.get("summary") or {}
    try:
        return int(summary.get("total_changes_count") or 0)
    except (TypeError, ValueError):
        return 0


def should_notify_optimization_results(results: list[dict]) -> bool:
    if not results:
        return False
    if _env_bool("NOTIFY_ON_NO_CHANGES", False):
        return True
    return any(
        _result_change_count(entry) > 0 or (entry.get("result") or {}).get("error")
        for entry in results
    )


def _entry_label(entry: dict) -> str:
    return str(entry.get("label") or entry.get("team_key") or "Team")


def _entry_league(entry: dict) -> str:
    return str(entry.get("league_label") or entry.get("league_key") or "League")


def _apply_status(result: dict, section: str, detail: dict, dry_run: bool) -> str:
    if dry_run:
        return "Preview"

    apply_result_key = f"{section.lower()}_apply_result"
    apply_result = result.get(apply_result_key) or {}
    if not apply_result:
        return "Not attempted"

    player_key = detail.get("player_key")
    to_position = detail.get("to")
    applied = {
        (move.get("player_key"), move.get("position"))
        for move in apply_result.get("applied", [])
    }
    failed = {
        (move.get("player_key"), move.get("position"))
        for move in apply_result.get("failed", [])
    }
    move_key = (player_key, to_position)
    if move_key in applied:
        return "Applied"
    if move_key in failed:
        return "Failed"
    return "Unknown"


def _move_rows(entry: dict, section: str, details: list[dict], dry_run: bool) -> list[str]:
    result = entry.get("result") or {}
    league = html.escape(_entry_league(entry))
    label = html.escape(_entry_label(entry))
    rows = []
    for detail in details:
        update_status = html.escape(_apply_status(result, section, detail, dry_run))
        player = html.escape(str(detail.get("player") or "Unknown"))
        action = html.escape(str(detail.get("action") or "move").title())
        from_position = html.escape(str(detail.get("from") or ""))
        to_position = html.escape(str(detail.get("to") or ""))
        reason = html.escape(str(detail.get("reason") or ""))
        transition = (
            f"{from_position} -> {to_position}"
            if from_position
            else f"-> {to_position}"
        )
        rows.append(
            "<tr>"
            f"<td>{league}</td>"
            f"<td>{label}</td>"
            f"<td>{html.escape(section)}</td>"
            f"<td>{player}</td>"
            f"<td>{action}</td>"
            f"<td>{transition}</td>"
            f"<td>{update_status}</td>"
            f"<td>{reason}</td>"
            "</tr>"
        )
    return rows


def _format_team_section(entry: dict) -> str:
    result = entry.get("result") or {}
    summary = result.get("summary") or {}
    label = html.escape(_entry_label(entry))
    league = html.escape(_entry_league(entry))
    league_key = html.escape(str(entry.get("league_key") or summary.get("league_key") or ""))
    team_key = html.escape(str(entry.get("team_key") or summary.get("team_key") or ""))
    date = html.escape(str(summary.get("date") or ""))
    total = _result_change_count(entry)
    pitcher_count = len(result.get("pitcher_changes") or [])
    batter_count = len(result.get("batter_changes") or [])
    error = result.get("error")

    status = "Error" if error else ("Changes found" if total else "No changes")
    error_html = f"<p><strong>Error:</strong> {html.escape(str(error))}</p>" if error else ""
    return (
        "<section>"
        f"<h2>{label}</h2>"
        f"<p><strong>Status:</strong> {status}<br>"
        f"<strong>League:</strong> {league}<br>"
        f"<strong>League key:</strong> {league_key}<br>"
        f"<strong>Date:</strong> {date}<br>"
        f"<strong>Team key:</strong> {team_key}<br>"
        f"<strong>Total changes:</strong> {total} "
        f"({pitcher_count} pitcher, {batter_count} batter)</p>"
        f"{error_html}"
        "</section>"
    )


def format_optimization_email(results: list[dict], dry_run: bool) -> tuple[str, str]:
    total_changes = sum(_result_change_count(entry) for entry in results)
    errors = [entry for entry in results if (entry.get("result") or {}).get("error")]
    labels = [_entry_label(entry) for entry in results]
    target = labels[0] if len(labels) == 1 else f"{len(labels)} teams"
    action_word = "suggested" if dry_run else "applied"

    if errors:
        subject = (
            f"Fantasy Lineup: {len(errors)} error(s), "
            f"{total_changes} changes {action_word} ({target})"
        )
    else:
        subject = f"Fantasy Lineup: {total_changes} changes {action_word} ({target})"

    rows: list[str] = []
    for entry in results:
        result = entry.get("result") or {}
        rows.extend(_move_rows(entry, "Pitcher", result.get("pitcher_details") or [], dry_run))
        rows.extend(_move_rows(entry, "Batter", result.get("batter_details") or [], dry_run))

    table_html = ""
    if rows:
        table_html = (
            "<h2>Lineup Changes</h2>"
            "<table border=\"1\" cellpadding=\"6\" cellspacing=\"0\">"
            "<thead><tr>"
            "<th>League</th><th>Team</th><th>Group</th><th>Player</th>"
            "<th>Action</th><th>Move</th><th>Update</th><th>Reason</th>"
            "</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody>"
            "</table>"
        )
    else:
        table_html = "<p>No lineup changes were needed.</p>"

    mode = "Dry run" if dry_run else "Auto apply"
    body = (
        "<html><body>"
        f"<h1>Fantasy Lineup Optimization</h1>"
        f"<p><strong>Mode:</strong> {mode}</p>"
        f"{''.join(_format_team_section(entry) for entry in results)}"
        f"{table_html}"
        "</body></html>"
    )
    return subject, body


def send_notification(subject: str, body_html: str) -> bool:
    if not _env_bool("NOTIFY_EMAIL_ENABLED", False):
        return False

    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = _env_int("SMTP_PORT", 587)
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    recipient = os.getenv("NOTIFY_EMAIL_TO", "").strip()

    if not smtp_host or not smtp_user or not smtp_password or not recipient:
        logger.warning("Email notifications are enabled but SMTP configuration is incomplete")
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_user
    message["To"] = recipient
    message.set_content("Your email client does not support HTML messages.")
    message.add_alternative(body_html, subtype="html")

    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15) as smtp:
                smtp.login(smtp_user, smtp_password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
                smtp.starttls()
                smtp.login(smtp_user, smtp_password)
                smtp.send_message(message)
    except Exception:
        logger.exception("Failed to send lineup notification email")
        return False

    logger.info("Sent lineup notification email to %s", recipient)
    return True
