"""PySide6 desktop window with a system-tray icon and Windows notifications."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from typing import Any

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton, QSpinBox,
    QStatusBar, QSystemTrayIcon, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser,
    QVBoxLayout, QWidget, QPlainTextEdit,
)

from .. import APP_NAME, __version__
from ..config import Settings, data_dir, load_settings, load_sources, save_settings, save_sources
from ..db import Database
from ..engine import Engine

log = logging.getLogger(__name__)


def make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#1f6feb"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 12, 12)
    p.setPen(QPen(QColor("white"), 6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    path = QPainterPath(QPointF(12, 46))
    for x, y in ((24, 34), (34, 40), (52, 16)):
        path.lineTo(x, y)
    p.drawPath(path)
    p.end()
    return QIcon(pm)


class Worker(QObject):
    """Runs engine cycles off the UI thread."""
    finished = Signal(str)
    failed = Signal(str)
    notified = Signal(str, str)

    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.engine: Engine | None = None

    @Slot()
    def run(self) -> None:
        try:
            if self.engine is None:
                self.engine = Engine(db=Database(), settings=self.settings,
                                     notify=lambda t, b: self.notified.emit(t, b))
            self.engine.settings = self.settings
            self.finished.emit(self.engine.run_cycle())
        except Exception as exc:  # keep the app alive
            log.exception("Cycle failed")
            self.failed.emit(str(exc))


class LineChart(QWidget):
    """Tiny dependency-free line chart for account value history."""

    def __init__(self) -> None:
        super().__init__()
        self.points: list[tuple[str, float]] = []
        self.setMinimumHeight(220)

    def set_points(self, pts: list[tuple[str, float]]) -> None:
        self.points = pts
        self.update()

    def paintEvent(self, _e: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(50, 15, -15, -30)
        p.setPen(QColor("#888"))
        p.drawRect(r)
        if len(self.points) < 2:
            p.drawText(r, Qt.AlignCenter, "Account history appears after two refreshes.")
            return
        vals = [v for _, v in self.points]
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1.0
        path = QPainterPath()
        for i, v in enumerate(vals):
            x = r.left() + r.width() * i / (len(vals) - 1)
            y = r.bottom() - r.height() * (v - lo) / span
            path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
        p.setPen(QPen(QColor("#1f6feb"), 2))
        p.drawPath(path)
        p.setPen(QColor("#888"))
        p.drawText(QRectF(0, r.top() - 8, 48, 16), Qt.AlignRight, f"{hi:,.0f}")
        p.drawText(QRectF(0, r.bottom() - 8, 48, 16), Qt.AlignRight, f"{lo:,.0f}")
        p.drawText(QRectF(r.left(), r.bottom() + 6, 200, 16), Qt.AlignLeft, self.points[0][0][:16])
        p.drawText(QRectF(r.right() - 200, r.bottom() + 6, 200, 16), Qt.AlignRight, self.points[-1][0][:16])


def table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSortingEnabled(True)
    return t


def fill(t: QTableWidget, rows: list[list[Any]]) -> None:
    t.setSortingEnabled(False)
    t.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, v in enumerate(row):
            it = QTableWidgetItem()
            if isinstance(v, (int, float)):
                it.setData(Qt.DisplayRole, round(v, 4) if isinstance(v, float) else v)
            else:
                it.setText("" if v is None else str(v))
            t.setItem(i, j, it)
    t.setSortingEnabled(True)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}  (advisory only, never trades)")
        self.setWindowIcon(make_icon())
        self.resize(1200, 800)
        self.settings = load_settings()
        self.db = Database()

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.report = QTextBrowser()
        self.report.setOpenExternalLinks(True)
        self.tabs.addTab(self.report, "Report")

        self.ideas = table(["Ticker", "Predicted move %", "Last price", "Predicted price",
                            "Confidence %", "News", "Momentum", "Fundamentals", "Items", "Top headline"])
        self.tabs.addTab(self.ideas, "Buy ideas")

        self.events = table(["When", "Event", "Tickers", "Link"])
        self.tabs.addTab(self.events, "Events")

        self.news = table(["Published", "Source", "Tickers", "Sentiment", "Headline", "Link"])
        self.tabs.addTab(self.news, "News feed")

        acc = QWidget()
        al = QVBoxLayout(acc)
        self.acc_label = QLabel("Connect Trading212 in Settings, or log in to Trading212 in your debug Chrome.")
        self.acc_label.setFont(QFont("", 12))
        self.chart = LineChart()
        self.positions = table(["Ticker", "Quantity", "Avg price", "Current", "Value", "P/L"])
        al.addWidget(self.acc_label)
        al.addWidget(self.chart)
        al.addWidget(self.positions)
        self.tabs.addTab(acc, "Account")

        self.tabs.addTab(self._sources_tab(), "Sources")
        self.tabs.addTab(self._settings_tab(), "Settings")

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.refresh_btn = QPushButton("Refresh now")
        self.refresh_btn.clicked.connect(self.start_cycle)
        self.status.addPermanentWidget(self.refresh_btn)

        self._setup_tray()
        self._setup_worker()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.start_cycle)
        self.timer.start(max(5, self.settings.refresh_minutes) * 60 * 1000)
        self.load_views()
        QTimer.singleShot(1500, self.start_cycle)

    # ------------------------------------------------------------ tray etc.
    def _setup_tray(self) -> None:
        self.tray = QSystemTrayIcon(make_icon(), self)
        menu = QMenu()
        show = QAction("Open Investor Helper", self)
        show.triggered.connect(self.showNormal)
        refresh = QAction("Refresh now", self)
        refresh.triggered.connect(self.start_cycle)
        quit_ = QAction("Quit", self)
        quit_.triggered.connect(QApplication.instance().quit)
        for a in (show, refresh, quit_):
            menu.addAction(a)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self.showNormal() if r == QSystemTrayIcon.Trigger else None)
        self.tray.setToolTip(APP_NAME)
        self.tray.show()

    def notify(self, title: str, body: str) -> None:
        if QSystemTrayIcon.supportsMessages():
            self.tray.showMessage(title, body, QSystemTrayIcon.Information, 10000)

    def closeEvent(self, e: Any) -> None:
        # Closing the window keeps the app running in the tray.
        if self.tray.isVisible():
            self.hide()
            self.notify(APP_NAME, "Still running in the tray. Right-click the icon to quit.")
            e.ignore()
        else:
            e.accept()

    def _setup_worker(self) -> None:
        self.thread_ = QThread(self)
        self.worker = Worker(self.settings)
        self.worker.moveToThread(self.thread_)
        self.worker.finished.connect(self.on_cycle_done)
        self.worker.failed.connect(self.on_cycle_failed)
        self.worker.notified.connect(self.notify)
        self.thread_.start()
        QApplication.instance().aboutToQuit.connect(self._shutdown)
        self.running = False

    def _shutdown(self) -> None:
        self.thread_.quit()
        self.thread_.wait(5000)

    def start_cycle(self) -> None:
        if self.running:
            return
        self.running = True
        self.refresh_btn.setEnabled(False)
        self.status.showMessage("Refreshing news, filings, X and Trading212...")
        self._invoke_worker()

    def _invoke_worker(self) -> None:
        from PySide6.QtCore import QMetaObject
        QMetaObject.invokeMethod(self.worker, "run", Qt.QueuedConnection)

    def on_cycle_done(self, _md: str) -> None:
        self.running = False
        self.refresh_btn.setEnabled(True)
        eng = self.worker.engine
        problems = {k: v for k, v in (eng.status if eng else {}).items() if not v.startswith("ok")}
        msg = f"Updated {datetime.now().strftime('%H:%M')}"
        if problems:
            msg += f"  |  {len(problems)} source issue(s): " + "; ".join(f"{k}: {v}" for k, v in list(problems.items())[:2])
        self.status.showMessage(msg)
        self.load_views()

    def on_cycle_failed(self, err: str) -> None:
        self.running = False
        self.refresh_btn.setEnabled(True)
        self.status.showMessage(f"Refresh failed: {err}")

    # --------------------------------------------------------------- views
    def load_views(self) -> None:
        rep = self.db.latest_report()
        self.report.setMarkdown(rep["markdown"] if rep else "# Welcome\n\nThe first report appears after the first refresh.")

        eng = self.worker.engine if hasattr(self, "worker") else None
        ranked = getattr(eng, "last_ranked", None) or []
        fill(self.ideas, [[s.ticker, s.predicted_move * 100, s.last_price, s.predicted_price, s.confidence * 100,
                           s.news_score, s.momentum_score, s.fundamental_score, s.n_items,
                           s.headlines[0]["title"] if s.headlines else ""] for s in ranked])

        fill(self.events, [[(e.get("starts_at") or "tbc")[:16], e["title"], ", ".join(e["tickers"]), e.get("url", "")]
                           for e in self.db.upcoming_events(200)])

        from datetime import timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        fill(self.news, [[i["published_at"][:16].replace("T", " "), i["source_id"], ", ".join(i["tickers"]),
                          i["sentiment"], i["title"], i["url"]] for i in self.db.recent_items(since)[:500]])

        hist = self.db.account_history()
        self.chart.set_points([(h["taken_at"], h["total"]) for h in hist if h["total"] is not None])
        if hist:
            h = hist[-1]
            self.acc_label.setText(f"Account value {h['total']:,.2f} {h['currency'] or ''}   cash {h['cash'] or 0:,.2f}   "
                                   f"invested {h['invested'] or 0:,.2f}   unrealised P/L {h['ppl'] or 0:+,.2f}")
        acc = getattr(eng, "last_account", None) or {}
        fill(self.positions, [[p["ticker"], p["quantity"], p["average_price"], p["current_price"], p["value"], p["ppl"]]
                              for p in acc.get("positions", [])])
        self._load_sources_table()

    # ------------------------------------------------------------- sources
    def _sources_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Weights are learned from how each source's calls turned out (1.0 = neutral, 2.0 = best). "
                             "Poor sources are swapped for vetted alternatives automatically when auto-rotation is on."))
        self.src_table = table(["Enabled", "Kind", "Name", "Handle / URL", "Weight", "Right", "Wrong", "Status", "Id"])
        self.src_table.setSortingEnabled(False)
        lay.addWidget(self.src_table)
        row = QHBoxLayout()
        for label, fn in (("Toggle enabled", self._toggle_source), ("Add X account", self._add_x),
                          ("Add RSS feed", self._add_rss), ("Add page to watch", self._add_page),
                          ("Remove", self._remove_source)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        return w

    def _load_sources_table(self) -> None:
        sources = load_sources()
        weights = self.db.get_weights()
        status = getattr(self.worker.engine, "status", {}) if self.worker.engine else {}
        rows = []
        for s in sources:
            w = weights.get(s["id"], {})
            rows.append(["yes" if s.get("enabled") else "no", s["kind"], s["name"],
                         s.get("handle") or s.get("url", ""), w.get("weight", 1.0), w.get("hits", 0),
                         w.get("misses", 0), status.get(s["id"], s.get("disabled_reason", "")), s["id"]])
        fill(self.src_table, rows)

    def _selected_id(self) -> str | None:
        r = self.src_table.currentRow()
        if r < 0:
            return None
        return self.src_table.item(r, 8).text()

    def _toggle_source(self) -> None:
        sid = self._selected_id()
        if not sid:
            return
        sources = load_sources()
        for s in sources:
            if s["id"] == sid:
                s["enabled"] = not s.get("enabled")
        save_sources(sources)
        self._load_sources_table()

    def _add(self, src: dict[str, Any]) -> None:
        sources = load_sources()
        if any(s["id"] == src["id"] for s in sources):
            QMessageBox.information(self, APP_NAME, "That source is already in the list.")
            return
        sources.append(src)
        save_sources(sources)
        self._load_sources_table()

    def _add_x(self) -> None:
        h, ok = QInputDialog.getText(self, "Add X account", "Handle (without @):")
        if ok and h.strip():
            h = h.strip().lstrip("@")
            self._add({"id": f"x:{h}", "kind": "x", "name": f"@{h}", "handle": h, "enabled": True})

    def _add_rss(self) -> None:
        url, ok = QInputDialog.getText(self, "Add RSS feed", "Feed URL:")
        if ok and url.strip():
            name, _ = QInputDialog.getText(self, "Add RSS feed", "Name:")
            self._add({"id": f"rss:{abs(hash(url)) % 10**8}", "kind": "rss", "name": name or url,
                       "url": url.strip(), "enabled": True})

    def _add_page(self) -> None:
        url, ok = QInputDialog.getText(self, "Watch a page", "Page URL (you'll be notified when it changes):")
        if ok and url.strip():
            name, _ = QInputDialog.getText(self, "Watch a page", "Name:")
            self._add({"id": f"page:{abs(hash(url)) % 10**8}", "kind": "page", "name": name or url,
                       "url": url.strip(), "enabled": True})

    def _remove_source(self) -> None:
        sid = self._selected_id()
        if sid and QMessageBox.question(self, APP_NAME, f"Remove {sid}?") == QMessageBox.Yes:
            save_sources([s for s in load_sources() if s["id"] != sid])
            self._load_sources_table()

    # ------------------------------------------------------------ settings
    def _settings_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        s = self.settings
        self.f_cdp = QCheckBox("Read X and Trading212 live through my Chrome")
        self.f_cdp.setChecked(s.use_chrome_cdp)
        self.f_cdp_url = QLineEdit(s.chrome_cdp_url)
        self.f_t212 = QLineEdit(s.trading212_api_key)
        self.f_t212.setEchoMode(QLineEdit.Password)
        self.f_t212_secret = QLineEdit(s.trading212_api_secret)
        self.f_t212_secret.setEchoMode(QLineEdit.Password)
        self.f_env = QComboBox()
        self.f_env.addItems(["live", "demo"])
        self.f_env.setCurrentText(s.trading212_environment)
        self.f_x = QLineEdit(s.x_bearer_token)
        self.f_x.setEchoMode(QLineEdit.Password)
        self.f_ua = QLineEdit(s.sec_user_agent)
        self.f_watch = QLineEdit(", ".join(s.watchlist))
        self.f_refresh = QSpinBox()
        self.f_refresh.setRange(5, 720)
        self.f_refresh.setValue(s.refresh_minutes)
        self.f_horizon = QSpinBox()
        self.f_horizon.setRange(1, 10)
        self.f_horizon.setValue(s.horizon_days)
        self.f_rotate = QCheckBox("Automatically replace sources that keep being wrong")
        self.f_rotate.setChecked(s.auto_rotate_sources)
        self.f_events = QPlainTextEdit(json.dumps(s.custom_events, indent=2))
        self.f_events.setPlaceholderText('[{"title": "Apple shareholder meeting", "starts_at": "2026-11-20T15:00:00Z", "url": "https://..."}]')
        f.addRow(QLabel("<b>Live access (recommended)</b>"))
        f.addRow(self.f_cdp)
        f.addRow("Chrome debug address", self.f_cdp_url)
        test = QPushButton("Test Chrome connection")
        test.clicked.connect(self._test_chrome)
        f.addRow(test)
        f.addRow(QLabel("<b>Optional API keys (stay on this PC)</b>"))
        f.addRow("Trading212 API key (read-only)", self.f_t212)
        f.addRow("Trading212 API secret (if given)", self.f_t212_secret)
        f.addRow("Trading212 environment", self.f_env)
        f.addRow("X API bearer token", self.f_x)
        f.addRow("SEC User-Agent (name + email)", self.f_ua)
        f.addRow(QLabel("<b>Analysis</b>"))
        f.addRow("Watchlist tickers", self.f_watch)
        f.addRow("Refresh every (minutes)", self.f_refresh)
        f.addRow("Prediction horizon (days)", self.f_horizon)
        f.addRow(self.f_rotate)
        f.addRow("My events (JSON)", self.f_events)
        save = QPushButton("Save settings")
        save.clicked.connect(self._save_settings)
        f.addRow(save)
        f.addRow(QLabel(f"Data folder: {data_dir()}"))
        return w

    def _test_chrome(self) -> None:
        from ..sources.chrome_live import check_connection
        try:
            QMessageBox.information(self, APP_NAME, check_connection(self.f_cdp_url.text().strip()))
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))

    def _save_settings(self) -> None:
        try:
            events = json.loads(self.f_events.toPlainText() or "[]")
        except json.JSONDecodeError as exc:
            QMessageBox.warning(self, APP_NAME, f"My events is not valid JSON: {exc}")
            return
        s = self.settings
        s.use_chrome_cdp = self.f_cdp.isChecked()
        s.chrome_cdp_url = self.f_cdp_url.text().strip()
        s.trading212_api_key = self.f_t212.text().strip()
        s.trading212_api_secret = self.f_t212_secret.text().strip()
        s.trading212_environment = self.f_env.currentText()
        s.x_bearer_token = self.f_x.text().strip()
        s.sec_user_agent = self.f_ua.text().strip()
        s.watchlist = [t.strip().upper() for t in self.f_watch.text().split(",") if t.strip()]
        s.refresh_minutes = self.f_refresh.value()
        s.horizon_days = self.f_horizon.value()
        s.auto_rotate_sources = self.f_rotate.isChecked()
        s.custom_events = events
        save_settings(s)
        self.worker.settings = s
        self.timer.start(s.refresh_minutes * 60 * 1000)
        self.status.showMessage("Settings saved.")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(data_dir() / "investor_helper.log", encoding="utf-8"),
                  logging.StreamHandler()],
    )
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow()
    win.show()
    return app.exec()
