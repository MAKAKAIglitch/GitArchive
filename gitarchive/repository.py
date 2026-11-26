"""Git repository URL parsing and local archive path helpers."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .i18n import tr


_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9_.~+-]+")
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
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
        raise ValueError(tr("Вставь HTTPS- или SSH-ссылку на Git-репозиторий."))

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
            raise ValueError(tr("Некорректный адрес Git-сервера.")) from error

        if parsed.scheme not in {"https", "ssh"}:
            raise ValueError(tr("Поддерживаются только HTTPS и SSH. Обычный HTTP не используется."))
        if not host or not parsed.path:
            raise ValueError(tr("В ссылке должны быть адрес сервера и путь к репозиторию."))
        if parsed.password is not None or (parsed.scheme == "https" and parsed.username is not None):
            raise ValueError(tr("Не вставляй пароль или токен прямо в URL. Используй Git Credential Manager или SSH-ключ."))
        if parsed.scheme == "ssh" and parsed.username is not None and not re.fullmatch(r"[A-Za-z0-9_.-]+", parsed.username):
            raise ValueError(tr("В SSH-ссылке недопустимое имя пользователя."))
        if parsed.query or parsed.fragment:
            raise ValueError(tr("Удали параметры после ? или # и вставь чистую ссылку на репозиторий."))

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
        raise ValueError(tr("В ссылке не найден путь к репозиторию."))

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
        raise ValueError(tr("Не удалось распознать namespace/repository в ссылке."))

    parts[-1] = re.sub(r"\.git$", "", parts[-1], flags=re.IGNORECASE)
    if not all(_SAFE_COMPONENT.fullmatch(part or "") and part not in {".", ".."} for part in parts):
        raise ValueError(tr("В пути репозитория есть недопустимые символы."))

    namespace = "/".join(parts[:-1])
    name = parts[-1]
    if not name:
        raise ValueError(tr("Не удалось определить имя репозитория."))

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
        provider = tr("Свой Git-сервер")

    host_key = host + (f":{port}" if port else "")
    return normalized, namespace, name, host_key, provider



def parse_repository_url(value: str) -> tuple[str, str, str, str]:
    """Return normalized clone URL, host key, namespace and repository name."""
    url, namespace, name, host, _provider = parse_git_url(value)
    return url, host, namespace, name


def provider_name(host: str) -> str:
    """Return a friendly provider label without requiring provider-specific APIs."""
    hostname = host.split(":", 1)[0].strip("[]").lower()
    if hostname in {"github.com", "www.github.com"}:
        return "GitHub"
    if hostname == "gitlab.com":
        return "GitLab"
    if hostname == "codeberg.org":
        return "Codeberg"
    if hostname == "bitbucket.org":
        return "Bitbucket"
    if hostname == "git.sr.ht":
        return "SourceHut"
    return tr("Свой Git-сервер")


def parse_branch_refs(output: str) -> list[tuple[str, str]]:
    """Parse branch refs into (full_ref, display_name) pairs for the history filter."""
    branches: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_line in output.splitlines():
        ref = raw_line.strip()
        if ref.startswith("refs/heads/"):
            label = ref[len("refs/heads/"):]
        elif ref.startswith("refs/remotes/"):
            label = ref[len("refs/remotes/"):]
            # Symbolic remote HEAD entries are not selectable branches.
            if label.endswith("/HEAD") or label == "HEAD":
                continue
        else:
            continue

        parts = label.split("/")
        if not label or any(not part or part in {".", ".."} for part in parts):
            continue
        if ref in seen:
            continue
        seen.add(ref)
        branches.append((ref, label))

    return sorted(branches, key=lambda item: (item[1].casefold(), item[1], item[0]))


def safe_path_part(value: str) -> str:
    """Validate one user-controlled namespace or repository path component."""
    if not _SAFE_COMPONENT.fullmatch(value or "") or value in {".", ".."}:
        raise ValueError(tr("В пути репозитория есть недопустимые символы."))
    return value


def storage_key(host: str) -> str:
    """Convert a host (including an optional port) into one safe folder name."""
    key = re.sub(r"[^A-Za-z0-9.-]+", "_", host.lower()).strip("._")
    if not key:
        raise ValueError(tr("Некорректное имя Git-сервера."))
    return key

def parse_github_url(value: str) -> tuple[str, str, str]:
    """Backward-compatible GitHub-only parser for existing integrations."""
    url, namespace, name, host, _provider = parse_git_url(value)
    if host != "github.com":
        raise ValueError(tr("Нужна ссылка именно на github.com."))
    if "/" in namespace:
        raise ValueError(tr("Для GitHub укажи owner/repository."))
    return url, namespace, name


def _safe_path_component(value: str) -> str:
    """Avoid Windows reserved device names and trailing-dot aliases."""
    value = value.rstrip(".") or "_"
    device_stem = value.split(".", 1)[0].upper()
    return f"_{value}" if device_stem in _WINDOWS_RESERVED_NAMES else value


def archive_path(
    storage_root: Path,
    host: str,
    namespace: str,
    name: str,
    storage_format: str = "folder",
) -> Path:
    """Build a collision-safe path for a bare mirror or compressed mirror."""
    extensions = {"folder": ".git", "directory": ".git", "git": ".git", "zip": ".zip", "7z": ".7z"}
    extension = extensions.get(str(storage_format).lower())
    if extension is None:
        raise ValueError(tr("Неизвестный формат хранения. Используй folder, zip или 7z."))
    components = [_safe_path_component(part) for part in namespace.split("/")]
    safe_name = _safe_path_component(name)
    if host == "github.com":
        return storage_root.joinpath(*components, f"{safe_name}{extension}")

    safe_host = re.sub(r"[^A-Za-z0-9.-]+", "_", host.lower()).strip("._")
    if not safe_host:
        raise ValueError(tr("Некорректное имя Git-сервера."))
    safe_host = _safe_path_component(safe_host)
    return storage_root / "_custom" / safe_host / Path(*components) / f"{safe_name}{extension}"


def find_7zip() -> str | None:
    """Find an installed 7-Zip command-line executable without downloading or bundling it."""
    names = ("7z.exe", "7zz.exe", "7z", "7zz") if os.name == "nt" else ("7zz", "7z")
    for name in names:
        executable = shutil.which(name)
        if executable and Path(executable).is_file():
            return str(Path(executable).resolve())

    if os.name == "nt":
        roots = []
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            value = os.environ.get(variable)
            if value:
                roots.append(Path(value))
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            roots.append(Path(local_app_data) / "Programs")
        for root in roots:
            candidate = root / "7-Zip" / "7z.exe"
            if candidate.is_file():
                return str(candidate.resolve())
    return None


def folder_size(path: Path) -> int:
    """Return the size of an archive file or the total size of a directory tree."""
    if str(path) in {"", "."}:
        return 0
    try:
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    if not path.is_dir():
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
    for unit in (tr("Б"), tr("КБ"), tr("МБ"), tr("ГБ"), tr("ТБ")):
        if value < 1024 or unit == tr("ТБ"):
            return f"{int(value)} {unit}" if unit == tr("Б") else f"{value:.1f} {unit}"
        value /= 1024
    return tr(f"{value:.1f} ТБ")
