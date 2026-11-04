"""mosaic_gnss_recorder の GUI。起動: python -m mosaic_gnss_recorder

受信機の設定は [受信設定を投入] と受信機設定タブの [変更を送信] を押したときだけ変更する
(Current のみ。Boot には保存しない)。
緯度経度と地図は既定で OFF にし、「表示」メニューで切り替える。
地図は ON にするまで作らないので、OFF の間は地図サーバへ接続しない。
"""
# pylint: disable=no-member  # Qt のシグナルは pylint から見えない
import os
import queue
import sys

from PySide6.QtCore import QStandardPaths, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget)

from . import charts
from . import engines
from . import kml_export
from . import sbf_parser as sp
from . import serial_link as link
from . import settings
from . import tcp_server
from .map_view import MapView
from .state import BANDS

REFRESH_MS = 1000
MAX_CONSOLE_LINES = 2000
REPLAY_SPEEDS = (("1倍速", 1.0), ("10倍速", 10.0), ("60倍速", 60.0), ("最大", 0.0))
QZSS_COLUMNS = ("衛星", "仰角[°]", "L1CA", "L2C", "L5", "L6")
L6_COLUMNS = ("チャネル", "衛星", "受信数", "RS成功", "成功率[%]", "直近60個[%]",
              "訂正数平均")
SIGNAL_COLUMNS = ("信号", "衛星数", "平均C/N0")
SATELLITE_COLUMNS = ("衛星", "方位[°]", "仰角[°]", *BANDS)
MODE_COLORS = {0: "#7f8c8d", 1: "#d35400", 3: "#d35400", 6: "#b7950b", 2: "#b7950b",
               10: "#2471a3", 5: "#1e8449", 8: "#1e8449", 4: "#0b6e3a", 7: "#0b6e3a"}
MODE_STYLE = ("color: white; background-color: %s; font-size: 22px; font-weight: bold;"
              " padding: 6px; border-radius: 4px;")


def make_table(columns):
    """読み取り専用の表を作る。"""
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.verticalHeader().hide()
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    return table


def fill_table(table, rows):
    """表の内容を rows で置き換える。"""
    table.setRowCount(len(rows))
    for row, values in enumerate(rows):
        for col, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(row, col, item)


def format_value(value, digits=1):
    """数値の表示文字列。値が無ければ「—」。"""
    return "—" if value is None else f"{value:.{digits}f}"


def qzss_rows(snap):
    """QZSS 衛星の表の行を作る。"""
    rows = []
    for name in sorted(snap["qzss"]):
        sig = snap["qzss"][name]
        elev = snap["elevations"].get(name)
        rows.append((name, "—" if elev is None else elev,
                     *(format_value(sig.get(s)) for s in ("L1CA", "L2C", "L5", "L6"))))
    return rows


def l6_rows(snap):
    """L6 メッセージの表の行を作る。"""
    return [(chan, name, row["total"], row["good"], f"{row['rate']:.1f}",
             f"{row['recent']:.1f}", f"{row['rs_mean']:.2f}")
            for (chan, name), row in sorted(snap["l6"].items())]


def signal_rows(snap):
    """信号別の追尾数の表の行を作る。"""
    return [(name, len(vals), f"{sum(vals) / len(vals):.1f}")
            for name, vals in sorted(snap["signals"].items())]


def satellite_rows(snap):
    """全衛星の表の行を作る。"""
    rows = []
    for name in sorted(snap["sat_bands"]):
        azimuth, elevation = snap["positions"].get(name, (None, None))
        bands = snap["sat_bands"][name]
        rows.append((name, "—" if azimuth is None else azimuth,
                     "—" if elevation is None else elevation,
                     *(format_value(bands.get(band)) for band in BANDS)))
    return rows


