from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QProcess, QProcessEnvironment, QLockFile, QRunnable, QStandardPaths, QThreadPool, Qt, QTimer, Signal, QEasingCurve, QPropertyAnimation
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap, QTextCursor
from .repository import archive_path, find_7zip, folder_size, human_size, parse_git_url
from .i18n import tr
from .throttle import ProxyConfigurationError, ThrottledHttpProxy, normalize_download_limit
from urllib.parse import urlsplit
from .priority import (
    apply_memory_priority_to_handle,
    normalize_cpu_priority,
    normalize_memory_priority,
    process_creation_flags,
)
from .lan import DEFAULT_PORT, LanPeerService, discover_local_peers, import_incoming_bundle

from PySide6.QtSvg import QSvgRenderer

from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QSpinBox,
    QScrollArea, QStackedWidget, QStyle, QSystemTrayIcon, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)

APP_NAME = "GitArchive"
GIB = 1024 ** 3


_GIT_PROGRESS_PATTERN = re.compile(
    r"(Enumerating objects|Counting objects|Compressing objects|Receiving objects|Resolving deltas|Updating files):\s*(\d{1,3})%",
    re.IGNORECASE,
)
_GENERIC_PROGRESS_PATTERN = re.compile(r"(?<!\d)(\d{1,3})%(?=\s|[\r\n]|$)")


def parse_sync_progress(output: str) -> tuple[int, str] | None:
    """Extract progress from Git's phase output or 7-Zip's percentage lines."""
    matches = list(_GIT_PROGRESS_PATTERN.finditer(output))
    if matches:
        match = matches[-1]
        return min(100, int(match.group(2))), match.group(1).strip()
    generic = list(_GENERIC_PROGRESS_PATTERN.finditer(output))
    if generic:
        return min(100, int(generic[-1].group(1))), tr("Архивация / распаковка")
    return None


def resource_path(*parts: str) -> Path:
    """Resolve bundled resources in source and PyInstaller one-folder builds."""
    bundled_root = getattr(sys, "_MEIPASS", None)
    root = Path(bundled_root) if bundled_root else Path(__file__).resolve().parents[1]
    return root.joinpath(*parts)


def app_icon() -> QIcon:
    path = resource_path("assets", "GitArchive.svg")
    renderer = QSvgRenderer(str(path))
    if not renderer.isValid():
        return QIcon()
    pixmap = QPixmap(256, 256)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)


def repository_label(repo: dict) -> str:
    host = str(repo.get("host") or "github.com")
    namespace = str(repo.get("namespace") or repo.get("owner") or "")
    name = str(repo.get("name") or "")
    return f"{namespace}/{name}  ·  {host}"


def repository_storage_format(repo: dict) -> str:
    configured = str(repo.get("storage_format") or "").lower()
    if configured in {"folder", "zip", "7z"}:
        return configured
    suffix = Path(str(repo.get("local_path") or "")).suffix.lower()
    return {".zip": "zip", ".7z": "7z"}.get(suffix, "folder")


def data_dir() -> Path:
    value = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    path = Path(value) if value else Path.home() / "AppData" / "Local" / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def defaults() -> dict:
    return {
        "storage_path": str(Path.home() / "GitArchive"),
        "new_storage_format": "folder",
        "compression_level": "medium",
        "background_cpu_priority": "idle",
        "background_memory_priority": "very_low",
        "max_storage_gb": 100,
        "sync_interval_minutes": 10,
        "max_parallel": 3,
        "sync_download_limit_mbps": 3,
        "theme": "system",
        "close_to_tray": True,
        "start_with_windows": False,
        "proxy_enabled": False,
        "proxy_type": "SOCKS5",
        "proxy_host": "127.0.0.1",
        "proxy_port": 1080,
        "repositories": [],
    }


def set_windows_autostart(enabled: bool) -> None:
    """Register or remove the current app for the current Windows user only."""
    if sys.platform != "win32":
        if enabled:
            raise OSError(tr("Автозапуск Windows доступен только в Windows."))
        return
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            if getattr(sys, "frozen", False):
                command = subprocess.list2cmdline([sys.executable, "--background"])
            else:
                pythonw = Path(sys.executable).with_name("pythonw.exe")
                executable = pythonw if pythonw.is_file() else Path(sys.executable)
                command = subprocess.list2cmdline([str(executable), "-m", "gitarchive", "--background"])
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def proxy_url(config: dict) -> str | None:
    if not config.get("proxy_enabled"):
        return None
    schemes = {"HTTP": "http", "HTTPS": "https", "SOCKS4": "socks4a", "SOCKS5": "socks5h"}
    proxy_type = str(config.get("proxy_type", "SOCKS5")).upper()
    if proxy_type not in schemes:
        raise ValueError(tr("Неизвестный тип прокси."))
    host = str(config.get("proxy_host", "")).strip()
    if not host or any(char.isspace() or char in "/\\@?#" for char in host):
        raise ValueError(tr("Укажи корректный адрес прокси без логина и пароля."))
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = int(config.get("proxy_port", 0))
    if not 1 <= port <= 65535:
        raise ValueError(tr("Порт прокси должен быть от 1 до 65535."))
    return f"{schemes[proxy_type]}://{host}:{port}"


def background_process_environment(config: dict, throttle_proxy_url: str | None = None) -> QProcessEnvironment:
    """Pass resource and optional proxy preferences to the Rust background engine."""
    environment = QProcessEnvironment.systemEnvironment()
    environment.insert(
        "GITARCHIVE_CPU_PRIORITY",
        normalize_cpu_priority(config.get("background_cpu_priority", "idle")),
    )
    environment.insert(
        "GITARCHIVE_MEMORY_PRIORITY",
        normalize_memory_priority(config.get("background_memory_priority", "very_low")),
    )
    environment.remove("GITARCHIVE_HTTP_PROXY")
    if throttle_proxy_url:
        environment.insert("GITARCHIVE_HTTP_PROXY", throttle_proxy_url)
        # The local proxy applies NO_PROXY itself and chains to the configured
        # upstream proxy. Prevent libcurl from bypassing that local limiter.
        for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
            "NO_PROXY", "no_proxy",
        ):
            environment.remove(key)
    else:
        proxy = proxy_url(config)
        if proxy:
            try:
                config_count = max(0, int(environment.value("GIT_CONFIG_COUNT") or "0"))
            except ValueError:
                config_count = 0
            environment.insert("GIT_CONFIG_COUNT", str(config_count + 1))
            environment.insert(f"GIT_CONFIG_KEY_{config_count}", "http.proxy")
            environment.insert(f"GIT_CONFIG_VALUE_{config_count}", proxy)
    return environment


def load_config() -> dict:
    path = data_dir() / "config.json"
    if not path.exists():
        return defaults()
    try:
        config = defaults()
        saved = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            config.update(saved)
        if not isinstance(config.get("repositories"), list):
            config["repositories"] = []
        if str(config.get("new_storage_format", "folder")).lower() not in {"folder", "zip", "7z"}:
            config["new_storage_format"] = "folder"
        if str(config.get("compression_level", "medium")).lower() not in {"min", "medium", "max"}:
            config["compression_level"] = "medium"
        config["sync_download_limit_mbps"] = normalize_download_limit(
            config.get("sync_download_limit_mbps", 3)
        )
        config["background_cpu_priority"] = normalize_cpu_priority(
            config.get("background_cpu_priority", "idle")
        )
        config["background_memory_priority"] = normalize_memory_priority(
            config.get("background_memory_priority", "very_low")
        )
        return config
    except (OSError, ValueError):
        return defaults()


def save_config(config: dict) -> None:
    path = data_dir() / "config.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class GitCommandSignals(QObject):
    completed = Signal(int, str, str)


class GitCommand(QRunnable):
    """Run local Git inspection/export commands without blocking the UI thread."""

    def __init__(
        self,
        args: list[str],
        timeout: int = 120,
        cpu_priority: str = "idle",
        memory_priority: str = "very_low",
    ) -> None:
        super().__init__()
        self.args = args
        self.timeout = timeout
        self.cpu_priority = normalize_cpu_priority(cpu_priority)
        self.memory_priority = normalize_memory_priority(memory_priority)
        self.signals = GitCommandSignals()

    def run(self) -> None:
        process = None
        try:
            environment = os.environ.copy()
            environment["GITARCHIVE_CPU_PRIORITY"] = self.cpu_priority
            environment["GITARCHIVE_MEMORY_PRIORITY"] = self.memory_priority
            process = subprocess.Popen(
                self.args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                creationflags=process_creation_flags(self.cpu_priority),
            )
            if os.name == "nt":
                apply_memory_priority_to_handle(process._handle, self.memory_priority)
            try:
                stdout, stderr = process.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                raise subprocess.TimeoutExpired(self.args, self.timeout, output=stdout, stderr=stderr)

            if len(stdout) > 1_500_000:
                stdout = stdout[:1_500_000] + tr("\n\n[GitArchive] Вывод обрезан после 1,5 МБ. Полный коммит остаётся в локальном архиве.")
            self.signals.completed.emit(process.returncode, stdout, stderr)
        except (OSError, subprocess.TimeoutExpired) as error:
            self.signals.completed.emit(-1, "", str(error))


