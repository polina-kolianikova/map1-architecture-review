#!/usr/bin/env python3
"""Notify the configured backup monitor without depending on Docker or third-party packages."""
import json
import os
import smtplib
import ssl
import sys
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def _check_header(value):
    if not value or any(
        ord(char) < 32 or 127 <= ord(char) <= 159 or char in "\u2028\u2029" for char in value
    ):
        raise ValueError("Alert headers must be nonempty single lines")


def _mailbox(value):
    _check_header(value)
    try:
        address = Address(addr_spec=value)
    except (ValueError, IndexError, HeaderParseError):
        raise ValueError("Invalid alert mailbox") from None
    if not address.username or not address.domain or address.addr_spec != value:
        raise ValueError("Configure one plain mailbox per alert address")
    return value


def _send_email(recipient, service):
    recipient = _mailbox(recipient)
    sender = _mailbox(os.environ.get("BACKUP_ALERT_EMAIL_FROM", ""))
    _check_header(service)
    host = os.environ.get("BACKUP_SMTP_HOST", "")
    port = os.environ.get("BACKUP_SMTP_PORT", "")
    username = os.environ.get("BACKUP_SMTP_USERNAME", "")
    password = os.environ.get("BACKUP_SMTP_PASSWORD", "")
    _check_header(host)
    if port not in ("465", "587") or not username or not password:
        raise ValueError("Authenticated SMTP requires TLS port 465 or 587 and credentials")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = f"Map 1 backup failure: {service}"
    message.set_content(
        f"A Map 1 backup or freshness check failed.\nService: {service}\n"
        "Review the host system journal for details.\n"
    )
    context = ssl.create_default_context()
    if port == "465":
        connection = smtplib.SMTP_SSL(host, 465, timeout=15, context=context)
    else:
        connection = smtplib.SMTP(host, 587, timeout=15)
    with connection as smtp:
        if port == "587":
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(username, password)
        if smtp.send_message(message, from_addr=sender, to_addrs=[recipient]):
            raise smtplib.SMTPException("Alert recipient was refused")


def _send_webhook(endpoint, service):
    try:
        parsed = urlsplit(endpoint)
        valid = parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
    except ValueError:
        valid = False
    if not valid:
        print("Backup alert endpoint must be an HTTPS URL without embedded credentials", file=sys.stderr)
        return 1
    payload = json.dumps({"project": "Map 1", "event": "backup_failed", "service": service}).encode()
    try:
        request = Request(endpoint, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=15) as response:
            response.read(1024)
    except Exception:
        print("Backup failure notification could not be delivered", file=sys.stderr)
        return 1
    print("Backup failure notification delivered")
    return 0


def main():
    recipient = os.environ.get("BACKUP_ALERT_EMAIL_TO", "")
    endpoint = os.environ.get("BACKUP_ALERT_WEBHOOK", "")
    service = sys.argv[1] if len(sys.argv) > 1 else "backup"
    if recipient:
        try:
            _send_email(recipient, service)
        except ValueError:
            print(
                "Backup email alert configuration is invalid or incomplete; require a relay-verified sender, "
                "SMTP host, port 465 or 587, username and password",
                file=sys.stderr,
            )
        except Exception:
            print("Backup failure email could not be accepted by SMTP relay", file=sys.stderr)
        else:
            print("Backup failure email accepted by SMTP relay")
            return 0
        if not endpoint:
            return 1
    if endpoint:
        return _send_webhook(endpoint, service)
    print("Backup failure recorded in journal; no external alert endpoint configured", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
