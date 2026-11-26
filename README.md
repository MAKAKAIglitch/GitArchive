# GitArchive

<p align="center">
  <img src="assets/GitArchive.svg" alt="GitArchive application icon" width="160">
</p>

**GitArchive** is a local-first desktop application for keeping persistent Git mirrors of remote repositories. The desktop interface is built with PySide6 and follows the visual language of Windows 11 / WinUI 3; the archive engine is written in Rust.

> Current status: early MVP foundation. GitArchive archives Git repository data from GitHub, GitLab, Codeberg, Bitbucket, Gitea, Forgejo, SourceHut, and custom/self-hosted Git servers through standard HTTPS or SSH clone URLs.

## Features

- Add Git repository clone URLs using HTTPS or SSH.
- Supports common hosting services and arbitrary custom Git server hostnames; no provider-specific REST API is required for Git mirroring.
- Handles nested namespaces such as GitLab groups and subgroups.
- Store new mirrors as a normal bare `.git` folder, a ZIP archive, or a 7z archive.
- Select minimum, medium, or maximum compression for ZIP/7z storage.
- Automatically prefill the Add Repository field when the clipboard contains a valid HTTPS/SSH repository URL.
- Create bare Git mirrors containing branches, tags, and Git objects.
- Refresh repositories every **10 minutes** by default; the interval is configurable.
- Preserve old ref tips before each fetch under `refs/gitarchive/snapshots/`, retaining history after force-pushes.
- Restore a normal working copy from a local mirror, including archived historical refs.
- Never prune remote refs or delete a local mirror when its source becomes unavailable.
- Run up to three folder-format syncs concurrently by default; compressed archive updates are serialized to keep CPU and disk load predictable.
- Show local archive sizes and apply a global **soft** storage limit.
- Configure low CPU and memory priority for background engine / Git / 7-Zip processes, with a live synchronization progress panel and a no-output watchdog in the sidebar.
- Keep a persistent local activity log and prevent multiple app instances from writing to the same mirrors.
- Minimize to the Windows notification area (system tray).
- Automatically select Russian or English from the current Windows user UI language (other system languages fall back to Russian).
- Follow Windows light/dark preference and system accent color; request Mica on supported Windows 11 builds.
- Browse up to 250 commits from the local archive, inspect commit metadata and changed-file summaries, and export any selected commit as a ZIP snapshot.
- Open a repository context menu with history, sync, open archive, restore, and remove-from-list actions. Removing a list entry never deletes the archive.
- Optional current-user Windows startup and minimize-to-tray behavior.
- Optional HTTP, HTTPS, SOCKS4 and SOCKS5 proxy settings for HTTPS Git remotes. SOCKS proxies use remote DNS resolution; proxy credentials are not stored. SSH remotes use the configured SSH transport and do not use Git's HTTP proxy setting.

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

## Storage formats and compression

The **New archive format** setting applies to repositories added after the setting is saved. Existing repositories keep their recorded format; changing the setting never migrates or rewrites existing archives. The compression level is a global preference for all compressed repositories and takes effect on their next synchronization.

- **Folder (.git)** is the recommended default. Git fetches directly into the bare mirror, so there is no full archive extraction/repacking step and no extra archiver process.
- **ZIP (.zip)** is broadly compatible. Updates extract the compressed mirror into a temporary folder, run Git fetch, then create a replacement ZIP.
- **7z (.7z)** generally offers a smaller archive than ZIP at the cost of longer compression/decompression. Updates use the same safe temporary-workspace flow.