class CommitHistoryDialog(QDialog):
    def __init__(
        self,
        archive: Path,
        repo_name: str,
        pool: QThreadPool,
        parent=None,
        cpu_priority: str = "idle",
        memory_priority: str = "very_low",
    ) -> None:
        super().__init__(parent)
        self.archive = archive
        self.repo_name = repo_name
        self.pool = pool
        self.cpu_priority = normalize_cpu_priority(cpu_priority)
        self.memory_priority = normalize_memory_priority(memory_priority)
        self.workers: list[GitCommand] = []
        self.dialog_open = True
        self.cleanup_path: Path | None = None
        self.commits: dict[str, dict[str, str]] = {}
        self.selected_sha: str | None = None
        self.setWindowTitle(tr(f"История коммитов — {repo_name}"))
        self.resize(1050, 680)
        self.setMinimumSize(760, 480)

        layout = QVBoxLayout(self)
        header = QLabel(tr(f"История локального архива · {repo_name}"))
        header.setObjectName("Section")
        layout.addWidget(header)
        body = QHBoxLayout()
        self.commit_list = QListWidget()
        self.commit_list.setMinimumWidth(330)
        self.commit_list.currentItemChanged.connect(self.show_commit)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText(tr("Выбери коммит, чтобы посмотреть его описание и изменённые файлы."))
        body.addWidget(self.commit_list, 1)
        body.addWidget(self.details, 2)
        layout.addLayout(body, 1)
        actions = QHBoxLayout()
        self.download = QPushButton(tr("Скачать выбранный коммит (.zip)"))
        self.download.setObjectName("Accent")
        self.download.setEnabled(False)
        self.download.clicked.connect(self.export_commit)
        close = QPushButton(tr("Закрыть"))
        close.setObjectName("Secondary")
        close.clicked.connect(self.accept)
        actions.addWidget(self.download)
        actions.addStretch(1)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.load_history()

    def run_git(self, args: list[str], callback, timeout: int = 120) -> None:
        worker = GitCommand(
            args,
            timeout,
            cpu_priority=self.cpu_priority,
            memory_priority=self.memory_priority,
        )
        self.workers.append(worker)
        worker.signals.completed.connect(
            lambda code, stdout, stderr, w=worker: self._worker_done(w, callback, code, stdout, stderr)
        )
        self.pool.start(worker)

    def _worker_done(self, worker, callback, code: int, stdout: str, stderr: str) -> None:
        if worker in self.workers:
            self.workers.remove(worker)
        if self.dialog_open:
            callback(code, stdout, stderr)
        self._cleanup_when_idle()

    def request_cleanup(self, path: Path) -> None:
        # Keep the extracted mirror alive until every outstanding Git command
        # has stopped using it, even if the dialog is closed during a request.
        self.dialog_open = False
        self.cleanup_path = path
        self._cleanup_when_idle()

    def _cleanup_when_idle(self) -> None:
        if self.cleanup_path is not None and not self.workers:
            shutil.rmtree(self.cleanup_path, ignore_errors=True)
            self.cleanup_path = None

    def load_history(self) -> None:
        self.commit_list.clear()
        self.commit_list.addItem(tr("Загрузка истории…"))
        self.run_git([
            "git", "--git-dir", str(self.archive),
            "log", "-n", "250", "--date=iso-strict",
            "--format=%H%x1f%h%x1f%an%x1f%aI%x1f%s",
            "--all", "--decorate=short",
        ], self.history_loaded)

    def history_loaded(self, code: int, stdout: str, stderr: str) -> None:
        self.commit_list.clear()
        if code != 0:
            self.details.setPlainText(stderr or tr("Не удалось прочитать историю Git."))
            return
        for line in stdout.splitlines():
            parts = line.split("\x1f", 4)
            if len(parts) != 5:
                continue
            sha, short_sha, author, date, subject = parts
            if not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
                continue
            self.commits[sha] = {
                "short": short_sha, "author": author, "date": date, "subject": subject
            }
            item = QListWidgetItem(f"{subject}\n{short_sha} · {author} · {date}")
            item.setData(Qt.ItemDataRole.UserRole, sha)
            self.commit_list.addItem(item)
        if not self.commits:
            self.details.setPlainText(tr("В архиве пока нет коммитов."))
        elif self.commit_list.count():
            self.commit_list.setCurrentRow(0)

    def show_commit(self, current, _previous=None) -> None:
        if current is None:
            return
        sha = current.data(Qt.ItemDataRole.UserRole)
        if not sha:
            return
        self.selected_sha = str(sha)
        self.download.setEnabled(True)
        self.details.setPlainText(tr("Загрузка сведений о коммите…"))
        requested_sha = self.selected_sha
        self.run_git([
            "git", "--git-dir", str(self.archive),
            "show", "--no-ext-diff", "--no-color", "--no-renames", "--unified=3",
            "--stat", "--patch", "--summary", "--format=fuller", requested_sha,
        ], lambda code, stdout, stderr: self.commit_loaded(requested_sha, code, stdout, stderr))

    def commit_loaded(self, requested_sha: str, code: int, stdout: str, stderr: str) -> None:
        if requested_sha != self.selected_sha:
            return
        self.details.setPlainText(stdout if code == 0 else (stderr or tr("Не удалось прочитать коммит.")))

    def export_commit(self) -> None:
        sha = self.selected_sha
        if not sha or not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            return
        destination, _ = QFileDialog.getSaveFileName(
            self, tr("Скачать коммит"), f"{self.repo_name}-{sha[:12]}.zip", tr("ZIP-архив (*.zip)")
        )
        if not destination:
            return
        if not destination.lower().endswith(".zip"):
            destination += ".zip"
        self.download.setEnabled(False)
        self.details.append(tr("\n\nСоздание ZIP-архива…"))
        self.run_git([
            "git", "--git-dir", str(self.archive),
            "archive", "--format=zip", f"--output={destination}", sha,
        ], lambda code, out, err: self.export_finished(destination, code, out, err), timeout=3600)

    def export_finished(self, destination: str, code: int, stdout: str, stderr: str) -> None:
        self.download.setEnabled(bool(self.selected_sha))
        if code == 0 and Path(destination).is_file():
            self.details.append(tr(f"\nГотово: {destination}"))
            QMessageBox.information(self, tr("Коммит сохранён"), tr(f"Архив коммита создан:\n{destination}"))
        else:
            self.details.append(tr(f"\nОшибка экспорта: {stderr or stdout}"))


class LanTaskSignals(QObject):
    completed = Signal(object, object)
    failed = Signal(object, str)


class LanTask(QRunnable):
    def __init__(self, callback, *args) -> None:
        super().__init__()
        self.callback = callback
        self.args = args
        self.signals = LanTaskSignals()

    def run(self) -> None:
        try:
            self.signals.completed.emit(self, self.callback(*self.args))
        except Exception as error:
            self.signals.failed.emit(self, str(error))


