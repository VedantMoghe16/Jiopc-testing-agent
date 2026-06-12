"""Bonus: opt-in SMTP summary email after a run.

Disabled by default (``agent.email.enabled: false``). Credentials are never
stored in the YAML — ``SMTP_USER`` / ``SMTP_PASSWORD`` are read from the
environment; if absent the message is sent unauthenticated (fine for a LAN
relay). Every failure is reported to stderr and swallowed: a broken mail
server must never affect the validation run or its exit code (the runner
additionally wraps this call and honours ``--no-email``).
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Mapping

from jiopc_agent.config import AgentConfig

_TIMEOUT_S = 20.0


def _build_message(
    cfg: AgentConfig, summary: Mapping[str, Any], log_path: Path
) -> EmailMessage:
    """Compose the plain-text summary email from the run summary dict."""
    email = cfg.agent.email
    exit_code = summary.get("exit_code")
    verdict = "PASS" if exit_code == 0 else "FAIL"
    total = summary.get("total", 0)
    passed = summary.get("passed", 0)

    msg = EmailMessage()
    msg["From"] = email.from_addr
    msg["To"] = email.to_addr
    msg["Subject"] = (
        f"[jiopc-agent] {verdict}: {passed}/{total} passed "
        f"(exit {exit_code}) - {log_path.name}"
    )

    lines = [
        "JioPC testing agent run summary",
        "",
        f"Verdict      : {verdict} (exit code {exit_code})",
        f"Tests        : {passed}/{total} passed, "
        f"{summary.get('failed', 0)} failed, {summary.get('blocked', 0)} blocked",
        f"Duration     : {summary.get('duration_s', '?')} s",
        f"By result    : {summary.get('by_result', {})}",
        f"By component : {summary.get('by_component', {})}",
    ]
    regressions = summary.get("regressions") or []
    if regressions:
        lines.append("")
        lines.append("Regressions vs previous run:")
        lines.extend(f"  - {r}" for r in regressions)
    lines += [
        "",
        f"Full JSONL log: {log_path}",
        f"Config: {cfg.path}",
    ]
    msg.set_content("\n".join(lines))
    return msg


def send_summary(
    cfg: AgentConfig, summary: Mapping[str, Any], log_path: Path
) -> None:
    """Send the run-summary email if enabled and configured. Never raises."""
    email = cfg.agent.email
    if not email.enabled:
        return
    if not (email.smtp_host and email.from_addr and email.to_addr):
        print(
            "email notification skipped: agent.email needs smtp_host, from and to",
            file=sys.stderr,
        )
        return

    try:
        msg = _build_message(cfg, summary, Path(log_path))
        recipients = [a.strip() for a in email.to_addr.split(",") if a.strip()]
        with smtplib.SMTP(email.smtp_host, email.smtp_port, timeout=_TIMEOUT_S) as smtp:
            if email.use_tls:
                smtp.starttls(context=ssl.create_default_context())
            user = os.environ.get("SMTP_USER", "")
            password = os.environ.get("SMTP_PASSWORD", "")
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg, from_addr=email.from_addr, to_addrs=recipients)
        print(f"summary email sent to {email.to_addr}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - notification must never break a run
        print(
            f"email notification failed ({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
