# GitArchive

**GitArchive** is a local-first desktop application for keeping persistent Git mirrors of remote repositories. The desktop interface is built with PySide6 and follows the visual language of Windows 11 / WinUI 3; the archive engine is written in Rust.

> Current status: early MVP foundation. GitArchive archives Git repository data from GitHub, GitLab, Codeberg, Bitbucket, Gitea, Forgejo, SourceHut, and custom/self-hosted Git servers through standard HTTPS or SSH clone URLs.

## Features

- Add Git repository clone URLs using HTTPS or SSH.
- Supports common hosting services and arbitrary custom Git server hostnames; no provider-specific REST API is required for Git mirroring.
- Handles nested namespaces such as GitLab groups and subgroups.
- Create bare Git mirrors containing branches, tags, and Git objects.
- Refresh repositories every **10 minutes** by default; the interval is configurable.
- Preserve old ref tips before each fetch under `refs/gitarchive/snapshots/`, retaining history after force-pushes.
- Restore a normal working copy from a local mirror, including archived historical refs.
- Never prune remote refs or delete a local mirror when its source becomes unavailable.
- Run several repository syncs concurrently (three by default) and stream Git transfer progress into the activity log.
- Show local archive sizes and apply a global **soft** storage limit.
- Keep a persistent local activity log and prevent multiple app instances from writing to the same mirrors.
- Minimize to the Windows notification area (system tray).
- Follow Windows light/dark preference and system accent color; request Mica on supported Windows 11 builds.

## Supported Git servers

The app accepts standard HTTPS and SSH clone URLs from:

- GitHub
- GitLab, including nested groups and subgroups
- Codeberg
- Bitbucket
- Gitea and Forgejo
- SourceHut
- Custom and self-hosted servers, such as a private GitLab, Gitea, Forgejo, or another server implementing Git's HTTPS/SSH transport

For a custom server, paste its repository's **Clone** URL. Examples:

```text
https://git.example.net/team/project.git
ssh://git@git.example.net:2222/team/project.git
git@git.example.net:team/project.git
```

GitArchive does not need to know the brand of a custom server: the Git client handles the Git transport. Hosting-provider names are used for display and organization only. HTTPS and SSH are supported; plain HTTP, local filesystem paths, URL-embedded passwords/tokens, query strings, and unsupported remote-helper transports are rejected. Configure private-repository credentials through Git Credential Manager or SSH keys. GitArchive does not ask for or store personal access tokens.

The mirror engine archives Git repository data, not each hosting site's complete web platform. Provider-specific Releases attachments, wiki pages, Issues, Pull Requests/Merge Requests, CI artifacts, Git LFS object payloads, and the separate repositories referenced by submodules are not downloaded in this version. Git LFS pointers remain in Git history until dedicated LFS archiving is added; submodule repositories should be added to GitArchive separately.

## Run on Windows

1. Install **Python 3.11+**, **Rust stable** (rustup), and **Git for Windows**.
2. Clone this repository.
3. Double-click `start.bat`.

The launcher creates `.venv`, installs `requirements.txt`, builds the Rust engine in release mode, and starts the app. It does not install system-wide packages or change Windows settings. The standard Rust MSVC toolchain may also require **Visual Studio Build Tools** with the C++ build tools workload; install it if Cargo reports that `link.exe` or the MSVC linker is missing.

- Python: https://www.python.org/downloads/windows/
- Rust: https://rustup.rs/
- Git for Windows: https://git-scm.com/download/win/

## Data locations

Settings are stored in the current user's local application data directory (`GitArchive/config.json`). The local activity log is stored in `GitArchive/logs/gitarchive.log` with one rotated backup. New mirrors are stored under the folder configured in Settings. Mirrors are separated by host and namespace so repositories with the same owner/project path on different servers do not collide.

A mirror is a standard bare Git repository. In the desktop app, select a repository and choose **Restore working copy**. The restore command creates a separate working folder and also fetches the preserved historical refs; it never overwrites an existing destination or changes the source archive.

You can also restore it manually:

```powershell
git --git-dir "$HOME\GitArchive\github.com\owner\repo.git" show-ref
git clone "$HOME\GitArchive\github.com\owner\repo.git" ".\repo-restored"
git -C ".\repo-restored" fetch --no-tags --force "$HOME\GitArchive\github.com\owner\repo.git" "+refs/gitarchive/snapshots/*:refs/gitarchive/snapshots/*"
```

## Archive boundaries and storage limit

This version archives **Git repository data**: branches, tags, and reachable Git objects. It does not yet download hosting-platform release attachments, wiki pages, issues, pull/merge requests, CI artifacts, Git LFS object payloads, or the separate repositories referenced by submodules. Git LFS pointers remain in Git history until dedicated LFS archiving is added; submodule repositories should be added to GitArchive separately.

The storage limit is intentionally a **soft limit**. Git cannot provide a reliable exact estimate of every object a fetch will add before downloading it. GitArchive checks total archive size before starting queued work and pauses new jobs after the configured limit is reached; already-running parallel fetches can exceed it by the total amount downloaded before they finish. Existing archives are never automatically deleted to make room.

If a fetch fails (for example, because a repository was deleted, made private, or the network is unavailable), GitArchive reports an error and leaves the last local mirror untouched. A fetch error alone cannot prove that a repository was deleted. A restored working copy uses the local archive as its `origin`, so it can continue to reference the archive even when the original remote is gone.

## Development

```powershell
cargo build --release --manifest-path rust-core/Cargo.toml
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m gitarchive
```

## Tests

Run the Python utility tests from the repository root:

```powershell
py -3 -m unittest discover -s tests -v
```

Run the Rust integration tests (requires the stable Rust toolchain):

```powershell
cargo test --manifest-path rust-core/Cargo.toml
```

The URL tests cover GitHub, GitLab nested groups, Codeberg, SSH, custom domains and ports, and unsafe URL rejection. The Rust integration tests create temporary local Git repositories and verify force-push retention, deleted-branch retention with pruning enabled, idempotent snapshots, safe restoration, and accepted/rejected Git transports.

## License

MIT. See [LICENSE](LICENSE).
