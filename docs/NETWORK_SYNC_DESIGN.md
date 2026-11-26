# GitArchive peer-to-peer archive exchange — design

**Status: design only; no peer listener, discovery, pairing, or transfer code is enabled yet.**

This document defines the security and resource constraints before implementing LAN / ZeroTier synchronization. The feature must remain opt-in and must not expose archives simply because another computer is on the same subnet or ZeroTier network.

## Goals

- Discover GitArchive peers on a directly connected LAN.
- Support ZeroTier-connected peers without depending on a public relay or cloud service.
- Show a peer's shared repository inventory and last-known commit/ref metadata.
- Let the user explicitly select a repository and approve an import.
- Compare local and remote refs/objects and transfer only missing Git objects where possible.
- Mark imported repositories with a visible provenance label such as **Получено из локальной сети** or **Получено через ZeroTier**.
- Keep local archives if the peer disappears or sends invalid data.

## Discovery and transport

1. LAN discovery uses a small UDP multicast announcement with protocol version, ephemeral service port, random peer ID, and a public-key fingerprint. It contains no repository names, paths, credentials, or archive data.
2. ZeroTier support uses the ZeroTier virtual interface as a transport path. The app should enumerate network interfaces and bind discovery/peer sockets to the selected interface rather than scanning every interface.
3. Discovery is only a convenience. A peer must still be paired and trusted before repository metadata or Git data can be requested.
4. A manual peer address option is required for networks where multicast is disabled or filtered. ZeroTier documentation notes that multicast is configurable and broadcast may be disabled; therefore discovery cannot depend on broadcast being available.
5. The app must not join, create, authorize, or reconfigure ZeroTier networks. It only uses an already-connected interface.

## Authentication and encryption

- Use TLS 1.3 with mutual authentication (both peers present certificates).
- Each installation generates a local private identity key. Keep it in the user's profile with restrictive ACLs; never transmit it.
- First pairing requires explicit approval on both sides. Display the peer identity and full SHA-256 certificate fingerprint. Use a high-entropy one-time pairing token with a short expiry, not a short numeric PIN as the only secret.
- Pin the peer certificate/public key after pairing. Reject identity changes until the user explicitly removes and re-pairs the peer.
- Do not trust a peer only because its IP is in the LAN or ZeroTier address range.
- No plaintext fallback. If TLS authentication fails, abort the transfer.

## Repository exchange protocol

- Exchange only structured metadata first: repository ID, canonical Git host/namespace/name, refs, object format, approximate archive size, and the peer's last verification result.
- Repository sharing is disabled by default. Each peer explicitly chooses which repositories to share.
- Imports go into a quarantine/staging directory created by GitArchive, never to a peer-supplied filesystem path.
- Use Git's pack/object transfer semantics over the authenticated channel. Do not accept arbitrary shell commands, filesystem paths, executable files, hooks, or remote helper names from peers.
- Enforce per-peer limits: maximum concurrent transfers, maximum announced repository count, maximum transfer size, connection/read timeouts, and rate limits.
- After transfer, run `git fsck --full --strict` and validate refs before promotion. Object integrity verification proves Git object consistency, not that the content is benign or trustworthy.
- Only after verification and explicit user approval should the staging mirror be merged/promoted into the normal archive store.
- Never automatically overwrite a newer local ref. Keep snapshots of current local refs before any merge and report conflicts.

## Provenance and UI

- Every imported repository stores source peer ID, peer fingerprint, transfer time, transport (LAN / ZeroTier), and verification status.
- Display a clear **Получено из локальной сети** / **Получено через ZeroTier** marker in the repository list and details.
- Show whether the peer's archive passed Git integrity checks, the check time, and the exact refs received.
- A user can revoke a peer, stop sharing, and remove a peer's access without deleting local archives.

## Resource budget

- Discovery announcements should be infrequent (for example, every 60 seconds) with jitter and low packet size.
- Do not continuously scan IP ranges or enumerate remote repositories in a tight loop.
- Keep one small listener per selected interface, use bounded async tasks, and cap simultaneous transfers.
- Idle peer mode should use negligible CPU and no periodic full archive scans.
- Hash/integrity scans should run in a background worker and be scheduled after transfer or on user request.

## Integrity check

A local archive can be checked independently with:

```powershell
git --git-dir "PATH_TO_ARCHIVE" fsck --full --strict
```

The peer protocol must never treat a successful network connection or matching repository name as proof that an archive is authentic. The pinned peer identity authenticates the sender; TLS protects transport; Git fsck validates Git object structure. These are separate checks and all are required.