ZIP and 7z use the installed **7-Zip command-line executable**. GitArchive does not download or bundle 7-Zip. It looks for `7z.exe` / `7zz` in PATH and common Windows installation folders. Install 7-Zip from [the official site](https://www.7-zip.org/) and restart GitArchive; compressed formats remain unavailable when its CLI is not found.

Compression levels map to 7-Zip levels 1 / 5 / 9 (fastest / normal / ultra). The 7z mode uses LZMA2 with a 16 / 32 / 64 MiB dictionary cap, and both formats use one compression thread. Compressed synchronization is serialized so it does not compete with other repository jobs for CPU and disk bandwidth. “Maximum” is still slower and more memory-intensive than the other levels.

Most repository data is already stored in Git packfiles, where objects are compressed or represented as deltas. Wrapping the mirror in ZIP/7z may therefore save only a modest amount of additional space; the result depends on the repository. Compare the actual sizes of a sample mirror before using compressed storage for a large collection.

A compressed mirror must be unpacked before Git can update it. During synchronization, free space may be needed for an additional uncompressed copy of that repository. The previous archive is left untouched until the replacement archive has been created successfully. When no refs changed, GitArchive leaves the compressed file untouched and skips repacking. A changed compression level triggers a repack on the next synchronization. Commit history and restore operations unpack to temporary folders and remove those folders afterward.

## Repository history and context menu

Double-click a repository row or right-click it to open the WinUI-inspired context menu. The history viewer reads commit objects directly from the local bare mirror, including retained snapshots. It shows up to 250 commits and their metadata / changed-file summaries. Select a commit and choose **Download selected commit (.zip)** to export that exact tree. This exports the repository files at that commit; it is not a second copy of the entire Git history.

The context menu also provides sync, open archive folder, restore working copy, and remove from list. **Remove from list does not delete the archive directory.**

## Proxy and startup settings

Settings support HTTP, HTTPS, SOCKS4 and SOCKS5 proxies for HTTPS Git remotes. SOCKS4 uses `socks4a://` and SOCKS5 uses `socks5h://` so DNS resolution is delegated to the proxy. The proxy address and port are stored in the local app config; proxy authentication fields are not implemented. Git's `http.proxy` setting does not proxy SSH clone URLs.

The Windows startup option writes a per-user Run entry (no administrator rights). Background startup starts directly in the tray. The close-to-tray option keeps the app running when the window is closed; use **Exit** in the tray menu to stop it.

## Background process resource priority

The **Background CPU priority** and **Background memory priority** settings apply to the Rust archive engine and the Git / 7-Zip processes it launches. Git commands used by the commit-history viewer and compressed-archive history extraction use the same settings. The visible desktop interface stays at the normal priority so it remains responsive.

- CPU: **Idle** (default), **Below normal**, or **Normal**. Idle-priority work runs only when the scheduler has no higher-priority work to run, so a busy PC can make archive synchronization take longer.
- Memory: **Very low** (default), **Low**, or **Normal**. This is Windows' working-set trimming priority: when Windows reclaims physical pages, low-priority pages are more likely to be removed first. It does **not** impose a RAM usage cap or stop a process from allocating memory.

The selected values are passed to background processes at launch. They do not change the priority of the main GitArchive window.

The HTTPS download limit defaults to **3 MB/s** (3,000,000 bytes/s). It is configurable in Settings from **0** (unlimited) to **100 MB/s**. One shared limit is used across all active HTTPS repository syncs, not a separate limit per repository. GitArchive routes HTTPS CONNECT traffic through a local-only `127.0.0.1` proxy; when a GitArchive, Git, or environment proxy is configured, the local proxy chains through it. The relay does not decrypt TLS or inspect repository contents. SSH remotes are not rate-limited. Reducing download saturation can help reduce ping spikes while gaming, but cannot guarantee a particular ping because upload traffic, router queueing, and ISP congestion also matter.

The sidebar shows only the current synchronization batch. Its percentage is a live estimate based on Git / 7-Zip progress phases and equally weighted active repositories; it is not a byte-weighted overall transfer percentage. The progress bar animates as Git reports progress and the card disappears after the batch reaches 100%.

GitArchive also writes a **sync heartbeat once per minute** while a Rust synchronization process is still running, even when Git has not produced new output. After three minutes without output, the repository row changes to **No Git output** and the log records elapsed time and output-idle time. This is a diagnostic state, not an automatic cancellation: Git may be waiting for the network, disk, or local object processing, and a large but active transfer is left running. When Git output resumes, the row returns to **Synchronizing**.

For HTTPS, Git is configured to abort a transfer only when throughput remains below **512 bytes/s for 180 seconds**. Default OpenSSH connections use a 20-second connection/handshake timeout and TCP keepalive probes; an explicitly configured SSH command is preserved. The default SSH command is non-interactive, so a passphrase-protected key must be available through `ssh-agent`. These network guards do not impose a maximum total duration on a healthy large transfer.

## Build a Windows portable app and installer

Install Python 3.11+, Rust stable, Git for Windows, and (for the installer) Inno Setup 6 or 7. Then run:

```bat
build_windows.bat
```

The script generates the Windows multi-resolution icon from the source SVG, builds the Rust engine with one Cargo job, packages the PySide6 app as a one-folder portable application with PyInstaller, places the Rust engine beside it, and invokes Inno Setup when its compiler is installed. Every build run captures standard output and errors in a timestamped \`yyyy-MM-dd_HH-mm-ss.log\` file in the same directory as \`build_windows.bat\`; the script prints the captured log when the build ends and returns the original build exit code. The log files are ignored by Git. Outputs:

- Portable application: `dist\\GitArchive\\GitArchive.exe` plus its adjacent runtime files.
- Installer: `dist-installer\\GitArchive-Setup.exe` (when Inno Setup is installed).

Git for Windows remains a runtime dependency and is not bundled. The installer is per-user and does not require administrator privileges. This build script has not yet been validated on a clean Windows machine; the first packaged build must be smoke-tested before distribution.

### One-click GitHub Actions build

The repository includes a **manual-only** Windows installer workflow at `.github/workflows/windows-installer.yml`. It does not run on pushes, pull requests, or a schedule. It uses one Windows runner, one Cargo build job, no build matrix or cache, and uploads only the finished installer with a 7-day retention period.

To create the first artifact, enable GitHub Actions for the account and repository, open **Actions → Build Windows installer → Run workflow**, and wait for the manual run to finish. Download **GitArchive-Windows-Installer** from that run's Artifacts section. The artifact contains `GitArchive-Setup.exe`. The workflow also runs the Python unit tests before uploading it; a failed build or test does not publish an installer artifact.

## LAN and ZeroTier peer exchange

The first LAN / ZeroTier transfer implementation is available from **Repositories → LAN / ZeroTier**. ZeroTier does not need a special API or SDK: GitArchive enumerates the operating system's network interfaces with \`psutil\` and treats a ZeroTier virtual adapter like Wi-Fi or Ethernet.

- Discovery is started manually. It probes private IPv4 subnets attached to the computer's interfaces, including eligible ZeroTier / overlay interfaces. It scans only networks with at most 512 addresses (\`/23\` or smaller); larger networks can be used by entering the peer's private IP manually. Public Internet addresses are not scanned or accepted as peer endpoints.
- A device must explicitly enable **Принимать подключения**. The listener binds to the computer's private interface addresses on the selected TCP port (default \`47652\`); it is not opened automatically at app startup. Windows Firewall may ask whether to allow the app on the relevant private network.
- First pairing uses a one-time high-entropy code generated on the receiving device. It expires after 10 minutes and is consumed once. The discovered certificate fingerprint is checked when a device was selected from the scan list.
- After pairing, transfers use TLS 1.3 with mutual certificate authentication and pinned peer certificates. Removing a device from the paired list revokes its trust on this computer. Both computers must trust each other for later transfer attempts to succeed in both directions.
- Discovery reveals only a peer ID and certificate fingerprint; it does not request repository names, paths, or archive contents.
- The sender exports a standard Git bundle from a bare folder mirror (\`.git\`) and sends it to the paired peer. The bundle is limited to 20 GiB and protected by a SHA-256 check. ZIP and 7z source archives are not yet directly supported for sending; use folder storage for repositories you want to transfer.
- Received bundles are stored in the local incoming area under the app-data directory. They do not enter the main archive automatically. Select **Проверить и импортировать** to verify the bundle, clone it into a temporary mirror, run \`git fsck --full --strict\`, and then approve the import.
- When the same repository already exists as a folder mirror, incoming refs are placed in \`refs/gitarchive/peers/<peer-id>/<timestamp>/\`; normal local branches and tags are not replaced. For an existing ZIP/7z archive, the imported folder mirror is created alongside the compressed file; the old compressed file is preserved.
- The receiving computer needs enough free space for the incoming bundle and temporary verification mirror. At most two incoming transfers are accepted concurrently.

This is an early implementation, not yet a fully tested release. It currently supports private IPv4 discovery and folder mirrors; IPv6 discovery, automatic subnet discovery for very large overlay networks, incremental object-only transfer, compressed archive transfer, and transfer progress reporting remain future work. See [docs/NETWORK_SYNC_DESIGN.md](docs/NETWORK_SYNC_DESIGN.md) for protocol and limitations.

## Language selection

GitArchive currently supports Russian and English. On Windows it uses the current user's UI language via `GetUserDefaultUILanguage`: English UI selects English; other UI languages fall back to Russian. The language is selected at application start, so restart GitArchive after changing the Windows display language. The app does not change Windows language settings.

## Run on Windows

1. Install **Python 3.11+**, **Rust stable** (rustup), and **Git for Windows**.
2. Clone this repository.
3. Double-click `start.bat`.

The launcher creates `.venv`, installs `requirements.txt`, builds the Rust engine in release mode, and starts the app. It does not install system-wide packages or change Windows settings. The standard Rust MSVC toolchain may also require **Visual Studio Build Tools** with the C++ build tools workload; install it if Cargo reports that `link.exe` or the MSVC linker is missing.

- Python: https://www.python.org/downloads/windows/
- Rust: https://rustup.rs/
- Git for Windows: https://git-scm.com/download/win/
- 7-Zip (optional; required only for ZIP/7z storage): https://www.7-zip.org/

## Data locations

Settings are stored in the current user's local application data directory (`GitArchive/config.json`). The local activity log is stored in `GitArchive/logs/gitarchive.log` with one rotated backup. Archives are stored under the folder configured in Settings. GitHub folder mirrors keep the original `<storage>/<owner>/<repo>.git` layout for compatibility; compressed repositories use `.zip` or `.7z` at the same location. Other hosts are stored under `<storage>/_custom/<host>/<namespace>/` with the selected `.git`, `.zip` or `.7z` extension, so identical project paths on different servers do not collide.

A mirror is a standard bare Git repository. In the desktop app, select a repository and choose **Restore working copy**. The restore command creates a separate working folder and also fetches the preserved historical refs; it never overwrites an existing destination or changes the source archive.

You can also restore it manually:

```powershell
git --git-dir "$HOME\GitArchive\owner\repo.git" show-ref
git clone "$HOME\GitArchive\owner\repo.git" ".\repo-restored"
git -C ".\repo-restored" fetch --no-tags --force "$HOME\GitArchive\owner\repo.git" "+refs/gitarchive/snapshots/*:refs/gitarchive/snapshots/*"
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

The URL tests cover GitHub, GitLab nested groups, Codeberg, SSH, custom domains and ports, and unsafe URL rejection. Additional tests cover folder/ZIP/7z path generation and compressed-file size accounting. The Rust integration tests create temporary local Git repositories and verify force-push retention, deleted-branch retention with pruning enabled, idempotent snapshots, safe restoration, supported storage/compression options, archive path traversal rejection, and accepted/rejected Git transports.

## License

MIT. See [LICENSE](LICENSE).
