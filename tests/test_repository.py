import tempfile
import unittest
from pathlib import Path

from gitarchive.repository import folder_size, human_size, parse_github_url


class GitHubUrlTests(unittest.TestCase):
    def test_https_repository_url(self):
        self.assertEqual(
            parse_github_url("https://github.com/MAKAKAIglitch/GitArchive"),
            (
                "https://github.com/MAKAKAIglitch/GitArchive.git",
                "MAKAKAIglitch",
                "GitArchive",
            ),
        )

    def test_repository_subpage_is_normalized(self):
        self.assertEqual(
            parse_github_url("https://github.com/owner/repo/tree/main"),
            ("https://github.com/owner/repo.git", "owner", "repo"),
        )

    def test_ssh_repository_url(self):
        self.assertEqual(
            parse_github_url("git@github.com:owner/repo.git"),
            ("git@github.com:owner/repo.git", "owner", "repo"),
        )

    def test_www_host_is_normalized(self):
        self.assertEqual(
            parse_github_url("https://www.github.com/owner/repo"),
            ("https://github.com/owner/repo.git", "owner", "repo"),
        )

    def test_rejects_external_host(self):
        with self.assertRaises(ValueError):
            parse_github_url("https://example.com/owner/repo")

    def test_rejects_missing_repository(self):
        with self.assertRaises(ValueError):
            parse_github_url("https://github.com/owner")

    def test_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            parse_github_url("https://github.com/../repo")

    def test_rejects_invalid_owner_or_name(self):
        with self.assertRaises(ValueError):
            parse_github_url("https://github.com/owner/repo%2f..")


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
