import importlib.util
import io
import json
import os
from pathlib import Path
import smtplib
import ssl
import unittest
from unittest.mock import call, patch

spec = importlib.util.spec_from_file_location(
    "backup_alert", Path(__file__).parents[1] / "backup_alert.py"
)
alert = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alert)

SMTP_ENV = {
    "BACKUP_ALERT_EMAIL_TO": "recipient@example.test",
    "BACKUP_ALERT_EMAIL_FROM": "backups@example.test",
    "BACKUP_SMTP_HOST": "smtp.example.test",
    "BACKUP_SMTP_PORT": "465",
    "BACKUP_SMTP_USERNAME": "relay-user",
    "BACKUP_SMTP_PASSWORD": "relay-secret",
}
WEBHOOK = "https://monitor.example.test/private-path?token=private-token"
SERVICE = "map1-offsite-backup.service"


class BackupAlertTest(unittest.TestCase):
    def start_patch(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def setUp(self):
        self.start_patch(patch.dict(os.environ, {}, clear=True))
        self.start_patch(patch.object(alert.sys, "argv", ["backup_alert.py", SERVICE]))
        self.stdout = self.start_patch(patch("sys.stdout", new_callable=io.StringIO))
        self.stderr = self.start_patch(patch("sys.stderr", new_callable=io.StringIO))
        # Every transport is mocked; fail immediately if a test ever reaches a socket.
        self.start_patch(patch("socket.create_connection", side_effect=AssertionError("Network disabled")))
        self.smtp_ssl = self.start_patch(patch.object(alert.smtplib, "SMTP_SSL", autospec=True))
        self.smtp = self.start_patch(patch.object(alert.smtplib, "SMTP", autospec=True))
        self.ssl_client = self.smtp_ssl.return_value.__enter__.return_value
        self.tls_client = self.smtp.return_value.__enter__.return_value
        self.ssl_client.send_message.return_value = {}
        self.tls_client.send_message.return_value = {}
        self.webhook = self.start_patch(patch.object(alert, "urlopen", autospec=True))
        self.webhook.return_value.__enter__.return_value.read.return_value = b"ok"

    def assert_no_transport(self):
        self.smtp_ssl.assert_not_called()
        self.smtp.assert_not_called()
        self.webhook.assert_not_called()

    def assert_redacted(self):
        output = self.stdout.getvalue() + self.stderr.getvalue()
        private_values = [value for key, value in SMTP_ENV.items() if key != "BACKUP_SMTP_PORT"]
        for value in (*private_values, WEBHOOK, "private-path", "private-token"):
            self.assertNotIn(value, output)

    def test_webhook_works_without_docker_or_cloud_credentials(self):
        os.environ["BACKUP_ALERT_WEBHOOK"] = WEBHOOK
        self.assertEqual(alert.main(), 0)
        request = self.webhook.call_args.args[0]
        self.assertEqual(request.full_url, WEBHOOK)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), {
            "project": "Map 1", "event": "backup_failed", "service": SERVICE,
        })
        self.assertEqual(self.webhook.call_args.kwargs, {"timeout": 15})
        self.webhook.return_value.__enter__.return_value.read.assert_called_once_with(1024)
        self.smtp_ssl.assert_not_called()
        self.smtp.assert_not_called()

    def test_missing_or_insecure_endpoint_fails_honestly(self):
        for endpoint in ("", "http://monitor.example.test/backup", "https://<USER>:<PASSWORD>@example.test/", "https://["):
            with self.subTest(endpoint=endpoint):
                os.environ["BACKUP_ALERT_WEBHOOK"] = endpoint
                self.assertEqual(alert.main(), 1)
                self.assert_no_transport()
                self.assertEqual(self.stdout.getvalue(), "")

    def test_webhook_delivery_failure_redacts_endpoint(self):
        os.environ["BACKUP_ALERT_WEBHOOK"] = WEBHOOK
        self.webhook.side_effect = OSError(f"Unable to reach {WEBHOOK}")
        self.assertEqual(alert.main(), 1)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assert_redacted()

    def test_ssl_email_uses_verified_tls_authentication_and_explicit_envelope(self):
        os.environ.update(SMTP_ENV, BACKUP_ALERT_WEBHOOK=WEBHOOK)
        self.assertEqual(alert.main(), 0)
        context = self.smtp_ssl.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.smtp_ssl.assert_called_once_with("smtp.example.test", 465, timeout=15, context=context)
        self.smtp.assert_not_called()
        self.ssl_client.login.assert_called_once_with("relay-user", "relay-secret")
        message = self.ssl_client.send_message.call_args.args[0]
        self.assertIsInstance(message, alert.EmailMessage)
        self.assertEqual(str(message["From"]), SMTP_ENV["BACKUP_ALERT_EMAIL_FROM"])
        self.assertEqual(str(message["To"]), SMTP_ENV["BACKUP_ALERT_EMAIL_TO"])
        self.assertEqual(str(message["Subject"]), f"Map 1 backup failure: {SERVICE}")
        self.assertIn(f"Service: {SERVICE}", message.get_content())
        self.ssl_client.send_message.assert_called_once_with(
            message, from_addr=SMTP_ENV["BACKUP_ALERT_EMAIL_FROM"],
            to_addrs=[SMTP_ENV["BACKUP_ALERT_EMAIL_TO"]],
        )
        self.assertEqual(self.ssl_client.method_calls, [
            call.login("relay-user", "relay-secret"),
            call.send_message(message, from_addr=SMTP_ENV["BACKUP_ALERT_EMAIL_FROM"],
                              to_addrs=[SMTP_ENV["BACKUP_ALERT_EMAIL_TO"]]),
        ])
        self.smtp_ssl.return_value.__exit__.assert_called_once()
        self.webhook.assert_not_called()
        self.assertIn("accepted by SMTP relay", self.stdout.getvalue())
        self.assertNotIn("delivered", self.stdout.getvalue())
        self.assertEqual(self.stderr.getvalue(), "")
        self.assert_redacted()

    def test_starttls_upgrades_and_repeats_ehlo_before_authentication(self):
        os.environ.update(SMTP_ENV, BACKUP_SMTP_PORT="587")
        self.assertEqual(alert.main(), 0)
        self.smtp.assert_called_once_with("smtp.example.test", 587, timeout=15)
        context = self.tls_client.starttls.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        message = self.tls_client.send_message.call_args.args[0]
        self.assertEqual(self.tls_client.method_calls, [
            call.ehlo(), call.starttls(context=context), call.ehlo(),
            call.login("relay-user", "relay-secret"),
            call.send_message(message, from_addr=SMTP_ENV["BACKUP_ALERT_EMAIL_FROM"],
                              to_addrs=[SMTP_ENV["BACKUP_ALERT_EMAIL_TO"]]),
        ])
        self.smtp_ssl.assert_not_called()
        self.webhook.assert_not_called()

    def test_default_service_name(self):
        os.environ.update(SMTP_ENV)
        with patch.object(alert.sys, "argv", ["backup_alert.py"]):
            self.assertEqual(alert.main(), 0)
        message = self.ssl_client.send_message.call_args.args[0]
        self.assertEqual(str(message["Subject"]), "Map 1 backup failure: backup")

    def test_missing_or_empty_smtp_settings_fail_before_connecting(self):
        for key in SMTP_ENV.keys() - {"BACKUP_ALERT_EMAIL_TO"}:
            for value in (None, ""):
                with self.subTest(setting=key, value=value), patch.dict(os.environ, SMTP_ENV, clear=True):
                    if value is None:
                        del os.environ[key]
                    else:
                        os.environ[key] = value
                    self.assertEqual(alert.main(), 1)
                    self.assert_no_transport()
                    self.assertIn("configuration is invalid or incomplete", self.stderr.getvalue())
                    self.assertEqual(self.stdout.getvalue(), "")
                    self.assert_redacted()

    def test_only_tls_submission_ports_are_allowed(self):
        for port in ("25", "2525", "0", "-1", "465.0", " 465", "smtp"):
            with self.subTest(port=port), patch.dict(os.environ, SMTP_ENV, clear=True):
                os.environ["BACKUP_SMTP_PORT"] = port
                self.assertEqual(alert.main(), 1)
                self.assert_no_transport()
                self.assertEqual(self.stdout.getvalue(), "")

    def test_header_injection_and_control_characters_are_rejected(self):
        for separator in ("\r", "\n", "\r\n", "\x01", "\x85", "\u2028", "\u2029"):
            for field in ("BACKUP_ALERT_EMAIL_TO", "BACKUP_ALERT_EMAIL_FROM", "service"):
                with self.subTest(field=field, separator=repr(separator)), \
                        patch.dict(os.environ, SMTP_ENV, clear=True):
                    injected = f"injected@example.test{separator}Bcc: other@example.test"
                    if field == "service":
                        argv = ["backup_alert.py", injected]
                    else:
                        os.environ[field] = injected
                        argv = ["backup_alert.py", SERVICE]
                    with patch.object(alert.sys, "argv", argv):
                        self.assertEqual(alert.main(), 1)
                    self.assert_no_transport()
                    self.assertNotIn("injected@example.test", self.stderr.getvalue())
                    self.assertEqual(self.stdout.getvalue(), "")

    def test_addresses_require_one_plain_mailbox(self):
        for address in ("missing-at", "backups@", "@example.test", "a@example.test, b@example.test",
                        "Alerts <backups@example.test>", "backups@example.test (comment)", "backups@example.test "):
            for field in ("BACKUP_ALERT_EMAIL_TO", "BACKUP_ALERT_EMAIL_FROM"):
                with self.subTest(field=field, address=address), patch.dict(os.environ, SMTP_ENV, clear=True):
                    os.environ[field] = address
                    self.assertEqual(alert.main(), 1)
                    self.assert_no_transport()
                    self.assertIn("configuration is invalid or incomplete", self.stderr.getvalue())

    def test_starttls_failure_never_authenticates_or_sends(self):
        os.environ.update(SMTP_ENV, BACKUP_SMTP_PORT="587")
        for error in (smtplib.SMTPNotSupportedError("no STARTTLS"),
                      ssl.SSLCertVerificationError("untrusted certificate"), TimeoutError("timed out")):
            with self.subTest(error=type(error).__name__):
                self.tls_client.starttls.side_effect = error
                self.assertEqual(alert.main(), 1)
                self.tls_client.login.assert_not_called()
                self.tls_client.send_message.assert_not_called()
                self.smtp_ssl.assert_not_called()
                self.webhook.assert_not_called()
                self.assertEqual(self.stdout.getvalue(), "")

    def test_ssl_certificate_failure_does_not_downgrade(self):
        os.environ.update(SMTP_ENV)
        self.smtp_ssl.side_effect = ssl.SSLCertVerificationError("untrusted certificate")
        self.assertEqual(alert.main(), 1)
        self.smtp_ssl.assert_called_once()
        self.smtp.assert_not_called()
        self.webhook.assert_not_called()
        self.ssl_client.login.assert_not_called()
        self.ssl_client.send_message.assert_not_called()
        self.assertEqual(self.stdout.getvalue(), "")

    def test_authentication_failure_never_sends_and_redacts_details(self):
        os.environ.update(SMTP_ENV)
        private_details = " ".join(SMTP_ENV.values()).encode()
        self.ssl_client.login.side_effect = smtplib.SMTPAuthenticationError(535, private_details)
        self.assertEqual(alert.main(), 1)
        self.ssl_client.login.assert_called_once()
        self.ssl_client.send_message.assert_not_called()
        self.webhook.assert_not_called()
        self.assertEqual(self.stdout.getvalue(), "")
        self.assert_redacted()

    def test_smtp_send_errors_are_redacted(self):
        os.environ.update(SMTP_ENV)
        private_details = " ".join(SMTP_ENV.values())
        for error in (OSError(private_details), TimeoutError(private_details),
                      smtplib.SMTPRecipientsRefused({SMTP_ENV["BACKUP_ALERT_EMAIL_TO"]: (550, private_details)})):
            with self.subTest(error=type(error).__name__):
                self.ssl_client.send_message.side_effect = error
                self.assertEqual(alert.main(), 1)
                self.webhook.assert_not_called()
                self.assertEqual(self.stdout.getvalue(), "")
                self.assertIn("could not be accepted", self.stderr.getvalue())
                self.assert_redacted()

    def test_refused_recipient_is_not_reported_as_accepted(self):
        os.environ.update(SMTP_ENV)
        self.ssl_client.send_message.return_value = {SMTP_ENV["BACKUP_ALERT_EMAIL_TO"]: (550, b"refused")}
        self.assertEqual(alert.main(), 1)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assert_redacted()

    def test_smtp_settings_without_recipient_keep_webhook_compatibility(self):
        os.environ.update(SMTP_ENV, BACKUP_ALERT_WEBHOOK=WEBHOOK)
        del os.environ["BACKUP_ALERT_EMAIL_TO"]
        self.assertEqual(alert.main(), 0)
        self.webhook.assert_called_once()
        self.smtp_ssl.assert_not_called()
        self.smtp.assert_not_called()

    def test_incomplete_email_configuration_uses_existing_webhook(self):
        os.environ.update(SMTP_ENV, BACKUP_ALERT_WEBHOOK=WEBHOOK)
        del os.environ["BACKUP_SMTP_PASSWORD"]
        self.assertEqual(alert.main(), 0)
        self.smtp_ssl.assert_not_called()
        self.smtp.assert_not_called()
        self.webhook.assert_called_once()
        self.assertIn("configuration is invalid or incomplete", self.stderr.getvalue())
        self.assertIn("notification delivered", self.stdout.getvalue())
        self.assertNotIn("email accepted", self.stdout.getvalue())
        self.assert_redacted()

    def test_smtp_delivery_failure_uses_existing_webhook(self):
        os.environ.update(SMTP_ENV, BACKUP_ALERT_WEBHOOK=WEBHOOK)
        self.ssl_client.login.side_effect = smtplib.SMTPAuthenticationError(535, b"relay-secret")
        self.assertEqual(alert.main(), 0)
        self.webhook.assert_called_once()
        self.ssl_client.send_message.assert_not_called()
        self.assertIn("could not be accepted", self.stderr.getvalue())
        self.assertIn("notification delivered", self.stdout.getvalue())
        self.assert_redacted()

    def test_failed_email_and_webhook_do_not_claim_delivery(self):
        os.environ.update(SMTP_ENV, BACKUP_ALERT_WEBHOOK=WEBHOOK)
        self.ssl_client.login.side_effect = smtplib.SMTPAuthenticationError(535, b"relay-secret")
        self.webhook.side_effect = OSError(WEBHOOK)
        self.assertEqual(alert.main(), 1)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assert_redacted()


if __name__ == "__main__":
    unittest.main()
