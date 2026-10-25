import os
import sys
import posixpath
import time
from ftplib import FTP, FTP_TLS, error_perm
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QFileDialog,
    QProgressBar, QComboBox, QSpinBox, QPlainTextEdit, QMessageBox, QSplitter,
    QHeaderView
)

APP_NAME = "FileSwift"
BLOCK_SIZE = 256 * 1024


def fmt_size(size):
    if size is None:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return str(size)


class FTPConnection:
    def __init__(self, host, port, username, password, tls=False, timeout=25):
        cls = FTP_TLS if tls else FTP
        self.ftp = cls(timeout=timeout, encoding="utf-8")
        self.ftp.connect(host, port)
        self.ftp.login(username or "anonymous", password or "anonymous@")
        if tls:
            self.ftp.prot_p()
        self.ftp.set_pasv(True)

    def close(self):
        try:
            self.ftp.quit()
        except Exception:
            try:
                self.ftp.close()
            except Exception:
                pass


class ConnectWorker(QThread):
    connected = Signal(str, object)
    failed = Signal(str)

    def __init__(self, settings):
        super().__init__()
        self.settings = settings

    def run(self):
        try:
            conn = FTPConnection(**self.settings)
            pwd = conn.ftp.pwd()
            conn.close()
            self.connected.emit(pwd, self.settings)
        except Exception as exc:
            self.failed.emit(str(exc))


class ListingWorker(QThread):
    finished_listing = Signal(str, list)
    failed = Signal(str)

    def __init__(self, settings, path):
        super().__init__()
        self.settings, self.path = settings, path

    def run(self):
        conn = None
        try:
            conn = FTPConnection(**self.settings)
            ftp = conn.ftp
            ftp.cwd(self.path)
            rows = []
            try:
                for name, facts in ftp.mlsd():
                    if name in (".", ".."):
                        continue
                    typ = facts.get("type", "file").lower()
                    is_dir = typ == "dir"
                    size = None
                    try:
                        size = int(facts["size"]) if "size" in facts else None
                    except (ValueError, TypeError):
                        pass
                    rows.append({"name": name, "is_dir": is_dir, "size": size})
            except Exception:
                # Fallback for servers without MLSD support.
                names = ftp.nlst()
                current = ftp.pwd()
                for raw in names:
                    name = raw.rstrip("/").split("/")[-1]
                    if not name or name in (".", ".."):
                        continue
                    is_dir, size = False, None
                    try:
                        old = ftp.pwd()
                        ftp.cwd(name)
                        ftp.cwd(old)
                        is_dir = True
                    except Exception:
                        try:
                            size = ftp.size(name)
                        except Exception:
                            pass
                    rows.append({"name": name, "is_dir": is_dir, "size": size})
            rows.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            self.finished_listing.emit(self.path, rows)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if conn:
                conn.close()


