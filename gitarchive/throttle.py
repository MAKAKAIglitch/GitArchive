"""Loopback HTTPS CONNECT proxy with a shared aggregate download-rate limit."""
from __future__ import annotations

import select
import socket
import socketserver
import ssl
import threading
import time
from urllib.parse import urlsplit


class ProxyConfigurationError(ValueError):
    """An upstream proxy cannot be used without changing its semantics."""


def normalize_download_limit(value: object, default: int = 3) -> int:
    """Return an integer MB/s limit in the supported 0..100 range; zero means unlimited."""
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(0, min(100, parsed))


class SharedDownloadLimiter:
    """Thread-safe token bucket shared across all active HTTPS tunnels."""

    def __init__(self, bytes_per_second: int) -> None:
        self._lock = threading.Lock()
        self._rate = 0
        self._capacity = 0.0
        self._tokens = 0.0
        self._updated = time.monotonic()
        self.set_limit(bytes_per_second)

    def set_limit(self, bytes_per_second: int) -> None:
        rate = max(0, int(bytes_per_second))
        with self._lock:
            self._rate = rate
            self._capacity = min(64 * 1024, max(1.0, rate * 0.1)) if rate else 0.0
            self._tokens = self._capacity
            self._updated = time.monotonic()

    def take(self, maximum: int) -> int:
        maximum = max(1, int(maximum))
        while True:
            with self._lock:
                if self._rate <= 0:
                    return maximum
                now = time.monotonic()
                elapsed = max(0.0, now - self._updated)
                self._updated = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                allowed = min(maximum, int(self._tokens))
                if allowed:
                    self._tokens -= allowed
                    return allowed
                wait = max(0.001, (1.0 - self._tokens) / self._rate)
            time.sleep(min(wait, 0.1))


class _ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def __init__(self, owner: "ThrottledHttpProxy") -> None:
        self.owner = owner
        super().__init__(("127.0.0.1", 0), _ConnectHandler)


