import socket
import threading
import time
import unittest

from gitarchive.throttle import (
    ProxyConfigurationError,
    ThrottledHttpProxy,
    normalize_download_limit,
)


class DownloadLimitNormalizationTests(unittest.TestCase):
    def test_default_and_invalid_values(self):
        self.assertEqual(normalize_download_limit(None), 3)
        self.assertEqual(normalize_download_limit("not-a-number"), 3)

    def test_zero_disables_the_cap(self):
        self.assertEqual(normalize_download_limit(0), 0)

    def test_values_are_clamped(self):
        self.assertEqual(normalize_download_limit(-5), 0)
        self.assertEqual(normalize_download_limit(150), 100)


class ThrottledHttpProxyTests(unittest.TestCase):
    def test_proxy_only_listens_on_loopback(self):
        proxy = ThrottledHttpProxy(3_000_000)
        try:
            address = proxy.start()
            self.assertTrue(address.startswith("http://127.0.0.1:"))
            with socket.create_connection(
                ("127.0.0.1", int(address.rsplit(":", 1)[1])), timeout=2
            ):
                pass
        finally:
            proxy.close()

    def test_upstream_proxy_rejects_credentials_instead_of_dropping_them(self):
        proxy = ThrottledHttpProxy(3_000_000)
        try:
            with self.assertRaises(ProxyConfigurationError):
                proxy.set_route("git.example", 443, "http://user:secret@127.0.0.1:8080")
        finally:
            proxy.close()

    def test_host_specific_upstream_routes(self):
        proxy = ThrottledHttpProxy(3_000_000)
        proxy.set_route("git.example.com", 443, "http://127.0.0.1:8080")
        self.assertEqual(
            proxy._settings("GIT.EXAMPLE.COM", 443)[0],
            "http://127.0.0.1:8080",
        )

    def test_no_proxy_domain_matching(self):
        self.assertTrue(
            ThrottledHttpProxy._matches_no_proxy("api.example.com", 443, ".example.com")
        )
        self.assertFalse(
            ThrottledHttpProxy._matches_no_proxy("example.org", 443, ".example.com")
        )

    def test_download_limit_is_shared_across_multiple_tunnels(self):
        payload = b"y" * 800
        target = socket.socket()
        target.bind(("127.0.0.1", 0))
        target.listen(2)
        target.settimeout(5)
        target_port = target.getsockname()[1]
        errors = []
        received_payloads = []

        def serve_targets():
            connections = []
            try:
                for _ in range(2):
                    connection, _ = target.accept()
                    connections.append(connection)
                for connection in connections:
                    connection.sendall(payload)
            except OSError as error:
                errors.append(error)
            finally:
                for connection in connections:
                    connection.close()

        server_thread = threading.Thread(target=serve_targets, daemon=True)
        proxy = ThrottledHttpProxy(1000)
        clients = []

        def download_one():
            try:
                port = int(proxy.url.rsplit(":", 1)[1])
                with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
                    client.settimeout(4)
                    client.sendall(
                        (
                            f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\\r\\n"
                            f"Host: 127.0.0.1:{target_port}\\r\\n\\r\\n"
                        ).encode("ascii")
                    )
                    response = bytearray()
                    while b"\\r\\n\\r\\n" not in response:
                        response.extend(client.recv(1024))
                    header, _separator, initial_payload = response.partition(b"\\r\\n\\r\\n")
                    if b"200 Connection Established" not in header:
                        raise AssertionError("CONNECT tunnel was not established")
                    data = bytearray(initial_payload)
                    while len(data) < len(payload):
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        data.extend(chunk)
                    received_payloads.append(bytes(data))
            except Exception as error:
                errors.append(error)

        try:
            proxy.start()
            server_thread.start()
            started = time.monotonic()
            clients = [threading.Thread(target=download_one, daemon=True) for _ in range(2)]
            for client_thread in clients:
                client_thread.start()
            for client_thread in clients:
                client_thread.join(timeout=5)
            elapsed = time.monotonic() - started
            server_thread.join(timeout=2)
            self.assertTrue(all(not client_thread.is_alive() for client_thread in clients))
            self.assertEqual(errors, [])
            self.assertEqual(received_payloads, [payload, payload])
            # 1.6 KB over one shared 1 KB/s bucket, not two independent buckets.
            self.assertGreaterEqual(elapsed, 1.2)
        finally:
            proxy.close()
            target.close()
            if server_thread.is_alive():
                server_thread.join(timeout=1)

    def test_download_is_rate_limited(self):
        payload = b"x" * 1500
        target = socket.socket()
        target.bind(("127.0.0.1", 0))
        target.listen(1)
        target_port = target.getsockname()[1]

        def serve_target():
            connection, _ = target.accept()
            with connection:
                connection.sendall(payload)

        target_thread = threading.Thread(target=serve_target, daemon=True)
        target_thread.start()
        proxy = ThrottledHttpProxy(1000)
        try:
            proxy_port = int(proxy.start().rsplit(":", 1)[1])
            with socket.create_connection(("127.0.0.1", proxy_port), timeout=3) as client:
                client.settimeout(4)
                client.sendall(
                    (
                        f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{target_port}\r\n\r\n"
                    ).encode("ascii")
                )
                response = bytearray()
                while b"\r\n\r\n" not in response:
                    response.extend(client.recv(1024))
                header, _separator, initial_payload = response.partition(b"\r\n\r\n")
                self.assertIn(b"200 Connection Established", header)
                received = bytearray(initial_payload)
                started = time.monotonic()
                while len(received) < len(payload):
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    received.extend(chunk)
                elapsed = time.monotonic() - started
                self.assertEqual(bytes(received), payload)
                self.assertGreaterEqual(elapsed, 1.0)
        finally:
            proxy.close()
            target.close()
            target_thread.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