def status_text(snap):
    """測位状態の表示文字列を dict で返す。"""
    pvt = snap["pvt"]
    if pvt is None:
        return dict.fromkeys(("mode", "nrsv", "hacc", "vacc", "tow", "speed", "lat", "lon",
                              "height"), "—")
    return {"mode": sp.PVT_MODES.get(pvt.mode, str(pvt.mode)),
            "nrsv": "—" if pvt.nrsv is None else str(pvt.nrsv),
            "hacc": f"{format_value(pvt.haccuracy, 2)} m",
            "vacc": f"{format_value(pvt.vaccuracy, 2)} m",
            "tow": f"{pvt.tow:.0f} s(週 {pvt.wnc})",
            "speed": f"{format_value(pvt.speed, 2)} m/s",
            "lat": f"{format_value(pvt.latitude, 8)}°",
            "lon": f"{format_value(pvt.longitude, 8)}°",
            "height": f"{format_value(pvt.height, 2)} m"}


class PvtPanel(QWidget):
    """測位履歴のグラフをまとめた画面。位置は平均からの差だけを表示する。"""

    def __init__(self):
        super().__init__()
        self.charts = {
            "mode": charts.ModeChart(),
            "offset": charts.LineChart("位置の変化(平均からの差)", "[m]"),
            "accuracy": charts.LineChart("推定精度", "[m]", y_floor=0),
            "nrsv": charts.LineChart("使用衛星数", "[機]", y_floor=0, integer=True),
        }
        grid = QGridLayout(self)
        for index, chart in enumerate(self.charts.values()):
            grid.addWidget(chart, index // 2, index % 2)

    def update_history(self, points, window):
        """測位履歴でグラフを描き直す。"""
        for key, data in charts.pvt_series(points, window).items():
            self.charts[key].set_data(data, window)

    def clear(self):
        """全グラフを消す。"""
        for chart in self.charts.values():
            chart.clear()


def default_folder():
    """記録の既定の保存先を返す。exe 版はインストール先に書けないことがある。"""
    if getattr(sys, "frozen", False):
        documents = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation)
        return os.path.join(documents, "mosaic_gnss_recorder")
    return os.path.join(os.getcwd(), "logs")


