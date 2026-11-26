import tempfile
import unittest
from pathlib import Path

from gitarchive.repository import archive_path, folder_size, human_size, parse_git_url


class GitRemoteUrlTests(unittest.TestCase):
    def test_github_https_repository_url(self):
        self.assertEqual(
            parse_git_url("https://github.com/MAKAKAIglitch/GitArchive"),
            (
                "https://github.com/MAKAKAIglitch/GitArchive.git",
                "MAKAKAIglitch",
                "GitArchive",
                "github.com",
                "GitHub",
            ),
        )

    def test_github_repository_subpage_is_normalized(self):
        self.assertEqual(
            parse_git_url("https://github.com/owner/repo/tree/main"),
            ("https://github.com/owner/repo.git", "owner", "repo", "github.com", "GitHub"),
        )

    def test_www_github_host_is_normalized(self):
        self.assertEqual(
            parse_git_url("https://www.github.com/owner/repo"),
            ("https://github.com/owner/repo.git", "owner", "repo", "github.com", "GitHub"),
        )

    def test_github_ssh_repository_url(self):
        self.assertEqual(
            parse_git_url("git@github.com:owner/repo.git"),
            ("git@github.com:owner/repo.git", "owner", "repo", "github.com", "GitHub"),
        )

    def test_gitlab_nested_group(self):
        self.assertEqual(
            parse_git_url("https://gitlab.com/group/subgroup/project.git"),
            (
                "https://gitlab.com/group/subgroup/project.git",
                "group/subgroup",
                "project",
                "gitlab.com",
                "GitLab",
            ),
        )

    def test_gitlab_web_page_suffix_is_removed(self):
        self.assertEqual(
            parse_git_url("https://gitlab.com/group/subgroup/project/-/tree/main"),
            (
                "https://gitlab.com/group/subgroup/project.git",
                "group/subgroup",
                "project",
                "gitlab.com",
                "GitLab",
            ),
        )

    def test_codeberg_repository(self):
        self.assertEqual(
            parse_git_url("https://codeberg.org/owner/repo"),
            ("https://codeberg.org/owner/repo.git", "owner", "repo", "codeberg.org", "Codeberg"),
        )

    def test_bitbucket_repository(self):
        self.assertEqual(
            parse_git_url("https://bitbucket.org/workspace/repo.git"),
            (
                "https://bitbucket.org/workspace/repo.git",
                "workspace",
                "repo",
                "bitbucket.org",
                "Bitbucket",
            ),
        )

    def test_sourcehut_namespace(self):
        self.assertEqual(
            parse_git_url("https://git.sr.ht/~alice/project"),
            (
                "https://git.sr.ht/~alice/project.git",
                "~alice",
                "project",
                "git.sr.ht",
                "SourceHut",
            ),
        )

    def test_custom_https_server_and_port(self):
        self.assertEqual(
            parse_git_url("https://git.example.net:8443/dev/team/repo.git"),
            (
                "https://git.example.net:8443/dev/team/repo.git",
                "dev/team",
                "repo",
                "git.example.net:8443",
                "Свой Git-сервер",
            ),
        )

    def test_custom_host_browser_route(self):
        self.assertEqual(
            parse_git_url("https://git.internal.lan/team/project/src/branch/main"),
            (
                "https://git.internal.lan/team/project.git",
                "team",
                "project",
                "git.internal.lan",
                "Свой Git-сервер",
            ),
        )

    def test_custom_ssh_url(self):
        self.assertEqual(
            parse_git_url("ssh://git@git.example.net:2222/team/project.git"),
            (
                "ssh://git@git.example.net:2222/team/project.git",
                "team",
                "project",
                "git.example.net:2222",
                "Свой Git-сервер",
            ),
        )

    def test_custom_scp_style_ssh_url(self):
        self.assertEqual(
            parse_git_url("git@git.example.net:team/project"),
            (
                "git@git.example.net:team/project.git",
                "team",
                "project",
                "git.example.net",
                "Свой Git-сервер",
            ),
        )

    def test_archive_paths_separate_custom_hosts_and_preserve_github_layout(self):
        root = Path("archives")
        self.assertEqual(
            archive_path(root, "github.com", "owner", "repo"),
            root / "owner" / "repo.git",
        )
        self.assertEqual(
            archive_path(root, "git.example.net:8443", "dev/team", "repo"),
            root / "_custom" / "git.example.net_8443" / "dev" / "team" / "repo.git",
        )

    def test_archive_paths_handle_windows_reserved_names(self):
        self.assertEqual(
            archive_path(Path("archives"), "gitlab.com", "CON/team", "PRN"),
            Path("archives") / "_custom" / "gitlab.com" / "_CON" / "team" / "_PRN.git",
        )

    def test_rejects_plain_http(self):
        with self.assertRaises(ValueError):
            parse_git_url("http://git.example.net/group/repo.git")

    def test_rejects_missing_repository(self):
        with self.assertRaises(ValueError):
            parse_git_url("https://github.com/owner")

    def test_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            parse_git_url("https://github.com/../repo")

    def test_rejects_encoded_path_separator(self):
        with self.assertRaises(ValueError):
            parse_git_url("https://github.com/owner/repo%2f..")

    def test_rejects_credentials_in_https_url(self):
        with self.assertRaises(ValueError):
            parse_git_url("https://user:token@git.example.net/team/repo.git")

    def test_rejects_ssh_password(self):
        with self.assertRaises(ValueError):
            parse_git_url("ssh://git:password@git.example.net/team/repo.git")

    def test_rejects_query_fragment_and_unsupported_transport(self):
        values = (
            "https://git.example.net/team/repo.git?token=secret",
            "https://git.example.net/team/repo.git#branch",
            "git://git.example.net/team/repo.git",
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_git_url(value)


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
