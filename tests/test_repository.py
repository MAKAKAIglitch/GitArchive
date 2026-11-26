import tempfile
import unittest
from pathlib import Path

from gitarchive.repository import archive_path, folder_size, human_size, parse_git_url, parse_github_url


class GitRemoteUrlTests(unittest.TestCase):
    def test_github_https_url_and_legacy_parser(self):
        expected = ("https://github.com/MAKAKAIglitch/GitArchive.git", "MAKAKAIglitch", "GitArchive")
        parsed = parse_git_url("https://github.com/MAKAKAIglitch/GitArchive")
        self.assertEqual(parsed[:3], expected)
        self.assertEqual(parse_github_url("https://github.com/MAKAKAIglitch/GitArchive"), expected)
        self.assertEqual(parsed[3:], ("github.com", "GitHub"))

    def test_github_repository_subpage_is_normalized(self):
        self.assertEqual(
            parse_git_url("https://github.com/owner/repo/tree/main")[:3],
            ("https://github.com/owner/repo.git", "owner", "repo"),
        )

    def test_github_scp_ssh_url(self):
        self.assertEqual(
            parse_git_url("git@github.com:owner/repo.git")[:3],
            ("git@github.com:owner/repo.git", "owner", "repo"),
        )

    def test_gitlab_nested_group_and_project_page(self):
        self.assertEqual(
            parse_git_url("https://gitlab.com/group/subgroup/project/-/tree/main")[:3],
            ("https://gitlab.com/group/subgroup/project.git", "group/subgroup", "project"),
        )

    def test_gitlab_project_root(self):
        self.assertEqual(
            parse_git_url("https://gitlab.com/group/subgroup/project")[:3],
            ("https://gitlab.com/group/subgroup/project.git", "group/subgroup", "project"),
        )

    def test_codeberg_repository_subpage(self):
        self.assertEqual(
            parse_git_url("https://codeberg.org/owner/repo/src/branch/main")[:3],
            ("https://codeberg.org/owner/repo.git", "owner", "repo"),
        )
        self.assertEqual(parse_git_url("https://codeberg.org/owner/repo")[4], "Codeberg")

    def test_custom_https_server_with_nested_namespace(self):
        result = parse_git_url("https://git.example.net/team/subteam/project.git")
        self.assertEqual(result[:3], ("https://git.example.net/team/subteam/project.git", "team/subteam", "project"))
        self.assertEqual(result[3:], ("git.example.net", "Свой Git-сервер"))

    def test_custom_ssh_server_with_port(self):
        result = parse_git_url("ssh://git@git.example.net:2222/team/project")
        self.assertEqual(result[:3], ("ssh://git@git.example.net:2222/team/project.git", "team", "project"))
        self.assertEqual(result[3], "git.example.net:2222")

    def test_custom_scp_ssh_server(self):
        self.assertEqual(
            parse_git_url("git@git.example.net:team/subteam/project.git")[:3],
            ("git@git.example.net:team/subteam/project.git", "team/subteam", "project"),
        )

    def test_rejects_insecure_http(self):
        with self.assertRaises(ValueError):
            parse_git_url("http://git.example.net/team/project")

    def test_rejects_embedded_https_credentials(self):
        with self.assertRaises(ValueError):
            parse_git_url("https://user:token@git.example.net/team/project")

    def test_rejects_query_and_fragment(self):
        with self.assertRaises(ValueError):
            parse_git_url("https://git.example.net/team/project?token=secret")

    def test_rejects_missing_repository(self):
        with self.assertRaises(ValueError):
            parse_git_url("https://gitlab.com/group")

    def test_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            parse_git_url("https://git.example.net/team/../project")

    def test_rejects_encoded_path_traversal(self):
        with self.assertRaises(ValueError):
            parse_git_url("https://git.example.net/team/%2e%2e/project")

    def test_rejects_invalid_repo_path_separator(self):
        with self.assertRaises(ValueError):
            parse_git_url("https://git.example.net/team/project%2frepo")

    def test_archive_paths_keep_github_layout_and_isolate_hosts(self):
        root = Path("C:/GitArchive")
        self.assertEqual(archive_path(root, "github.com", "owner", "repo"), root / "owner" / "repo.git")
        self.assertEqual(
            archive_path(root, "gitlab.com", "group/subgroup", "repo"),
            root / "_custom" / "gitlab.com" / "group" / "subgroup" / "repo.git",
        )


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
