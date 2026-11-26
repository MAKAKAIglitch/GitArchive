import unittest

from gitarchive.app import parse_sync_progress, proxy_url


class ProxySettingsTests(unittest.TestCase):
    def test_http_proxy(self):
        self.assertEqual(
            proxy_url({"proxy_enabled": True, "proxy_type": "HTTP", "proxy_host": "127.0.0.1", "proxy_port": 8080}),
            "http://127.0.0.1:8080",
        )

    def test_https_proxy(self):
        self.assertEqual(
            proxy_url({"proxy_enabled": True, "proxy_type": "HTTPS", "proxy_host": "proxy.example.net", "proxy_port": 8443}),
            "https://proxy.example.net:8443",
        )

    def test_socks4_uses_proxy_dns(self):
        self.assertEqual(
            proxy_url({"proxy_enabled": True, "proxy_type": "SOCKS4", "proxy_host": "127.0.0.1", "proxy_port": 9050}),
            "socks4a://127.0.0.1:9050",
        )

    def test_socks5_uses_proxy_dns(self):
        self.assertEqual(
            proxy_url({"proxy_enabled": True, "proxy_type": "SOCKS5", "proxy_host": "localhost", "proxy_port": 1080}),
            "socks5h://localhost:1080",
        )

    def test_disabled_proxy_returns_none(self):
        self.assertIsNone(proxy_url({"proxy_enabled": False}))

    def test_rejects_invalid_host(self):
        with self.assertRaises(ValueError):
            proxy_url({"proxy_enabled": True, "proxy_type": "SOCKS5", "proxy_host": "bad/host", "proxy_port": 1080})

    def test_rejects_invalid_port(self):
        with self.assertRaises(ValueError):
            proxy_url({"proxy_enabled": True, "proxy_type": "HTTP", "proxy_host": "localhost", "proxy_port": 70000})




class SyncProgressTests(unittest.TestCase):
    def test_parses_git_receiving_objects_progress(self):
        self.assertEqual(
            parse_sync_progress("Receiving objects:  42% (21/50), 1.2 MiB"),
            (42, "Receiving objects"),
        )

    def test_parses_git_resolving_deltas_progress(self):
        self.assertEqual(
            parse_sync_progress("Resolving deltas: 100% (8/8)"),
            (100, "Resolving deltas"),
        )

    def test_parses_seven_zip_percentage_output(self):
        self.assertEqual(
            parse_sync_progress("  67%\r"),
            (67, "Архивация / распаковка"),
        )

    def test_ignores_output_without_progress(self):
        self.assertIsNone(parse_sync_progress("INFO: Fetching remote refs without pruning."))


if __name__ == "__main__":
    unittest.main()