class TransferWorker(QThread):
    progress = Signal(int, str)
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, settings, direction, local_path, remote_path, remote_base="/", remote_is_dir=False):
        super().__init__()
        self.settings = settings
        self.direction = direction
        self.local_path = local_path
        self.remote_path = remote_path
        self.remote_base = remote_base
        self.remote_is_dir = remote_is_dir
        self._cancel = False
        self._bytes = 0
        self._started = 0

    def cancel(self):
        self._cancel = True

    def emit_progress(self, message=""):
        elapsed = max(time.monotonic() - self._started, 0.001)
        speed = self._bytes / elapsed
        self.progress.emit(int(speed / (1024 * 1024) * 10), f"{fmt_size(self._bytes)} transferred · {speed / (1024*1024):.2f} MB/s {message}".strip())

    def run(self):
        conn = None
        try:
            conn = FTPConnection(**self.settings)
            conn.ftp.cwd(self.remote_base)
            self._started = time.monotonic()
            if self.direction == "upload":
                self._upload(conn.ftp, self.local_path, self.remote_path)
            else:
                self._download(conn.ftp, self.remote_path, self.local_path)
            self.completed.emit(f"{self.direction.title()} completed: {self.local_path}")
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if conn:
                conn.close()

    def _upload(self, ftp, local, remote):
        local = Path(local)
        if self._cancel:
            return
        if local.is_dir():
            try:
                ftp.mkd(remote)
            except error_perm:
                pass
            old = ftp.pwd()
            ftp.cwd(remote)
            try:
                for child in local.iterdir():
                    if self._cancel:
                        return
                    self._upload(ftp, str(child), child.name)
            finally:
                ftp.cwd(old)
        else:
            with local.open("rb") as file_obj:
                def callback(block):
                    self._bytes += len(block)
                    self.emit_progress(f"· {local.name}")
                ftp.storbinary(f"STOR {remote}", file_obj, blocksize=BLOCK_SIZE, callback=callback)

    def _download(self, ftp, remote, local):
        if self._cancel:
            return
        local = Path(local)
        old = ftp.pwd()
        try:
            ftp.cwd(remote)
            is_dir = True
        except Exception:
            ftp.cwd(old)
            is_dir = False
        if is_dir:
            local.mkdir(parents=True, exist_ok=True)
            ftp.cwd(remote)
            try:
                try:
                    entries = list(ftp.mlsd())
                    for name, facts in entries:
                        if name in (".", ".."):
                            continue
                        if self._cancel:
                            return
                        if facts.get("type", "").lower() == "dir":
                            self._download(ftp, name, local / name)
                        else:
                            self._download_file(ftp, name, local / name)
                except Exception:
                    for name in ftp.nlst():
                        base = name.rstrip("/").split("/")[-1]
                        if base and base not in (".", ".."):
                            self._download(ftp, base, local / base)
            finally:
                ftp.cwd(old)
        else:
            self._download_file(ftp, remote, local)

    def _download_file(self, ftp, remote_name, local_path):
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with local_path.open("wb") as file_obj:
            def callback(block):
                if self._cancel:
                    raise InterruptedError("Transfer cancelled")
                file_obj.write(block)
                self._bytes += len(block)
                self.emit_progress(f"· {Path(remote_name).name}")
            ftp.retrbinary(f"RETR {remote_name}", callback, blocksize=BLOCK_SIZE)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SwiftFTP")
        self.resize(1180, 760)
        self.settings = None
        self.remote_path = "/"
        self.local_path = str(Path.home())
        self.list_worker = None
        self.connect_worker = None
        self.transfer_workers = []
        self._build_ui()
        self._style()
        self.refresh_local()

    def _build_ui(self):
        root = QWidget()
        main = QVBoxLayout(root)
        main.setContentsMargins(18, 18, 18, 14)
        main.setSpacing(12)

        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title = QLabel("SwiftFTP")
        title.setObjectName("title")
        subtitle = QLabel("Fast file transfers for macOS")
        subtitle.setObjectName("subtitle")
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        header.addLayout(title_col)
        header.addStretch()
        self.status_label = QLabel("Not connected")
        self.status_label.setObjectName("status")
        header.addWidget(self.status_label)
        main.addLayout(header)

        connect_box = QWidget()
        grid = QGridLayout(connect_box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        self.host = QLineEdit()
        self.host.setPlaceholderText("Server address / IP")
        self.port = QLineEdit("21")
        self.username = QLineEdit("anonymous")
        self.password = QLineEdit()
        self.password.setPlaceholderText("Password")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.tls = QComboBox()
        self.tls.addItems(["FTP", "Explicit FTPS"])
        self.threads = QSpinBox()
        self.threads.setRange(1, 8)
        self.threads.setValue(3)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.clicked.connect(self.connect_ftp)
        grid.addWidget(QLabel("Host"), 0, 0)
        grid.addWidget(self.host, 0, 1)
        grid.addWidget(QLabel("Port"), 0, 2)
        grid.addWidget(self.port, 0, 3)
        grid.addWidget(QLabel("Username"), 0, 4)
        grid.addWidget(self.username, 0, 5)
        grid.addWidget(QLabel("Password"), 0, 6)
        grid.addWidget(self.password, 0, 7)
        grid.addWidget(self.tls, 0, 8)
        grid.addWidget(self.connect_btn, 0, 9)
        grid.addWidget(QLabel("Concurrent transfers"), 1, 0, 1, 2)
        grid.addWidget(self.threads, 1, 2)
        grid.addWidget(QLabel("Start with 2–3; some servers limit connections."), 1, 3, 1, 5)
        main.addWidget(connect_box)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.local_tree = QTreeWidget()
        self.remote_tree = QTreeWidget()
        for tree in (self.local_tree, self.remote_tree):
            tree.setColumnCount(2)
            tree.setHeaderLabels(["Name", "Size"])
            tree.setRootIsDecorated(False)
            tree.setAlternatingRowColors(True)
            tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            tree.itemDoubleClicked.connect(self.on_item_double_clicked)
        local_panel = QWidget()
        ll = QVBoxLayout(local_panel)
        ll.setContentsMargins(0, 0, 0, 0)
        local_head = QHBoxLayout()
        local_head.addWidget(QLabel("ON THIS MAC"))
        self.local_up = QPushButton("↑ Up")
        self.local_up.clicked.connect(self.local_parent)
        local_head.addStretch()
        local_head.addWidget(self.local_up)
        ll.addLayout(local_head)
        self.local_path_edit = QLineEdit(self.local_path)
        self.local_path_edit.returnPressed.connect(self.go_local_path)
        ll.addWidget(self.local_path_edit)
        ll.addWidget(self.local_tree)
        local_buttons = QHBoxLayout()
        self.choose_local = QPushButton("Choose folder")
        self.choose_local.clicked.connect(self.choose_folder)
        self.upload_btn = QPushButton("↑ Upload selected")
        self.upload_btn.setObjectName("primary")
        self.upload_btn.clicked.connect(self.upload_selected)
        local_buttons.addWidget(self.choose_local)
        local_buttons.addStretch()
        local_buttons.addWidget(self.upload_btn)
        ll.addLayout(local_buttons)

        remote_panel = QWidget()
        rl = QVBoxLayout(remote_panel)
        rl.setContentsMargins(0, 0, 0, 0)
        remote_head = QHBoxLayout()
        remote_head.addWidget(QLabel("FTP SERVER"))
        self.remote_up = QPushButton("↑ Up")
        self.remote_up.clicked.connect(self.remote_parent)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_remote)
        remote_head.addStretch()
        remote_head.addWidget(self.refresh_btn)
        remote_head.addWidget(self.remote_up)
        rl.addLayout(remote_head)
        self.remote_path_edit = QLineEdit("/")
        self.remote_path_edit.returnPressed.connect(self.go_remote_path)
        rl.addWidget(self.remote_path_edit)
        rl.addWidget(self.remote_tree)
        remote_buttons = QHBoxLayout()
        self.download_btn = QPushButton("↓ Download selected")
        self.download_btn.setObjectName("primary")
        self.download_btn.clicked.connect(self.download_selected)
        remote_buttons.addStretch()
        remote_buttons.addWidget(self.download_btn)
        rl.addLayout(remote_buttons)

        splitter.addWidget(local_panel)
        splitter.addWidget(remote_panel)
        splitter.setSizes([570, 570])
        main.addWidget(splitter, 1)

        transfer_head = QHBoxLayout()
        transfer_head.addWidget(QLabel("TRANSFER ACTIVITY"))
        transfer_head.addStretch()
        self.cancel_btn = QPushButton("Cancel active transfers")
        self.cancel_btn.clicked.connect(self.cancel_transfers)
        transfer_head.addWidget(self.cancel_btn)
        main.addLayout(transfer_head)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("Ready")
        main.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setFixedHeight(105)
        main.addWidget(self.log)
        self.setCentralWidget(root)

    def _style(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #111318; color: #E7EAF0; font-size: 13px; }
            QLabel#title { font-size: 28px; font-weight: 700; color: #F5F7FA; }
            QLabel#subtitle { color: #8992A3; font-size: 12px; }
            QLabel#status { color: #A9B7D0; padding: 7px 12px; background: #202530; border-radius: 8px; }
            QLineEdit, QSpinBox, QComboBox, QTreeWidget, QPlainTextEdit {
                background: #191D25; border: 1px solid #303644; border-radius: 7px;
                padding: 7px; selection-background-color: #3B66D9;
            }
            QTreeWidget { alternate-background-color: #151922; padding: 5px; }
            QHeaderView::section { background: #202530; color: #AEB7C7; padding: 7px; border: none; }
            QPushButton { background: #252B37; border: 1px solid #343C4C; padding: 8px 12px; border-radius: 7px; }
            QPushButton:hover { background: #30394A; }
            QPushButton#primary { background: #4169E1; border-color: #4169E1; color: white; font-weight: 600; }
            QPushButton#primary:hover { background: #5279EE; }
            QProgressBar { background: #1B2029; border: 1px solid #303644; border-radius: 6px; text-align: center; height: 18px; }
            QProgressBar::chunk { background: #4169E1; border-radius: 5px; }
            QSplitter::handle { background: #2A303C; width: 2px; }
        """)

    def log_line(self, text):
        self.log.appendPlainText(text)

    def current_settings(self):
        host = self.host.text().strip()
        if not host:
            raise ValueError("Introduce la IP o el dominio del servidor.")
        try:
            port = int(self.port.text().strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            raise ValueError("El puerto debe ser un número entre 1 y 65535.")
        return {
            "host": host, "port": port,
            "username": self.username.text().strip() or "anonymous",
            "password": self.password.text(),
            "tls": self.tls.currentText() == "Explicit FTPS",
        }

    def connect_ftp(self):
        try:
            settings = self.current_settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Connection details", str(exc))
            return
        self.connect_btn.setEnabled(False)
        self.status_label.setText("Connecting…")
        self.connect_worker = ConnectWorker(settings)
        self.connect_worker.connected.connect(self.on_connected)
        self.connect_worker.failed.connect(self.on_connect_failed)
        self.connect_worker.start()

    def on_connected(self, remote_path, settings):
        self.settings = settings
        self.remote_path = remote_path or "/"
        self.remote_path_edit.setText(self.remote_path)
        self.status_label.setText(f"Connected · {settings['host']}")
        self.connect_btn.setEnabled(True)
        self.log_line(f"Connected to {settings['host']}:{settings['port']}")
        self.refresh_remote()

    def on_connect_failed(self, error):
        self.connect_btn.setEnabled(True)
        self.status_label.setText("Connection failed")
        self.log_line(f"Connection error: {error}")
        QMessageBox.warning(self, "FTP connection failed", error)

    def refresh_local(self):
        self.local_tree.clear()
        self.local_path_edit.setText(self.local_path)
        try:
            entries = sorted(Path(self.local_path).iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            for path in entries:
                item = QTreeWidgetItem([path.name, "Folder" if path.is_dir() else fmt_size(path.stat().st_size)])
                item.setData(0, Qt.ItemDataRole.UserRole, str(path))
                item.setData(0, Qt.ItemDataRole.UserRole + 1, path.is_dir())
                self.local_tree.addTopLevelItem(item)
        except Exception as exc:
            self.log_line(f"Local folder error: {exc}")

    def refresh_remote(self):
        if not self.settings:
            return
        self.refresh_btn.setEnabled(False)
        self.list_worker = ListingWorker(self.settings, self.remote_path)
        self.list_worker.finished_listing.connect(self.on_remote_listing)
        self.list_worker.failed.connect(self.on_remote_listing_failed)
        self.list_worker.start()

    def on_remote_listing(self, path, rows):
        self.remote_path = path
        self.remote_path_edit.setText(path)
        self.remote_tree.clear()
        for row in rows:
            item = QTreeWidgetItem([row["name"], "Folder" if row["is_dir"] else fmt_size(row["size"])])
            item.setData(0, Qt.ItemDataRole.UserRole, row["name"])
            item.setData(0, Qt.ItemDataRole.UserRole + 1, row["is_dir"])
            self.remote_tree.addTopLevelItem(item)
        self.refresh_btn.setEnabled(True)
        self.log_line(f"Listed {len(rows)} entries in {path}")

    def on_remote_listing_failed(self, error):
        self.refresh_btn.setEnabled(True)
        self.log_line(f"Remote listing error: {error}")
        QMessageBox.warning(self, "FTP listing failed", error)

    def go_local_path(self):
        path = os.path.expanduser(self.local_path_edit.text().strip())
        if os.path.isdir(path):
            self.local_path = os.path.abspath(path)
            self.refresh_local()
        else:
            QMessageBox.warning(self, "Local path", "That folder does not exist.")

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Choose local folder", self.local_path)
        if path:
            self.local_path = path
            self.refresh_local()

    def local_parent(self):
        parent = str(Path(self.local_path).parent)
        if parent != self.local_path:
            self.local_path = parent
            self.refresh_local()

    def go_remote_path(self):
        if not self.settings:
            return
        path = self.remote_path_edit.text().strip() or "/"
        self._navigate_remote(path)

    def _navigate_remote(self, path):
        self.remote_path = path
        self.refresh_remote()

    def remote_parent(self):
        if not self.settings:
            return
        self._navigate_remote(posixpath.dirname(self.remote_path.rstrip("/")) or "/")

    def on_item_double_clicked(self, item, column):
        is_dir = item.data(0, Qt.ItemDataRole.UserRole + 1)
        name = item.data(0, Qt.ItemDataRole.UserRole)
        tree = self.sender()
        if not is_dir:
            return
        if tree is self.local_tree:
            self.local_path = str(Path(self.local_path) / name)
            self.refresh_local()
        else:
            self._navigate_remote(posixpath.join(self.remote_path.rstrip("/"), name) or "/")

    def selected_items(self, tree):
        return tree.selectedItems()

    def upload_selected(self):
        if not self.settings:
            QMessageBox.information(self, "Not connected", "Connect to an FTP server first.")
            return
        items = self.selected_items(self.local_tree)
        if not items:
            QMessageBox.information(self, "Select files", "Select one or more local files or folders first.")
            return
        for item in items:
            local = item.data(0, Qt.ItemDataRole.UserRole)
            name = os.path.basename(local.rstrip(os.sep))
            remote = name
            self.start_transfer("upload", local, remote, item.data(0, Qt.ItemDataRole.UserRole + 1))

    def download_selected(self):
        if not self.settings:
            QMessageBox.information(self, "Not connected", "Connect to an FTP server first.")
            return
        items = self.selected_items(self.remote_tree)
        if not items:
            QMessageBox.information(self, "Select files", "Select one or more remote files or folders first.")
            return
        for item in items:
            name = item.data(0, Qt.ItemDataRole.UserRole)
            local = str(Path(self.local_path) / name)
            self.start_transfer("download", local, name, item.data(0, Qt.ItemDataRole.UserRole + 1))

    def start_transfer(self, direction, local, remote, is_dir):
        worker = TransferWorker(self.settings, direction, local, remote, self.remote_path, is_dir)
        worker.progress.connect(self.on_transfer_progress)
        worker.completed.connect(self.on_transfer_completed)
        worker.failed.connect(self.on_transfer_failed)
        worker.finished.connect(self._start_queued_transfers)
        self.transfer_workers.append(worker)
        self._start_queued_transfers()
        self.log_line(f"Queued {direction}: {Path(local).name}")

    def _start_queued_transfers(self):
        # Limit simultaneous FTP sessions to avoid overwhelming the server.
        running = sum(1 for w in self.transfer_workers if w.isRunning())
        limit = self.threads.value()
        for worker in self.transfer_workers:
            if running >= limit:
                break
            if not worker.isRunning() and not worker.isFinished():
                worker.start()
                running += 1

    def on_transfer_progress(self, scaled, message):
        self.progress.setFormat(message)
        self.progress.setValue(min(100, scaled))

    def on_transfer_completed(self, message):
        self.log_line(message)
        self.progress.setFormat("Transfer completed")
        self.progress.setValue(100)
        self.refresh_local()
        if self.settings:
            self.refresh_remote()
        self._start_queued_transfers()

    def on_transfer_failed(self, error):
        self.log_line(f"Transfer error: {error}")
        self.progress.setFormat("Transfer failed")
        self._start_queued_transfers()

    def cancel_transfers(self):
        for worker in self.transfer_workers:
            if worker.isRunning():
                worker.cancel()
        self.log_line("Cancellation requested. Some FTP servers may finish the current operation first.")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
