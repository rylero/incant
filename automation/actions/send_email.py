"""Send Email — send a message over SMTP.

SMTP is the MVP transport (Gmail / Google APIs come later). The login is a named
Credential resolved outside the pipeline folder (ADR-0002), so the pipeline
itself carries no secret.

This is the canonical "irreversible" step: pair it with ``delay_until_finished``
in the editor so an upstream failure prevents the send.

config:
  credential : name of the SMTP Credential (default "smtp"). The credential is a
               JSON object: host, port, username, password, optional from, tls.
  from       : override sender (else credential ``from`` or ``username``)

required payload fields: to, subject, body
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from .base import Action, ActionContext
from ..payload import Payload
from .. import credentials


class SendEmail(Action):
    type = "send_email"
    required_inputs = ["to", "subject", "body"]

    def run(self, inbound: Payload, config: dict, ctx: ActionContext) -> Payload:
        to, subject, body = self.require(inbound, "to", "subject", "body")
        cred = credentials.resolve(config.get("credential", "smtp"))

        host = cred["host"]
        port = int(cred.get("port", 587))
        username = cred.get("username")
        password = cred.get("password")
        sender = config.get("from") or cred.get("from") or username
        use_tls = cred.get("tls", True)

        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)

        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if use_tls:
                smtp.starttls()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(msg)
        ctx.log(f"[send_email] sent to {to}")
        return {"to": to}
