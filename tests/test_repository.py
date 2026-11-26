import tempfile
import unittest
from pathlib import Path

from gitarchive.repository import (
    folder_size,
    human_size,
    parse_repository_url,
    provider_name,
    safe_path_part,
    storage_key,
)


class GitRemoteUrlTests(unittest.TestCase):
    def test_github_https_repository_url(self):
        self.assertEqual(
            parse_repository_url("https://github.com/MAKAKAIglitch/GitArchive"),
            (
                "https://github.com/MAKAKAIglitch/GitArchive.git",
                "github.com",
                "MAKAKAIglitch",
                "GitArchive",
            ),
        )

    def test_github_repository_subpage_is_normalized(self):
        self.assertEqual(
            parse_repository_url("https://github.com/owner/repo/tree/main"),
            ("https://github.com/owner/repo.git", "github.com", "owner", "repo"),
        )

    def test_github_ssh_repository_url(self):
        self.assertEqual(
            parse_repository_url("git@github.com:owner/repo.git"),
            ("git@github.com:owner/repo.git", "github.com", "owner", "repo"),
        )

    def test_gitlab_nested_group(self):
        self.assertEqual(
            parse_repository_url("https://gitlab.com/group/subgroup/project.git"),
            (
                "https://gitlab.com/group/subgroup/project.git",
                "gitlab.com",
                "group/subgroup",
                "project",
            ),
        )

    def test_gitlab_web_page_suffix_is_removed(self):
        self.assertEqual(
            parse_repository_url("https://gitlab.com/group/subgroup/project/-/tree/main"),
            (
                "https://gitlab.com/group/subgroup/project.git",
                "gitlab.com",
                "group/subgroup",
                "project",
            ),
        )

    def test_codeberg_repository(self):
        self.assertEqual(
            parse_repository_url("https://codeberg.org/owner/repo"),
            ("https://codeberg.org/owner/repo.git", "codeberg.org", "owner", "repo"),
        )

    def test_custom_https_server_and_port(self):
        self.assertEqual(
            parse_repository_url("https://git.example.net:8443/dev/team/repo.git"),
            (
                "https://git.example.net:8443/dev/team/repo.git",
                "git.example.net:8443",
                "dev/team",
                "repo",
            ),
        )

    def test_custom_ssh_url(self):
        self.assertEqual(
            parse_repository_url("ssh://git@git.example.net:2222/team/project.git"),
            (
                "ssh://git@git.example.net:2222/team/project.git",
                "git.example.net:2222",
                "team",
                "project",
            ),
        )

    def test_custom_scp_style_ssh_url(self):
        self.assertEqual(
            parse_repository_url("git@git.example.net:team/project"),
            (
                "git@git.example.net:team/project.git",
                "git.example.net",
                "team",
                "project",
            ),
        )

    def test_friendly_provider_names(self):
        self.assertEqual(provider_name("github.com"), "GitHub")
        self.assertEqual(provider_name("gitlab.com"), "GitLab")
        self.assertEqual(provider_name("codeberg.org"), "Codeberg")
        self.assertEqual(provider_name("git.internal.lan"), "Свой Git-сервер")

    def test_storage_key_is_windows_safe(self):
        self.assertEqual(storage_key("git.example.net:8443"), "git.example.net_8443")
        self.assertEqual(safe_path_part("CON"), "_CON")
        self.assertEqual(safe_path_part("project"), "project")

    def test_rejects_plain_http(self):
        with self.assertRaises(ValueError):
            parse_repository_url("http://git.example.net/group/repo.git")

    def test_rejects_missing_repository(self):
        with self.assertRaises(ValueError):
            parse_repository_url("https://github.com/owner")

    def test_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            parse_repository_url("https://github.com/../repo")

    def test_rejects_encoded_path_separator(self):
        with self.assertRaises(ValueError):
            parse_repository_url("https://github.com/owner/repo%2f..")

    def test_rejects_credentials_in_https_url(self):
        with self.assertRaises(ValueError):
            parse_repository_url("https://user:token@git.example.net/team/repo.git")

    def test_rejects_query_and_fragment(self):
        for value in (
            "https://git.example.net/team/repo.git?token=secret",
            "https://git.example.net/team/repo.git#branch",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_repository_url(value)

    def test_rejects_unsupported_git_transport(self):
        with self.assertRaises(ValueError):
            parse_repository_url("git://git.example.net/team/repo.git")


class ArchiveSizeTests(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(human_size(0), "0 Б")
        self.assertEqual(human_size(1024), "1.0 КБ")
        self.assertEqual(human_size(1024 ** 2), "1.0 МБ")

    def test_folder_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one.bin").write_bytes(b"123")
            subdir = root / "nested"
            subdir.mkdir()
            (subdir / "two.bin").write_bytes(b"4567")
            self.assertEqual(folder_size(root), 7)

    def test_missing_folder_size_is_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(folder_size(Path(temporary) / "missing"), 0)


if __name__ == "__main__":
    unittest.main()
