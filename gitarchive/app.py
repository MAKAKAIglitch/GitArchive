from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QProcess, QProcessEnvironment, QLockFile, QRunnable, QStandardPaths, QThreadPool, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap, QTextCursor
from .repository import archive_path, find_7zip, folder_size, human_size, parse_git_url

from PySide6.QtSvg import QSvgRenderer

from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QSpinBox,
    QStackedWidget, QStyle, QSystemTrayIcon, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)

APP_NAME = "GitArchive"
GIB = 1024 ** 3


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
        "max_storage_gb": 100,
        "sync_interval_minutes": 10,
        "max_parallel": 3,
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
            raise OSError("Автозапуск Windows доступен только в Windows.")
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
        raise ValueError("Неизвестный тип прокси.")
    host = str(config.get("proxy_host", "")).strip()
    if not host or any(char.isspace() or char in "/\\@?#" for char in host):
        raise ValueError("Укажи корректный адрес прокси без логина и пароля.")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = int(config.get("proxy_port", 0))
    if not 1 <= port <= 65535:
        raise ValueError("Порт прокси должен быть от 1 до 65535.")
    return f"{schemes[proxy_type]}://{host}:{port}"


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

    def __init__(self, args: list[str], timeout: int = 120) -> None:
        super().__init__()
        self.args = args
        self.timeout = timeout
        self.signals = GitCommandSignals()

    def run(self) -> None:
        try:
            result = subprocess.run(
                self.args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            stdout = result.stdout
            if len(stdout) > 1_500_000:
                stdout = stdout[:1_500_000] + "\n\n[GitArchive] Вывод обрезан после 1,5 МБ. Полный коммит остаётся в локальном архиве."
            self.signals.completed.emit(result.returncode, stdout, result.stderr)
        except (OSError, subprocess.TimeoutExpired) as error:
            self.signals.completed.emit(-1, "", str(error))


class CommitHistoryDialog(QDialog):
    def __init__(self, archive: Path, repo_name: str, pool: QThreadPool, parent=None) -> None:
        super().__init__(parent)
        self.archive = archive
        self.repo_name = repo_name
        self.pool = pool
        self.workers: list[GitCommand] = []
        self.commits: dict[str, dict[str, str]] = {}
        self.selected_sha: str | None = None
        self.setWindowTitle(f"История коммитов — {repo_name}")
        self.resize(1050, 680)
        self.setMinimumSize(760, 480)

        layout = QVBoxLayout(self)
        header = QLabel(f"История локального архива · {repo_name}")
        header.setObjectName("Section")
        layout.addWidget(header)
        body = QHBoxLayout()
        self.commit_list = QListWidget()
        self.commit_list.setMinimumWidth(330)
        self.commit_list.currentItemChanged.connect(self.show_commit)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("Выбери коммит, чтобы посмотреть его описание и изменённые файлы.")
        body.addWidget(self.commit_list, 1)
        body.addWidget(self.details, 2)
        layout.addLayout(body, 1)
        actions = QHBoxLayout()
        self.download = QPushButton("Скачать выбранный коммит (.zip)")
        self.download.setObjectName("Accent")
        self.download.setEnabled(False)
        self.download.clicked.connect(self.export_commit)
        close = QPushButton("Закрыть")
        close.setObjectName("Secondary")
        close.clicked.connect(self.accept)
        actions.addWidget(self.download)
        actions.addStretch(1)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.load_history()

    def run_git(self, args: list[str], callback, timeout: int = 120) -> None:
        worker = GitCommand(args, timeout)
        self.workers.append(worker)
        worker.signals.completed.connect(
            lambda code, stdout, stderr, w=worker: self._worker_done(w, callback, code, stdout, stderr)
        )
        self.pool.start(worker)

    def _worker_done(self, worker, callback, code: int, stdout: str, stderr: str) -> None:
        if worker in self.workers:
            self.workers.remove(worker)
        callback(code, stdout, stderr)

    def load_history(self) -> None:
        self.commit_list.clear()
        self.commit_list.addItem("Загрузка истории…")
        self.run_git([
            "git", "--git-dir", str(self.archive),
            "log", "-n", "250", "--date=iso-strict",
            "--format=%H%x1f%h%x1f%an%x1f%aI%x1f%s",
            "--all", "--decorate=short",
        ], self.history_loaded)

    def history_loaded(self, code: int, stdout: str, stderr: str) -> None:
        self.commit_list.clear()
        if code != 0:
            self.details.setPlainText(stderr or "Не удалось прочитать историю Git.")
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
            self.details.setPlainText("В архиве пока нет коммитов.")
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
        self.details.setPlainText("Загрузка сведений о коммите…")
        requested_sha = self.selected_sha
        self.run_git([
            "git", "--git-dir", str(self.archive),
            "show", "--no-ext-diff", "--no-color", "--no-renames", "--unified=3",
            "--stat", "--patch", "--summary", "--format=fuller", requested_sha,
        ], lambda code, stdout, stderr: self.commit_loaded(requested_sha, code, stdout, stderr))

    def commit_loaded(self, requested_sha: str, code: int, stdout: str, stderr: str) -> None:
        if requested_sha != self.selected_sha:
            return
        self.details.setPlainText(stdout if code == 0 else (stderr or "Не удалось прочитать коммит."))

    def export_commit(self) -> None:
        sha = self.selected_sha
        if not sha or not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            return
        destination, _ = QFileDialog.getSaveFileName(
            self, "Скачать коммит", f"{self.repo_name}-{sha[:12]}.zip", "ZIP-архив (*.zip)"
        )
        if not destination:
            return
        if not destination.lower().endswith(".zip"):
            destination += ".zip"
        self.download.setEnabled(False)
        self.details.append("\n\nСоздание ZIP-архива…")
        self.run_git([
            "git", "--git-dir", str(self.archive),
            "archive", "--format=zip", f"--output={destination}", sha,
        ], lambda code, out, err: self.export_finished(destination, code, out, err), timeout=3600)

    def export_finished(self, destination: str, code: int, stdout: str, stderr: str) -> None:
        self.download.setEnabled(bool(self.selected_sha))
        if code == 0 and Path(destination).is_file():
            self.details.append(f"\nГотово: {destination}")
            QMessageBox.information(self, "Коммит сохранён", f"Архив коммита создан:\n{destination}")
        else:
            self.details.append(f"\nОшибка экспорта: {stderr or stdout}")


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
        self.size_cache: dict[str, int] = {}
        self.size_scan_running = False
        self.size_scan_ready = False
        self.size_worker: SizeScan | None = None
        self.thread_pool = QThreadPool.globalInstance()
        self.history_workers: list[GitCommand] = []
        self.seven_zip_path = find_7zip()
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
        for index, title in enumerate(("Обзор", "Репозитории", "Журнал", "Настройки")):
            button = QPushButton(title)
            button.setObjectName("Nav")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, page=index: self.go(page))
            self.nav.append(button)
            side.addWidget(button)
        self.nav[0].setChecked(True)
        side.addStretch(1)
        footer = QLabel("LOCAL-FIRST ARCHIVER\nGit mirrors stay on this PC")
        footer.setObjectName("Muted")
        side.addWidget(footer)

        right = QWidget()
        body = QVBoxLayout(right)
        body.setContentsMargins(28, 24, 28, 20)
        body.setSpacing(16)
        top = QHBoxLayout()
        self.heading = QLabel("Обзор")
        self.heading.setObjectName("Heading")
        self.heading.setFont(QFont("Segoe UI Variable", 25, QFont.Weight.DemiBold))
        top.addWidget(self.heading)
        top.addStretch(1)
        sync = QPushButton("⟳  Синхронизировать")
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
            ("repos", "Репозитории", "0", "В списке"),
            ("mirrors", "Локальные копии", "0", "Git mirror"),
            ("size", "Занято", "0 Б", "Все архивы"),
            ("running", "Синхронизация", "0", "Активных задач"),
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
        label = QLabel("Ваши проекты")
        label.setObjectName("Section")
        bar.addWidget(label)
        bar.addStretch(1)
        add = QPushButton("＋  Добавить репозиторий")
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
            ("＋  Добавить", "Accent", self.add_repository),
            ("⟳  Обновить выбранный", "Secondary", self.sync_selected),
            ("Открыть архив", "Secondary", self.open_selected_folder),
            ("Восстановить копию", "Secondary", self.restore_selected),
        ):
            button = QPushButton(text)
            button.setObjectName(name)
            button.clicked.connect(callback)
            bar.addWidget(button)
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
        self.log_view.setPlaceholderText("События синхронизации появятся здесь.")
        self.log_view.setPlainText("\n".join(self.logs))
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)
        a.addWidget(self.log_view)
        self.pages.addWidget(activity)

        settings = QWidget()
        s = QVBoxLayout(settings)
        s.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("Card")
        form = QFormLayout(card)
        form.setContentsMargins(22, 22, 22, 22)
        form.setVerticalSpacing(15)
        path_row = QHBoxLayout()
        self.storage_edit = QLineEdit(self.config["storage_path"])
        browse = QPushButton("Обзор…")
        browse.setObjectName("Secondary")
        browse.clicked.connect(self.choose_storage)
        path_row.addWidget(self.storage_edit, 1)
        path_row.addWidget(browse)
        path_widget = QWidget()
        path_widget.setLayout(path_row)
        form.addRow("Папка новых архивов", path_widget)
        self.limit = QSpinBox()
        self.limit.setRange(1, 100000)
        self.limit.setSuffix(" ГБ")
        self.limit.setValue(int(self.config["max_storage_gb"]))
        form.addRow("Общий лимит", self.limit)
        self.interval = QSpinBox()
        self.interval.setRange(1, 1440)
        self.interval.setSuffix(" мин")
        self.interval.setValue(int(self.config["sync_interval_minutes"]))
        form.addRow("Интервал синхронизации", self.interval)
        self.parallel = QSpinBox()
        self.parallel.setRange(1, 8)
        self.parallel.setValue(int(self.config["max_parallel"]))
        form.addRow("Параллельные задачи", self.parallel)

        self.storage_format = QComboBox()
        self.storage_format.addItem("Папка .git — без сжатия", "folder")
        self.storage_format.addItem("ZIP (.zip)", "zip")
        self.storage_format.addItem("7z (.7z)", "7z")
        if not self.seven_zip_path:
            model = self.storage_format.model()
            for index in (1, 2):
                item = model.item(index) if hasattr(model, "item") else None
                if item:
                    item.setEnabled(False)
            self.storage_format.setToolTip("Для ZIP и 7z нужен установленный 7-Zip с командной строкой. Перезапусти GitArchive после установки.")
        saved_format = str(self.config.get("new_storage_format", "folder")).lower()
        if saved_format not in {"folder", "zip", "7z"} or (saved_format != "folder" and not self.seven_zip_path):
            saved_format = "folder"
        self.storage_format.setCurrentIndex(max(0, self.storage_format.findData(saved_format)))
        form.addRow("Формат новых архивов", self.storage_format)

        self.compression_level = QComboBox()
        self.compression_level.addItem("Минимум — быстрее", "min")
        self.compression_level.addItem("Среднее", "medium")
        self.compression_level.addItem("Максимум — медленнее", "max")
        saved_level = str(self.config.get("compression_level", "medium")).lower()
        self.compression_level.setCurrentIndex(max(0, self.compression_level.findData(saved_level)))
        form.addRow("Сила сжатия", self.compression_level)
        self.compression_note = QLabel()
        self.compression_note.setObjectName("Muted")
        self.compression_note.setWordWrap(True)
        form.addRow("", self.compression_note)
        self.storage_format.currentIndexChanged.connect(self.update_storage_format_controls)
        self.update_storage_format_controls()

        self.theme = QComboBox()
        for label, value in (("Как в Windows", "system"), ("Светлая", "light"), ("Тёмная", "dark")):
            self.theme.addItem(label, value)
        self.theme.setCurrentIndex(max(0, self.theme.findData(self.config["theme"])))
        form.addRow("Тема", self.theme)
        self.start_with_windows = QCheckBox("Запускать вместе с Windows")
        self.start_with_windows.setChecked(bool(self.config.get("start_with_windows", False)))
        form.addRow("Автозапуск", self.start_with_windows)
        self.close_to_tray = QCheckBox("При закрытии сворачивать в системный трей")
        self.close_to_tray.setChecked(bool(self.config.get("close_to_tray", True)))
        form.addRow("Поведение окна", self.close_to_tray)
        self.proxy_enabled = QCheckBox("Использовать прокси для HTTPS Git-соединений")
        self.proxy_enabled.setChecked(bool(self.config.get("proxy_enabled", False)))
        form.addRow("Прокси", self.proxy_enabled)
        self.proxy_type = QComboBox()
        for proxy_label in ("HTTP", "HTTPS", "SOCKS4", "SOCKS5"):
            self.proxy_type.addItem(proxy_label, proxy_label)
        self.proxy_type.setCurrentIndex(max(0, self.proxy_type.findData(self.config.get("proxy_type", "SOCKS5"))))
        form.addRow("Тип прокси", self.proxy_type)
        proxy_row = QHBoxLayout()
        self.proxy_host = QLineEdit(str(self.config.get("proxy_host", "127.0.0.1")))
        self.proxy_host.setPlaceholderText("Адрес прокси")
        self.proxy_port = QSpinBox()
        self.proxy_port.setRange(1, 65535)
        self.proxy_port.setValue(int(self.config.get("proxy_port", 1080)))
        self.proxy_port.setFixedWidth(120)
        proxy_row.addWidget(self.proxy_host, 1)
        proxy_row.addWidget(self.proxy_port)
        proxy_widget = QWidget()
        proxy_widget.setLayout(proxy_row)
        form.addRow("Сервер / порт", proxy_widget)
        s.addWidget(card)
        note = QLabel("Лимит мягкий: при достижении порога новые задачи ставятся на паузу. Уже начатая загрузка может превысить его. Архивы автоматически не удаляются.")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        s.addWidget(note)
        save = QPushButton("Сохранить настройки")
        save.setObjectName("Accent")
        save.clicked.connect(self.save_settings)
        s.addWidget(save, 0, Qt.AlignmentFlag.AlignLeft)
        s.addStretch(1)
        self.pages.addWidget(settings)

    def make_table(self) -> QTableWidget:
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["Репозиторий", "Состояние", "Последняя синхронизация", "Размер"])
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
        self.tray.setToolTip("GitArchive — локальный архив Git-репозиториев")
        menu = QMenu()
        show = QAction("Открыть GitArchive", self)
        show.triggered.connect(self.show_from_tray)
        sync = QAction("Синхронизировать сейчас", self)
        sync.triggered.connect(self.sync_all)
        exit_action = QAction("Выход", self)
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
        self.heading.setText(("Обзор", "Репозитории", "Журнал", "Настройки")[page])
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
            self.tray.showMessage(APP_NAME, "GitArchive продолжает работу в системном трее.", QSystemTrayIcon.MessageIcon.Information, 2500)
        else:
            event.accept()

    def exit_app(self) -> None:
        self.allow_exit = True
        for process in list(self.processes.values()):
            if process.state() != QProcess.ProcessState.NotRunning:
                process.terminate()
        if self.restore_process and self.restore_process.state() != QProcess.ProcessState.NotRunning:
            self.restore_process.terminate()
        self.tray.hide()
        QApplication.quit()

    def add_repository(self) -> None:
        prompt = (
            "HTTPS или SSH clone URL (GitHub, GitLab, Codeberg, Bitbucket, "
            "Gitea, Forgejo или свой сервер):"
        )
        clipboard_value = QApplication.clipboard().text().strip()
        try:
            parse_git_url(clipboard_value)
        except ValueError:
            clipboard_value = ""

        value, ok = QInputDialog.getText(
            self,
            "Добавить Git-репозиторий",
            prompt,
            QLineEdit.EchoMode.Normal,
            clipboard_value,
        )
        if not ok:
            return
        try:
            url, namespace, name, host, provider = parse_git_url(value)
        except ValueError as error:
            QMessageBox.warning(self, "Ссылка не распознана", str(error))
            return

        storage_format = str(self.config.get("new_storage_format", "folder")).lower()
        compression_level = str(self.config.get("compression_level", "medium")).lower()
        seven_zip = self.seven_zip_path or find_7zip()
        if storage_format not in {"folder", "zip", "7z"}:
            storage_format = "folder"
        if storage_format != "folder" and not seven_zip:
            QMessageBox.warning(
                self,
                "Нужен 7-Zip",
                "Для хранения в ZIP или 7z установи 7-Zip и перезапусти GitArchive. "
                "Новые репозитории пока можно сохранять в обычной папке .git.",
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
                    self, "Уже добавлен", "Этот репозиторий уже есть в GitArchive."
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
            "status": "В очереди",
            "last_sync": None,
            "last_error": "",
        }
        self.config["repositories"].append(repo)
        save_config(self.config)
        self.log(f"Добавлен {repository_label(repo)} · формат: {storage_format}")
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
        open_history = menu.addAction("Открыть")
        sync = menu.addAction("Обновить")
        open_folder = menu.addAction("Открыть папку архива")
        restore = menu.addAction("Восстановить рабочую копию")
        menu.addSeparator()
        remove = menu.addAction("Удалить из списка")
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
                    self, "Архив ещё не готов",
                    "Сначала дождись первой успешной синхронизации этого репозитория."
                )
                return
            self.show_commit_history(archive, repo.get("name", "repository"))
            return

        if not archive.is_file():
            QMessageBox.information(self, "Архив ещё не готов", "Сначала дождись первой успешной синхронизации этого репозитория.")
            return
        if not self.core.is_file():
            QMessageBox.warning(self, "Rust-движок не найден", "Запусти start.bat, чтобы собрать движок.")
            return
        seven_zip = self.seven_zip_path or find_7zip()
        if not seven_zip:
            QMessageBox.warning(self, "Нужен 7-Zip", "Для чтения сжатого архива установи 7-Zip и перезапусти GitArchive.")
            return

        temporary_root = Path(tempfile.mkdtemp(prefix=".gitarchive-history-", dir=str(archive.parent)))
        worker = GitCommand([
            str(self.core), "materialize",
            "--archive", str(archive),
            "--destination", str(temporary_root),
            "--storage-format", storage_format,
            "--seven-zip", seven_zip,
        ], timeout=3600)
        self.history_workers.append(worker)
        worker.signals.completed.connect(
            lambda code, stdout, stderr, w=worker, root=temporary_root, name=repo.get("name", "repository"):
                self.history_materialized(w, root, name, code, stdout, stderr)
        )
        self.thread_pool.start(worker)
        self.log(f"Распаковка {repository_label(repo)} во временную папку для просмотра коммитов.")

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
                self, "Не удалось открыть историю",
                stderr or stdout or "Не удалось безопасно распаковать архив для просмотра."
            )
            return
        self.show_commit_history(mirror, repo_name)
        shutil.rmtree(temporary_root, ignore_errors=True)

    def show_commit_history(self, archive: Path, repo_name: str) -> None:
        dialog = CommitHistoryDialog(
            archive, repo_name, self.thread_pool, self
        )
        dialog.setStyleSheet(self.styleSheet())
        dialog.exec()

    def remove_repository(self, repo: dict) -> None:
        answer = QMessageBox.question(
            self,
            "Убрать репозиторий из списка?",
            f"{repository_label(repo)} будет удалён из списка GitArchive.\n"
            "Локальный архив на диске останется нетронутым.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if repo.get("id") in self.processes:
            QMessageBox.warning(
                self, "Синхронизация выполняется",
                "Дождись завершения синхронизации перед удалением записи."
            )
            return
        self.config["repositories"] = [
            item for item in self.config["repositories"] if item.get("id") != repo.get("id")
        ]
        self.queue = [repo_id for repo_id in self.queue if repo_id != repo.get("id")]
        save_config(self.config)
        self.log(f"Убран из списка (архив сохранён): {repository_label(repo)}")
        self.refresh()
        self.scan_sizes()

    def selected_repo(self, table: QTableWidget) -> dict | None:
        row = table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Выбор проекта", "Выбери репозиторий в таблице.")
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
            QMessageBox.information(self, "Восстановление уже идёт", "Дождись завершения текущего восстановления.")
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
            QMessageBox.information(self, "Архив не найден", "Сначала дождись первой успешной синхронизации репозитория.")
            return
        if not self.core.is_file():
            QMessageBox.warning(self, "Rust-движок не найден", "Запусти start.bat, чтобы собрать движок.")
            return

        seven_zip = self.seven_zip_path or find_7zip()
        if storage_format != "folder" and not seven_zip:
            QMessageBox.warning(self, "Нужен 7-Zip", "Для восстановления ZIP/7z установи 7-Zip и перезапусти GitArchive.")
            return

        parent = QFileDialog.getExistingDirectory(
            self,
            "Куда восстановить рабочую копию",
            str(Path.home()),
        )
        if not parent:
            return
        destination = Path(parent) / repo["name"]
        if destination.exists():
            QMessageBox.warning(
                self,
                "Папка уже существует",
                f"GitArchive не будет перезаписывать существующую папку:\n{destination}",
            )
            return

        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(lambda p=process: self.read_restore_output(p))
        process.finished.connect(lambda code, _status, p=process: self.restore_finished(p, code))
        process.errorOccurred.connect(lambda error, p=process: self.restore_error(p, error))
        self.restore_process = process
        self.restore_destination = destination
        self.log(f"Восстановление {repository_label(repo)} → {destination}")
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
            self.log(f"Рабочая копия восстановлена: {destination}")
            QMessageBox.information(
                self,
                "Восстановление завершено",
                f"Рабочая копия и сохранённые исторические refs восстановлены в:\n{destination}",
            )
            if sys.platform == "win32":
                os.startfile(str(destination))
        else:
            self.log(f"Восстановление завершилось с ошибкой (код {code}). Локальный архив не изменён.")
            QMessageBox.warning(
                self,
                "Ошибка восстановления",
                "Не удалось полностью восстановить рабочую копию. Подробности находятся в журнале. Исходный архив не изменялся.",
            )

    def restore_error(self, process: QProcess, error: QProcess.ProcessError) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return
        if self.restore_process is process:
            self.restore_process = None
            self.restore_destination = None
        process.deleteLater()
        self.log("Не удалось запустить Rust-движок для восстановления.")
        QMessageBox.warning(self, "Ошибка запуска", "Не удалось запустить Rust-движок.")

    def open_selected_folder(self, table: QTableWidget | None = None) -> None:
        repo = self.selected_repo(table or self.repo_table)
        if not repo:
            return
        path = Path(repo["local_path"])
        if not path.exists():
            QMessageBox.information(self, "Архива пока нет", "Файл или папка появится после первой успешной синхронизации.")
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
            QMessageBox.warning(self, "Rust-движок не найден", "Запусти start.bat: он соберёт Rust-движок и откроет приложение.")
            return
        self.queue.append(repo_id)
        self.start_queued()

    def sync_all(self) -> None:
        for repo in self.config["repositories"]:
            repo_id = repo.get("id")
            if repo_id and repo_id not in self.processes and repo_id not in self.queue:
                self.queue.append(repo_id)
        self.start_queued()
        self.refresh()

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
                for repo_id in self.queue:
                    repo = self.find_repo(repo_id)
                    if repo:
                        repo["status"] = "Ожидание лимита"
                self.queue.clear()
                self.log("Достигнут лимит хранилища. Новые синхронизации приостановлены; архивы сохранены.")
                break

            repo_id = self.queue.pop(0)
            repo = self.find_repo(repo_id)
            if not repo:
                continue

            storage_format = repository_storage_format(repo)
            if storage_format != "folder" and self.processes:
                # Compression repacks the whole repository; run it alone to avoid
                # competing with other Git tasks for CPU, RAM and disk bandwidth.
                self.queue.insert(0, repo_id)
                break

            seven_zip = self.seven_zip_path or find_7zip()
            if storage_format != "folder" and not seven_zip:
                repo["status"] = "Ошибка — нужен 7-Zip"
                repo["last_error"] = "Для ZIP/7z требуется установленный 7-Zip CLI."
                save_config(self.config)
                self.log(f"Пропуск {repository_label(repo)}: 7-Zip не найден.")
                continue

            process = QProcess(self)
            process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            proxy = proxy_url(self.config)
            if proxy:
                environment = QProcessEnvironment.systemEnvironment()
                try:
                    config_count = max(0, int(environment.value("GIT_CONFIG_COUNT") or "0"))
                except ValueError:
                    config_count = 0
                environment.insert("GIT_CONFIG_COUNT", str(config_count + 1))
                environment.insert(f"GIT_CONFIG_KEY_{config_count}", "http.proxy")
                environment.insert(f"GIT_CONFIG_VALUE_{config_count}", proxy)
                process.setProcessEnvironment(environment)
            process.readyReadStandardOutput.connect(lambda p=process: self.read_output(p))
            process.finished.connect(lambda code, _status, p=process: self.finished(p, code))
            process.errorOccurred.connect(lambda error, p=process: self.process_error(p, error))
            self.processes[repo_id] = process
            repo["status"] = "Синхронизация"
            repo["last_error"] = ""
            save_config(self.config)
            self.log(f"Начата синхронизация {repository_label(repo)}")
            arguments = [
                "sync",
                "--url", repo["url"],
                "--destination", repo["local_path"],
                "--storage-format", storage_format,
                "--compression-level", str(repo.get("compression_level") or "medium"),
            ]
            if storage_format != "folder" and seven_zip:
                arguments.extend(["--seven-zip", seven_zip])
            process.start(str(self.core), arguments)
        self.refresh()

    def read_output(self, process: QProcess) -> None:
        text = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        repo_id = next((key for key, item in self.processes.items() if item is process), None)
        repo = self.find_repo(repo_id) if repo_id else None
        prefix = f"{repository_label(repo)}: " if repo else ""
        for line in text.splitlines():
            if line.strip():
                self.log(prefix + line.strip())

    def process_error(self, process: QProcess, error: QProcess.ProcessError) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return
        self.read_output(process)
        repo_id = next((key for key, item in self.processes.items() if item is process), None)
        repo = self.find_repo(repo_id) if repo_id else None
        if repo:
            repo["status"] = "Ошибка запуска"
            repo["last_error"] = "Не удалось запустить Rust-движок."
            self.processes.pop(repo_id, None)
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
        repo = self.find_repo(repo_id)
        if repo:
            if code == 0:
                repo["status"] = "Готово"
                repo["last_sync"] = datetime.now().astimezone().isoformat(timespec="minutes")
                repo["last_error"] = ""
                self.log(f"Готово: {repository_label(repo)}")
            else:
                repo["status"] = "Ошибка — архив сохранён"
                repo["last_error"] = f"Код завершения: {code}"
                self.log(f"Ошибка обновления {repository_label(repo)}; локальное зеркало оставлено на месте.")
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
                values = (repository_label(repo), repo.get("status", "Добавлен"), last, human_size(bytes_used) if bytes_used else "—")
                for col, value in enumerate(values):
                    cell = QTableWidgetItem(value)
                    if col == 0:
                        cell.setData(Qt.ItemDataRole.UserRole, repo.get("id"))
                    if col == 1:
                        cell.setToolTip(repo.get("last_error", ""))
                    table.setItem(row, col, cell)
            table.resizeRowsToContents()
        self.hint.setText(f"Занято {human_size(size)} из {self.config['max_storage_gb']} ГБ. Архивы не удаляются при недоступности исходного проекта.")

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

        # Keep one small rotated log locally; no telemetry or remote uploads.
        try:
            log_dir = data_dir() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "gitarchive.log"
            if log_path.exists() and log_path.stat().st_size > 2 * 1024 * 1024:
                rotated = log_dir / "gitarchive.log.1"
                rotated.unlink(missing_ok=True)
                log_path.replace(rotated)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        except OSError:
            pass

        if hasattr(self, "log_view"):
            self.log_view.setPlainText("\n".join(self.logs))
            self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def choose_storage(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Папка для новых архивов", self.storage_edit.text())
        if path:
            self.storage_edit.setText(path)

    def update_storage_format_controls(self, *_args) -> None:
        storage_format = self.storage_format.currentData()
        compressed = storage_format in {"zip", "7z"}
        self.compression_level.setEnabled(compressed and bool(self.seven_zip_path))
        if not self.seven_zip_path:
            note = "ZIP и 7z требуют установленный 7-Zip с консольным файлом 7z.exe. После установки перезапусти GitArchive."
        elif not compressed:
            note = "Папка .git — рекомендуемый вариант: Git обновляет её на месте без повторной распаковки и сжатия."
        elif storage_format == "zip":
            note = (
                "ZIP совместим почти со всеми архиваторами. При каждом обновлении зеркало временно распаковывается "
                "и собирается заново. GitArchive использует один поток 7-Zip."
            )
        else:
            note = (
                "7z обычно сжимает лучше ZIP, но обновление медленнее. Используется один поток и ограниченный "
                "словарь до 64 МБ, чтобы ограничить расход памяти."
            )
        note += " Формат применяется только к новым репозиториям после сохранения настроек; существующие не меняются."
        self.compression_note.setText(note)

    def save_settings(self) -> None:
        storage = self.storage_edit.text().strip()
        if not storage:
            QMessageBox.warning(self, "Папка не указана", "Укажи папку для новых архивов.")
            return
        try:
            Path(storage).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError as error:
            QMessageBox.warning(self, "Ошибка папки", str(error))
            return

        new_storage_format = str(self.storage_format.currentData() or "folder")
        compression_level = str(self.compression_level.currentData() or "medium")
        self.seven_zip_path = find_7zip()
        if new_storage_format in {"zip", "7z"} and not self.seven_zip_path:
            QMessageBox.warning(self, "Нужен 7-Zip", "Для ZIP/7z установи 7-Zip и перезапусти GitArchive.")
            return

        self.config.update({
            "storage_path": str(Path(storage).expanduser()),
            "max_storage_gb": self.limit.value(),
            "sync_interval_minutes": self.interval.value(),
            "max_parallel": self.parallel.value(),
            "new_storage_format": new_storage_format,
            "compression_level": compression_level,
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
            QMessageBox.warning(self, "Настройки не применены", str(error))
            return
        save_config(self.config)
        self.sync_timer.setInterval(self.interval.value() * 60000)
        self.apply_theme()
        self.refresh()
        self.start_queued()
        self.log("Настройки сохранены.")
        QMessageBox.information(self, "GitArchive", "Настройки сохранены.")

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
            "GitArchive уже запущен",
            "Программа уже работает. Открой её из системного трея.",
        )
        return 0

    window = GitArchiveWindow()
    window.instance_lock = instance_lock
    if "--background" not in sys.argv:
        window.show()
    return app.exec()
