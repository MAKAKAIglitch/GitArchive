import unittest

from gitarchive.app import background_process_environment, parse_sync_progress, proxy_url
from gitarchive.i18n import tr
from gitarchive.throttle import normalize_download_limit


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


class DownloadLimitTests(unittest.TestCase):
    def test_download_limit_defaults_to_three_megabytes_per_second(self):
        self.assertEqual(normalize_download_limit(None), 3)
        self.assertEqual(normalize_download_limit("invalid"), 3)

    def test_zero_means_unlimited(self):
        self.assertEqual(normalize_download_limit(0), 0)

    def test_download_limit_is_clamped(self):
        self.assertEqual(normalize_download_limit(-5), 0)
        self.assertEqual(normalize_download_limit(150), 100)

    def test_local_limiter_proxy_is_passed_to_the_rust_engine(self):
        proxy = "http://127.0.0.1:45678"
        environment = background_process_environment({"proxy_enabled": False}, proxy)
        self.assertEqual(environment.value("GITARCHIVE_HTTP_PROXY"), proxy)
        for name in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
            "NO_PROXY", "no_proxy",
        ):
            self.assertFalse(environment.contains(name), name)


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
            (67, tr("Архивация / распаковка")),
        )

    def test_ignores_output_without_progress(self):
        self.assertIsNone(parse_sync_progress("INFO: Fetching remote refs without pruning."))


if __name__ == "__main__":
    unittest.main()
