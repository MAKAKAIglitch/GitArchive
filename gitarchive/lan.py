"""Authenticated local-network peer exchange for GitArchive.

Discovery is deliberately limited to directly attached private IPv4 subnets.
ZeroTier and other overlay adapters are treated like any other interface.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psutil
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .priority import apply_memory_priority_to_handle, process_creation_flags
from .i18n import tr
from .repository import archive_path, parse_git_url


PROTOCOL_VERSION = 1
DEFAULT_PORT = 47652
MAX_HEADER_BYTES = 64 * 1024
MAX_BUNDLE_BYTES = 20 * 1024**3
MAX_SCAN_ADDRESSES = 512
SCAN_WORKERS = 48
CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _is_local_address(value: ipaddress._BaseAddress) -> bool:
    return (
        value.version == 4
        and not value.is_loopback
        and not value.is_link_local
        and not value.is_multicast
        and (value.is_private or value in CGNAT_NETWORK)
    )


def _require_private_ipv4(address: str) -> str:
    try:
        parsed = ipaddress.ip_address(str(address).strip())
    except ValueError as error:
        raise ValueError(tr("Укажи корректный IPv4-адрес устройства.")) from error
    if not _is_local_address(parsed):
        raise ValueError(tr("Разрешены только частные IPv4-адреса локальной сети, ZeroTier или CGNAT."))
    return str(parsed)


def local_ipv4_addresses() -> list[str]:
    """Return private IPv4 addresses for listener binding, including large subnets."""
    found: set[str] = set()
    for addresses in psutil.net_if_addrs().values():
        for address in addresses:
            if address.family != socket.AF_INET or not address.address:
                continue
            try:
                ip = ipaddress.ip_address(address.address.split("%", 1)[0])
            except ValueError:
                continue
            if _is_local_address(ip):
                found.add(str(ip))
    return sorted(found, key=lambda value: int(ipaddress.ip_address(value)))


def local_ipv4_interfaces() -> list[tuple[str, ipaddress.IPv4Network]]:
    """Return small private IPv4 interface networks eligible for automatic scans."""
    found: dict[str, ipaddress.IPv4Network] = {}
    for addresses in psutil.net_if_addrs().values():
        for address in addresses:
            if address.family != socket.AF_INET or not address.address or not address.netmask:
                continue
            try:
                ip = ipaddress.ip_address(address.address.split("%", 1)[0])
                if not _is_local_address(ip):
                    continue
                network = ipaddress.ip_interface(f"{ip}/{address.netmask}").network
            except ValueError:
                continue
            if network.num_addresses <= MAX_SCAN_ADDRESSES:
                found[str(ip)] = network
    return sorted(found.items(), key=lambda item: (int(ipaddress.ip_address(item[0])), str(item[1])))


def _send_json(connection: ssl.SSLSocket, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(payload) > MAX_HEADER_BYTES:
        raise ValueError(tr("Сетевой заголовок слишком большой."))
    connection.sendall(payload)


def _read_json(connection: ssl.SSLSocket) -> dict[str, Any]:
    payload = bytearray()
    while len(payload) <= MAX_HEADER_BYTES:
        part = connection.recv(1)
        if not part:
            raise ConnectionError(tr("Соединение закрыто до получения заголовка."))
        if part == b"\n":
            break
        payload.extend(part)
    else:
        raise ValueError(tr("Сетевой заголовок превышает лимит."))
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(tr("Некорректный сетевой заголовок."))
    return value


def _read_exact(connection: ssl.SSLSocket, length: int, destination) -> str:
    digest = hashlib.sha256()
    remaining = length
    while remaining:
        chunk = connection.recv(min(1024 * 1024, remaining))
        if not chunk:
            raise ConnectionError(tr("Передача оборвалась до получения всего Git bundle."))
        destination.write(chunk)
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def _run_process(arguments: list[str], timeout: int = 3600) -> tuple[int, str, str]:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    flags = process_creation_flags("idle")
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        creationflags=flags,
    )
    if os.name == "nt":
        try:
            apply_memory_priority_to_handle(process._handle, "very_low")
        except (OSError, ValueError):
            pass
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise TimeoutError(tr(f"Команда превысила лимит времени ({timeout} с): {' '.join(arguments[:3])}"))
    return process.returncode, stdout, stderr


def _require_process_success(arguments: list[str], timeout: int = 3600) -> tuple[str, str]:
    code, stdout, stderr = _run_process(arguments, timeout)
    if code != 0:
        message = (stderr or stdout or tr(f"Код завершения: {code}")).strip()
        raise RuntimeError(message[-6000:])
    return stdout, stderr


def _certificate_identity(certificate: x509.Certificate) -> str:
    values = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if len(values) != 1:
        raise ValueError(tr("У сертификата пира нет уникального идентификатора."))
    try:
        return str(uuid.UUID(values[0].value))
    except ValueError as error:
        raise ValueError(tr("Сертификат содержит некорректный идентификатор пира.")) from error


def _validate_identity_certificate(pem: str) -> tuple[x509.Certificate, str, str]:
    if not isinstance(pem, str) or len(pem.encode("utf-8")) > 16 * 1024:
        raise ValueError(tr("Сертификат пира имеет недопустимый размер."))
    certificate = x509.load_pem_x509_certificate(pem.encode("ascii"))
    if certificate.issuer != certificate.subject:
        raise ValueError(tr("Сертификат пира не является самоподписанным."))
    public_key = certificate.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise ValueError(tr("Не поддерживается тип ключа сертификата пира."))
    public_key.verify(
        certificate.signature,
        certificate.tbs_certificate_bytes,
        ec.ECDSA(certificate.signature_hash_algorithm),
    )
    constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
    if not constraints.ca:
        raise ValueError(tr("Сертификат пира не может использоваться как закреплённый корень доверия."))
    key_usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
    if not key_usage.digital_signature or not key_usage.key_cert_sign:
        raise ValueError(tr("Сертификат пира не содержит нужные ограничения ключа."))
    extended_usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    if not {ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH}.issubset(set(extended_usage)):
        raise ValueError(tr("Сертификат пира не разрешает клиентскую и серверную TLS-аутентификацию."))
    names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    if x509.DNSName("gitarchive-peer") not in names:
        raise ValueError(tr("Сертификат пира имеет неизвестное назначение."))
    now = datetime.now(timezone.utc)
    if not certificate.not_valid_before_utc <= now <= certificate.not_valid_after_utc:
        raise ValueError(tr("Срок действия сертификата пира истёк или ещё не начался."))
    peer_id = _certificate_identity(certificate)
    der = certificate.public_bytes(serialization.Encoding.DER)
    return certificate, peer_id, hashlib.sha256(der).hexdigest()


def _make_identity(directory: Path) -> tuple[str, str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    key_path = directory / "identity-key.pem"
    cert_path = directory / "identity-cert.pem"
    if key_path.exists() != cert_path.exists():
        raise RuntimeError(tr("Файлы сетевой идентичности неполные. Не удаляй их: требуется восстановление пары ключ/сертификат."))
    if key_path.exists():
        key_pem = key_path.read_bytes()
        cert_pem = cert_path.read_text(encoding="ascii")
        _validate_identity_certificate(cert_pem)
        return key_pem.decode("ascii"), cert_pem, _certificate_identity(
            x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
        )

    peer_id = str(uuid.uuid4())
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, peer_id)])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before((now - timedelta(minutes=5)).replace(tzinfo=None))
        .not_valid_after((now + timedelta(days=3650)).replace(tzinfo=None))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("gitarchive-peer")]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    key_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
    key_path.write_bytes(key_pem)
    cert_path.write_text(cert_pem, encoding="ascii")
    try:
        os.chmod(key_path, 0o600)
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return key_pem.decode("ascii"), cert_pem, peer_id


def _scan_probe(address: str, port: int, timeout: float) -> dict[str, str] | None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        raw = socket.create_connection((address, port), timeout=timeout)
        with context.wrap_socket(raw, server_hostname="gitarchive-peer") as connection:
            connection.settimeout(timeout)
            certificate = connection.getpeercert(binary_form=True)
            _send_json(connection, {"op": "hello", "version": PROTOCOL_VERSION})
            response = _read_json(connection)
            if response.get("ok") is not True or response.get("version") != PROTOCOL_VERSION:
                return None
            fingerprint = hashlib.sha256(certificate).hexdigest()
            if not hmac.compare_digest(fingerprint, str(response.get("fingerprint", ""))):
                return None
            return {
                "peer_id": str(response.get("peer_id", "")),
                "address": address,
                "port": str(port),
                "fingerprint": fingerprint,
            }
    except (OSError, ssl.SSLError, ValueError, ConnectionError, json.JSONDecodeError):
        return None


def discover_local_peers(port: int = DEFAULT_PORT, timeout: float = 0.35) -> list[dict[str, str]]:
    """Probe up to 512 addresses per private IPv4 interface subnet, including overlays."""
    interfaces = local_ipv4_interfaces()
    local_ips = {address for address, _network in interfaces}
    targets: set[str] = set()
    for address, network in interfaces:
        for host in network.hosts():
            value = str(host)
            if value not in local_ips:
                targets.add(value)
    if not targets:
        return []

    discovered: dict[str, dict[str, str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        futures = {
            executor.submit(_scan_probe, address, int(port), float(timeout)): address
            for address in targets
        }
        for future in concurrent.futures.as_completed(futures):
            peer = future.result()
            if peer and peer["peer_id"]:
                discovered[peer["peer_id"]] = peer
    return sorted(discovered.values(), key=lambda peer: peer["peer_id"])


class _PeerTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, service):
        self.service = service
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        super().__init__(address, _PeerRequestHandler)


class _PeerRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        service: LanPeerService = self.server.service
        connection = None
        try:
            context = service._server_tls_context()
            connection = context.wrap_socket(self.request, server_side=True)
            connection.settimeout(120)
            message = _read_json(connection)
            operation = message.get("op")
            if operation == "hello":
                _send_json(connection, {
                    "ok": True,
                    "version": PROTOCOL_VERSION,
                    "peer_id": service.peer_id,
                    "fingerprint": service.fingerprint,
                    "port": service.port,
                })
            elif operation == "pair":
                service._handle_pair(connection, message, self.client_address[0])
            elif operation == "bundle":
                service._handle_bundle(connection, message, str(self.client_address[0]))
            else:
                _send_json(connection, {"ok": False, "error": tr("Неизвестная операция.")})
        except (OSError, ssl.SSLError, ValueError, RuntimeError, json.JSONDecodeError, ConnectionError) as error:
            if connection is not None:
                try:
                    _send_json(connection, {"ok": False, "error": str(error)[:1000]})
                except OSError:
                    pass
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass


class LanPeerService:
    """TLS 1.3 peer service. First pairing uses a one-time high-entropy code;
    all subsequent transfers use mutual TLS with pinned peer certificates.
    """

    def __init__(self, application_data: Path):
        self.root = Path(application_data) / "network"
        self.incoming_dir = self.root / "incoming"
        self.peers_dir = self.root / "peers"
        self.incoming_dir.mkdir(parents=True, exist_ok=True)
        self.peers_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
            os.chmod(self.incoming_dir, 0o700)
            os.chmod(self.peers_dir, 0o700)
        except OSError:
            pass
        key_pem, cert_pem, peer_id = _make_identity(self.root)
        self.key_pem = key_pem
        self.cert_pem = cert_pem
        self.peer_id = peer_id
        certificate = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
        self.cert_der = certificate.public_bytes(serialization.Encoding.DER)
        self.fingerprint = hashlib.sha256(self.cert_der).hexdigest()
        self.key_path = self.root / "identity-key.pem"
        self.cert_path = self.root / "identity-cert.pem"
        self._lock = threading.RLock()
        self._servers: list[_PeerTCPServer] = []
        self._threads: list[threading.Thread] = []
        self.port = DEFAULT_PORT
        self.pairing_code: str | None = None
        self.pairing_expires = 0.0
        self.transfer_slots = threading.BoundedSemaphore(2)

    @property
    def is_running(self) -> bool:
        return bool(self._servers)

    def local_addresses(self) -> list[str]:
        return local_ipv4_addresses()

    def start(self, port: int = DEFAULT_PORT) -> list[str]:
        if self.is_running:
            return self.local_addresses()
        if not 1024 <= int(port) <= 65535:
            raise ValueError(tr("Порт должен быть в диапазоне 1024–65535."))
        addresses = self.local_addresses()
        if not addresses:
            raise RuntimeError(tr("Не найдено частных IPv4-интерфейсов для приёма подключений."))
        self.port = int(port)
        errors = []
        for address in addresses:
            try:
                server = _PeerTCPServer((address, self.port), self)
            except OSError as error:
                errors.append(f"{address}: {error}")
                continue
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.5},
                name=f"GitArchive-LAN-{address}",
                daemon=True,
            )
            thread.start()
            self._servers.append(server)
            self._threads.append(thread)
        if not self._servers:
            raise OSError(tr("Не удалось открыть LAN-порт: ") + "; ".join(errors[:3]))
        return [str(server.server_address[0]) for server in self._servers]

    def stop(self) -> None:
        servers, self._servers = self._servers, []
        for server in servers:
            try:
                server.shutdown()
                server.server_close()
            except OSError:
                pass
        self._threads.clear()
        with self._lock:
            self.pairing_code = None
            self.pairing_expires = 0.0

    def issue_pairing_code(self) -> str:
        if not self.is_running:
            raise RuntimeError(tr("Сначала включи приём подключений."))
        with self._lock:
            self.pairing_code = secrets.token_urlsafe(24)
            self.pairing_expires = time.monotonic() + 600
            return self.pairing_code

    def _peer_file(self, peer_id: str) -> Path:
        safe_id = str(uuid.UUID(peer_id))
        return self.peers_dir / f"{safe_id}.pem"

    def _load_peers(self) -> list[dict[str, Any]]:
        peers = []
        for cert_path in self.peers_dir.glob("*.pem"):
            try:
                pem = cert_path.read_text(encoding="ascii")
                _certificate, peer_id, fingerprint = _validate_identity_certificate(pem)
                if cert_path.stem == peer_id:
                    metadata_path = self.peers_dir / f"{peer_id}.json"
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
                    peers.append({
                        "peer_id": peer_id,
                        "fingerprint": fingerprint,
                        "cert_pem": pem,
                        "address": str(metadata.get("address", "")),
                        "port": int(metadata.get("port", self.port)),
                    })
            except (OSError, ValueError, TypeError):
                continue
        return peers

    def peers(self) -> list[dict[str, Any]]:
        stored = self._load_peers()
        by_id = {peer["peer_id"]: peer for peer in stored}
        return sorted(by_id.values(), key=lambda peer: peer["peer_id"])

    def forget_peer(self, peer_id: str) -> None:
        """Revoke a paired peer by deleting its pinned certificate and endpoint."""
        safe_id = str(uuid.UUID(peer_id))
        self._peer_file(safe_id).unlink(missing_ok=True)
        (self.peers_dir / f"{safe_id}.json").unlink(missing_ok=True)

    def refresh_peer_endpoint(
        self, peer_id: str, address: str, port: int, fingerprint: str
    ) -> bool:
        """Update a paired peer's address only when its pinned fingerprint matches discovery."""
        peer = self.find_peer(peer_id)
        if not peer or not hmac.compare_digest(peer["fingerprint"], str(fingerprint).lower()):
            return False
        self._store_peer(peer_id, peer["cert_pem"], address, int(port))
        return True

    def find_peer(self, peer_id: str) -> dict[str, Any] | None:
        try:
            target = str(uuid.UUID(peer_id))
        except ValueError:
            return None
        return next((peer for peer in self.peers() if peer["peer_id"] == target), None)

    def _store_peer(self, peer_id: str, cert_pem: str, address: str, port: int) -> dict[str, Any]:
        address = _require_private_ipv4(address)
        port = int(port)
        if not 1024 <= port <= 65535:
            raise ValueError(tr("Порт сопряжённого устройства вне допустимого диапазона."))
        _certificate, parsed_id, fingerprint = _validate_identity_certificate(cert_pem)
        if parsed_id != str(uuid.UUID(peer_id)):
            raise ValueError(tr("Идентификатор не совпадает с сертификатом пира."))
        path = self._peer_file(peer_id)
        temporary = path.with_suffix(".pem.tmp")
        temporary.write_text(cert_pem, encoding="ascii")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(path)
        metadata_path = self.peers_dir / f"{parsed_id}.json"
        metadata_temporary = metadata_path.with_suffix(".json.tmp")
        metadata_temporary.write_text(
            json.dumps({"address": address, "port": int(port)}, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        metadata_temporary.replace(metadata_path)
        return {
            "peer_id": parsed_id,
            "fingerprint": fingerprint,
            "cert_pem": cert_pem,
            "address": address,
            "port": int(port),
        }

    def _server_tls_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(str(self.cert_path), str(self.key_path))
        peer_pems = [peer["cert_pem"] for peer in self.peers()]
        if peer_pems:
            context.load_verify_locations(cadata="\n".join(peer_pems))
        context.verify_mode = ssl.CERT_OPTIONAL
        return context

    def _client_tls_context(self, peer: dict[str, Any]) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cadata=peer["cert_pem"])
        context.load_cert_chain(str(self.cert_path), str(self.key_path))
        return context

    def _handle_pair(self, connection: ssl.SSLSocket, message: dict[str, Any], address: str) -> None:
        address = _require_private_ipv4(address)
        with self._lock:
            code = self.pairing_code
            expires = self.pairing_expires
        if not code or time.monotonic() > expires:
            raise PermissionError(tr("Код сопряжения не создан или истёк. Создай новый код на принимающем устройстве."))
        peer_id = str(message.get("peer_id", ""))
        nonce = str(message.get("nonce", ""))
        cert_pem = str(message.get("certificate", ""))
        _certificate, parsed_id, client_fp = _validate_identity_certificate(cert_pem)
        if parsed_id != str(uuid.UUID(peer_id)):
            raise ValueError(tr("Идентификатор отправителя не совпадает с сертификатом."))
        if not re.fullmatch(r"[0-9a-f]{64}", nonce):
            raise ValueError(tr("Некорректный одноразовый challenge."))
        payload = f"GITARCHIVE-PAIR-V1\n{nonce}\n{peer_id}\n{client_fp}\n{self.fingerprint}".encode("ascii")
        expected = hmac.new(code.encode("ascii"), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, str(message.get("proof", ""))):
            raise PermissionError(tr("Неверный код сопряжения."))
        remote_port = int(message.get("listen_port", DEFAULT_PORT))
        if not 1024 <= remote_port <= 65535:
            raise ValueError(tr("Порт удалённого устройства вне допустимого диапазона."))
        with self._lock:
            if self.pairing_code != code or time.monotonic() > self.pairing_expires:
                raise PermissionError(tr("Код сопряжения уже использован или истёк. Создай новый код."))
            self.pairing_code = None
            self.pairing_expires = 0.0
        self._store_peer(peer_id, cert_pem, address, remote_port)
        ack_payload = f"GITARCHIVE-ACK-V1\n{nonce}\n{client_fp}\n{self.fingerprint}\n{self.peer_id}".encode("ascii")
        proof = hmac.new(code.encode("ascii"), ack_payload, hashlib.sha256).hexdigest()
        _send_json(connection, {
            "ok": True,
            "peer_id": self.peer_id,
            "certificate": self.cert_pem,
            "fingerprint": self.fingerprint,
            "proof": proof,
        })

    def pair_with_peer(
        self,
        address: str,
        port: int,
        pairing_code: str,
        expected_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        address = _require_private_ipv4(address)
        port = int(port)
        if not 1024 <= port <= 65535:
            raise ValueError(tr("Порт удалённого устройства вне допустимого диапазона."))
        code = str(pairing_code).strip()
        if len(code) < 24 or len(code) > 128:
            raise ValueError(tr("Код должен быть полным одноразовым кодом сопряжения."))
        raw = socket.create_connection((address, int(port)), timeout=5)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with context.wrap_socket(raw, server_hostname="gitarchive-peer") as connection:
            connection.settimeout(15)
            server_der = connection.getpeercert(binary_form=True)
            server_fp = hashlib.sha256(server_der).hexdigest()
            if expected_fingerprint and not hmac.compare_digest(
                server_fp, str(expected_fingerprint).lower()
            ):
                raise PermissionError(tr("Отпечаток сертификата не совпадает с найденным устройством."))
            nonce = secrets.token_hex(32)
            client_certificate = x509.load_pem_x509_certificate(self.cert_pem.encode("ascii"))
            client_der = client_certificate.public_bytes(serialization.Encoding.DER)
            client_fp = hashlib.sha256(client_der).hexdigest()
            payload = f"GITARCHIVE-PAIR-V1\n{nonce}\n{self.peer_id}\n{client_fp}\n{server_fp}".encode("ascii")
            proof = hmac.new(code.encode("ascii"), payload, hashlib.sha256).hexdigest()
            _send_json(connection, {
                "op": "pair",
                "peer_id": self.peer_id,
                "nonce": nonce,
                "certificate": self.cert_pem,
                "listen_port": self.port,
                "proof": proof,
            })
            response = _read_json(connection)
            if response.get("ok") is not True:
                raise PermissionError(str(response.get("error") or tr("Сопряжение отклонено.")))
            remote_pem = str(response.get("certificate", ""))
            _certificate, remote_id, remote_fp = _validate_identity_certificate(remote_pem)
            if remote_fp != server_fp or remote_id != str(response.get("peer_id")):
                raise PermissionError(tr("Сертификат удалённого устройства изменился во время сопряжения."))
            ack_payload = f"GITARCHIVE-ACK-V1\n{nonce}\n{client_fp}\n{server_fp}\n{remote_id}".encode("ascii")
            expected = hmac.new(code.encode("ascii"), ack_payload, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, str(response.get("proof", ""))):
                raise PermissionError(tr("Не удалось подтвердить владение кодом сопряжения."))
            return self._store_peer(remote_id, remote_pem, address, int(port))

    def _handle_bundle(self, connection: ssl.SSLSocket, message: dict[str, Any], address: str) -> None:
        address = _require_private_ipv4(address)
        client_der = connection.getpeercert(binary_form=True)
        if not client_der:
            raise PermissionError(tr("Для передачи требуется сопряжённое устройство."))
        fingerprint = hashlib.sha256(client_der).hexdigest()
        peer = next((item for item in self.peers() if item["fingerprint"] == fingerprint), None)
        if not peer:
            raise PermissionError(tr("Сертификат отправителя отсутствует в списке доверенных устройств."))
        if not self.transfer_slots.acquire(blocking=False):
            raise RuntimeError(tr("На этом устройстве уже выполняются две передачи."))
        try:
            metadata = message.get("repository")
            if not isinstance(metadata, dict):
                raise ValueError(tr("Не переданы сведения о Git-репозитории."))
            url, namespace, name, host, provider = parse_git_url(str(metadata.get("url", "")))
            if (str(metadata.get("namespace")) != namespace or
                    str(metadata.get("name")) != name or
                    str(metadata.get("host")) != host):
                raise ValueError(tr("Метаданные репозитория не совпадают с его Git URL."))
            length = int(message.get("size", 0))
            digest = str(message.get("sha256", ""))
            if not 0 < length <= MAX_BUNDLE_BYTES or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(tr("Размер или контрольная сумма bundle некорректны."))
            if shutil.disk_usage(self.incoming_dir).free < length + 64 * 1024 * 1024:
                raise OSError(tr("Недостаточно свободного места для входящего архива (нужен дополнительный запас 64 МБ)."))
            transfer_id = uuid.uuid4().hex
            bundle_path = self.incoming_dir / f"{transfer_id}.bundle"
            temporary = bundle_path.with_suffix(".bundle.part")
            _send_json(connection, {"ok": True, "ready": True})
            try:
                with temporary.open("wb") as stream:
                    received_digest = _read_exact(connection, length, stream)
                if not hmac.compare_digest(received_digest, digest):
                    raise ValueError(tr("Контрольная сумма Git bundle не совпала."))
                temporary.replace(bundle_path)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            sidecar = {
                "id": transfer_id,
                "file": bundle_path.name,
                "repository": {"url": url, "namespace": namespace, "name": name, "host": host, "provider": provider},
                "source_peer_id": peer["peer_id"],
                "source_fingerprint": fingerprint,
                "source_address": address,
                "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "size": length,
                "sha256": digest,
                "transport": "local-network",
                "imported": False,
            }
            sidecar_path = bundle_path.with_suffix(".json")
            temporary_sidecar = sidecar_path.with_suffix(".json.tmp")
            temporary_sidecar.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary_sidecar.replace(sidecar_path)
            _send_json(connection, {"ok": True, "received": True, "transfer_id": transfer_id})
        finally:
            self.transfer_slots.release()

    def send_bundle(self, peer_id: str, repository: dict[str, Any]) -> dict[str, Any]:
        peer = self.find_peer(peer_id)
        if not peer:
            raise PermissionError(tr("Устройство ещё не сопряжено."))
        peer_address = _require_private_ipv4(str(peer.get("address", "")))
        if str(repository.get("storage_format", "folder")).lower() != "folder":
            raise ValueError(tr("Прямая передача ZIP/7z пока не включена. Сначала используй формат папки .git."))
        mirror = Path(str(repository.get("local_path", "")))
        if not (mirror.is_dir() and (mirror / "HEAD").is_file() and (mirror / "objects").is_dir()):
            raise FileNotFoundError(tr("Локальное bare-зеркало ещё не создано или недоступно."))
        url, namespace, name, host, provider = parse_git_url(str(repository.get("url", "")))
        if (str(repository.get("namespace")) != namespace or
                str(repository.get("name")) != name or
                str(repository.get("host")) != host):
            raise ValueError(tr("Метаданные в локальном списке не совпадают с Git URL."))
        with tempfile.TemporaryDirectory(prefix="gitarchive-peer-") as temporary_dir:
            bundle_path = Path(temporary_dir) / f"{name}.bundle"
            _require_process_success([
                "git", "--git-dir", str(mirror), "bundle", "create", str(bundle_path), "--all"
            ], timeout=7200)
            length = bundle_path.stat().st_size
            if not 0 < length <= MAX_BUNDLE_BYTES:
                raise ValueError(tr(f"Размер bundle превышает лимит {MAX_BUNDLE_BYTES // (1024**3)} ГБ."))
            digest = hashlib.sha256()
            with bundle_path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            metadata = {
                "url": url,
                "namespace": namespace,
                "name": name,
                "host": host,
                "provider": provider,
            }
            context = self._client_tls_context(peer)
            raw = socket.create_connection((peer_address, int(peer["port"])), timeout=10)
            with context.wrap_socket(raw, server_hostname="gitarchive-peer") as connection:
                connection.settimeout(120)
                _send_json(connection, {
                    "op": "bundle",
                    "repository": metadata,
                    "size": length,
                    "sha256": digest.hexdigest(),
                })
                response = _read_json(connection)
                if response.get("ok") is not True or response.get("ready") is not True:
                    raise RuntimeError(str(response.get("error") or tr("Удалённое устройство не готово принять bundle.")))
                with bundle_path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        connection.sendall(chunk)
                result = _read_json(connection)
                if result.get("ok") is not True or result.get("received") is not True:
                    raise RuntimeError(str(result.get("error") or tr("Удалённое устройство не подтвердило получение.")))
                return {"transfer_id": result.get("transfer_id"), "size": length, "repository": metadata}

    def list_incoming(self) -> list[dict[str, Any]]:
        result = []
        for sidecar_path in self.incoming_dir.glob("*.json"):
            try:
                item = json.loads(sidecar_path.read_text(encoding="utf-8"))
                bundle = self.incoming_dir / str(item.get("file", ""))
                if bundle.is_file() and not item.get("imported"):
                    item["bundle_path"] = str(bundle)
                    result.append(item)
            except (OSError, ValueError, TypeError):
                continue
        return sorted(result, key=lambda item: str(item.get("received_at", "")), reverse=True)


def import_incoming_bundle(
    bundle_path: str | Path,
    storage_root: str | Path,
    existing_repositories: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate an incoming Git bundle, then create or safely extend a local mirror.

    Incoming refs are namespaced under refs/gitarchive/peers/<peer>/<timestamp>/;
    existing local branches and tags are never overwritten by a peer import.
    """
    bundle = Path(bundle_path).resolve()
    sidecar_path = bundle.with_suffix(".json")
    if not bundle.is_file() or not sidecar_path.is_file():
        raise FileNotFoundError(tr("Полученный bundle или его метаданные не найдены."))
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    expected_size = int(sidecar.get("size", 0))
    expected_digest = str(sidecar.get("sha256", ""))
    if not 0 < expected_size <= MAX_BUNDLE_BYTES or bundle.stat().st_size != expected_size:
        raise ValueError(tr("Размер входящего Git bundle не совпадает с метаданными."))
    digest = hashlib.sha256()
    with bundle.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if not hmac.compare_digest(digest.hexdigest(), expected_digest):
        raise ValueError(tr("SHA-256 входящего Git bundle не совпадает с метаданными."))
    metadata = sidecar.get("repository")
    if not isinstance(metadata, dict):
        raise ValueError(tr("Метаданные входящего репозитория некорректны."))
    url, namespace, name, host, provider = parse_git_url(str(metadata.get("url", "")))
    if (str(metadata.get("namespace")) != namespace or
            str(metadata.get("name")) != name or
            str(metadata.get("host")) != host):
        raise ValueError(tr("Git URL и метаданные репозитория не совпадают."))
    peer_id = str(uuid.UUID(str(sidecar.get("source_peer_id"))))
    peer_slug = peer_id.replace("-", "")
    timestamp = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    target = archive_path(Path(storage_root).expanduser(), host, namespace, name, "folder")
    target.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(target.parent).free < expected_size + 64 * 1024 * 1024:
        raise OSError(tr("Недостаточно места для временного зеркала: оставь место ещё как минимум под размер bundle и запас 64 МБ."))

    temporary_mirror = target.parent / f".{target.name}.incoming-{uuid.uuid4().hex}"
    try:
        _require_process_success(["git", "bundle", "verify", str(bundle)], timeout=900)
        _require_process_success(["git", "clone", "--mirror", str(bundle), str(temporary_mirror)], timeout=7200)
        _require_process_success([
            "git", "--git-dir", str(temporary_mirror), "fsck", "--full", "--strict"
        ], timeout=7200)
        if target.exists():
            if not (target.is_dir() and (target / "HEAD").is_file() and (target / "objects").is_dir()):
                raise FileExistsError(tr("Путь архива уже занят, но это не bare Git-зеркало."))
            refspec = f"+refs/*:refs/gitarchive/peers/{peer_slug}/{timestamp}/*"
            _require_process_success([
                "git", "--git-dir", str(target), "fetch", "--no-tags", "--no-write-fetch-head",
                str(temporary_mirror), refspec,
            ], timeout=7200)
            shutil.rmtree(temporary_mirror, ignore_errors=True)
            local_path = target
            status = tr("Получено по LAN · refs добавлены отдельно")
        else:
            _require_process_success([
                "git", "--git-dir", str(temporary_mirror), "config", "remote.origin.url", url
            ], timeout=30)
            os.replace(temporary_mirror, target)
            local_path = target
            status = tr("Получено по LAN")
    except Exception:
        shutil.rmtree(temporary_mirror, ignore_errors=True)
        raise

    repo = next((
        item for item in existing_repositories
        if item.get("host") == host and item.get("namespace") == namespace and item.get("name") == name
    ), None)
    entry = dict(repo or {})
    entry.update({
        "id": str(entry.get("id") or uuid.uuid4()),
        "url": url,
        "namespace": namespace,
        "name": name,
        "host": host,
        "provider": provider,
        "local_path": str(local_path),
        "storage_format": "folder",
        "status": status,
        "last_error": "",
        "origin_peer_id": peer_id,
        "origin_transport": str(sidecar.get("transport") or "local-network"),
        "last_peer_transfer": str(sidecar.get("received_at") or ""),
    })
    sidecar["imported"] = True
    sidecar["imported_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    temporary_sidecar = sidecar_path.with_suffix(".json.tmp")
    temporary_sidecar.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_sidecar.replace(sidecar_path)
    return entry
