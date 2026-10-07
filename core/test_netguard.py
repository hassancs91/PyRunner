"""
core/netguard.py — the one SSRF guard for user-supplied outbound URLs
(vector seam Stage 0, roadmap decision #7).

The matrix: {loopback, RFC-1918, public name -> private IP, link-local / cloud
metadata, unspecified, public} x PYRUNNER_ALLOW_PRIVATE_NETWORK {off, on}.
Off must behave exactly as the pre-flag guard did; on opens loopback / private /
reserved but never the metadata range or the unspecified addresses.
"""

import socket
from unittest import mock

from django.test import SimpleTestCase, override_settings

from core import netguard
from core.netguard import is_safe_endpoint_url

PUBLIC_IP = "93.184.216.34"


def _resolving_to(*ips):
    """Patch DNS so a hostname resolves to the given addresses (no network)."""
    return mock.patch(
        "core.netguard.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", (ip, 0)) for ip in ips],
    )


class GuardOffTests(SimpleTestCase):
    """Default flag: byte-for-byte the previous hard block."""

    def test_default_flag_is_off(self):
        self.assertFalse(netguard.private_network_allowed())

    def test_empty_url_is_unset_not_unsafe(self):
        self.assertEqual(is_safe_endpoint_url(""), (True, ""))

    def test_no_hostname_rejected(self):
        ok, msg = is_safe_endpoint_url("not a url")
        self.assertFalse(ok)
        self.assertIn("no hostname", msg)

    def test_localhost_names_blocked_without_dns(self):
        with mock.patch("core.netguard.socket.getaddrinfo") as gai:
            for host in ("localhost", "LOCALHOST", "localhost.localdomain", "minio.localhost"):
                ok, msg = is_safe_endpoint_url(f"http://{host}:9000")
                self.assertFalse(ok, host)
                self.assertIn("localhost", msg)
            gai.assert_not_called()

    def test_loopback_literals_blocked(self):
        for url in ("http://127.0.0.1:9000", "http://127.1.2.3/", "http://[::1]:6333"):
            ok, msg = is_safe_endpoint_url(url)
            self.assertFalse(ok, url)
            self.assertIn("Loopback", msg)

    def test_rfc1918_literals_blocked(self):
        for ip in ("10.0.0.5", "172.16.0.1", "172.31.255.254", "192.168.1.1"):
            ok, msg = is_safe_endpoint_url(f"http://{ip}/hook")
            self.assertFalse(ok, ip)
            self.assertIn("Private IP", msg)

    def test_public_name_resolving_to_private_ip_blocked(self):
        with _resolving_to("10.1.2.3"):
            ok, msg = is_safe_endpoint_url("https://evil.example/h")
        self.assertFalse(ok)
        self.assertIn("10.1.2.3", msg)

    def test_name_with_one_private_address_among_public_blocked(self):
        with _resolving_to(PUBLIC_IP, "192.168.0.9"):
            ok, _ = is_safe_endpoint_url("https://mixed.example/")
        self.assertFalse(ok)

    def test_public_allowed(self):
        self.assertEqual(is_safe_endpoint_url(f"https://{PUBLIC_IP}/"), (True, ""))
        with _resolving_to(PUBLIC_IP):
            self.assertEqual(is_safe_endpoint_url("https://ok.example/h"), (True, ""))

    def test_unresolvable_hostname_allowed_with_warning(self):
        with mock.patch(
            "core.netguard.socket.getaddrinfo", side_effect=socket.gaierror("nope")
        ), self.assertLogs("core.netguard", level="WARNING") as logs:
            self.assertEqual(is_safe_endpoint_url("https://nope.invalid/"), (True, ""))
        self.assertIn("does not resolve", logs.output[0])

    def test_blocked_message_names_the_flag(self):
        _, msg = is_safe_endpoint_url("http://10.0.0.5/")
        self.assertIn("PYRUNNER_ALLOW_PRIVATE_NETWORK", msg)


