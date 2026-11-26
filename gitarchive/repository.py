"""Generic Git remote parsing and local archive utility helpers."""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit


_SEGMENT = re.compile(r"[A-Za-z0-9_.~+-]+\Z")
_SCP_URL = re.compile(
    r"(?P<user>[A-Za-z0-9._-]+)@(?P<host>\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+):(?P<path>.+)\Z"
)
_RESERVED_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _normalize_host(hostname: str, port: int | None) -> str:
    """Validate and normalize a DNS name or IP address for display/storage."""
    hostname = hostname.strip().rstrip(".")
    if not hostname:
        raise ValueError("В ссылке не указан адрес Git-сервера.")

    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
        normalized = f"[{address.compressed}]" if address.version == 6 else address.compressed
    except ValueError:
        try:
            normalized = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as error:
            raise ValueError("Некорректное имя Git-сервера.") from error
        if len(normalized) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in normalized.split(".")
        ):
            raise ValueError("Некорректное имя Git-сервера.")

    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Порт Git-сервера должен быть от 1 до 65535.")
    return f"{normalized}:{port}" if port is not None else normalized


def _repository_parts(path: str, host: str) -> tuple[list[str], str]:
    """Extract namespace and project from a repository or common web-page URL."""
    path = path.strip("/")
    if not path:
        raise ValueError("В ссылке не указан путь репозитория.")

    # GitLab repository pages append /-/tree, /-/issues, etc. after the project.
    path = path.split("/-/", 1)[0]
    raw_parts = path.split("/")
    parts = [unquote(part) for part in raw_parts]

    # On single-owner hosting sites, browser links can include /tree/main,
    # /src/branch/main, /issues, etc. Their repository identity is the first
    # two path segments. GitLab and custom hosts keep nested namespaces.
    single_namespace_hosts = {
        "github.com", "www.github.com", "codeberg.org", "gitea.com",
        "bitbucket.org", "git.sr.ht", "sr.ht",
    }
    if host.split(":", 1)[0].strip("[]") in single_namespace_hosts and len(parts) > 2:
        parts = parts[:2]

    if len(parts) < 2:
        raise ValueError(
            "Не удалось распознать репозиторий. Нужна ссылка вида "
            "https://git.example.net/group/project.git."
        )

    if parts[-1].lower().endswith(".git"):
        parts[-1] = parts[-1][:-4]

    if any(
        part in {"", ".", ".."}
        or not _SEGMENT.fullmatch(part)
        or "/" in part
        or "\\" in part
        for part in parts
    ):
        raise ValueError("В адресе репозитория есть недопустимые символы.")

    namespace = "/".join(parts[:-1])
    name = parts[-1]
    if not namespace or not name:
        raise ValueError("Не удалось распознать группу и имя репозитория.")
    return parts, namespace


def parse_repository_url(value: str) -> tuple[str, str, str, str]:
    """Normalize a Git repository URL to (clone URL, host, namespace, name).

    HTTPS, ssh:// and standard scp-like SSH clone URLs are supported.
    Arbitrary Git hosting domains are accepted; credentials embedded in URLs,
    plain HTTP, local paths, query strings and unsupported Git transports are
    rejected. Credentials should be managed by Git Credential Manager or SSH.
    """
    value = value.strip()
    if not value or any(character.isspace() for character in value):
        raise ValueError("Ссылка на репозиторий пуста или содержит пробелы.")

    scp_match = _SCP_URL.fullmatch(value)
    if scp_match:
        user = scp_match.group("user")
        host_text = scp_match.group("host")
        path = scp_match.group("path")
        host = _normalize_host(host_text, None)
        parts, namespace = _repository_parts(path, host)
        name = parts[-1]
        clone_path = "/".join(parts)
        clone_url = f"{user}@{host}:{clone_path}.git"
        return clone_url, host, namespace, name

    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme not in {"https", "ssh"}:
        raise ValueError(
            "Поддерживаются HTTPS и SSH-ссылки. Вставь clone URL Git-репозитория."
        )
    if not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Ссылка должна содержать адрес сервера и путь репозитория без query/fragment.")
    if parsed.password is not None or (scheme == "https" and parsed.username is not None):
        raise ValueError(
            "Не добавляй пароль или токен прямо в ссылку. Используй Git Credential Manager или SSH-ключ."
        )

    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("В ссылке указан некорректный порт.") from error

    host = _normalize_host(parsed.hostname or "", port)
    parts, namespace = _repository_parts(parsed.path, host)
    name = parts[-1]
    clone_path = "/" + "/".join(parts) + ".git"

    if scheme == "https":
        clone_url = urlunsplit(("https", host, clone_path, "", ""))
    else:
        username = parsed.username
        auth = f"{username}@" if username else ""
        clone_url = urlunsplit(("ssh", auth + host, clone_path, "", ""))

    return clone_url, host, namespace, name


def provider_name(host: str) -> str:
    """Return a friendly label for common hosts, or Custom Git server."""
    hostname = host.split(":", 1)[0].strip("[]").lower()
    providers = {
        "github.com": "GitHub",
        "gitlab.com": "GitLab",
        "codeberg.org": "Codeberg",
        "bitbucket.org": "Bitbucket",
        "gitea.com": "Gitea",
        "git.sr.ht": "SourceHut",
        "sr.ht": "SourceHut",
    }
    return providers.get(hostname, "Свой Git-сервер")


def storage_key(host: str) -> str:
    """Make a safe, deterministic directory name for a host (including port)."""
    value = re.sub(r"[^A-Za-z0-9._-]", "_", host).strip("._")
    return value or "custom-git"


def safe_path_part(value: str) -> str:
    """Avoid Windows device names while preserving valid Git path components."""
    if value.upper() in _RESERVED_WINDOWS_NAMES:
        return f"_{value}"
    return value


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
