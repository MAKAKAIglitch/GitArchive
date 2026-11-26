# GitArchive peer-to-peer archive exchange

**Status: first LAN / ZeroTier transfer implementation is present; IPv4-only MVP. It needs Windows and multi-device testing before being considered stable.**

## Goals and current scope

- Discover GitArchive peers on directly connected private IPv4 networks.
- Treat ZeroTier, Tailscale-like overlays, Wi-Fi, and Ethernet as ordinary OS network interfaces. No ZeroTier API, SDK, account, or cloud service is required.
- Pair devices with a one-time high-entropy code, then use TLS 1.3 mutual authentication and pinned peer certificates.
- Transfer standard Git bundles from folder-format bare mirrors.
- Stage incoming data separately, verify it, and require an explicit user import action.
- Preserve local refs when importing from another peer.

## Network discovery

The discovery implementation uses \`psutil.net_if_addrs()\` to enumerate OS network interfaces and \`ipaddress\` to calculate directly attached IPv4 networks.

- Only private IPv4 addresses and the shared CGNAT range (\`100.64.0.0/10\`) are eligible.
- The app scans only when the user presses **Найти устройства в локальных сетях**.
- Automatic scans are limited to networks with at most 512 addresses (\`/23\` or narrower). Larger networks require a manually entered private IPv4 address. This prevents an accidental sweep through a very large overlay subnet.
- Loopback, link-local, multicast, public IPv4, and IPv6 addresses are excluded.
- Discovery probes TCP port \`47652\` by default. A peer returns only its protocol version, UUID, and SHA-256 certificate fingerprint. Repository inventory and archive metadata are not disclosed during scanning.
- There is no dependency on multicast. Multicast availability varies by overlay/network configuration, so subnet probes and manual addresses are used instead.
- The listener binds only to enumerated private interface addresses, not to \`0.0.0.0\` or every host interface. It is enabled explicitly by the user.

A ZeroTier interface is handled like any other interface that has an eligible private IPv4 address and a subnet no larger than the scan limit. An adapter with a larger subnet remains usable by manually entering the peer's assigned private address.

## Pairing and trust

- Each installation generates a P-256 ECDSA private key and a self-signed identity certificate. The private key remains in the local app-data directory.
- The receiving user enables the listener and creates a one-time pairing token. It has 192 bits of random entropy, expires after 10 minutes, and is consumed once.
- The initiating peer sends its UUID, certificate, a fresh 256-bit nonce, and an HMAC-SHA256 proof binding both certificate fingerprints to the token.
- The receiver validates the proof and stores the initiating certificate. The receiver returns its own certificate and a matching proof, allowing the initiating peer to validate and pin it.
- If the initiator selected a discovered peer, it also checks that the certificate fingerprint still matches the one observed during discovery.
- Subsequent transfer connections require TLS 1.3 and mutual certificate authentication. Both sides pin the peer certificate. A LAN or overlay IP address alone is never a trust signal.
- A user can remove a paired peer from the UI. That removes its pinned certificate and saved endpoint on this device. Re-pair on both devices to restore mutual trust.

No plaintext transfer fallback is implemented. No personal access tokens, passwords, or repository credentials are sent over this protocol.

## Transfer protocol

- The sender currently supports folder-format bare mirrors (\`.git\`) and creates a Git bundle containing its refs with \`git bundle create --all\`.
- The receiver accepts the bundle only from a paired mTLS identity and only when its size is within the 20 GiB per-transfer limit and enough free space remains.
- The transfer includes the expected size and SHA-256 digest. The receiver checks the digest before finalizing the staged bundle; import repeats the size and digest check in case the local staged file changed.
- A partial \`.part\` file is removed after a failed or interrupted receive.
- Incoming bundles and sidecar metadata are placed under the app's local \`network/incoming\` staging directory, not in the archive directory.
- The user must select the incoming item and press **Проверить и импортировать**. The importer runs \`git bundle verify\`, clones to a temporary bare mirror, and runs \`git fsck --full --strict\`.
- If the corresponding local folder mirror already exists, incoming refs are fetched into \`refs/gitarchive/peers/<peer-id>/<timestamp>/*\`. Existing local branch and tag names are not overwritten.
- If no folder mirror exists, the validated mirror is installed as a new folder archive. If a compressed ZIP/7z mirror exists at a different path, it is left untouched and the folder mirror is created alongside it.
- The imported repository keeps its original upstream URL as \`remote.origin.url\`, while metadata records the peer ID, transport, and receive time.

The bundle is a Git object/ref transport, not an entire provider backup. It does not include Git LFS payloads or separate submodule repositories unless they are separately archived.

## Resource and security limits

- Peer scanning is manual, bounded to at most 512 target addresses per interface network, and uses a fixed worker pool.
- The listener accepts at most two concurrent bundle transfers.
- Each incoming bundle is capped at 20 GiB and requires an additional 64 MiB free-space reserve.
- Connection timeouts are applied. Git child processes use the configured background CPU/memory priority defaults (Idle / Very low).
- Peer-provided repository paths are never used as filesystem destinations. The destination is generated locally through GitArchive's repository path rules.
- The peer protocol does not accept shell commands, executable paths, Git hooks, arbitrary remote helpers, or destination paths from the sender.
- The receiver authenticates the sender, verifies Git bundle structure and runs a strict Git object check. These checks establish identity and Git object consistency; they do not prove that repository source code is harmless.

## Current limitations / next work

- Only private IPv4 and CGNAT peer addresses are supported. IPv6 ULA, link-local scope IDs, and large-subnet discovery are not implemented.
- Automatic scanning is intentionally skipped for networks larger than 512 addresses; manual IP entry is available.
- Outbound transfer currently supports folder mirrors only. ZIP/7z transfer is not implemented.
- The first implementation sends one Git bundle; it does not negotiate missing objects for incremental synchronization.
- The UI reports completion and errors but does not yet show byte-level transfer progress or pause/resume.
- Peer endpoints are refreshed when a later discovery result matches an already pinned fingerprint. If a peer changes address and cannot be discovered, update it by re-pairing.
- The protocol has not yet been validated with real Windows Defender Firewall rules, multiple physical/overlay adapters, network changes during a transfer, or long-running large repositories. Treat this version as experimental until those tests pass.

## Manual integrity check

A local mirror can be inspected independently:

~~~powershell
git --git-dir "PATH_TO_ARCHIVE" fsck --full --strict
~~~