@override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=True)
class GuardOnTests(SimpleTestCase):
    """Operator opt-in: compose siblings and localhost become reachable."""

    def test_flag_read_from_settings(self):
        self.assertTrue(netguard.private_network_allowed())

    def test_localhost_and_loopback_allowed(self):
        for url in ("http://localhost:9000", "http://127.0.0.1:9000", "http://[::1]:6333"):
            self.assertEqual(is_safe_endpoint_url(url), (True, ""), url)

    def test_rfc1918_allowed(self):
        for ip in ("10.0.0.5", "172.18.0.4", "192.168.1.1"):
            self.assertEqual(is_safe_endpoint_url(f"http://{ip}:6333"), (True, ""), ip)

    def test_public_name_resolving_to_private_allowed(self):
        with _resolving_to("172.18.0.4"):
            self.assertEqual(is_safe_endpoint_url("http://qdrant/"), (True, ""))

    def test_reserved_allowed(self):
        self.assertEqual(is_safe_endpoint_url("http://240.0.0.1/"), (True, ""))

    def test_explicit_kwarg_pins_a_stricter_policy(self):
        ok, msg = is_safe_endpoint_url("http://10.0.0.5/", allow_private=False)
        self.assertFalse(ok)
        self.assertIn("Private IP", msg)

    @override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=False)
    def test_explicit_kwarg_can_also_loosen(self):
        self.assertEqual(is_safe_endpoint_url("http://10.0.0.5/", allow_private=True), (True, ""))


class AlwaysBlockedTests(SimpleTestCase):
    """The metadata carve-out: no flag value opens these."""

    METADATA_URLS = (
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.0.1/",
        "http://[fe80::1]/",
        "http://[fd00:ec2::254]/latest/meta-data/",
        "http://[::ffff:169.254.169.254]/",  # IPv4-mapped IPv6 spelling
        "http://[::ffff:a9fe:a9fe]/",  # same address, hex spelling
    )

    UNSPECIFIED_URLS = ("http://0.0.0.0:8000/", "http://[::]:8000/")

    def _assert_blocked_both_ways(self, url, fragment):
        for flag in (False, True):
            ok, msg = is_safe_endpoint_url(url, allow_private=flag)
            self.assertFalse(ok, f"{url} flag={flag}")
            self.assertIn(fragment, msg, f"{url} flag={flag}")

    def test_metadata_and_link_local_literals(self):
        for url in self.METADATA_URLS:
            self._assert_blocked_both_ways(url, "cloud-metadata")

    def test_unspecified_literals(self):
        for url in self.UNSPECIFIED_URLS:
            self._assert_blocked_both_ways(url, "Unspecified")

    def test_public_name_resolving_to_metadata(self):
        with _resolving_to("169.254.169.254"):
            self._assert_blocked_both_ways("http://meta.example/", "cloud-metadata")

    def test_scoped_ipv6_literal_from_resolver_is_classified_not_rejected_as_invalid(self):
        with _resolving_to("fe80::1%eth0"):
            ok, msg = is_safe_endpoint_url("http://link.example/", allow_private=True)
        self.assertFalse(ok)
        self.assertIn("cloud-metadata", msg)
        self.assertNotIn("Invalid endpoint URL", msg)

    def test_metadata_wins_over_private_classification(self):
        # 169.254/16 is also "private" in ipaddress terms; the message must say
        # why it can never be allowed, not suggest the flag.
        _, msg = is_safe_endpoint_url("http://169.254.169.254/", allow_private=False)
        self.assertNotIn("PYRUNNER_ALLOW_PRIVATE_NETWORK", msg)

    def test_ula_is_private_but_imds_v6_address_still_blocked(self):
        # The flag opens fd00::/8 in general ...
        self.assertEqual(is_safe_endpoint_url("http://[fd00::10]/", allow_private=True), (True, ""))
        # ... but not the one IMDS address inside it.
        ok, _ = is_safe_endpoint_url("http://[fd00:ec2::254]/", allow_private=True)
        self.assertFalse(ok)


class CallerWiringTests(SimpleTestCase):
    """Every existing user-URL validator goes through the shared guard."""

    def test_s3_service_re_exports_the_same_function(self):
        from core.services import s3_service

        self.assertIs(s3_service.is_safe_endpoint_url, netguard.is_safe_endpoint_url)

    def test_webhook_scheme_check_stays_local(self):
        from core.services.notification_service import NotificationService

        self.assertFalse(NotificationService._is_safe_webhook_url("ftp://example.com/h"))
        self.assertFalse(NotificationService._is_safe_webhook_url("http:///h"))

    def test_webhook_follows_the_flag(self):
        from core.services.notification_service import NotificationService

        self.assertFalse(NotificationService._is_safe_webhook_url("http://10.0.0.5/h"))
        self.assertFalse(NotificationService._is_safe_webhook_url("http://0.0.0.0/h"))
        with override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=True):
            self.assertTrue(NotificationService._is_safe_webhook_url("http://10.0.0.5/h"))
            self.assertFalse(NotificationService._is_safe_webhook_url("http://169.254.169.254/h"))
            self.assertFalse(NotificationService._is_safe_webhook_url("http://0.0.0.0/h"))
