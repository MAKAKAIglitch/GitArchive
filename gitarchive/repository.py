"""GitHub URL parsing and local archive utility helpers."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit


def parse_github_url(value: str) -> tuple[str, str, str]:
    """Normalize a GitHub repository URL to a clone URL, owner and name."""
    value = value.strip()
    if value.startswith("git@github.com:"):
        parts = value.split(":", 1)[1].strip("/").split("/")
        url_type = "ssh"
    else:
        parsed = urlsplit(value if "://" in value else "https://" + value)
        if parsed.hostname not in {"github.com", "www.github.com"}:
            raise ValueError("Нужна ссылка именно на github.com.")
        parts = parsed.path.strip("/").split("/")
        url_type = "https"

    if len(parts) < 2:
        raise ValueError("Формат ссылки: https://github.com/owner/repository")

    owner, name = (re.sub(r"\.git$", "", part) for part in parts[:2])
    if (
        owner in {".", ".."}
        or name in {".", ".."}
        or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part or "") for part in (owner, name))
    ):
        raise ValueError("Не удалось распознать owner/repository.")

    url = (
        f"git@github.com:{owner}/{name}.git"
        if url_type == "ssh"
        else f"https://github.com/{owner}/{name}.git"
    )
    return url, owner, name


def folder_size(path: Path) -> int:
    """Return the total size of files under a directory without following symlinks."""
    if str(path) in {"", "."} or not path.is_dir():
        return 0

    total = 0
    for root, _, files in os.walk(path):
        for filename in files:
            try:
                total += (Path(root) / filename).stat().st_size
            except OSError:
                pass
    return total


def human_size(size: int) -> str:
    """Format a byte count for the UI."""
    value = float(max(size, 0))
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{int(value)} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"