class MainWindow(QMainWindow):
    """メインウィンドウ。"""
    # pylint: disable=too-many-instance-attributes,attribute-defined-outside-init

    def __init__(self):
        super().__init__()
        self.setWindowTitle("mosaic_gnss_recorder")
        self.resize(1280, 860)
        self.pipeline = link.Pipeline()
        self.source = None
        self.server = None
        self.madoca = engines.MadocaEngine()
        self.clas_rt = engines.ClasRtEngine()
        self.clas_job = None
        self.status = {}
        self.map_view = None
        self._build_menu()
        self._build_ui()
        self.refresh_ports()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(REFRESH_MS)
        self._update_buttons()

    def _build_menu(self):
        """「表示」メニューを作る。"""
        menu = self.menuBar().addMenu("表示")
        self.position_action = QAction("緯度経度を表示", self, checkable=True)
        self.position_action.toggled.connect(self.set_position_visible)
        self.map_action = QAction("地図を表示(OpenStreetMap に接続)", self, checkable=True)
        self.map_action.toggled.connect(self.set_map_visible)
        menu.addAction(self.position_action)
        menu.addAction(self.map_action)

    def _build_ui(self):
        """画面を組み立てる。"""
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addLayout(self._build_connection_bar())
        layout.addLayout(self._build_record_bar())
        layout.addLayout(self._build_engine_bar())
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self._build_tables())
        split.addWidget(self._build_lower())
        split.setSizes([340, 460])
        layout.addWidget(split)
        self.setCentralWidget(root)
        self.set_position_visible(False)

    def _build_connection_bar(self):
        """接続・設定の操作列を作る。"""
        row = QHBoxLayout()
        self.port_box = QComboBox()
        self.port_box.setMinimumWidth(320)
        self.refresh_button = QPushButton("更新")
        self.refresh_button.clicked.connect(self.refresh_ports)
        self.connect_button = QPushButton("接続")
        self.connect_button.clicked.connect(self.toggle_connection)
        self.setup_button = QPushButton("受信設定を投入")
        self.setup_button.clicked.connect(self.apply_setup)
        self.stop_output_button = QPushButton("SBF出力を停止")
        self.stop_output_button.clicked.connect(self.stop_output)
        for widget in (QLabel("ポート:"), self.port_box, self.refresh_button,
                       self.connect_button, self.setup_button, self.stop_output_button):
            row.addWidget(widget)
        self.info_label = QLabel("未接続")
        row.addWidget(self.info_label, 1)
        return row

    def _build_record_bar(self):
        """記録・再生の操作列を作る。"""
        row = QHBoxLayout()
        self.folder_edit = QLineEdit(default_folder())
        folder_button = QPushButton("参照")
        folder_button.clicked.connect(self.choose_folder)
        self.record_button = QPushButton("記録開始")
        self.record_button.clicked.connect(self.toggle_record)
        self.speed_box = QComboBox()
        for label, speed in REPLAY_SPEEDS:
            self.speed_box.addItem(label, speed)
        self.replay_button = QPushButton("SBF再生")
        self.replay_button.clicked.connect(self.toggle_replay)
        kml_button = QPushButton("KML出力")
        kml_button.clicked.connect(self.export_kml)
        self.tcp_check = QCheckBox("TCP配信")
        self.tcp_check.setToolTip("受信した SBF を 127.0.0.1 の指定ポートで配信する")
        self.tcp_check.toggled.connect(self.set_tcp_server)
        self.tcp_port = QSpinBox()
        self.tcp_port.setRange(1024, 65535)
        self.tcp_port.setValue(tcp_server.DEFAULT_PORT)
        for widget in (QLabel("保存先:"), self.folder_edit, folder_button,
                       self.record_button, self.speed_box, self.replay_button, kml_button,
                       self.tcp_check, self.tcp_port):
            row.addWidget(widget)
        return row

    def _build_engine_bar(self):
        """補正 PPP(MADOCA / CLAS リアルタイム、CLAS 後処理)の操作列を作る。"""
        row = QHBoxLayout()
        self.motion_box = QComboBox()
        for key, label in engines.MOTIONS.items():
            self.motion_box.addItem(label, key)
        self.motion_box.setToolTip("MADOCA / CLAS の設定を移動体向けか静止向けにする。\n"
                                   "開始したときの選択を使う(実行中に変えたら、止めて開始し直す)")
        self.madoca_button = QPushButton("MADOCA リアルタイム開始")
        self.madoca_button.setToolTip("受信した SBF の L6E で MADOCA-PPP を計算する(TCP 配信を使う)")
        self.madoca_button.clicked.connect(self.toggle_madoca)
        self.madoca_label = QLabel("停止中")
        self.clas_rt_button = QPushButton("CLAS リアルタイム開始")
        self.clas_rt_button.setToolTip("受信した SBF の L6D で CLAS(PPP-RTK)を計算する(TCP 配信を使う)")
        self.clas_rt_button.clicked.connect(self.toggle_clas_rt)
        self.clas_rt_label = QLabel("停止中")
        self.clas_button = QPushButton("CLAS 後処理")
        self.clas_button.setToolTip("記録済みの SBF を CLAS で処理し、pos と KML を作る")
        self.clas_button.clicked.connect(self.start_clas)
        self.clas_label = QLabel("")
        for widget in (QLabel("補正 PPP:"), self.motion_box, self.madoca_button, self.madoca_label,
                       self.clas_rt_button, self.clas_rt_label, self.clas_button, self.clas_label):
            row.addWidget(widget)
        row.addStretch(1)
        return row

    def _build_status_box(self):
        """測位の状態欄を作る。"""
        box = QGroupBox("測位")
        self.status_form = QFormLayout(box)
        self.mode_label = QLabel("—")
        self.mode_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mode_label.setStyleSheet(MODE_STYLE % MODE_COLORS[0])
        self.status_form.addRow(self.mode_label)
        for key, label in (("lat", "緯度"), ("lon", "経度"), ("height", "楕円体高"),
                           ("nrsv", "使用衛星数"), ("hacc", "水平精度"), ("vacc", "垂直精度"),
                           ("speed", "速度"), ("tow", "TOW"), ("blocks", "受信ブロック"),
                           ("crc", "CRC/解析エラー"), ("record", "記録"), ("tcp", "TCP配信"),
                           ("madoca", "MADOCA"), ("clas_rt", "CLAS")):
            self.status[key] = QLabel("—")
            self.status_form.addRow(label, self.status[key])
        self.warning_label = QLabel("")
        self.warning_label.setStyleSheet("color: #c0392b;")
        self.warning_label.setWordWrap(True)
        self.status_form.addRow(self.warning_label)
        return box

    def _build_tables(self):
        """状態と表の領域を作る。"""
        area = QWidget()
        grid = QGridLayout(area)
        grid.addWidget(self._build_status_box(), 0, 0, 2, 1)
        self.qzss_table = make_table(QZSS_COLUMNS)
        self.l6_table = make_table(L6_COLUMNS)
        self.signal_table = make_table(SIGNAL_COLUMNS)
        grid.addWidget(self._titled("QZSS 衛星(C/N0 [dB-Hz])", self.qzss_table), 0, 1)
        grid.addWidget(self._titled("L6 メッセージ", self.l6_table), 1, 1)
        grid.addWidget(self._titled("信号別の追尾数", self.signal_table), 0, 2, 2, 1)
        grid.setColumnStretch(1, 3)
        grid.setColumnStretch(2, 2)
        return area

    @staticmethod
    def _titled(title, widget):
        """枠付きの見出しを付ける。"""
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        layout.addWidget(widget)
        return box

    def _build_lower(self):
        """グラフとコンソールの領域を作る。"""
        split = QSplitter(Qt.Orientation.Horizontal)
        self.tabs = QTabWidget()
        self.cn0_chart = charts.LineChart("QZSS C/N0", "C/N0 [dB-Hz]", (20, 55))
        self.pvt_panel = PvtPanel()
        self.sky_plot = charts.SkyPlot()
        self.satellite_table = make_table(SATELLITE_COLUMNS)
        self.tabs.addTab(self.cn0_chart, "QZSS C/N0")
        self.tabs.addTab(self.pvt_panel, "測位履歴")
        self.tabs.addTab(self.sky_plot, "スカイプロット")
        self.settings_panel = settings.SettingsPanel(self.read_settings, self.send_settings)
        self.tabs.addTab(self.satellite_table, "全衛星")
        self.tabs.addTab(self.settings_panel, "受信機設定")
        self.window_box = QComboBox()
        for label, seconds in charts.TIME_WINDOWS:
            self.window_box.addItem(label, seconds)
        self.window_box.setCurrentIndex(charts.DEFAULT_WINDOW)
        self.window_box.currentIndexChanged.connect(lambda _index: self.refresh())
        self.tabs.setCornerWidget(self.window_box)
        split.addWidget(self.tabs)
        console = QWidget()
        layout = QVBoxLayout(console)
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(MAX_CONSOLE_LINES)
        self.command_edit = QLineEdit()
        self.command_edit.setPlaceholderText("コマンドを入力して Enter")
        self.command_edit.returnPressed.connect(self.send_command)
        layout.addWidget(self.console)
        layout.addWidget(self.command_edit)
        split.addWidget(console)
        split.setSizes([820, 440])
        return split

    def set_position_visible(self, visible):
        """緯度経度の行を切り替える。"""
        for key in ("lat", "lon", "height"):
            self.status_form.setRowVisible(self.status[key], visible)

    def set_map_visible(self, visible):
        """地図のタブを切り替える。OFF にすると破棄して接続を止める。"""
        if visible and self.map_view is None:
            self.map_view = MapView()
            self.tabs.setCurrentIndex(self.tabs.addTab(self.map_view, "地図"))
        elif not visible and self.map_view is not None:
            self.tabs.removeTab(self.tabs.indexOf(self.map_view))
            self.map_view.deleteLater()
            self.map_view = None

    def _is_serial(self):
        """実機に接続中か。"""
        return isinstance(self.source, link.SerialSource) and self.source.is_alive()

    def _is_replay(self):
        """再生中か。"""
        return isinstance(self.source, link.ReplaySource) and self.source.is_alive()

    def _update_buttons(self):
        """状態に合わせてボタンを切り替える。"""
        serial_on = self._is_serial()
        replay_on = self._is_replay()
        recording = self.pipeline.log_status() is not None
        self.connect_button.setText("切断" if serial_on else "接続")
        self.connect_button.setEnabled(not replay_on)
        self.setup_button.setEnabled(serial_on)
        self.stop_output_button.setEnabled(serial_on)
        self.command_edit.setEnabled(serial_on)
        self.settings_panel.setEnabled(serial_on)
        self.replay_button.setText("再生停止" if replay_on else "SBF再生")
        self.replay_button.setEnabled(not serial_on)
        self.record_button.setText("記録停止" if recording else "記録開始")
        self.record_button.setEnabled(serial_on or replay_on or recording)

    def refresh_ports(self):
        """COM ポートの一覧を更新する。"""
        self.port_box.clear()
        for device, description in link.available_ports():
            self.port_box.addItem(f"{device}  {description}", device)

    def _start(self, source):
        """データ源を切り替えて開始する。"""
        self.pipeline = link.Pipeline()
        self.pipeline.server = self.server
        source.pipeline = self.pipeline
        self.cn0_chart.clear()
        self.pvt_panel.clear()
        self.source = source
        source.start()
        self._update_buttons()

    def _stop_source(self):
        """データ源と記録を止める。"""
        if self.source is not None:
            self.source.stop()
        self.source = None
        self.pipeline.stop_logging()
        self._update_buttons()

    def toggle_connection(self):
        """接続/切断を切り替える。"""
        if self._is_serial():
            self._stop_source()
            self.info_label.setText("切断しました")
            return
        port = self.port_box.currentData()
        if port is None:
            QMessageBox.warning(self, "mosaic_gnss_recorder", "COM ポートが見つかりません。")
            return
        self._start(link.SerialSource(self.pipeline, port))
        self.read_settings()

    def toggle_replay(self):
        """SBF の再生を開始/停止する。"""
        if self._is_replay():
            self._stop_source()
            return
        path, _ = QFileDialog.getOpenFileName(self, "SBF を開く", self.folder_edit.text(),
                                              "SBF (*.sbf *.*)")
        if path:
            self.start_replay(path, self.speed_box.currentData())

    def start_replay(self, path, speed):
        """指定の SBF を再生する。"""
        self._start(link.ReplaySource(self.pipeline, path, speed))
        self.info_label.setText(f"再生中: {os.path.basename(path)}")

    def export_kml(self):
        """記録済みの SBF を KML に変換する。"""
        paths, _ = QFileDialog.getOpenFileNames(self, "KML にする SBF を選ぶ",
                                                self.folder_edit.text(), "SBF (*.sbf *.*)")
        if not paths:
            return
        output, _ = QFileDialog.getSaveFileName(self, "KML の保存先",
                                                os.path.splitext(paths[0])[0] + ".kml",
                                                "KML (*.kml)")
        if not output:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            title = os.path.splitext(os.path.basename(output))[0]
            count = kml_export.export(paths, output, title)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "mosaic_gnss_recorder", f"KML を作れません: {exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        QMessageBox.information(self, "mosaic_gnss_recorder", f"{count} 点を出力しました。\n{output}")

    def set_tcp_server(self, enabled):
        """SBF の TCP 配信を開始/停止する。"""
        if self.server is not None:
            self.pipeline.server = None
            self.server.close()
            self.server = None
        if enabled:
            try:
                self.server = tcp_server.SbfTcpServer(self.tcp_port.value())
            except OSError as exc:
                QMessageBox.warning(self, "mosaic_gnss_recorder", f"TCP 配信を開始できません: {exc}")
                self.tcp_check.setChecked(False)
                return
            self.pipeline.server = self.server
        self.tcp_port.setEnabled(not enabled)

    def toggle_madoca(self):
        """MADOCA リアルタイムを開始/停止する。"""
        self._toggle_engine(self.madoca, "MADOCA")

    def toggle_clas_rt(self):
        """CLAS リアルタイムを開始/停止する。"""
        self._toggle_engine(self.clas_rt, "CLAS")

    def _toggle_engine(self, engine, name):
        """リアルタイムエンジンを開始/停止する。入力は TCP 配信なので、止まっていれば開始する。"""
        if engine.running:
            engine.stop()
            return
        if self.server is None:
            self.tcp_check.setChecked(True)
            if self.server is None:
                return
        try:
            pos = engine.start(self.server.port, self.folder_edit.text(),
                               self.motion_box.currentData())
        except OSError as exc:
            QMessageBox.warning(self, "mosaic_gnss_recorder", f"{name} を開始できません: {exc}")
            return
        self.info_label.setText(f"{name} の解: {pos}")

    def start_clas(self):
        """記録済みの SBF を選んで CLAS の後処理を始める。"""
        paths, _ = QFileDialog.getOpenFileNames(self, "CLAS で処理する SBF を選ぶ",
                                                self.folder_edit.text(), "SBF (*.sbf *.*)")
        if not paths:
            return
        self.clas_job = engines.ClasJob(sorted(paths), os.path.splitext(paths[0])[0],
                                        motion=self.motion_box.currentData())
        self.clas_job.start()

    def _update_engines(self):
        """補正 PPP の状態表示と、CLAS 後処理の完了を扱う。"""
        for engine, key, name, button, label in (
                (self.madoca, "madoca", "MADOCA", self.madoca_button, self.madoca_label),
                (self.clas_rt, "clas_rt", "CLAS", self.clas_rt_button, self.clas_rt_label)):
            running = engine.running
            button.setText(f"{name} 停止" if running else f"{name} リアルタイム開始")
            label.setText(engines.quality_text(engine.snapshot()[0]) if running else "停止中")
            self.status[key].setText(label.text())
        job = self.clas_job
        self.clas_button.setEnabled(job is None or not job.is_alive())
        if job is None:
            return
        if job.is_alive():
            self.clas_label.setText(f"CLAS: {job.step}")
            return
        self.clas_job = None
        if job.error:
            self.clas_label.setText("CLAS: 失敗")
            QMessageBox.warning(self, "mosaic_gnss_recorder",
                                f"CLAS の処理に失敗しました。\n{job.error}")
            return
        pos, kml, counts = job.result
        summary = "、".join(f"{name} {count}" for name, count in counts.items())
        self.clas_label.setText(f"CLAS: 完了({summary})")
        QMessageBox.information(self, "mosaic_gnss_recorder",
                                f"CLAS の処理が終わりました。\n{summary}\n{pos}\n{kml}")

    def choose_folder(self):
        """保存先を選ぶ。"""
        folder = QFileDialog.getExistingDirectory(self, "保存先", self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)

    def toggle_record(self):
        """記録を開始/停止する。"""
        if self.pipeline.log_status() is not None:
            self.pipeline.stop_logging()
        else:
            try:
                self.pipeline.start_logging(self.folder_edit.text())
            except OSError as exc:
                QMessageBox.critical(self, "mosaic_gnss_recorder", f"記録を開始できません。\n{exc}")
        self._update_buttons()

    def apply_setup(self):
        """記録用の設定を投入する。接続ポート名はプロンプトから得る。"""
        if not self.pipeline.prompt:
            QMessageBox.warning(self, "mosaic_gnss_recorder", "受信機の応答を待っています。")
            return
        capable = self.pipeline.info.get("l6_capable", True)
        self.source.send(link.l6_setup_commands(self.pipeline.prompt, capable))

    def stop_output(self):
        """記録用の SBF 出力を止める。"""
        self.source.send(link.l6_stop_commands())

    def send_command(self):
        """入力されたコマンドを送る。"""
        command = self.command_edit.text().strip()
        if command:
            self.source.send([command])
            self.command_edit.clear()

    def read_settings(self):
        """受信機の現在の設定を読み出す。"""
        self.source.send(settings.READ_COMMANDS)

    def send_settings(self, commands):
        """確認のうえ設定を送り、結果を読み直す。"""
        text = "次のコマンドを送信します。Boot には保存しません。\n\n" + "\n".join(commands)
        if settings.restarts_tracking(commands):
            text += "\n\n衛星の追尾がいったん止まり、再捕捉が始まります。"
        answer = QMessageBox.question(self, "mosaic_gnss_recorder", text)
        if answer == QMessageBox.StandardButton.Yes:
            self.pipeline.info.pop("error", None)
            self.settings_panel.status_label.setText("送信しました。")
            self.source.send([*commands, *settings.READ_COMMANDS])

    def _drain_console(self):
        """受信した文字列をコンソールへ移す。"""
        chunks = []
        try:
            while True:
                chunks.append(self.pipeline.console.get_nowait())
        except queue.Empty:
            pass
        if chunks:
            self.console.moveCursor(self.console.textCursor().MoveOperation.End)
            self.console.insertPlainText("".join(chunks).replace("\r", ""))
            self.console.ensureCursorVisible()

    def _check_source(self):
        """データ源の終了や切断を検出する。"""
        source = self.source
        if source is None or source.is_alive():
            return
        message = f"通信が切れました: {source.error}" if source.error else "再生が終わりました"
        self.info_label.setText(message)
        self.source = None
        self.pipeline.stop_logging()
        self._update_buttons()

    def _update_info(self):
        """接続中の受信機の情報を表示する。"""
        if not self._is_serial():
            return
        info = self.pipeline.info
        port = self.pipeline.prompt or "応答待ち"
        self.info_label.setText(f"{info.get('product', '受信機')} "
                                f"FW {info.get('firmware', '?')}  接続ポート {port}")

    def _warnings(self, snap):
        """注意事項を作る。"""
        notes = []
        if self.pipeline.info.get("l6_capable") is False:
            notes.append("Capabilities に QZSL6 がありません。")
        if snap["scrambled"]:
            notes.append("観測量がスクランブルされています。後処理に使えません。")
        if snap["blocks"] and not snap["l6"]:
            notes.append("L6 メッセージが届いていません。")
        return " ".join(notes)

    def _update_status(self, snap):
        """測位の状態欄を更新する。"""
        for key, value in status_text(snap).items():
            if key == "mode":
                self.mode_label.setText(value)
            else:
                self.status[key].setText(value)
        mode = snap["pvt"].mode if snap["pvt"] else 0
        self.mode_label.setStyleSheet(MODE_STYLE % MODE_COLORS.get(mode, MODE_COLORS[0]))
        self.status["blocks"].setText(str(snap["blocks"]))
        self.status["crc"].setText(
            f"{self.pipeline.stream.crc_errors} / {snap['decode_errors']}")
        log = self.pipeline.log_status()
        self.status["record"].setText(
            f"{os.path.basename(log[0])}\nSBF {log[1] / 1e6:.1f} MB / L6 {log[2]} 個"
            if log else "停止中")
        self.status["tcp"].setText(
            f"127.0.0.1:{self.server.port} / 接続 {self.server.client_count}"
            if self.server else "停止中")
        self.warning_label.setText(self._warnings(snap))

    def _update_views(self, snap):
        """表示中のタブだけ更新する(履歴の描画が重いため)。"""
        current = self.tabs.currentWidget()
        window = self.window_box.currentData()
        if current is self.cn0_chart:
            self.cn0_chart.set_data(charts.cn0_series(snap["history"], window), window)
        elif current is self.pvt_panel:
            self.pvt_panel.update_history(snap["pvt_history"], window)
        elif current is self.sky_plot:
            self.sky_plot.set_positions(snap["tracked"])
        elif current is self.satellite_table:
            fill_table(self.satellite_table, satellite_rows(snap))
        if self.map_view is not None:
            self.map_view.set_track(snap["pvt_history"])
            self.map_view.set_engine_track("madoca", self.madoca.snapshot()[1])
            self.map_view.set_engine_track("clas", self.clas_rt.snapshot()[1])

    def refresh(self):
        """画面を更新する。"""
        self._check_source()
        self._drain_console()
        self._update_info()
        self.settings_panel.update_from(self.pipeline.settings,
                                        self.pipeline.info.get("signals"),
                                        self.pipeline.info.get("error"))
        snap = self.pipeline.state.snapshot()
        self._update_status(snap)
        fill_table(self.qzss_table, qzss_rows(snap))
        fill_table(self.l6_table, l6_rows(snap))
        fill_table(self.signal_table, signal_rows(snap))
        self._update_views(snap)
        self._update_engines()
        self._update_buttons()

    def closeEvent(self, event):  # pylint: disable=invalid-name
        """閉じるときにスレッドと記録と配信を止める。"""
        self._stop_source()
        self.madoca.stop()
        self.clas_rt.stop()
        self.set_tcp_server(False)
        event.accept()


def main():
    """アプリケーションを起動する。"""
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
