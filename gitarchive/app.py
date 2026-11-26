from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QEvent, QObject, QProcess, QRunnable, QStandardPaths, QThreadPool, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMenu, QMessageBox, QPushButton, QSpinBox, QStackedWidget, QStyle,
    QSystemTrayIcon, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

APP_NAME = "GitArchive"
GIB = 1024 ** 3


def data_dir() -> Path:
    value = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    path = Path(value) if value else Path.home() / "AppData" / "Local" / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def defaults() -> dict:
    return {
        "storage_path": str(Path.home() / "GitArchive"),
        "max_storage_gb": 100,
        "sync_interval_minutes": 10,
        "max_parallel": 3,
        "theme": "system",
        "close_to_tray": True,
        "repositories": [],
    }


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


def parse_github_url(value: str) -> tuple[str, str, str]:
    value = value.strip()
    if value.startswith("git@github.com:"):
        parts = value.split(":", 1)[1].strip("/").split("/")
        url_type = "ssh"
    else:
        parsed = urlsplit(value if "://" in value else "https://" + value)
        if parsed.hostname not in {"github.com", "www.github.com"}:
            raise ValueError("Нужна ссылка именно на github.com.")
        parts = parsed.path.strip("/").split("/")
        url_type = "https"
    if len(parts) < 2:
        raise ValueError("Формат ссылки: https://github.com/owner/repository")
    owner, name = (re.sub(r"\.git$", "", part) for part in parts[:2])
    if owner in {".", ".."} or name in {".", ".."} or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part or "") for part in (owner, name)):
        raise ValueError("Не удалось распознать owner/repository.")
    url = f"git@github.com:{owner}/{name}.git" if url_type == "ssh" else f"https://github.com/{owner}/{name}.git"
    return url, owner, name


