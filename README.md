# GitArchive

**GitArchive** is a local-first desktop application for keeping persistent Git mirrors of GitHub repositories. The desktop interface is built with PySide6 and follows the visual language of Windows 11 / WinUI 3; the archive engine is written in Rust.

> Current status: early MVP foundation. This first version focuses on persistent Git mirrors, scheduled synchronization, a Windows desktop shell, and a safe non-deleting archive policy.

## Features

- Add GitHub repository URLs (HTTPS or SSH).
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

## Run on Windows

1. Install **Python 3.11+**, **Rust stable** (rustup), and **Git for Windows**.
2. Clone this repository.
3. Double-click `start.bat`.

The launcher creates `.venv`, installs `requirements.txt`, builds the Rust engine in release mode, and starts the app. It does not install system-wide packages or change Windows settings. The standard Rust MSVC toolchain may also require **Visual Studio Build Tools** with the C++ build tools workload; install it if Cargo reports that `link.exe` or the MSVC linker is missing.

- Python: https://www.python.org/downloads/windows/
- Rust: https://rustup.rs/
- Git for Windows: https://git-scm.com/download/win/

## Data locations

Settings are stored in the current user's local application data directory (`GitArchive/config.json`). The local activity log is stored in `GitArchive/logs/gitarchive.log` with one rotated backup. New mirrors are stored under the folder configured in Settings; the default is `~/GitArchive/`.

A mirror is a standard bare Git repository. In the desktop app, select a repository and choose **Restore working copy**. The restore command creates a separate working folder and also fetches the preserved historical refs; it never overwrites an existing destination or changes the source archive.

You can also restore it manually:

```powershell
git --git-dir "$HOME\GitArchive\owner\repo.git" show-ref
git clone "$HOME\GitArchive\owner\repo.git" ".\repo-restored"
git -C ".\repo-restored" fetch --no-tags --force "$HOME\GitArchive\owner\repo.git" "+refs/gitarchive/snapshots/*:refs/gitarchive/snapshots/*"
```

## Archive boundaries and storage limit

This version archives **Git repository data**: branches, tags, and reachable Git objects. It does not yet download GitHub Releases attachments, the GitHub Wiki, Issues, Pull Requests, Actions artifacts, Git LFS object payloads, or the separate repositories referenced by submodules. Git LFS pointers remain in Git history until dedicated LFS archiving is added; submodule repositories should be added to GitArchive separately.

The storage limit is intentionally a **soft limit**. Git cannot provide a reliable exact estimate of every object a fetch will add before downloading it. GitArchive checks total archive size before starting queued work and pauses new jobs after the configured limit is reached; already-running parallel fetches can exceed it by the total amount downloaded before they finish. Existing archives are never automatically deleted to make room.

If a fetch fails (for example, because a repository was deleted, made private, or the network is unavailable), GitArchive reports an error and leaves the last local mirror untouched. A fetch error alone cannot prove that a repository was deleted. A restored working copy uses the local archive as its `origin`, so it can continue to reference the archive even when the original GitHub repository is gone.

Private repositories use credentials already configured for Git (for example, Git Credential Manager or SSH keys). GitArchive does not ask for or store a GitHub personal access token.

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

The Rust tests create temporary local Git repositories and verify force-push retention, deleted-branch retention with pruning enabled, idempotent snapshots, and safe restoration of working copies with their historical refs.

## License

MIT. See [LICENSE](LICENSE).