class _ConnectHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client: socket.socket = self.request
        upstream: socket.socket | None = None
        established = False
        try:
            client.settimeout(15)
            request = self._read_headers(client)
            first_line = request.split(b"\r\n", 1)[0].decode("ascii", errors="strict")
            method, target, _version = first_line.split(" ", 2)
            if method.upper() != "CONNECT":
                self._reply(client, 405, "Only HTTPS CONNECT is supported")
                return
            host, port = self._parse_target(target)
            upstream = self.server.owner._open_tunnel(host, port)  # type: ignore[attr-defined]
            client.sendall(b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: GitArchive\r\n\r\n")
            established = True
            client.settimeout(None)
            upstream.settimeout(None)
            self.server.owner._relay(client, upstream)  # type: ignore[attr-defined]
        except (OSError, ValueError, UnicodeError, TimeoutError) as error:
            if not established:
                try:
                    self._reply(client, 502, "GitArchive local proxy could not connect")
                except OSError:
                    pass
            self.server.owner._record_error(str(error))  # type: ignore[attr-defined]
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass

    @staticmethod
    def _read_headers(sock: socket.socket) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Client closed before sending CONNECT headers")
            data.extend(chunk)
            if len(data) > 16 * 1024:
                raise ValueError("CONNECT headers exceed 16 KiB")
        return bytes(data)

    @staticmethod
    def _parse_target(target: str) -> tuple[str, int]:
        if target.startswith("["):
            closing = target.find("]")
            if closing < 0 or target[closing + 1:closing + 2] != ":":
                raise ValueError("Invalid IPv6 CONNECT target")
            host, port_text = target[1:closing], target[closing + 2:]
        else:
            host, separator, port_text = target.rpartition(":")
            if not separator:
                raise ValueError("CONNECT target must contain a port")
        port = int(port_text)
        if not host or not 1 <= port <= 65535:
            raise ValueError("Invalid CONNECT host or port")
        return host, port

    @staticmethod
    def _reply(sock: socket.socket, status: int, message: str) -> None:
        reason = {405: "Method Not Allowed", 502: "Bad Gateway"}.get(status, "Error")
        body = message.encode("utf-8", errors="replace")
        sock.sendall(
            f"HTTP/1.1 {status} {reason}\r\nConnection: close\r\nContent-Length: {len(body)}\r\n\r\n".encode("ascii")
            + body
        )


class ThrottledHttpProxy:
    """Loopback-only HTTPS tunnel proxy; all downloads share one byte budget."""

    def __init__(self, bytes_per_second: int, no_proxy: str | None = None) -> None:
        self.limiter = SharedDownloadLimiter(bytes_per_second)
        self._settings_lock = threading.Lock()
        self._upstream_proxy: str | None = None
        self._routes: dict[tuple[str, int], str | None] = {}
        self._no_proxy = no_proxy or ""
        self._server: _ProxyServer | None = None
        self._thread: threading.Thread | None = None
        self._last_error = ""

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("GitArchive bandwidth proxy has not been started")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def last_error(self) -> str:
        return self._last_error

    def start(self) -> str:
        if self._server is not None:
            return self.url
        server = _ProxyServer(self)
        thread = threading.Thread(target=server.serve_forever, name="GitArchiveBandwidthProxy", daemon=True)
        self._server, self._thread = server, thread
        thread.start()
        return self.url

    def set_limit(self, bytes_per_second: int) -> None:
        self.limiter.set_limit(bytes_per_second)

    def set_route(self, host: str, port: int, upstream_proxy: str | None) -> None:
        """Set a host-specific upstream proxy; None means connect directly."""
        normalized_host = str(host).strip("[]").lower().rstrip(".")
        normalized_port = int(port)
        if not normalized_host or not 1 <= normalized_port <= 65535:
            raise ProxyConfigurationError("Invalid HTTPS destination")
        if upstream_proxy:
            self._parse_proxy(upstream_proxy)
        with self._settings_lock:
            self._routes[(normalized_host, normalized_port)] = upstream_proxy

    def set_upstream_proxy(self, proxy: str | None, no_proxy: str | None = None) -> None:
        if proxy:
            self._parse_proxy(proxy)
        with self._settings_lock:
            self._upstream_proxy = proxy
            if no_proxy is not None:
                self._no_proxy = no_proxy

    def close(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)

    def _record_error(self, message: str) -> None:
        self._last_error = str(message)[:500]

    def _settings(self, host: str, port: int) -> tuple[str | None, str]:
        key = (host.strip("[]").lower().rstrip("."), int(port))
        with self._settings_lock:
            return self._routes.get(key, self._upstream_proxy), self._no_proxy

    def _open_tunnel(self, host: str, port: int) -> socket.socket:
        upstream_proxy, no_proxy = self._settings(host, port)
        if self._matches_no_proxy(host, port, no_proxy) or not upstream_proxy:
            return socket.create_connection((host, port), timeout=60)
        scheme, proxy_host, proxy_port = self._parse_proxy(upstream_proxy)
        connection = socket.create_connection((proxy_host, proxy_port), timeout=60)
        try:
            if scheme == "https":
                context = ssl.create_default_context()
                connection = context.wrap_socket(connection, server_hostname=proxy_host)
            if scheme in {"http", "https"}:
                self._http_connect(connection, host, port)
            elif scheme == "socks5h":
                self._socks5_connect(connection, host, port)
            elif scheme == "socks4a":
                self._socks4a_connect(connection, host, port)
            else:
                raise ProxyConfigurationError(f"Unsupported upstream proxy scheme: {scheme}")
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _parse_proxy(proxy: str) -> tuple[str, str, int]:
        parsed = urlsplit(str(proxy).strip())
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https", "socks4a", "socks5h"}:
            raise ProxyConfigurationError("Unsupported upstream proxy URL scheme")
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ProxyConfigurationError("Upstream proxy needs a host and cannot contain credentials")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ProxyConfigurationError("Upstream proxy URL cannot contain a path, query, or fragment")
        default_port = 443 if scheme == "https" else 1080 if scheme.startswith("socks") else 80
        try:
            port = parsed.port or default_port
        except ValueError as error:
            raise ProxyConfigurationError("Invalid upstream proxy port") from error
        if not 1 <= port <= 65535:
            raise ProxyConfigurationError("Invalid upstream proxy port")
        return scheme, parsed.hostname, port

    @staticmethod
    def _http_connect(connection: socket.socket, host: str, port: int) -> None:
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        connection.sendall(
            f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nProxy-Connection: Keep-Alive\r\n\r\n".encode("ascii")
        )
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = connection.recv(4096)
            if not chunk:
                raise ConnectionError("Upstream HTTP proxy closed during CONNECT")
            response.extend(chunk)
            if len(response) > 16 * 1024:
                raise ConnectionError("Upstream proxy response headers are too large")
        status_line = bytes(response).split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        fields = status_line.split()
        if len(fields) < 2 or fields[1] != "200":
            raise ConnectionError(f"Upstream proxy rejected CONNECT: {status_line[:180]}")

    @staticmethod
    def _recv_exact(connection: socket.socket, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            chunk = connection.recv(size - len(result))
            if not chunk:
                raise ConnectionError("Upstream SOCKS proxy closed the connection")
            result.extend(chunk)
        return bytes(result)

    @classmethod
    def _socks5_connect(cls, connection: socket.socket, host: str, port: int) -> None:
        connection.sendall(b"\x05\x01\x00")
        if cls._recv_exact(connection, 2) != b"\x05\x00":
            raise ConnectionError("SOCKS5 proxy does not support no-authentication mode")
        hostname = host.encode("idna")
        if len(hostname) > 255:
            raise ValueError("Target hostname is too long for SOCKS5")
        connection.sendall(b"\x05\x01\x00\x03" + bytes([len(hostname)]) + hostname + port.to_bytes(2, "big"))
        header = cls._recv_exact(connection, 4)
        if header[0] != 5 or header[1] != 0:
            raise ConnectionError(f"SOCKS5 proxy connection failed (code {header[1]})")
        if header[3] == 1:
            address_size = 4
        elif header[3] == 3:
            address_size = cls._recv_exact(connection, 1)[0]
        elif header[3] == 4:
            address_size = 16
        else:
            raise ConnectionError("SOCKS5 proxy returned an invalid address type")
        cls._recv_exact(connection, address_size + 2)

    @classmethod
    def _socks4a_connect(cls, connection: socket.socket, host: str, port: int) -> None:
        hostname = host.encode("idna")
        if len(hostname) > 255:
            raise ValueError("Target hostname is too long for SOCKS4a")
        connection.sendall(b"\x04\x01" + port.to_bytes(2, "big") + b"\x00\x00\x00\x01\x00" + hostname + b"\x00")
        response = cls._recv_exact(connection, 8)
        if response[1] != 0x5A:
            raise ConnectionError(f"SOCKS4a proxy connection failed (code {response[1]})")

    @staticmethod
    def _matches_no_proxy(host: str, port: int, rules: str) -> bool:
        host = host.strip("[]").lower().rstrip(".")
        for raw in (rules or "").split(","):
            rule = raw.strip().lower()
            if not rule:
                continue
            if rule == "*":
                return True
            rule_host, separator, rule_port = rule.rpartition(":")
            if separator and rule_host and not rule_host.endswith("]"):
                if rule_port.isdigit() and port != int(rule_port):
                    continue
                rule = rule_host
            elif rule.startswith("[") and "]:" in rule:
                rule_host, _, rule_port = rule[1:].partition("]:")
                if rule_port.isdigit() and port != int(rule_port):
                    continue
                rule = rule_host
            rule = rule.strip("[]").lstrip(".").rstrip(".")
            if host == rule or host.endswith("." + rule):
                return True
        return False

    def _relay(self, client: socket.socket, remote: socket.socket) -> None:
        closed: set[socket.socket] = set()
        while len(closed) < 2:
            sockets = [sock for sock in (client, remote) if sock not in closed]
            ready, _writable, exceptional = select.select(sockets, [], sockets, 0.5)
            if exceptional:
                break
            for source in ready:
                data = source.recv(64 * 1024)
                destination = remote if source is client else client
                if not data:
                    closed.add(source)
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                if source is remote:
                    offset = 0
                    while offset < len(data):
                        allowed = self.limiter.take(len(data) - offset)
                        destination.sendall(data[offset:offset + allowed])
                        offset += allowed
                else:
                    destination.sendall(data)
