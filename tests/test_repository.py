import tempfile
import unittest
from pathlib import Path

from gitarchive.i18n import tr
from gitarchive.repository import archive_path, folder_size, human_size, parse_branch_refs, parse_git_url


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
                tr("Свой Git-сервер"),
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
                tr("Свой Git-сервер"),
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
                tr("Свой Git-сервер"),
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
                tr("Свой Git-сервер"),
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

    def test_archive_paths_support_zip_and_7z_for_github(self):
        root = Path("archives")
        self.assertEqual(
            archive_path(root, "github.com", "owner", "repo", "zip"),
            root / "owner" / "repo.zip",
        )
        self.assertEqual(
            archive_path(root, "github.com", "owner", "repo", "7z"),
            root / "owner" / "repo.7z",
        )

    def test_archive_paths_support_compression_for_custom_hosts(self):
        self.assertEqual(
            archive_path(Path("archives"), "git.example.net", "team/subteam", "repo", "zip"),
            Path("archives") / "_custom" / "git.example.net" / "team" / "subteam" / "repo.zip",
        )

    def test_archive_paths_reject_unknown_formats(self):
        with self.assertRaises(ValueError):
            archive_path(Path("archives"), "github.com", "owner", "repo", "rar")

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

    def test_rejects_ssh_option_injection_username(self):
        with self.assertRaises(ValueError):
            parse_git_url("ssh://-oProxyCommand=bad@git.example.net/team/repo.git")

    def test_rejects_query_fragment_and_unsupported_transport(self):
        values = (
            "https://git.example.net/team/repo.git?token=secret",
            "https://git.example.net/team/repo.git#branch",
            "git://git.example.net/team/repo.git",
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_git_url(value)


class BranchRefTests(unittest.TestCase):
    def test_parse_local_and_remote_branches(self):
        output = "\n".join((
            "refs/heads/main",
            "refs/heads/feature/new-ui",
            "refs/heads/develop",
            "refs/remotes/upstream/feature/new-ui",
            "refs/remotes/origin/HEAD",
            "refs/tags/v1.0.0",
            "",
        ))
        self.assertEqual(
            parse_branch_refs(output),
            [
                ("refs/heads/develop", "develop"),
                ("refs/heads/feature/new-ui", "feature/new-ui"),
                ("refs/heads/main", "main"),
                ("refs/remotes/upstream/feature/new-ui", "upstream/feature/new-ui"),
            ],
        )

    def test_empty_branch_output_returns_empty_list(self):
        self.assertEqual(parse_branch_refs(""), [])


class ArchiveSizeTests(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(human_size(0), f"0 {tr('Б')}")
        self.assertEqual(human_size(1024), f"1.0 {tr('КБ')}")
        self.assertEqual(human_size(1024 ** 2), f"1.0 {tr('МБ')}")

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

    def test_folder_size_counts_compressed_archive_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "repo.zip"
            archive.write_bytes(b"compressed-data")
            self.assertEqual(folder_size(archive), len(b"compressed-data"))


if __name__ == "__main__":
    unittest.main()
