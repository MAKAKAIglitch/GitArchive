import ipaddress
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gitarchive import lan


class LanAddressTests(unittest.TestCase):
    def test_private_and_overlay_addresses_are_allowed(self):
        for value in ("10.0.0.2", "172.16.4.5", "192.168.1.10", "100.64.0.8"):
            with self.subTest(value=value):
                self.assertTrue(lan._is_local_address(ipaddress.ip_address(value)))

    def test_public_loopback_link_local_and_ipv6_are_not_scanned(self):
        for value in ("8.8.8.8", "127.0.0.1", "169.254.1.10", "224.0.0.1", "::1", "fd00::1"):
            with self.subTest(value=value):
                self.assertFalse(lan._is_local_address(ipaddress.ip_address(value)))

    def test_private_ipv4_peer_guard_rejects_public_and_ipv6(self):
        self.assertEqual(lan._require_private_ipv4("10.1.2.3"), "10.1.2.3")
        self.assertEqual(lan._require_private_ipv4("100.64.0.9"), "100.64.0.9")
        for address in ("8.8.8.8", "127.0.0.1", "169.254.1.2", "::1", "invalid"):
            with self.subTest(address=address):
                with self.assertRaises(ValueError):
                    lan._require_private_ipv4(address)

    def test_interface_discovery_includes_virtual_private_adapters_only(self):
        interfaces = {
            "Wi-Fi": [
                SimpleNamespace(
                    family=socket.AF_INET,
                    address="192.168.1.20",
                    netmask="255.255.255.0",
                )
            ],
            "ZeroTier One": [
                SimpleNamespace(
                    family=socket.AF_INET,
                    address="10.147.17.5",
                    netmask="255.255.255.0",
                )
            ],
            "Public": [
                SimpleNamespace(
                    family=socket.AF_INET,
                    address="8.8.8.8",
                    netmask="255.255.255.0",
                )
            ],
            "Large subnet": [
                SimpleNamespace(
                    family=socket.AF_INET,
                    address="10.2.0.10",
                    netmask="255.255.0.0",
                )
            ],
        }
        with patch.object(lan.psutil, "net_if_addrs", return_value=interfaces):
            result = lan.local_ipv4_interfaces()
        self.assertEqual(
            result,
            [
                ("10.147.17.5", ipaddress.ip_network("10.147.17.0/24")),
                ("192.168.1.20", ipaddress.ip_network("192.168.1.0/24")),
            ],
        )


class LanIdentityTests(unittest.TestCase):
    def test_identity_is_persisted_and_fingerprint_is_stable(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first = lan.LanPeerService(directory)
            second = lan.LanPeerService(directory)
            self.assertEqual(first.peer_id, second.peer_id)
            self.assertEqual(first.fingerprint, second.fingerprint)
            self.assertEqual(len(first.fingerprint), 64)
            certificate, peer_id, fingerprint = lan._validate_identity_certificate(first.cert_pem)
            self.assertEqual(peer_id, first.peer_id)
            self.assertEqual(fingerprint, first.fingerprint)
            self.assertEqual(certificate.subject, certificate.issuer)

    def test_pairing_pin_is_persisted_refreshed_and_revoked(self):
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            local = lan.LanPeerService(Path(first_dir))
            remote = lan.LanPeerService(Path(second_dir))
            local._store_peer(remote.peer_id, remote.cert_pem, "192.168.1.30", lan.DEFAULT_PORT)
            self.assertEqual(local.find_peer(remote.peer_id)["address"], "192.168.1.30")
            self.assertFalse(local.refresh_peer_endpoint(
                remote.peer_id, "192.168.1.31", lan.DEFAULT_PORT, "0" * 64
            ))
            self.assertTrue(local.refresh_peer_endpoint(
                remote.peer_id, "192.168.1.31", lan.DEFAULT_PORT, remote.fingerprint
            ))
            self.assertEqual(local.find_peer(remote.peer_id)["address"], "192.168.1.31")
            local.forget_peer(remote.peer_id)
            self.assertIsNone(local.find_peer(remote.peer_id))

    def test_invalid_identity_certificate_is_rejected(self):
        with self.assertRaises((ValueError, TypeError)):
            lan._validate_identity_certificate("not a PEM certificate")


if __name__ == "__main__":
    unittest.main()
