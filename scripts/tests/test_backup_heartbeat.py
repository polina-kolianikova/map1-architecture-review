import importlib.util
import io
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("backup_heartbeat", ROOT / "scripts/backup_heartbeat.py")
heartbeat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(heartbeat)
PING = "https://hc-ping.com/00000000-0000-4000-8000-000000000001"


class BackupHeartbeatTest(unittest.TestCase):
    def setUp(self):
        def start(patcher):
            self.addCleanup(patcher.stop)
            return patcher.start()
        start(patch.dict(os.environ, {"BACKUP_HEARTBEAT_URL": PING}, clear=True))
        start(patch.object(heartbeat.sys, "argv", ["backup_heartbeat.py", "success"]))
        start(patch("socket.create_connection", side_effect=AssertionError("Network disabled")))
        self.stdout = start(patch("sys.stdout", new_callable=io.StringIO))
        self.stderr = start(patch("sys.stderr", new_callable=io.StringIO))
        self.factory = start(patch.object(heartbeat, "build_opener"))
        self.opener = self.factory.return_value
        self.response = self.opener.open.return_value.__enter__.return_value
        self.response.status = 200
        self.response.read.return_value = b"OK\n"

    def test_start_and_success_use_distinct_bounded_post_requests(self):
        for event, suffix in (("start", "/start"), ("success", "")):
            with self.subTest(event=event), patch.object(heartbeat.sys, "argv", ["heartbeat", event]):
                self.assertEqual(heartbeat.main(), 0)
                request = self.opener.open.call_args.args[0]
                self.assertEqual(request.full_url, PING + suffix)
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(request.data, b"")
                self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 5})
                self.response.read.assert_called_with(16)
                self.assertIsInstance(self.factory.call_args.args[0], heartbeat.NoRedirect)
                self.assertNotIn(PING, self.stdout.getvalue())

    def test_invalid_or_injected_urls_fail_before_network(self):
        for value in ("", "http://hc-ping.com/00000000-0000-4000-8000-000000000001",
                      PING + "/fail", PING + "?private=secret", PING + "#secret", PING + "\n",
                      PING.replace("hc-ping.com", "user:secret@hc-ping.com"),
                      PING.replace("hc-ping.com", "hc-ping.com:443"),
                      PING.replace("hc-ping.com", "unrelated.example"), "https://hc-ping.com/not-a-uuid"):
            with self.subTest(value=value), patch.dict(os.environ, {"BACKUP_HEARTBEAT_URL": value}):
                self.assertEqual(heartbeat.main(), 1)
                self.factory.assert_not_called()
                self.assertEqual(self.stdout.getvalue(), "")
                self.assertNotIn("secret", self.stderr.getvalue())

    def test_unknown_event_and_extra_arguments_cannot_signal_success(self):
        for argv in (["heartbeat"], ["heartbeat", "fail"], ["heartbeat", "success", "extra"]):
            with self.subTest(argv=argv), patch.object(heartbeat.sys, "argv", argv):
                self.assertEqual(heartbeat.main(), 1)
                self.factory.assert_not_called()

    def test_timeout_redacts_ping_token_and_never_claims_acceptance(self):
        self.opener.open.side_effect = TimeoutError(PING)
        self.assertEqual(heartbeat.main(), 1)
        self.opener.open.assert_called_once()
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertNotIn(PING, self.stderr.getvalue())

    def test_monitor_rejection_is_not_accepted(self):
        for status, body in ((500, b"OK"), (200, b"not a ping response"), (204, b"")):
            with self.subTest(status=status, body=body):
                self.response.status = status
                self.response.read.return_value = body
                self.assertEqual(heartbeat.main(), 1)
                self.assertEqual(self.stdout.getvalue(), "")

    def test_redirects_cannot_forward_the_secret_url(self):
        with self.assertRaises(ValueError):
            heartbeat.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://unrelated.example")

    def test_unit_routing_and_ignored_ping_failures(self):
        daily = ROOT / "infra/systemd/map1-offsite-backup.service.d/20-healthchecks.conf"
        hourly = ROOT / "infra/systemd/map1-offsite-backup-check.service.d/20-healthchecks.conf"
        for source, instance in ((daily, "map1-offsite-backup.service"), (hourly, "map1-offsite-backup-check.service")):
            content = source.read_text()
            self.assertIn(f"EnvironmentFile=-/srv/map1/secrets/backup-alert-{instance}.env", content)
            self.assertIn("ExecStartPre=-/usr/bin/python3 /srv/map1/bin/backup_heartbeat.py start", content)
            self.assertIn("ExecStartPost=-/usr/bin/python3 /srv/map1/bin/backup_heartbeat.py success", content)
            self.assertIn("OnFailure=map1-backup-alert@%n.service", source.parent.parent.joinpath(instance).read_text())
        failure = ROOT / "infra/systemd/map1-backup-alert@.service.d/20-healthchecks.conf"
        self.assertIn("EnvironmentFile=/srv/map1/secrets/backup-alert-%i.env", failure.read_text())


if __name__ == "__main__":
    unittest.main()