class LanPeerDialog(QDialog):
    """Small control panel for explicit LAN/overlay pairing and Git bundle exchange."""

    def __init__(self, window: "GitArchiveWindow") -> None:
        super().__init__(window)
        self.window = window
        self.service = window.lan_service
        self.tasks: set[LanTask] = set()
        self.task_callbacks: dict[LanTask, object] = {}
        self.expected_fingerprint: str | None = None
        self.setWindowTitle("LAN / ZeroTier — GitArchive")
        self.resize(860, 760)
        self.setMinimumSize(720, 620)

        layout = QVBoxLayout(self)
        intro = QLabel(
            tr("Обмен архивами между устройствами в одной локальной сети или виртуальной сети. " + "ZeroTier используется как обычный сетевой адаптер — отдельная интеграция не нужна.")
        )
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        layout.addWidget(intro)

        listener_row = QHBoxLayout()
        self.listen = QCheckBox(tr("Принимать подключения"))
        self.listen.setChecked(self.service.is_running)
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(self.service.port if self.service.is_running else DEFAULT_PORT)
        self.port.setFixedWidth(110)
        self.listener_status = QLabel(tr("Приём включён") if self.service.is_running else tr("Приём выключен"))
        self.listener_status.setObjectName("Muted")
        listener_row.addWidget(self.listen)
        listener_row.addWidget(QLabel(tr("Порт")))
        listener_row.addWidget(self.port)
        listener_row.addStretch(1)
        listener_row.addWidget(self.listener_status)
        layout.addLayout(listener_row)
        self.listen.toggled.connect(self.toggle_listener)

        self.local_addresses = QLabel(self._listener_address_text())
        self.local_addresses.setWordWrap(True)
        self.local_addresses.setObjectName("Muted")
        layout.addWidget(self.local_addresses)

        code_row = QHBoxLayout()
        self.local_code = QLineEdit()
        self.local_code.setReadOnly(True)
        self.local_code.setPlaceholderText(tr("Одноразовый код появится здесь"))
        make_code = QPushButton(tr("Создать код"))
        make_code.setObjectName("Secondary")
        make_code.clicked.connect(self.make_pairing_code)
        copy_code = QPushButton(tr("Копировать"))
        copy_code.setObjectName("Secondary")
        copy_code.clicked.connect(lambda: QApplication.clipboard().setText(self.local_code.text()))
        code_row.addWidget(self.local_code, 1)
        code_row.addWidget(make_code)
        code_row.addWidget(copy_code)
        layout.addLayout(code_row)

        discovery_row = QHBoxLayout()
        discover = QPushButton(tr("Найти устройства в локальных сетях"))
        discover.setObjectName("Accent")
        discover.clicked.connect(self.discover)
        discovery_row.addWidget(discover)
        discovery_row.addStretch(1)
        layout.addLayout(discovery_row)

        self.discovered = QListWidget()
        self.discovered.setMaximumHeight(115)
        self.discovered.itemClicked.connect(self.select_discovered)
        layout.addWidget(self.discovered)

        pair_form = QFormLayout()
        self.remote_address = QLineEdit()
        self.remote_address.setPlaceholderText(tr("IP найденного устройства, например 192.168.1.20 или 10.x.x.x"))
        self.remote_port = QSpinBox()
        self.remote_port.setRange(1024, 65535)
        self.remote_port.setValue(DEFAULT_PORT)
        self.remote_code = QLineEdit()
        self.remote_code.setEchoMode(QLineEdit.EchoMode.Password)
        self.remote_code.setPlaceholderText(tr("Одноразовый код с принимающего устройства"))
        pair_form.addRow(tr("Адрес устройства"), self.remote_address)
        pair_form.addRow(tr("Порт"), self.remote_port)
        pair_form.addRow(tr("Код сопряжения"), self.remote_code)
        layout.addLayout(pair_form)
        pair_row = QHBoxLayout()
        pair_button = QPushButton(tr("Сопрячь устройство"))
        pair_button.setObjectName("Accent")
        pair_button.clicked.connect(self.pair)
        pair_row.addWidget(pair_button)
        pair_row.addStretch(1)
        layout.addLayout(pair_row)

        layout.addWidget(QLabel(tr("Сопряжённые устройства")))
        self.paired = QListWidget()
        self.paired.setMaximumHeight(105)
        self.paired.currentItemChanged.connect(self.update_send_state)
        layout.addWidget(self.paired)
        revoke_row = QHBoxLayout()
        self.revoke_button = QPushButton(tr("Удалить доверие к выбранному устройству"))
        self.revoke_button.setObjectName("Secondary")
        self.revoke_button.clicked.connect(self.forget_selected_peer)
        revoke_row.addWidget(self.revoke_button)
        revoke_row.addStretch(1)
        layout.addLayout(revoke_row)

        send_row = QHBoxLayout()
        self.repository = QComboBox()
        self.repository.setMinimumWidth(260)
        self.send_button = QPushButton(tr("Передать выбранный архив"))
        self.send_button.setObjectName("Accent")
        self.send_button.clicked.connect(self.send_selected)
        send_row.addWidget(self.repository, 1)
        send_row.addWidget(self.send_button)
        layout.addLayout(send_row)
        format_note = QLabel(
            tr("В первой версии передача поддерживает папки .git. ZIP/7z можно будет подключить " + "после отдельного этапа распаковки и упаковки; исходные сжатые архивы не изменяются.")
        )
        format_note.setWordWrap(True)
        format_note.setObjectName("Muted")
        layout.addWidget(format_note)

        layout.addWidget(QLabel(tr("Полученные архивы — сначала проверяются, затем импортируются вручную")))
        self.incoming = QListWidget()
        self.incoming.setMaximumHeight(125)
        layout.addWidget(self.incoming)
        import_row = QHBoxLayout()
        import_button = QPushButton(tr("Проверить и импортировать"))
        import_button.setObjectName("Accent")
        import_button.clicked.connect(self.import_selected)
        refresh_button = QPushButton(tr("Обновить"))
        refresh_button.setObjectName("Secondary")
        refresh_button.clicked.connect(self.refresh_lists)
        import_row.addWidget(import_button)
        import_row.addWidget(refresh_button)
        import_row.addStretch(1)
        layout.addLayout(import_row)

        self.status = QLabel(tr("Обнаружение работает только по нажатию кнопки."))
        self.status.setWordWrap(True)
        self.status.setObjectName("Muted")
        layout.addWidget(self.status)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_button = QPushButton(tr("Закрыть"))
        close_button.setObjectName("Secondary")
        close_button.clicked.connect(self.accept)
        close_row.addWidget(close_button)
        layout.addLayout(close_row)

        self.refresh_lists()
        self.incoming_timer = QTimer(self)
        self.incoming_timer.setInterval(2500)
        self.incoming_timer.timeout.connect(self.refresh_incoming)
        self.incoming_timer.start()

    def _listener_address_text(self) -> str:
        if not self.service.is_running:
            return tr("Приём выключен. Включи его только на тех устройствах, которые должны получать архивы.")
        return tr("Адреса приёма: ") + ", ".join(
            f"{address}:{self.service.port}" for address in self.service.local_addresses()
        )

    def toggle_listener(self, enabled: bool) -> None:
        try:
            if enabled:
                addresses = self.service.start(self.port.value())
                self.listener_status.setText(tr("Приём включён"))
                self.port.setEnabled(False)
                self.status.setText(tr("GitArchive слушает только частные IPv4-адреса сетевых интерфейсов."))
                self.local_addresses.setText(tr("Адреса приёма: ") + ", ".join(
                    f"{address}:{self.service.port}" for address in addresses
                ))
            else:
                self.service.stop()
                self.local_code.clear()
                self.listener_status.setText(tr("Приём выключен"))
                self.port.setEnabled(True)
                self.local_addresses.setText(self._listener_address_text())
                self.status.setText(tr("Приём подключений остановлен."))
        except Exception as error:
            self.listen.blockSignals(True)
            self.listen.setChecked(False)
            self.listen.blockSignals(False)
            self.port.setEnabled(True)
            QMessageBox.warning(self, tr("LAN-приём не запущен"), str(error))

    def make_pairing_code(self) -> None:
        try:
            code = self.service.issue_pairing_code()
        except Exception as error:
            QMessageBox.warning(self, tr("Не удалось создать код"), str(error))
            return
        self.local_code.setText(code)
        QApplication.clipboard().setText(code)
        self.status.setText(tr("Код скопирован. Он действует 10 минут и используется один раз."))

    def start_task(self, callback, done, *args) -> None:
        task = LanTask(callback, *args)
        self.tasks.add(task)
        self.task_callbacks[task] = done
        task.signals.completed.connect(self._task_completed)
        task.signals.failed.connect(self._task_failed)
        self.window.thread_pool.start(task)

    def _task_completed(self, task: LanTask, result) -> None:
        self.tasks.discard(task)
        callback = self.task_callbacks.pop(task, None)
        if callback:
            callback(result)

    def _task_failed(self, task: LanTask, message: str) -> None:
        self.tasks.discard(task)
        self.task_callbacks.pop(task, None)
        self.status.setText(tr("Ошибка: ") + message)
        QMessageBox.warning(self, "LAN / ZeroTier", message)

    def discover(self) -> None:
        self.discovered.clear()
        self.status.setText(tr("Идёт поиск по подключённым частным IPv4-подсетям…"))
        self.start_task(discover_local_peers, self.discovery_finished, self.remote_port.value())

    def discovery_finished(self, peers: list[dict[str, str]]) -> None:
        self.discovered.clear()
        for peer in peers:
            self.service.refresh_peer_endpoint(
                peer["peer_id"], peer["address"], int(peer["port"]), peer["fingerprint"]
            )
            item = QListWidgetItem(
                f"{peer['address']}:{peer['port']}  ·  {peer['peer_id'][:8]}  ·  SHA-256 {peer['fingerprint'][:16]}…"
            )
            item.setData(Qt.ItemDataRole.UserRole, peer)
            self.discovered.addItem(item)
        self.refresh_lists()
        self.status.setText(
            tr(f"Найдено устройств: {len(peers)}. Репозитории и их названия при поиске не передаются.")
        )

    def select_discovered(self, item: QListWidgetItem) -> None:
        peer = item.data(Qt.ItemDataRole.UserRole) or {}
        self.remote_address.setText(str(peer.get("address", "")))
        self.remote_port.setValue(int(peer.get("port", DEFAULT_PORT)))
        self.expected_fingerprint = str(peer.get("fingerprint", "")) or None

    def pair(self) -> None:
        address = self.remote_address.text().strip()
        code = self.remote_code.text().strip()
        if not address or not code:
            QMessageBox.information(self, tr("Сопряжение"), tr("Укажи IP-адрес и одноразовый код с принимающего устройства."))
            return
        port = self.remote_port.value()
        expected = self.expected_fingerprint
        self.status.setText(tr("Проверяется сертификат и выполняется сопряжение…"))
        self.start_task(
            self.service.pair_with_peer,
            self.pair_finished,
            address, port, code, expected,
        )

    def pair_finished(self, peer: dict[str, Any]) -> None:
        self.remote_code.clear()
        self.status.setText(
            tr(f"Устройство {peer['peer_id']} сопряжено. SHA-256: {peer['fingerprint']}")
        )
        self.refresh_lists()

    def refresh_lists(self) -> None:
        selected = self.paired.currentItem()
        selected_id = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self.paired.clear()
        for peer in self.service.peers():
            endpoint = f"{peer['address']}:{peer['port']}" if peer.get("address") else tr("адрес не сохранён")
            item = QListWidgetItem(
                f"{peer['peer_id'][:8]}  ·  {endpoint}  ·  {peer['fingerprint'][:16]}…"
            )
            item.setData(Qt.ItemDataRole.UserRole, peer["peer_id"])
            self.paired.addItem(item)
            if peer["peer_id"] == selected_id:
                self.paired.setCurrentItem(item)
        self.repository.clear()
        for repo in self.window.config.get("repositories", []):
            if repository_storage_format(repo) == "folder":
                self.repository.addItem(repository_label(repo), repo.get("id"))
        self.refresh_incoming()
        self.update_send_state()

    def forget_selected_peer(self) -> None:
        item = self.paired.currentItem()
        if not item:
            return
        peer_id = str(item.data(Qt.ItemDataRole.UserRole))
        answer = QMessageBox.question(
            self,
            tr("Удалить доверенное устройство"),
            tr("GitArchive перестанет принимать передачи с этого устройства и отправлять ему архивы. " + "Локальные репозитории не удаляются. Для повторного обмена потребуется новое сопряжение."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.service.forget_peer(peer_id)
        self.refresh_lists()
        self.status.setText(tr("Доверие к устройству отозвано."))

    def update_send_state(self, *_args) -> None:
        has_peer = self.paired.currentItem() is not None
        has_repo = self.repository.currentData() is not None
        self.send_button.setEnabled(has_peer and has_repo)
        self.revoke_button.setEnabled(has_peer)

    def send_selected(self) -> None:
        item = self.paired.currentItem()
        repo_id = self.repository.currentData()
        peer_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        repo = next((entry for entry in self.window.config["repositories"] if entry.get("id") == repo_id), None)
        if not peer_id or not repo:
            return
        self.status.setText(tr(f"Создаётся Git bundle и отправляется {repository_label(repo)}…"))
        self.start_task(self.service.send_bundle, self.send_finished, peer_id, dict(repo))

    def send_finished(self, result: dict[str, Any]) -> None:
        self.status.setText(
            tr(
                f"Передача завершена: {result['repository']['namespace']}/{result['repository']['name']} "
                f"({human_size(int(result['size']))})."
            )
        )

    def refresh_incoming(self) -> None:
        selected = self.incoming.currentItem()
        selected_path = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self.incoming.clear()
        for item in self.service.list_incoming():
            repo = item.get("repository") or {}
            text = tr(
                f"{repo.get('namespace', '')}/{repo.get('name', 'неизвестный репозиторий')}  ·  "
                f"{human_size(int(item.get('size', 0)))}  ·  {item.get('received_at', '')}"
            )
            row = QListWidgetItem(text)
            row.setData(Qt.ItemDataRole.UserRole, item["bundle_path"])
            self.incoming.addItem(row)
            if item["bundle_path"] == selected_path:
                self.incoming.setCurrentItem(row)

    def import_selected(self) -> None:
        item = self.incoming.currentItem()
        if not item:
            QMessageBox.information(self, tr("Входящие архивы"), tr("Выбери полученный bundle."))
            return
        path = str(item.data(Qt.ItemDataRole.UserRole))
        self.status.setText(tr("Проверяется Git bundle и целостность объектов…"))
        existing = [dict(repo) for repo in self.window.config.get("repositories", [])]
        self.start_task(
            import_incoming_bundle,
            self.import_finished,
            path,
            self.window.config["storage_path"],
            existing,
        )

    def import_finished(self, entry: dict[str, Any]) -> None:
        repositories = self.window.config.setdefault("repositories", [])
        index = next((
            i for i, current in enumerate(repositories)
            if current.get("host") == entry.get("host")
            and current.get("namespace") == entry.get("namespace")
            and current.get("name") == entry.get("name")
        ), None)
        if index is None:
            repositories.append(entry)
        else:
            repositories[index] = entry
        save_config(self.window.config)
        self.window.refresh()
        self.window.scan_sizes()
        self.window.log(tr(f"Импортировано из LAN: {repository_label(entry)}"))
        self.refresh_lists()
        self.status.setText(tr(f"Импортировано: {repository_label(entry)}. Локальные ветки не перезаписывались."))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if hasattr(self, "incoming_timer") and not self.incoming_timer.isActive():
            self.incoming_timer.start()

    def closeEvent(self, event) -> None:
        self.incoming_timer.stop()
        super().closeEvent(event)


class SizeSignals(QObject):
    completed = Signal(dict)


class SizeScan(QRunnable):
    def __init__(self, repositories: list[tuple[str, str]]) -> None:
        super().__init__()
        self.repositories = repositories
        self.signals = SizeSignals()

    def run(self) -> None:
        result = {
            repo_id: folder_size(Path(path))
            for repo_id, path in self.repositories
        }
        self.signals.completed.emit(result)


def windows_dark_theme() -> bool:
    if sys.platform != "win32":
        return True
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return int(value) == 0
    except OSError:
        return True


def windows_accent() -> str:
    if sys.platform != "win32":
        return "#60a5fa"
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM")
        value, _ = winreg.QueryValueEx(key, "ColorizationColor")
        winreg.CloseKey(key)
        # ColorizationColor is stored as ARGB (0xAARRGGBB).
        color = QColor((int(value) >> 16) & 255, (int(value) >> 8) & 255, int(value) & 255)
        if color.isValid():
            if color.lightness() > 160:
                color = color.darker(125)
            return color.name()
    except (OSError, ValueError, TypeError):
        pass
    return "#60a5fa"


def set_mica(window: QMainWindow, dark: bool) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = wintypes.HWND(int(window.winId()))
        dwm = ctypes.windll.dwmapi
        dark_value = wintypes.BOOL(dark)
        mica_value = wintypes.DWORD(2)  # DWMSBT_MAINWINDOW
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark_value), ctypes.sizeof(dark_value))
        dwm.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(mica_value), ctypes.sizeof(mica_value))
    except Exception:
        pass  # Keep the regular Windows frame if Mica is unavailable.


class GitArchiveWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.config = load_config()
        self.processes: dict[str, QProcess] = {}
        self.restore_process: QProcess | None = None
        self.restore_destination: Path | None = None
        self.queue: list[str] = []
        self.logs: list[str] = self.load_logs()
        self.pending_log_lines: list[str] = []
        self.log_flush_timer = QTimer(self)
        self.log_flush_timer.setSingleShot(True)
        self.log_flush_timer.setInterval(100)
        self.log_flush_timer.timeout.connect(self.flush_logs)
        self.size_cache: dict[str, int] = {}
        self.size_scan_running = False
        self.size_scan_ready = False
        self.size_worker: SizeScan | None = None
        self.thread_pool = QThreadPool.globalInstance()
        self.lan_service = LanPeerService(data_dir())
        self.lan_dialog: LanPeerDialog | None = None
        self.history_workers: list[GitCommand] = []
        self.seven_zip_path = find_7zip()
        self.download_proxy: ThrottledHttpProxy | None = None
        self._default_https_proxy_loaded = False
        self._default_https_proxy_cache: str | None = None
        self._https_proxy_cache: dict[tuple[str, str], str | None] = {}
        self._https_proxy_lookup_failed = False
        self.sync_batch_total = 0
        self.sync_batch_completed = 0
        self.sync_batch_errors = 0
        self.sync_batch_paused = False
        self.sync_progress: dict[str, dict[str, object]] = {}
        self.allow_exit = False
        self.dark = True
        self.accent = "#60a5fa"
        self.setWindowIcon(app_icon())
        self.setWindowTitle("GitArchive")
        self.resize(1180, 780)
        self.setMinimumSize(960, 650)
        self._build_shell()
        self._build_pages()
        self._build_tray()
        self.apply_theme()
        self.refresh()
        self.scan_sizes()

        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.sync_all)
        self.sync_timer.setInterval(max(1, int(self.config["sync_interval_minutes"])) * 60000)
        self.sync_timer.start()
        self.theme_timer = QTimer(self)
        self.theme_timer.timeout.connect(self.check_theme)
        self.theme_timer.start(5000)
        self.sync_watchdog_timer = QTimer(self)
        self.sync_watchdog_timer.timeout.connect(self.check_sync_watchdog)
        self.sync_watchdog_timer.start(30000)
        QTimer.singleShot(1000, self.sync_all)

    @property
    def core(self) -> Path:
        name = "gitarchive-core.exe" if sys.platform == "win32" else "gitarchive-core"
        if getattr(sys, "frozen", False):
            root = Path(sys.executable).resolve().parent
            return root / "rust-core" / name
        root = Path(__file__).resolve().parents[1]
        return root / "rust-core" / "target" / "release" / name

    def _build_shell(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = QFrame()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(220)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(14, 22, 14, 16)
        logo_row = QWidget()
        logo_layout = QHBoxLayout(logo_row)
        logo_layout.setContentsMargins(0, 0, 0, 0)
        logo_layout.setSpacing(9)
        logo_image = QLabel()
        logo_image.setFixedSize(32, 32)
        logo_image.setPixmap(app_icon().pixmap(32, 32))
        logo_layout.addWidget(logo_image)
        logo = QLabel("GitArchive")
        logo.setObjectName("Logo")
        logo.setFont(QFont("Segoe UI Variable", 17, QFont.Weight.DemiBold))
        logo_layout.addWidget(logo)
        logo_layout.addStretch(1)
        side.addWidget(logo_row)
        side.addSpacing(26)
        self.nav = []
        for index, title in enumerate((tr("Обзор"), tr("Репозитории"), tr("Журнал"), tr("Настройки"))):
            button = QPushButton(title)
            button.setObjectName("Nav")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, page=index: self.go(page))
            self.nav.append(button)
            side.addWidget(button)
        self.nav[0].setChecked(True)
        side.addStretch(1)

        sync_card = QFrame()
        sync_card.setObjectName("Card")
        sync_layout = QVBoxLayout(sync_card)
        sync_layout.setContentsMargins(10, 10, 10, 10)
        sync_layout.setSpacing(7)
        sync_title = QLabel(tr("СИНХРОНИЗАЦИЯ"))
        sync_title.setObjectName("Muted")
        self.sidebar_sync_label = QLabel(tr("Ожидание синхронизации"))
        self.sidebar_sync_label.setWordWrap(True)
        self.sidebar_sync_label.setStyleSheet("font-weight:600;")
        self.sidebar_sync_bar = QProgressBar()
        self.sidebar_sync_bar.setRange(0, 100)
        self.sidebar_sync_bar.setValue(0)
        self.sidebar_sync_bar.setTextVisible(False)
        self.sidebar_sync_bar.setFixedHeight(6)
        self.sidebar_progress_animation = QPropertyAnimation(self.sidebar_sync_bar, b"value", self)
        self.sidebar_progress_animation.setDuration(280)
        self.sidebar_progress_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.sidebar_progress_animation.finished.connect(self._hide_sidebar_progress_if_complete)
        self.sidebar_sync_detail = QLabel(tr("Добавь репозиторий"))
        self.sidebar_sync_detail.setObjectName("Muted")
        self.sidebar_sync_detail.setWordWrap(True)
        sync_layout.addWidget(sync_title)
        sync_layout.addWidget(self.sidebar_sync_label)
        sync_layout.addWidget(self.sidebar_sync_bar)
        sync_layout.addWidget(self.sidebar_sync_detail)
        self.sidebar_sync_card = sync_card
        side.addWidget(sync_card)
        sync_card.hide()
        side.addSpacing(12)

        footer = QLabel("LOCAL-FIRST ARCHIVER\nGit mirrors stay on this PC")
        footer.setObjectName("Muted")
        side.addWidget(footer)

        right = QWidget()
        body = QVBoxLayout(right)
        body.setContentsMargins(28, 24, 28, 20)
        body.setSpacing(16)
        top = QHBoxLayout()
        self.heading = QLabel(tr("Обзор"))
        self.heading.setObjectName("Heading")
        self.heading.setFont(QFont("Segoe UI Variable", 25, QFont.Weight.DemiBold))
        top.addWidget(self.heading)
        top.addStretch(1)
        sync = QPushButton(tr("⟳  Синхронизировать"))
        sync.setObjectName("Accent")
        sync.clicked.connect(self.sync_all)
        top.addWidget(sync)
        body.addLayout(top)
        self.pages = QStackedWidget()
        body.addWidget(self.pages, 1)
        layout.addWidget(self.sidebar)
        layout.addWidget(right, 1)

    def _build_pages(self) -> None:
        dashboard = QWidget()
        d = QVBoxLayout(dashboard)
        d.setContentsMargins(0, 0, 0, 0)
        cards = QGridLayout()
        cards.setSpacing(12)
        self.stats = {}
        for col, (key, title, initial, note) in enumerate((
            ("repos", tr("Репозитории"), "0", tr("В списке")),
            ("mirrors", tr("Локальные копии"), "0", "Git mirror"),
            ("size", tr("Занято"), tr("0 Б"), tr("Все архивы")),
            ("running", tr("Синхронизация"), "0", tr("Активных задач")),
        )):
            card = QFrame()
            card.setObjectName("Card")
            box = QVBoxLayout(card)
            box.setContentsMargins(17, 15, 17, 15)
            label = QLabel(title)
            label.setObjectName("Muted")
            value = QLabel(initial)
            value.setObjectName("Stat")
            note_label = QLabel(note)
            note_label.setObjectName("Muted")
            box.addWidget(label)
            box.addWidget(value)
            box.addWidget(note_label)
            cards.addWidget(card, 0, col)
            self.stats[key] = value
        d.addLayout(cards)
        bar = QHBoxLayout()
        label = QLabel(tr("Ваши проекты"))
        label.setObjectName("Section")
        bar.addWidget(label)
        bar.addStretch(1)
        add = QPushButton(tr("＋  Добавить репозиторий"))
        add.setObjectName("Secondary")
        add.clicked.connect(self.add_repository)
        bar.addWidget(add)
        d.addLayout(bar)
        self.dashboard_table = self.make_table()
        d.addWidget(self.dashboard_table, 1)
        self.hint = QLabel("")
        self.hint.setObjectName("Muted")
        self.hint.setWordWrap(True)
        d.addWidget(self.hint)
        self.pages.addWidget(dashboard)

        repositories = QWidget()
        r = QVBoxLayout(repositories)
        r.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        for text, name, callback in (
            (tr("＋  Добавить"), "Accent", self.add_repository),
            (tr("⟳  Обновить выбранный"), "Secondary", self.sync_selected),
            (tr("Открыть архив"), "Secondary", self.open_selected_folder),
            (tr("Восстановить копию"), "Secondary", self.restore_selected),
        ):
            button = QPushButton(text)
            button.setObjectName(name)
            button.clicked.connect(callback)
            bar.addWidget(button)
        lan_button = QPushButton("LAN / ZeroTier")
        lan_button.setObjectName("Secondary")
        lan_button.clicked.connect(self.open_lan_dialog)
        bar.addWidget(lan_button)
        bar.addStretch(1)
        r.addLayout(bar)
        self.repo_table = self.make_table()
        r.addWidget(self.repo_table, 1)
        self.pages.addWidget(repositories)

        activity = QWidget()
        a = QVBoxLayout(activity)
        a.setContentsMargins(0, 0, 0, 0)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setObjectName("Log")
        self.log_view.setPlaceholderText(tr("События синхронизации появятся здесь."))
        self.log_view.setPlainText("\n".join(self.logs))
        self.log_view.document().setMaximumBlockCount(1200)
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)
        a.addWidget(self.log_view)
        self.pages.addWidget(activity)

        settings = QWidget()
        settings_layout = QVBoxLayout(settings)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings_content = QWidget()
        s = QVBoxLayout(settings_content)
        s.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("Card")
        form = QFormLayout(card)
        form.setContentsMargins(22, 22, 22, 22)
        form.setVerticalSpacing(15)
        path_row = QHBoxLayout()
        self.storage_edit = QLineEdit(self.config["storage_path"])
        browse = QPushButton(tr("Обзор…"))
        browse.setObjectName("Secondary")
        browse.clicked.connect(self.choose_storage)
        path_row.addWidget(self.storage_edit, 1)
        path_row.addWidget(browse)
        path_widget = QWidget()
        path_widget.setLayout(path_row)
        form.addRow(tr("Папка новых архивов"), path_widget)
        self.limit = QSpinBox()
        self.limit.setRange(1, 100000)
        self.limit.setSuffix(tr(" ГБ"))
        self.limit.setValue(int(self.config["max_storage_gb"]))
        form.addRow(tr("Общий лимит"), self.limit)
        self.interval = QSpinBox()
        self.interval.setRange(1, 1440)
        self.interval.setSuffix(tr(" мин"))
        self.interval.setValue(int(self.config["sync_interval_minutes"]))
        form.addRow(tr("Интервал синхронизации"), self.interval)
        self.parallel = QSpinBox()
        self.parallel.setRange(1, 8)
        self.parallel.setValue(int(self.config["max_parallel"]))
        form.addRow(tr("Параллельные задачи"), self.parallel)
        self.download_limit = QSpinBox()
        self.download_limit.setRange(0, 100)
        self.download_limit.setSuffix(tr(" МБ/с"))
        self.download_limit.setSpecialValueText(tr("Без ограничений"))
        self.download_limit.setValue(normalize_download_limit(self.config.get("sync_download_limit_mbps", 3)))
        form.addRow(tr("Лимит загрузки HTTPS"), self.download_limit)
        download_limit_note = QLabel(
            tr("0 — без ограничений. Общий лимит для всех активных HTTPS-синхронизаций. SSH не ограничивается.")
        )
        download_limit_note.setObjectName("Muted")
        download_limit_note.setWordWrap(True)
        form.addRow("", download_limit_note)

        self.storage_format = QComboBox()
        self.storage_format.addItem(tr("Папка .git — без сжатия"), "folder")
        self.storage_format.addItem("ZIP (.zip)", "zip")
        self.storage_format.addItem("7z (.7z)", "7z")
        if not self.seven_zip_path:
            model = self.storage_format.model()
            for index in (1, 2):
                item = model.item(index) if hasattr(model, "item") else None
                if item:
                    item.setEnabled(False)
            self.storage_format.setToolTip(tr("Для ZIP и 7z нужен установленный 7-Zip с командной строкой. Перезапусти GitArchive после установки."))
        saved_format = str(self.config.get("new_storage_format", "folder")).lower()
        if saved_format not in {"folder", "zip", "7z"} or (saved_format != "folder" and not self.seven_zip_path):
            saved_format = "folder"
        self.storage_format.setCurrentIndex(max(0, self.storage_format.findData(saved_format)))
        form.addRow(tr("Формат новых архивов"), self.storage_format)

        self.compression_level = QComboBox()
        self.compression_level.addItem(tr("Минимум — быстрее"), "min")
        self.compression_level.addItem(tr("Среднее"), "medium")
        self.compression_level.addItem(tr("Максимум — медленнее"), "max")
        saved_level = str(self.config.get("compression_level", "medium")).lower()
        self.compression_level.setCurrentIndex(max(0, self.compression_level.findData(saved_level)))
        form.addRow(tr("Сила сжатия"), self.compression_level)
        self.compression_note = QLabel()
        self.compression_note.setObjectName("Muted")
        self.compression_note.setWordWrap(True)
        form.addRow("", self.compression_note)
        self.storage_format.currentIndexChanged.connect(self.update_storage_format_controls)
        self.update_storage_format_controls()

        self.cpu_priority = QComboBox()
        self.cpu_priority.addItem(tr("Минимальный — Idle"), "idle")
        self.cpu_priority.addItem(tr("Ниже обычного"), "below_normal")
        self.cpu_priority.addItem(tr("Обычный"), "normal")
        saved_cpu_priority = normalize_cpu_priority(self.config.get("background_cpu_priority", "idle"))
        self.cpu_priority.setCurrentIndex(max(0, self.cpu_priority.findData(saved_cpu_priority)))
        form.addRow(tr("Приоритет ЦП фоновых задач"), self.cpu_priority)

        self.memory_priority = QComboBox()
        self.memory_priority.addItem(tr("Минимальный"), "very_low")
        self.memory_priority.addItem(tr("Низкий"), "low")
        self.memory_priority.addItem(tr("Обычный"), "normal")
        saved_memory_priority = normalize_memory_priority(
            self.config.get("background_memory_priority", "very_low")
        )
        self.memory_priority.setCurrentIndex(max(0, self.memory_priority.findData(saved_memory_priority)))
        form.addRow(tr("Приоритет памяти фоновых задач"), self.memory_priority)

        priority_note = QLabel(
            tr("Настройки применяются к Rust-движку, Git и 7-Zip. Сам интерфейс остаётся с обычным приоритетом. " + "Приоритет памяти влияет на вытеснение страниц из ОЗУ, но не ограничивает объём выделенной памяти.")
        )
        priority_note.setObjectName("Muted")
        priority_note.setWordWrap(True)
        form.addRow("", priority_note)

        self.theme = QComboBox()
        for label, value in ((tr("Как в Windows"), "system"), (tr("Светлая"), "light"), (tr("Тёмная"), "dark")):
            self.theme.addItem(label, value)
        self.theme.setCurrentIndex(max(0, self.theme.findData(self.config["theme"])))
        form.addRow(tr("Тема"), self.theme)
        self.start_with_windows = QCheckBox(tr("Запускать вместе с Windows"))
        self.start_with_windows.setChecked(bool(self.config.get("start_with_windows", False)))
        form.addRow(tr("Автозапуск"), self.start_with_windows)
        self.close_to_tray = QCheckBox(tr("При закрытии сворачивать в системный трей"))
        self.close_to_tray.setChecked(bool(self.config.get("close_to_tray", True)))
        form.addRow(tr("Поведение окна"), self.close_to_tray)
        self.proxy_enabled = QCheckBox(tr("Использовать прокси для HTTPS Git-соединений"))
        self.proxy_enabled.setChecked(bool(self.config.get("proxy_enabled", False)))
        form.addRow(tr("Прокси"), self.proxy_enabled)
        self.proxy_type = QComboBox()
        for proxy_label in ("HTTP", "HTTPS", "SOCKS4", "SOCKS5"):
            self.proxy_type.addItem(proxy_label, proxy_label)
        self.proxy_type.setCurrentIndex(max(0, self.proxy_type.findData(self.config.get("proxy_type", "SOCKS5"))))
        form.addRow(tr("Тип прокси"), self.proxy_type)
        proxy_row = QHBoxLayout()
        self.proxy_host = QLineEdit(str(self.config.get("proxy_host", "127.0.0.1")))
        self.proxy_host.setPlaceholderText(tr("Адрес прокси"))
        self.proxy_port = QSpinBox()
        self.proxy_port.setRange(1, 65535)
        self.proxy_port.setValue(int(self.config.get("proxy_port", 1080)))
        self.proxy_port.setFixedWidth(120)
        proxy_row.addWidget(self.proxy_host, 1)
        proxy_row.addWidget(self.proxy_port)
        proxy_widget = QWidget()
        proxy_widget.setLayout(proxy_row)
        form.addRow(tr("Сервер / порт"), proxy_widget)
        s.addWidget(card)
        note = QLabel(tr("Лимит мягкий: при достижении порога новые задачи ставятся на паузу. Уже начатая загрузка может превысить его. Архивы автоматически не удаляются."))
        note.setObjectName("Muted")
        note.setWordWrap(True)
        s.addWidget(note)
        save = QPushButton(tr("Сохранить настройки"))
        save.setObjectName("Accent")
        save.clicked.connect(self.save_settings)
        s.addWidget(save, 0, Qt.AlignmentFlag.AlignLeft)
        s.addStretch(1)
        settings_scroll.setWidget(settings_content)
        settings_layout.addWidget(settings_scroll)
        self.pages.addWidget(settings)

    def make_table(self) -> QTableWidget:
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels([tr("Репозиторий"), tr("Состояние"), tr("Последняя синхронизация"), tr("Размер")])
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setShowGrid(False)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        table.cellDoubleClicked.connect(lambda *_args, t=table: self.open_commit_history(t))
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(
            lambda position, t=table: self.repository_context_menu(t, position)
        )
        return table

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        icon = app_icon()
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.tray.setIcon(icon)
        self.tray.setToolTip(tr("GitArchive — локальный архив Git-репозиториев"))
        menu = QMenu()
        show = QAction(tr("Открыть GitArchive"), self)
        show.triggered.connect(self.show_from_tray)
        sync = QAction(tr("Синхронизировать сейчас"), self)
        sync.triggered.connect(self.sync_all)
        exit_action = QAction(tr("Выход"), self)
        exit_action.triggered.connect(self.exit_app)
        menu.addAction(show)
        menu.addAction(sync)
        menu.addSeparator()
        menu.addAction(exit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_from_tray() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def go(self, page: int) -> None:
        self.pages.setCurrentIndex(page)
        self.heading.setText((tr("Обзор"), tr("Репозитории"), tr("Журнал"), tr("Настройки"))[page])
        for index, button in enumerate(self.nav):
            button.setChecked(index == page)

    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, lambda: set_mica(self, self.dark))

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            QTimer.singleShot(0, self.hide)
            QTimer.singleShot(0, lambda: self.setWindowState(Qt.WindowState.WindowNoState))

    def closeEvent(self, event) -> None:
        if self.config.get("close_to_tray", True) and self.tray.isVisible() and not self.allow_exit:
            event.ignore()
            self.hide()
            self.tray.showMessage(APP_NAME, tr("GitArchive продолжает работу в системном трее."), QSystemTrayIcon.MessageIcon.Information, 2500)
        else:
            self.flush_logs()
            self.lan_service.stop()
            event.accept()

    def exit_app(self) -> None:
        self.allow_exit = True
        self.flush_logs()
        self.lan_service.stop()
        if self.download_proxy is not None:
            self.download_proxy.close()
            self.download_proxy = None
        for process in list(self.processes.values()):
            if process.state() != QProcess.ProcessState.NotRunning:
                process.terminate()
        if self.restore_process and self.restore_process.state() != QProcess.ProcessState.NotRunning:
            self.restore_process.terminate()
        self.tray.hide()
        QApplication.quit()

    def open_lan_dialog(self) -> None:
        if self.lan_dialog is None:
            self.lan_dialog = LanPeerDialog(self)
        self.lan_dialog.refresh_lists()
        self.lan_dialog.show()
        self.lan_dialog.raise_()
        self.lan_dialog.activateWindow()

    def add_repository(self) -> None:
        prompt = (
            tr("HTTPS или SSH clone URL (GitHub, GitLab, Codeberg, Bitbucket, " + "Gitea, Forgejo или свой сервер):")
        )
        clipboard_value = QApplication.clipboard().text().strip()
        try:
            parse_git_url(clipboard_value)
        except ValueError:
            clipboard_value = ""

        value, ok = QInputDialog.getText(
            self,
            tr("Добавить Git-репозиторий"),
            prompt,
            QLineEdit.EchoMode.Normal,
            clipboard_value,
        )
        if not ok:
            return
        try:
            url, namespace, name, host, provider = parse_git_url(value)
        except ValueError as error:
            QMessageBox.warning(self, tr("Ссылка не распознана"), str(error))
            return

        storage_format = str(self.config.get("new_storage_format", "folder")).lower()
        compression_level = str(self.config.get("compression_level", "medium")).lower()
        seven_zip = self.seven_zip_path or find_7zip()
        if storage_format not in {"folder", "zip", "7z"}:
            storage_format = "folder"
        if storage_format != "folder" and not seven_zip:
            QMessageBox.warning(
                self,
                tr("Нужен 7-Zip"),
                tr("Для хранения в ZIP или 7z установи 7-Zip и перезапусти GitArchive. " + "Новые репозитории пока можно сохранять в обычной папке .git."),
            )
            return

        identity = (host.casefold(), namespace, name)
        for existing in self.config["repositories"]:
            existing_identity = (
                str(existing.get("host") or "github.com").casefold(),
                str(existing.get("namespace") or existing.get("owner") or ""),
                str(existing.get("name") or ""),
            )
            if existing_identity == identity:
                QMessageBox.information(
                    self, tr("Уже добавлен"), tr("Этот репозиторий уже есть в GitArchive.")
                )
                return

        path = archive_path(
            Path(self.config["storage_path"]),
            host,
            namespace,
            name,
            storage_format=storage_format,
        )
        repo = {
            "id": uuid.uuid4().hex,
            "host": host,
            "provider": provider,
            "namespace": namespace,
            "owner": namespace,
            "name": name,
            "url": url,
            "local_path": str(path),
            "storage_format": storage_format,
            "compression_level": compression_level,
            "status": tr("В очереди"),
            "last_sync": None,
            "last_error": "",
        }
        self.config["repositories"].append(repo)
        save_config(self.config)
        self.log(tr(f"Добавлен {repository_label(repo)} · формат: {storage_format}"))
        self.refresh()
        self.scan_sizes()
        self.sync_one(repo["id"])

    def repository_context_menu(self, table: QTableWidget, position) -> None:
        index = table.indexAt(position)
        if not index.isValid():
            return
        table.selectRow(index.row())
        repo = self.selected_repo(table)
        if not repo:
            return
        menu = QMenu(self)
        open_history = menu.addAction(tr("Открыть"))
        sync = menu.addAction(tr("Обновить"))
        open_folder = menu.addAction(tr("Открыть папку архива"))
        restore = menu.addAction(tr("Восстановить рабочую копию"))
        menu.addSeparator()
        remove = menu.addAction(tr("Удалить из списка"))
        chosen = menu.exec(table.viewport().mapToGlobal(position))
        if chosen == open_history:
            self.open_commit_history(table)
        elif chosen == sync:
            self.sync_one(repo["id"])
        elif chosen == open_folder:
            self.open_selected_folder(table)
        elif chosen == restore:
            self.restore_selected(repo)
        elif chosen == remove:
            self.remove_repository(repo)

    def open_commit_history(self, table: QTableWidget) -> None:
        repo = self.selected_repo(table)
        if not repo:
            return
        archive = Path(repo["local_path"])
        storage_format = repository_storage_format(repo)
        if storage_format == "folder":
            if not (archive / "HEAD").is_file() or not (archive / "objects").is_dir():
                QMessageBox.information(
                    self, tr("Архив ещё не готов"),
                    tr("Сначала дождись первой успешной синхронизации этого репозитория.")
                )
                return
            self.show_commit_history(archive, repo.get("name", "repository"))
            return

        if not archive.is_file():
            QMessageBox.information(self, tr("Архив ещё не готов"), tr("Сначала дождись первой успешной синхронизации этого репозитория."))
            return
        if not self.core.is_file():
            QMessageBox.warning(self, tr("Rust-движок не найден"), tr("Запусти start.bat, чтобы собрать движок."))
            return
        seven_zip = self.seven_zip_path or find_7zip()
        if not seven_zip:
            QMessageBox.warning(self, tr("Нужен 7-Zip"), tr("Для чтения сжатого архива установи 7-Zip и перезапусти GitArchive."))
            return

        temporary_root = Path(tempfile.mkdtemp(prefix=".gitarchive-history-", dir=str(archive.parent)))
        worker = GitCommand(
            [
                str(self.core), "materialize",
                "--archive", str(archive),
                "--destination", str(temporary_root),
                "--storage-format", storage_format,
                "--seven-zip", seven_zip,
            ],
            timeout=3600,
            cpu_priority=self.config.get("background_cpu_priority", "idle"),
            memory_priority=self.config.get("background_memory_priority", "very_low"),
        )
        self.history_workers.append(worker)
        worker.signals.completed.connect(
            lambda code, stdout, stderr, w=worker, root=temporary_root, name=repo.get("name", "repository"):
                self.history_materialized(w, root, name, code, stdout, stderr)
        )
        self.thread_pool.start(worker)
        self.log(tr(f"Распаковка {repository_label(repo)} во временную папку для просмотра коммитов."))

    def history_materialized(
        self,
        worker: GitCommand,
        temporary_root: Path,
        repo_name: str,
        code: int,
        stdout: str,
        stderr: str,
    ) -> None:
        if worker in self.history_workers:
            self.history_workers.remove(worker)
        mirror = temporary_root / "mirror.git"
        if code != 0 or not (mirror / "HEAD").is_file() or not (mirror / "objects").is_dir():
            shutil.rmtree(temporary_root, ignore_errors=True)
            QMessageBox.warning(
                self, tr("Не удалось открыть историю"),
                stderr or stdout or tr("Не удалось безопасно распаковать архив для просмотра.")
            )
            return
        self.show_commit_history(mirror, repo_name, cleanup_root=temporary_root)

    def show_commit_history(
        self,
        archive: Path,
        repo_name: str,
        cleanup_root: Path | None = None,
    ) -> None:
        dialog = CommitHistoryDialog(
            archive,
            repo_name,
            self.thread_pool,
            self,
            cpu_priority=self.config.get("background_cpu_priority", "idle"),
            memory_priority=self.config.get("background_memory_priority", "very_low"),
        )
        dialog.setStyleSheet(self.styleSheet())
        try:
            dialog.exec()
        finally:
            if cleanup_root is not None:
                dialog.request_cleanup(cleanup_root)

    def remove_repository(self, repo: dict) -> None:
        answer = QMessageBox.question(
            self,
            tr("Убрать репозиторий из списка?"),
            tr(f"{repository_label(repo)} будет удалён из списка GitArchive.\n" + "Локальный архив на диске останется нетронутым."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if repo.get("id") in self.processes:
            QMessageBox.warning(
                self, tr("Синхронизация выполняется"),
                tr("Дождись завершения синхронизации перед удалением записи.")
            )
            return
        self.config["repositories"] = [
            item for item in self.config["repositories"] if item.get("id") != repo.get("id")
        ]
        repo_id = str(repo.get("id") or "")
        was_queued = repo_id in self.queue
        self.queue = [queued_id for queued_id in self.queue if queued_id != repo_id]
        if was_queued:
            self.sync_progress.pop(repo_id, None)
            self.sync_batch_total = max(0, self.sync_batch_total - 1)
            if not self.queue and not self.processes:
                self.sync_batch_completed = 0
                self.sync_batch_errors = 0
                self.sync_batch_paused = False
            self.update_sidebar_progress()
        save_config(self.config)
        self.log(tr(f"Убран из списка (архив сохранён): {repository_label(repo)}"))
        self.refresh()
        self.scan_sizes()

    def selected_repo(self, table: QTableWidget) -> dict | None:
        row = table.currentRow()
        if row < 0:
            QMessageBox.information(self, tr("Выбор проекта"), tr("Выбери репозиторий в таблице."))
            return None
        cell = table.item(row, 0)
        repo_id = cell.data(Qt.ItemDataRole.UserRole) if cell else None
        return next((repo for repo in self.config["repositories"] if repo.get("id") == repo_id), None)

    def sync_selected(self) -> None:
        repo = self.selected_repo(self.repo_table)
        if repo:
            self.sync_one(repo["id"])

    def restore_selected(self, repo_override: dict | None = None) -> None:
        if self.restore_process and self.restore_process.state() != QProcess.ProcessState.NotRunning:
            QMessageBox.information(self, tr("Восстановление уже идёт"), tr("Дождись завершения текущего восстановления."))
            return

        repo = repo_override or self.selected_repo(self.repo_table)
        if not repo:
            return
        archive = Path(repo["local_path"])
        storage_format = repository_storage_format(repo)
        if storage_format == "folder":
            archive_ready = (archive / "HEAD").is_file() and (archive / "objects").is_dir()
        else:
            archive_ready = archive.is_file()
        if not archive_ready:
            QMessageBox.information(self, tr("Архив не найден"), tr("Сначала дождись первой успешной синхронизации репозитория."))
            return
        if not self.core.is_file():
            QMessageBox.warning(self, tr("Rust-движок не найден"), tr("Запусти start.bat, чтобы собрать движок."))
            return

        seven_zip = self.seven_zip_path or find_7zip()
        if storage_format != "folder" and not seven_zip:
            QMessageBox.warning(self, tr("Нужен 7-Zip"), tr("Для восстановления ZIP/7z установи 7-Zip и перезапусти GitArchive."))
            return

        parent = QFileDialog.getExistingDirectory(
            self,
            tr("Куда восстановить рабочую копию"),
            str(Path.home()),
        )
        if not parent:
            return
        destination = Path(parent) / repo["name"]
        if destination.exists():
            QMessageBox.warning(
                self,
                tr("Папка уже существует"),
                tr(f"GitArchive не будет перезаписывать существующую папку:\n{destination}"),
            )
            return

        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.setProcessEnvironment(background_process_environment(self.config))
        process.readyReadStandardOutput.connect(lambda p=process: self.read_restore_output(p))
        process.finished.connect(lambda code, _status, p=process: self.restore_finished(p, code))
        process.errorOccurred.connect(lambda error, p=process: self.restore_error(p, error))
        self.restore_process = process
        self.restore_destination = destination
        self.log(tr(f"Восстановление {repository_label(repo)} → {destination}"))
        arguments = [
            "restore", "--archive", str(archive),
            "--destination", str(destination),
            "--storage-format", storage_format,
        ]
        if storage_format != "folder" and seven_zip:
            arguments.extend(["--seven-zip", seven_zip])
        process.start(str(self.core), arguments)

    def read_restore_output(self, process: QProcess) -> None:
        output = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        for line in output.splitlines():
            if line.strip():
                self.log(line.strip())

    def restore_finished(self, process: QProcess, code: int) -> None:
        self.read_restore_output(process)
        destination = self.restore_destination
        if self.restore_process is process:
            self.restore_process = None
            self.restore_destination = None
        process.deleteLater()

        if code == 0 and destination is not None:
            self.log(tr(f"Рабочая копия восстановлена: {destination}"))
            QMessageBox.information(
                self,
                tr("Восстановление завершено"),
                tr(f"Рабочая копия и сохранённые исторические refs восстановлены в:\n{destination}"),
            )
            if sys.platform == "win32":
                os.startfile(str(destination))
        else:
            self.log(tr(f"Восстановление завершилось с ошибкой (код {code}). Локальный архив не изменён."))
            QMessageBox.warning(
                self,
                tr("Ошибка восстановления"),
                tr("Не удалось полностью восстановить рабочую копию. Подробности находятся в журнале. Исходный архив не изменялся."),
            )

    def restore_error(self, process: QProcess, error: QProcess.ProcessError) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return
        if self.restore_process is process:
            self.restore_process = None
            self.restore_destination = None
        process.deleteLater()
        self.log(tr("Не удалось запустить Rust-движок для восстановления."))
        QMessageBox.warning(self, tr("Ошибка запуска"), tr("Не удалось запустить Rust-движок."))

    def open_selected_folder(self, table: QTableWidget | None = None) -> None:
        repo = self.selected_repo(table or self.repo_table)
        if not repo:
            return
        path = Path(repo["local_path"])
        if not path.exists():
            QMessageBox.information(self, tr("Архива пока нет"), tr("Файл или папка появится после первой успешной синхронизации."))
            return
        target = path.parent if path.is_file() else path
        if sys.platform == "win32":
            os.startfile(str(target))
        else:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def sync_one(self, repo_id: str) -> None:
        if repo_id in self.processes or repo_id in self.queue:
            return
        if not self.core.is_file():
            QMessageBox.warning(self, tr("Rust-движок не найден"), tr("Запусти start.bat: он соберёт Rust-движок и откроет приложение."))
            return
        if not self.processes and not self.queue:
            self.sync_batch_total = 0
            self.sync_batch_completed = 0
            self.sync_batch_errors = 0
            self.sync_batch_paused = False
            self.sync_progress.clear()
        self.queue.append(repo_id)
        self.sync_batch_total += 1
        self.sync_progress[repo_id] = {"percent": 0, "phase": tr("Подготовка")}
        self.update_sidebar_progress()
        self.start_queued()

    def sync_all(self) -> None:
        if not self.processes and not self.queue:
            self.sync_batch_total = 0
            self.sync_batch_completed = 0
            self.sync_batch_errors = 0
            self.sync_batch_paused = False
            self.sync_progress.clear()
        for repo in self.config["repositories"]:
            repo_id = repo.get("id")
            if repo_id and repo_id not in self.processes and repo_id not in self.queue:
                self.queue.append(repo_id)
                self.sync_batch_total += 1
                self.sync_progress[repo_id] = {"percent": 0, "phase": tr("В очереди")}
        self.update_sidebar_progress()
        self.start_queued()
        self.refresh()

    @staticmethod
    def _normalize_proxy_scheme(value: str) -> str:
        value = value.strip()
        if value.lower().startswith("socks5://"):
            return "socks5h://" + value[9:]
        if value.lower().startswith("socks4://"):
            return "socks4a://" + value[9:]
        return value

    def _default_https_proxy(self) -> str | None:
        """Resolve and cache the generic proxy Git/cURL uses for HTTPS."""
        if self.config.get("proxy_enabled"):
            return proxy_url(self.config)
        if self._default_https_proxy_loaded:
            return self._default_https_proxy_cache

        try:
            result = subprocess.run(
                ["git", "config", "--get", "http.proxy"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1.5,
                check=False,
            )
            configured = result.stdout.strip() if result.returncode == 0 else ""
            if configured:
                self._default_https_proxy_cache = self._normalize_proxy_scheme(configured)
            else:
                for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
                    value = os.environ.get(key, "").strip()
                    if value:
                        self._default_https_proxy_cache = self._normalize_proxy_scheme(value)
                        break
        except subprocess.TimeoutExpired as error:
            self._https_proxy_lookup_failed = True
            raise ProxyConfigurationError(
                "Could not read Git proxy settings quickly enough; the limiter was skipped"
            ) from error
        except OSError:
            # Git may not be available on PATH yet; environment proxy values can
            # still be forwarded without changing their meaning.
            for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
                value = os.environ.get(key, "").strip()
                if value:
                    self._default_https_proxy_cache = self._normalize_proxy_scheme(value)
                    break

        self._default_https_proxy_loaded = True
        return self._default_https_proxy_cache

    def _configured_https_proxy(
        self, remote_url: str, repository_path: str | None = None
    ) -> str | None:
        """Resolve URL-specific proxy settings, including an existing bare mirror's config."""
        if self.config.get("proxy_enabled"):
            return proxy_url(self.config)

        git_prefix = ["git"]
        cache_path = ""
        if repository_path:
            archive = Path(repository_path).expanduser()
            if archive.is_dir() and (archive / "config").is_file() and (archive / "objects").is_dir():
                git_prefix.extend(["--git-dir", str(archive)])
                cache_path = str(archive.resolve())
        cache_key = (cache_path, remote_url)
        if cache_key in self._https_proxy_cache:
            return self._https_proxy_cache[cache_key]

        try:
            auth_probe = subprocess.run(
                git_prefix + ["config", "--get-urlmatch", "http.proxyAuthMethod", remote_url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1.5,
                check=False,
            )
            if auth_probe.returncode == 0 and auth_probe.stdout.strip():
                raise ProxyConfigurationError(
                    "Git proxy authentication methods are not forwarded by the bandwidth limiter"
                )

            result = subprocess.run(
                git_prefix + ["config", "--get-urlmatch", "http.proxy", remote_url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1.5,
                check=False,
            )
            configured = result.stdout.strip() if result.returncode == 0 else ""
            upstream = (
                self._normalize_proxy_scheme(configured)
                if configured
                else self._default_https_proxy()
            )
        except ProxyConfigurationError:
            raise
        except subprocess.TimeoutExpired as error:
            self._https_proxy_lookup_failed = True
            raise ProxyConfigurationError(
                "Could not resolve this remote's Git proxy settings; the limiter was skipped"
            ) from error
        except OSError:
            upstream = self._default_https_proxy()

        self._https_proxy_cache[cache_key] = upstream
        return upstream

    def prepare_download_proxy(
        self, remote_url: str, repository_path: str | None = None
    ) -> str | None:
        """Return the local aggregate limiter URL for HTTPS remotes only."""
        limit_mbps = normalize_download_limit(self.config.get("sync_download_limit_mbps", 3))
        if limit_mbps <= 0 or self._https_proxy_lookup_failed:
            return None
        try:
            parsed = urlsplit(remote_url)
            if parsed.scheme.lower() != "https" or not parsed.hostname:
                return None
            port = parsed.port or 443
            no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
            default_upstream = self._default_https_proxy()
            upstream = self._configured_https_proxy(remote_url, repository_path)
            if self.download_proxy is None:
                self.download_proxy = ThrottledHttpProxy(limit_mbps * 1_000_000, no_proxy)
            else:
                self.download_proxy.set_limit(limit_mbps * 1_000_000)
            self.download_proxy.set_upstream_proxy(default_upstream, no_proxy)
            self.download_proxy.set_route(parsed.hostname, port, upstream)
            return self.download_proxy.start()
        except (OSError, ValueError, RuntimeError, ProxyConfigurationError) as error:
            self.log(
                tr("Ограничение скорости не удалось включить; синхронизация продолжится без него: {}").format(
                    str(error)[:240]
                )
            )
            return None

    def _hide_sidebar_progress_if_complete(self) -> None:
        if (
            not self.processes
            and not self.queue
            and not self.sync_batch_paused
            and self.sync_batch_total <= self.sync_batch_completed
        ):
            self.sidebar_sync_card.hide()
            self.sidebar_sync_bar.setValue(0)

    def start_queued(self) -> None:
        if self.size_scan_running or not self.size_scan_ready:
            return
        maximum = max(1, int(self.config.get("max_parallel", 3)))
        while self.queue and len(self.processes) < maximum:
            active_compressed = any(
                (active_repo := self.find_repo(active_id)) is not None
                and repository_storage_format(active_repo) != "folder"
                for active_id in self.processes
            )
            if active_compressed:
                break

            if self.total_size() >= int(self.config.get("max_storage_gb", 100)) * GIB:
                pending_ids = list(self.queue)
                for repo_id in pending_ids:
                    repo = self.find_repo(repo_id)
                    if repo:
                        repo["status"] = tr("Ожидание лимита")
                    self.sync_progress.pop(repo_id, None)
                self.sync_batch_completed += len(pending_ids)
                self.sync_batch_paused = True
                self.queue.clear()
                self.log(tr("Достигнут лимит хранилища. Новые синхронизации приостановлены; архивы сохранены."))
                self.update_sidebar_progress()
                break

            repo_id = self.queue.pop(0)
            repo = self.find_repo(repo_id)
            if not repo:
                self.complete_sync_progress(repo_id, failed=True)
                continue

            storage_format = repository_storage_format(repo)
            if storage_format != "folder" and self.processes:
                # Compression repacks the whole repository; run it alone to avoid
                # competing with other Git tasks for CPU, RAM and disk bandwidth.
                self.queue.insert(0, repo_id)
                break

            seven_zip = self.seven_zip_path or find_7zip()
            if storage_format != "folder" and not seven_zip:
                repo["status"] = tr("Ошибка — нужен 7-Zip")
                repo["last_error"] = tr("Для ZIP/7z требуется установленный 7-Zip CLI.")
                save_config(self.config)
                self.log(tr(f"Пропуск {repository_label(repo)}: 7-Zip не найден."))
                self.complete_sync_progress(repo_id, failed=True)
                continue

            process = QProcess(self)
            process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            throttle_url = self.prepare_download_proxy(
                str(repo.get("url") or ""), str(repo.get("local_path") or "")
            )
            process.setProcessEnvironment(background_process_environment(self.config, throttle_url))
            process.readyReadStandardOutput.connect(lambda p=process: self.read_output(p))
            process.started.connect(lambda p=process: self.sync_process_started(p))
            process.finished.connect(lambda code, _status, p=process: self.finished(p, code))
            process.errorOccurred.connect(lambda error, p=process: self.process_error(p, error))
            self.processes[repo_id] = process
            self.sync_progress[repo_id] = {"percent": 0, "phase": tr("Запуск")}
            repo["status"] = tr("Синхронизация")
            repo["last_error"] = ""
            save_config(self.config)
            self.log(tr(f"Начата синхронизация {repository_label(repo)}"))
            current_compression = str(self.config.get("compression_level") or "medium")
            previous_compression = str(repo.get("compression_level") or "medium")
            arguments = [
                "sync",
                "--url", repo["url"],
                "--destination", repo["local_path"],
                "--storage-format", storage_format,
                "--compression-level", current_compression,
            ]
            if storage_format != "folder" and previous_compression != current_compression:
                arguments.append("--force-repack")
            if storage_format != "folder" and seven_zip:
                arguments.extend(["--seven-zip", seven_zip])
            process.setProperty("gitarchive_compression_level", current_compression)
            now = time.monotonic()
            process.setProperty("gitarchive_started_at", now)
            process.setProperty("gitarchive_last_output_at", now)
            process.setProperty("gitarchive_last_heartbeat_at", now)
            process.start(str(self.core), arguments)
        self.refresh()

    def check_sync_watchdog(self) -> None:
        """Log a heartbeat for active syncs even when Git has stopped producing output."""
        if not self.processes:
            return
        now = time.monotonic()
        for repo_id, process in list(self.processes.items()):
            started = float(process.property("gitarchive_started_at") or now)
            last_output = float(process.property("gitarchive_last_output_at") or started)
            last_heartbeat = float(process.property("gitarchive_last_heartbeat_at") or started)
            if now - last_heartbeat < 60:
                continue
            process.setProperty("gitarchive_last_heartbeat_at", now)
            elapsed = max(0, int(now - started))
            idle = max(0, int(now - last_output))
            repo = self.find_repo(repo_id)
            if idle >= 180 and repo:
                waiting_status = tr("Нет вывода от Git")
                if repo.get("status") != waiting_status:
                    repo["status"] = waiting_status
                    repo["last_error"] = tr(
                        "Git не отправлял вывод более 3 минут. Процесс ещё работает; это может быть ожидание сети, диска или локальная обработка объектов."
                    )
                    save_config(self.config)
                    self.refresh()
            label = repository_label(repo) if repo else repo_id
            self.log(
                f"{label}: "
                + tr("Синхронизация всё ещё выполняется. Прошло {} с; новых данных от Git нет {} с.").format(
                    elapsed, idle
                )
            )

    def sync_process_started(self, process: QProcess) -> None:
        repo_id = next((key for key, item in self.processes.items() if item is process), None)
        if repo_id is None:
            return
        now = time.monotonic()
        process.setProperty("gitarchive_started_at", now)
        process.setProperty("gitarchive_last_output_at", now)
        process.setProperty("gitarchive_last_heartbeat_at", now)
        self.log(tr("Запущен Rust-процесс синхронизации, PID: {}.").format(int(process.processId())))

    def read_output(self, process: QProcess) -> None:
        text = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        repo_id = next((key for key, item in self.processes.items() if item is process), None)
        repo = self.find_repo(repo_id) if repo_id else None
        if repo_id and text:
            process.setProperty("gitarchive_last_output_at", time.monotonic())
            if repo and repo.get("status") == tr("Нет вывода от Git"):
                repo["status"] = tr("Синхронизация")
                repo["last_error"] = ""
                self.refresh()
                self.log(tr("Git снова передал данные; синхронизация продолжается."))
            progress = parse_sync_progress(text)
            if progress:
                percent, phase = progress
                self.sync_progress[repo_id] = {"percent": percent, "phase": phase}
                self.update_sidebar_progress()
        prefix = f"{repository_label(repo)}: " if repo else ""
        for line in text.splitlines():
            if line.strip():
                self.log(prefix + line.strip())

    def complete_sync_progress(self, repo_id: str, failed: bool = False) -> None:
        self.sync_progress.pop(repo_id, None)
        self.sync_batch_completed = min(
            self.sync_batch_total,
            self.sync_batch_completed + 1,
        )
        if failed:
            self.sync_batch_errors += 1
        self.update_sidebar_progress()

    def update_sidebar_progress(self) -> None:
        if not hasattr(self, "sidebar_sync_bar"):
            return
        active_ids = list(self.processes)
        active_count = len(active_ids)
        queued_count = len(self.queue)
        total = max(
            self.sync_batch_total,
            self.sync_batch_completed + active_count + queued_count,
        )
        completed = min(self.sync_batch_completed, total)

        if total <= 0:
            self.sidebar_progress_animation.stop()
            self.sidebar_sync_bar.setValue(0)
            self.sidebar_sync_card.hide()
            return

        # A newly queued batch starts from zero even if the previous batch's
        # completion animation has not finished hiding the card yet.
        if completed == 0 and active_count == 0 and queued_count > 0:
            self.sidebar_progress_animation.stop()
            self.sidebar_sync_bar.setValue(0)

        # Keep the widget visible only while a batch is active or deliberately paused.
        self.sidebar_sync_card.show()
        if active_count == 0 and queued_count == 0 and completed >= total:
            if self.sync_batch_paused:
                target = 99
                self.sidebar_sync_label.setText(tr("Пауза · лимит хранилища"))
                self.sidebar_sync_detail.setText(tr("Часть задач ожидает освобождения места"))
            elif self.sync_batch_errors:
                target = 100
                success_count = max(0, completed - self.sync_batch_errors)
                self.sidebar_sync_label.setText(tr("Завершено с ошибками"))
                self.sidebar_sync_detail.setText(
                    tr(f"{success_count} успешно · {self.sync_batch_errors} ошибок")
                )
            else:
                target = 100
                self.sidebar_sync_label.setText(tr("Синхронизация завершена"))
                self.sidebar_sync_detail.setText(tr(f"{completed} из {total} репозиториев"))

            self.sidebar_progress_animation.stop()
            self.sidebar_progress_animation.setStartValue(self.sidebar_sync_bar.value())
            self.sidebar_progress_animation.setEndValue(target)
            self.sidebar_progress_animation.start()
            if target == 100 and self.sidebar_sync_bar.value() == 100:
                self._hide_sidebar_progress_if_complete()
            return

        active_percent = sum(
            int(self.sync_progress.get(repo_id, {}).get("percent", 0))
            for repo_id in active_ids
        )
        overall = int(round((completed * 100 + active_percent) / total))
        # A batch is not complete while any repository is running or queued.
        overall = max(0, min(99, overall))

        self.sidebar_progress_animation.stop()
        self.sidebar_progress_animation.setStartValue(self.sidebar_sync_bar.value())
        self.sidebar_progress_animation.setEndValue(overall)
        self.sidebar_progress_animation.start()

        if active_count:
            self.sidebar_sync_label.setText(tr(f"Синхронизация · {overall}%"))
        else:
            self.sidebar_sync_label.setText(tr("Ожидание запуска"))
        phases = [
            str(self.sync_progress.get(repo_id, {}).get("phase") or "")
            for repo_id in active_ids
        ]
        phase = next((item for item in phases if item), "")
        detail = tr(f"{active_count} активн. · {queued_count} в очереди · {completed}/{total} готово")
        self.sidebar_sync_detail.setText(f"{phase} · {detail}" if phase else detail)

    def process_error(self, process: QProcess, error: QProcess.ProcessError) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return
        self.read_output(process)
        repo_id = next((key for key, item in self.processes.items() if item is process), None)
        repo = self.find_repo(repo_id) if repo_id else None
        if repo:
            repo["status"] = tr("Ошибка запуска")
            repo["last_error"] = tr("Не удалось запустить Rust-движок.")
            self.processes.pop(repo_id, None)
            self.complete_sync_progress(repo_id, failed=True)
        process.deleteLater()
        save_config(self.config)
        self.refresh()
        self.start_queued()

    def finished(self, process: QProcess, code: int) -> None:
        self.read_output(process)
        repo_id = next((key for key, item in self.processes.items() if item is process), None)
        if repo_id is None:
            process.deleteLater()
            return
        self.processes.pop(repo_id, None)
        self.complete_sync_progress(repo_id, failed=code != 0)
        repo = self.find_repo(repo_id)
        if repo:
            if code == 0:
                repo["status"] = tr("Готово")
                repo["last_sync"] = datetime.now().astimezone().isoformat(timespec="minutes")
                repo["last_error"] = ""
                if repository_storage_format(repo) != "folder":
                    used_level = process.property("gitarchive_compression_level")
                    if used_level:
                        repo["compression_level"] = str(used_level)
                self.log(tr(f"Готово: {repository_label(repo)}"))
            else:
                repo["status"] = tr("Ошибка — архив сохранён")
                repo["last_error"] = tr(f"Код завершения: {code}")
                self.log(tr(f"Ошибка обновления {repository_label(repo)}; локальное зеркало оставлено на месте."))
        save_config(self.config)
        process.deleteLater()
        self.refresh()
        self.scan_sizes()

    def find_repo(self, repo_id: str | None) -> dict | None:
        if not repo_id:
            return None
        return next((repo for repo in self.config["repositories"] if repo.get("id") == repo_id), None)

    def scan_sizes(self) -> None:
        if self.size_scan_running:
            return
        self.size_scan_running = True
        self.size_scan_ready = False
        repositories = [
            (repo["id"], repo["local_path"])
            for repo in self.config["repositories"]
            if repo.get("id") and repo.get("local_path")
        ]
        self.size_worker = SizeScan(repositories)
        self.size_worker.signals.completed.connect(self.size_scan_finished)
        self.thread_pool.start(self.size_worker)

    def size_scan_finished(self, result: dict) -> None:
        self.size_cache.update(result)
        self.size_scan_running = False
        self.size_scan_ready = True
        self.size_worker = None
        if any(repo.get("id") not in result and repo.get("id") not in self.size_cache for repo in self.config["repositories"]):
            self.scan_sizes()
            return
        self.refresh()
        self.start_queued()

    def total_size(self) -> int:
        return sum(self.size_cache.values())

    def refresh(self) -> None:
        repos = self.config["repositories"]
        size = self.total_size()
        self.stats["repos"].setText(str(len(repos)))
        self.stats["mirrors"].setText(str(sum(bool(r.get("local_path")) and Path(r["local_path"]).exists() for r in repos)))
        self.stats["size"].setText(human_size(size))
        self.stats["running"].setText(str(len(self.processes)))
        for table in (self.dashboard_table, self.repo_table):
            table.setRowCount(len(repos))
            for row, repo in enumerate(repos):
                bytes_used = self.size_cache.get(repo.get("id"), 0)
                last = repo.get("last_sync") or "—"
                try:
                    if last != "—":
                        last = datetime.fromisoformat(last).strftime("%d.%m.%Y %H:%M")
                except ValueError:
                    pass
                values = (
                    repository_label(repo),
                    tr(str(repo.get("status") or tr("Добавлен"))),
                    last,
                    human_size(bytes_used) if bytes_used else "—",
                )
                for col, value in enumerate(values):
                    cell = QTableWidgetItem(value)
                    if col == 0:
                        cell.setData(Qt.ItemDataRole.UserRole, repo.get("id"))
                    if col == 1:
                        cell.setToolTip(tr(str(repo.get("last_error", ""))))
                    table.setItem(row, col, cell)
            table.resizeRowsToContents()
        self.hint.setText(tr(f"Занято {human_size(size)} из {self.config['max_storage_gb']} ГБ. Архивы не удаляются при недоступности исходного проекта."))
        self.update_sidebar_progress()

    def load_logs(self) -> list[str]:
        path = data_dir() / "logs" / "gitarchive.log"
        try:
            return path.read_text(encoding="utf-8").splitlines()[-1200:]
        except OSError:
            return []

    def log(self, message: str) -> None:
        line = f"[{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        self.logs.append(line)
        self.logs = self.logs[-1200:]
        self.pending_log_lines.append(line)
        if not self.log_flush_timer.isActive():
            self.log_flush_timer.start()

    def flush_logs(self) -> None:
        if self.log_flush_timer.isActive():
            self.log_flush_timer.stop()
        if not self.pending_log_lines:
            return
        pending, self.pending_log_lines = self.pending_log_lines, []
        try:
            log_dir = data_dir() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "gitarchive.log"
            if log_path.exists() and log_path.stat().st_size > 2 * 1024 * 1024:
                rotated = log_dir / "gitarchive.log.1"
                rotated.unlink(missing_ok=True)
                log_path.replace(rotated)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write("\n".join(pending) + "\n")
        except OSError:
            pass

        if hasattr(self, "log_view"):
            cursor = QTextCursor(self.log_view.document())
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText("\n".join(pending) + "\n")
            self.log_view.setTextCursor(cursor)
            if self.pages.currentWidget() is self.pages.widget(2):
                self.log_view.ensureCursorVisible()

    def choose_storage(self) -> None:
        path = QFileDialog.getExistingDirectory(self, tr("Папка для новых архивов"), self.storage_edit.text())
        if path:
            self.storage_edit.setText(path)

    def update_storage_format_controls(self, *_args) -> None:
        storage_format = self.storage_format.currentData()
        compressed = storage_format in {"zip", "7z"}
        self.compression_level.setEnabled(compressed and bool(self.seven_zip_path))
        if not self.seven_zip_path:
            note = tr("ZIP и 7z требуют установленный 7-Zip с консольным файлом 7z.exe. После установки перезапусти GitArchive.")
        elif not compressed:
            note = tr("Папка .git — рекомендуемый вариант: Git обновляет её на месте без повторной распаковки и сжатия.")
        elif storage_format == "zip":
            note = (
                tr("ZIP совместим почти со всеми архиваторами. При обновлении зеркало временно распаковывается " + "и собирается заново. Используется один поток 7-Zip; выигрыш в размере может быть небольшим.")
            )
        else:
            note = (
                tr("7z обычно сжимает лучше ZIP, но обновление медленнее. Один поток, словарь до 64 МБ; " + "дополнительная экономия места может быть небольшой.")
            )
        note += (
            tr(" Формат применяется только к новым репозиториям; уровень сжатия — ко всем ZIP/7z " + "при следующей синхронизации. Автоматической миграции формата нет.")
        )
        self.compression_note.setText(note)

    def save_settings(self) -> None:
        storage = self.storage_edit.text().strip()
        if not storage:
            QMessageBox.warning(self, tr("Папка не указана"), tr("Укажи папку для новых архивов."))
            return
        try:
            Path(storage).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError as error:
            QMessageBox.warning(self, tr("Ошибка папки"), str(error))
            return

        new_storage_format = str(self.storage_format.currentData() or "folder")
        compression_level = str(self.compression_level.currentData() or "medium")
        self.seven_zip_path = find_7zip()
        if new_storage_format in {"zip", "7z"} and not self.seven_zip_path:
            QMessageBox.warning(self, tr("Нужен 7-Zip"), tr("Для ZIP/7z установи 7-Zip и перезапусти GitArchive."))
            return

        self.config.update({
            "storage_path": str(Path(storage).expanduser()),
            "max_storage_gb": self.limit.value(),
            "sync_interval_minutes": self.interval.value(),
            "max_parallel": self.parallel.value(),
            "sync_download_limit_mbps": self.download_limit.value(),
            "new_storage_format": new_storage_format,
            "compression_level": compression_level,
            "background_cpu_priority": str(self.cpu_priority.currentData()),
            "background_memory_priority": str(self.memory_priority.currentData()),
            "theme": self.theme.currentData(),
            "start_with_windows": self.start_with_windows.isChecked(),
            "close_to_tray": self.close_to_tray.isChecked(),
            "proxy_enabled": self.proxy_enabled.isChecked(),
            "proxy_type": self.proxy_type.currentData(),
            "proxy_host": self.proxy_host.text().strip(),
            "proxy_port": self.proxy_port.value(),
        })
        try:
            proxy_url(self.config)
            set_windows_autostart(bool(self.config["start_with_windows"]))
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, tr("Настройки не применены"), str(error))
            return
        save_config(self.config)
        self._default_https_proxy_loaded = False
        self._default_https_proxy_cache = None
        self._https_proxy_cache.clear()
        self._https_proxy_lookup_failed = False
        if self.download_proxy is not None:
            self.download_proxy.set_limit(self.download_limit.value() * 1_000_000)
        self.sync_timer.setInterval(self.interval.value() * 60000)
        self.apply_theme()
        self.refresh()
        self.start_queued()
        self.log(tr("Настройки сохранены."))
        QMessageBox.information(self, "GitArchive", tr("Настройки сохранены."))

    def check_theme(self) -> None:
        if self.config.get("theme") == "system" and (self.dark != windows_dark_theme() or self.accent != windows_accent()):
            self.apply_theme()

    def apply_theme(self) -> None:
        mode = self.config.get("theme", "system")
        self.dark = windows_dark_theme() if mode == "system" else mode == "dark"
        self.accent = windows_accent()
        if self.dark:
            bg, side, card, field, fg, muted, border, hover = "#202020", "#1b1b1b", "#292929", "#303030", "#f5f5f5", "#a7a7a7", "#3a3a3a", "#353535"
        else:
            bg, side, card, field, fg, muted, border, hover = "#f3f3f3", "#f7f7f7", "#ffffff", "#ffffff", "#1a1a1a", "#666666", "#e5e5e5", "#e9e9e9"
        self.setStyleSheet(f"""
            QWidget {{ color:{fg}; font-family:'Segoe UI Variable','Segoe UI'; font-size:13px; }}
            QMainWindow {{ background:{bg}; }}
            #Sidebar {{ background:{side}; border-right:1px solid {border}; }}
            #Logo {{ font-size:17px; font-weight:650; }}
            #Heading {{ font-size:25px; font-weight:650; }}
            #Section {{ font-size:17px; font-weight:600; }}
            #Muted {{ color:{muted}; font-size:11px; }}
            #Nav {{ text-align:left; padding:11px 12px; border:0; border-radius:8px; background:transparent; }}
            #Nav:hover {{ background:{hover}; }}
            #Nav:checked {{ background:{self.accent}; color:white; font-weight:600; }}
            #Accent {{ background:{self.accent}; color:white; border:0; border-radius:7px; padding:9px 14px; font-weight:600; }}
            #Accent:hover {{ border:1px solid white; }}
            #Secondary {{ background:{card}; border:1px solid {border}; border-radius:7px; padding:8px 13px; }}
            #Secondary:hover {{ background:{hover}; }}
            #Card {{ background:{card}; border:1px solid {border}; border-radius:12px; }}
            #Stat {{ font-size:24px; font-weight:650; }}
            QProgressBar {{ background:{field}; border:0; border-radius:3px; height:6px; }}
            QProgressBar::chunk {{ background:{self.accent}; border-radius:3px; }}
            QTableWidget {{ background:{card}; border:1px solid {border}; border-radius:10px; padding:5px; selection-background-color:{hover}; }}
            QTableWidget::item {{ padding:10px 8px; border:0; }}
            QHeaderView::section {{ background:{card}; color:{muted}; border:0; border-bottom:1px solid {border}; padding:10px 8px; font-weight:600; }}
            QMenu {{ background:{card}; border:1px solid {border}; padding:5px; border-radius:8px; }}
            QMenu::item {{ padding:8px 28px 8px 12px; border-radius:5px; }}
            QMenu::item:selected {{ background:{hover}; }}
            QListWidget {{ background:{card}; border:1px solid {border}; border-radius:9px; padding:5px; }}
            QListWidget::item {{ padding:9px 7px; border-radius:6px; }}
            QListWidget::item:selected {{ background:{hover}; }}
            QCheckBox {{ spacing:8px; }}
            QCheckBox::indicator {{ width:17px; height:17px; }}
            QPushButton {{ font-family:'Segoe UI Variable','Segoe UI'; }}
            QLineEdit,QSpinBox,QComboBox {{ background:{field}; border:1px solid {border}; border-radius:6px; padding:7px 9px; min-height:20px; }}
            QComboBox QAbstractItemView {{ background:{card}; selection-background-color:{hover}; }}
            QTextEdit {{ background:{card}; border:1px solid {border}; border-radius:10px; padding:12px; font-family:Consolas; }}
            QScrollBar:vertical {{ background:transparent; width:10px; margin:2px; }}
            QScrollBar::handle:vertical {{ background:{muted}66; border-radius:4px; min-height:28px; }}
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{ height:0; }}
        """)
        set_mica(self, self.dark)


def main() -> int:
    QApplication.setOrganizationName(APP_NAME)
    QApplication.setApplicationName(APP_NAME)
    app = QApplication(sys.argv)
    icon = app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    app.setQuitOnLastWindowClosed(False)
    # Keep Qt's native Windows widget style; the app stylesheet adds Fluent layout and surfaces.

    # Do not let two instances update the same bare mirrors simultaneously.
    instance_lock = QLockFile(str(data_dir() / "instance.lock"))
    instance_lock.setStaleLockTime(30000)
    if not instance_lock.tryLock(0):
        QMessageBox.information(
            None,
            tr("GitArchive уже запущен"),
            tr("Программа уже работает. Открой её из системного трея."),
        )
        return 0

    window = GitArchiveWindow()
    window.instance_lock = instance_lock
    if "--background" not in sys.argv:
        window.show()
    return app.exec()