def folder_size(path: Path) -> int:
    if str(path) in {"", "."} or not path.is_dir():
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
    value = float(max(size, 0))
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{int(value)} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"


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
        color = QColor(int(value) & 255, (int(value) >> 8) & 255, (int(value) >> 16) & 255)
        if color.isValid() and color.lightness() > 45:
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
        self.queue: list[str] = []
        self.logs: list[str] = []
        self.size_cache: dict[str, int] = {}
        self.size_scan_running = False
        self.size_scan_ready = False
        self.size_worker: SizeScan | None = None
        self.thread_pool = QThreadPool.globalInstance()
        self.allow_exit = False
        self.dark = True
        self.accent = "#60a5fa"
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
        root = Path(__file__).resolve().parents[1]
        name = "gitarchive-core.exe" if sys.platform == "win32" else "gitarchive-core"
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
        logo = QLabel("◈  GitArchive")
        logo.setObjectName("Logo")
        logo.setFont(QFont("Segoe UI Variable", 17, QFont.Weight.DemiBold))
        side.addWidget(logo)
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
            ("Открыть папку", "Secondary", self.open_selected_folder),
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
        self.theme = QComboBox()
        for label, value in (("Как в Windows", "system"), ("Светлая", "light"), ("Тёмная", "dark")):
            self.theme.addItem(label, value)
        self.theme.setCurrentIndex(max(0, self.theme.findData(self.config["theme"])))
        form.addRow("Тема", self.theme)
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
        table.cellDoubleClicked.connect(lambda *_args, t=table: self.open_selected_folder(t))
        return table

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        self.tray.setToolTip("GitArchive — локальный архив GitHub")
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
        self.tray.hide()
        QApplication.quit()

    def add_repository(self) -> None:
        value, ok = QInputDialog.getText(self, "Добавить репозиторий", "Ссылка на GitHub:", QLineEdit.EchoMode.Normal, "https://github.com/owner/repository")
        if not ok:
            return
        try:
            url, owner, name = parse_github_url(value)
        except ValueError as error:
            QMessageBox.warning(self, "Ссылка не распознана", str(error))
            return
        if any(repo.get("url", "").lower() == url.lower() for repo in self.config["repositories"]):
            QMessageBox.information(self, "Уже добавлен", "Этот репозиторий уже есть в GitArchive.")
            return
        path = Path(self.config["storage_path"]) / owner / f"{name}.git"
        repo = {
            "id": uuid.uuid4().hex, "owner": owner, "name": name, "url": url,
            "local_path": str(path), "status": "В очереди", "last_sync": None, "last_error": "",
        }
        self.config["repositories"].append(repo)
        save_config(self.config)
        self.log(f"Добавлен {owner}/{name}")
        self.refresh()
        self.sync_one(repo["id"])

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

    def open_selected_folder(self, table: QTableWidget | None = None) -> None:
        repo = self.selected_repo(table or self.repo_table)
        if not repo:
            return
        path = Path(repo["local_path"])
        if not path.exists():
            QMessageBox.information(self, "Архива пока нет", "Папка появится после первой успешной синхронизации.")
        elif sys.platform == "win32":
            os.startfile(str(path))
        else:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

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
            process = QProcess(self)
            process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            process.readyReadStandardOutput.connect(lambda p=process: self.read_output(p))
            process.finished.connect(lambda code, _status, p=process: self.finished(p, code))
            process.errorOccurred.connect(lambda error, p=process: self.process_error(p, error))
            self.processes[repo_id] = process
            repo["status"] = "Синхронизация"
            repo["last_error"] = ""
            save_config(self.config)
            self.log(f"Начата синхронизация {repo['owner']}/{repo['name']}")
            process.start(str(self.core), ["sync", "--url", repo["url"], "--destination", repo["local_path"]])
        self.refresh()

    def read_output(self, process: QProcess) -> None:
        text = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        repo_id = next((key for key, item in self.processes.items() if item is process), None)
        repo = self.find_repo(repo_id) if repo_id else None
        prefix = f"{repo['owner']}/{repo['name']}: " if repo else ""
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
                self.log(f"Готово: {repo['owner']}/{repo['name']}")
            else:
                repo["status"] = "Ошибка — архив сохранён"
                repo["last_error"] = f"Код завершения: {code}"
                self.log(f"Ошибка обновления {repo['owner']}/{repo['name']}; локальное зеркало оставлено на месте.")
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
        self.stats["mirrors"].setText(str(sum(bool(r.get("local_path")) and Path(r["local_path"]).is_dir() for r in repos)))
        self.stats["size"].setText(human_size(size))
        self.stats["running"].setText(str(len(self.processes)))
        for table in (self.dashboard_table, self.repo_table):
            table.setRowCount(len(repos))
            for row, repo in enumerate(repos):
                local = Path(repo.get("local_path", ".")) if repo.get("local_path") else Path("__missing__")
                bytes_used = self.size_cache.get(repo.get("id"), 0)
                last = repo.get("last_sync") or "—"
                try:
                    if last != "—":
                        last = datetime.fromisoformat(last).strftime("%d.%m.%Y %H:%M")
                except ValueError:
                    pass
                values = (f"{repo.get('owner', '')}/{repo.get('name', '')}", repo.get("status", "Добавлен"), last, human_size(bytes_used) if bytes_used else "—")
                for col, value in enumerate(values):
                    cell = QTableWidgetItem(value)
                    if col == 0:
                        cell.setData(Qt.ItemDataRole.UserRole, repo.get("id"))
                    if col == 1:
                        cell.setToolTip(repo.get("last_error", ""))
                    table.setItem(row, col, cell)
            table.resizeRowsToContents()
        self.hint.setText(f"Занято {human_size(size)} из {self.config['max_storage_gb']} ГБ. Архивы не удаляются при недоступности исходного проекта.")

    def log(self, message: str) -> None:
        self.logs.append(f"[{datetime.now().astimezone().strftime('%H:%M:%S')}] {message}")
        self.logs = self.logs[-1200:]
        if hasattr(self, "log_view"):
            self.log_view.setPlainText("\n".join(self.logs))
            self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def choose_storage(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Папка для новых архивов", self.storage_edit.text())
        if path:
            self.storage_edit.setText(path)

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
        self.config.update({
            "storage_path": str(Path(storage).expanduser()),
            "max_storage_gb": self.limit.value(),
            "sync_interval_minutes": self.interval.value(),
            "max_parallel": self.parallel.value(),
            "theme": self.theme.currentData(),
        })
        save_config(self.config)
        self.sync_timer.setInterval(self.interval.value() * 60000)
        self.apply_theme()
        self.refresh()
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
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    window = GitArchiveWindow()
    window.show()
    return app.exec()
