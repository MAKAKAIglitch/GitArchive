"""Git repository URL parsing and local archive path helpers."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit


_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9_.-]+")
_SCP_URL = re.compile(r"^(?P<user>[A-Za-z0-9_.-]+)@(?P<host>[A-Za-z0-9.-]+):(?P<path>.+)$")
_WEB_ROUTES = {
    "archive", "blob", "browse", "commit", "commits", "compare",
    "merge_requests", "pull", "pulls", "raw", "src", "tree",
}


def parse_git_url(value: str) -> tuple[str, str, str, str, str]:
    """Return normalized clone URL, namespace, repo name, host and provider label.

    HTTPS and SSH remotes are supported. SCP-style SSH URLs such as
    git@example.com:group/project.git are supported as well. HTTP, URL-embedded
    passwords/tokens, query strings and fragments are rejected so secrets are
    not accidentally stored in GitArchive's local config.
    """
    value = value.strip()
    if not value:
        raise ValueError("Вставь HTTPS- или SSH-ссылку на Git-репозиторий.")

    scp = _SCP_URL.fullmatch(value) if "://" not in value else None
    if scp:
        host = scp.group("host").lower()
        user = scp.group("user")
        path = scp.group("path")
        port = None
        transport = "scp"
        authority = f"{user}@{host}"
    else:
        candidate = value if "://" in value else "https://" + value
        try:
            parsed = urlsplit(candidate)
            host = (parsed.hostname or "").lower()
            port = parsed.port
        except ValueError as error:
            raise ValueError("Некорректный адрес Git-сервера.") from error

        if parsed.scheme not in {"https", "ssh"}:
            raise ValueError("Поддерживаются только HTTPS и SSH. Обычный HTTP не используется.")
        if not host or not parsed.path:
            raise ValueError("В ссылке должны быть адрес сервера и путь к репозиторию.")
        if parsed.password is not None or (parsed.scheme == "https" and parsed.username is not None):
            raise ValueError("Не вставляй пароль или токен прямо в URL. Используй Git Credential Manager или SSH-ключ.")
        if parsed.query or parsed.fragment:
            raise ValueError("Удали параметры после ? или # и вставь чистую ссылку на репозиторий.")

        transport = parsed.scheme
        path = parsed.path
        authority = parsed.netloc
        if host == "www.github.com":
            host = "github.com"
            authority = "github.com" + (f":{port}" if port else "")
        elif port:
            authority = f"{authority}"

    raw_parts = [unquote(part) for part in path.strip("/").split("/") if part]
    if not raw_parts:
        raise ValueError("В ссылке не найден путь к репозиторию.")

    # GitHub repository pages have many suffix routes, but the repository is
    # always identified by the first two path components.
    if host == "github.com":
        parts = raw_parts[:2]
    # GitLab's /-/ separator marks the end of the project path, including
    # projects nested under groups and subgroups on self-managed instances.
    elif "-" in raw_parts and raw_parts.index("-") >= 2:
        parts = raw_parts[:raw_parts.index("-")]
    elif host in {"codeberg.org", "bitbucket.org", "git.sr.ht"}:
        parts = raw_parts[:2]
    else:
        # Generic hosts can use nested namespaces. Strip common browser routes
        # when present; otherwise treat every component as namespace/project.
        route_index = next(
            (index for index, part in enumerate(raw_parts)
             if index >= 2 and part.lower() in _WEB_ROUTES),
            None,
        )
        parts = raw_parts[:route_index] if route_index is not None else raw_parts

    if len(parts) < 2:
        raise ValueError("Не удалось распознать namespace/repository в ссылке.")

    parts[-1] = re.sub(r"\.git$", "", parts[-1], flags=re.IGNORECASE)
    if not all(_SAFE_COMPONENT.fullmatch(part or "") and part not in {".", ".."} for part in parts):
        raise ValueError("В пути репозитория есть недопустимые символы.")

    namespace = "/".join(parts[:-1])
    name = parts[-1]
    if not name:
        raise ValueError("Не удалось определить имя репозитория.")

    if transport == "scp":
        normalized = f"{authority}:{namespace}/{name}.git"
    else:
        normalized = f"{transport}://{authority}/{namespace}/{name}.git"

    if host == "github.com":
        provider = "GitHub"
    elif host == "gitlab.com":
        provider = "GitLab"
    elif host == "codeberg.org":
        provider = "Codeberg"
    elif host == "bitbucket.org":
        provider = "Bitbucket"
    elif host == "git.sr.ht":
        provider = "SourceHut"
    else:
        provider = "Свой Git-сервер"

    host_key = host + (f":{port}" if port else "")
    return normalized, namespace, name, host_key, provider


def parse_github_url(value: str) -> tuple[str, str, str]:
    """Backward-compatible GitHub-only parser for existing integrations."""
    url, namespace, name, host, _provider = parse_git_url(value)
    if host != "github.com":
        raise ValueError("Нужна ссылка именно на github.com.")
    if "/" in namespace:
        raise ValueError("Для GitHub укажи owner/repository.")
    return url, namespace, name


def archive_path(storage_root: Path, host: str, namespace: str, name: str) -> Path:
    """Build a collision-safe archive path; keep legacy GitHub paths unchanged."""
    components = namespace.split("/")
    if host == "github.com":
        return storage_root.joinpath(*components, f"{name}.git")

    safe_host = re.sub(r"[^A-Za-z0-9.-]+", "_", host.lower()).strip("._")
    if not safe_host:
        raise ValueError("Некорректное имя Git-сервера.")
    return storage_root / "_custom" / safe_host / Path(*components) / f"{name}.git"


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
