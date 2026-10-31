"""グラフとスカイプロットの部品。"""
import math

from PySide6.QtCharts import (
    QCategoryAxis, QChart, QChartView, QLineSeries, QValueAxis)
from PySide6.QtCore import QMargins, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

MAX_CHART_POINTS = 600
EARTH_RADIUS = 6378137.0
MODE_LABELS = ("No PVT", "Standalone", "SBAS", "DGNSS", "PPP", "RTK Float", "RTK Fixed")
# PVTGeodetic の Mode 値 → MODE_LABELS の順位(3: Fixed は静止設定の単独測位相当)
MODE_RANKS = {0: 0, 1: 1, 3: 1, 6: 2, 2: 3, 10: 4, 5: 5, 8: 5, 4: 6, 7: 6}
# 記録の長さ [分] の上限と、そのときの時間軸の単位・1 分あたりの換算係数
TIME_UNITS = ((2, "秒", 60.0), (120, "分", 1.0), (float("inf"), "時間", 1 / 60))
# グラフの表示幅 (表示名, 秒)。None は全期間
TIME_WINDOWS = (("表示幅 1 分", 60.0), ("表示幅 5 分", 300.0), ("表示幅 15 分", 900.0),
                ("表示幅 1 時間", 3600.0), ("全期間", None))
DEFAULT_WINDOW = 1
X_TICKS = 6
Y_TICKS = 4
SYSTEM_COLORS = {"G": "#2980b9", "R": "#c0392b", "E": "#27ae60", "C": "#8e44ad",
                 "J": "#d35400", "S": "#7f8c8d", "I": "#16a085"}


def nice_step(span, count, integer=False):
    """span を count 分割する程度の、1・2・5 × 10^n の目盛り間隔を返す。"""
    raw = max(span, 1e-9) / count
    power = 10.0 ** math.floor(math.log10(raw))
    step = next(m * power for m in (1, 2, 5, 10) if m * power >= raw * 0.999)
    return max(step, 1.0) if integer else step


def label_format(step):
    """目盛り間隔に見合う桁数の書式を返す。"""
    digits = max(0, -math.floor(math.log10(step) + 1e-9))
    return f"%.{digits}f"


def time_unit(span_minutes):
    """記録の長さに合う時間軸の (単位, 1 分あたりの換算係数) を返す。"""
    return next((unit, scale) for limit, unit, scale in TIME_UNITS if span_minutes < limit)


def set_ticks(axis, low, high, step):
    """数値軸の範囲と、切りのよい値に並ぶ目盛りを設定する。"""
    axis.setTickType(QValueAxis.TickType.TicksDynamic)
    axis.setTickAnchor(0.0)
    axis.setTickInterval(step)
    axis.setLabelFormat(label_format(step))
    axis.setRange(low, high)


class LineChart(QChartView):
    """複数系列の時系列グラフ。y_range を省くと縦軸を自動調整する。"""

    # pylint: disable-next=too-many-arguments,too-many-positional-arguments
    def __init__(self, title, y_title, y_range=None, y_axis=None, y_floor=None,
                 integer=False):
        self._chart = QChart()
        super().__init__(self._chart)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._chart.setTitle(title)
        self._chart.setMargins(QMargins(2, 2, 8, 2))
        self._chart.layout().setContentsMargins(0, 0, 0, 0)
        self._axis_x = QValueAxis()
        self._axis_x.setTitleText("経過時間 [分]")
        self._axis_y = y_axis or QValueAxis()
        self._axis_y.setTitleText(y_title)
        for axis in (self._axis_x, self._axis_y):
            axis.setLabelsFont(QFont(self.font().family(), 8))
        self._y_range = y_range
        self._y_floor = y_floor
        self._integer = integer
        if y_range and isinstance(self._axis_y, QValueAxis):
            set_ticks(self._axis_y, *y_range, 5.0)
        elif y_range:
            self._axis_y.setRange(*y_range)
        self._chart.addAxis(self._axis_x, Qt.AlignmentFlag.AlignBottom)
        self._chart.addAxis(self._axis_y, Qt.AlignmentFlag.AlignLeft)
        self._series = {}

    def _get_series(self, key):
        """系列を返す。無ければ作る。"""
        if key not in self._series:
            series = QLineSeries()
            series.setName(key)
            self._chart.addSeries(series)
            series.attachAxis(self._axis_x)
            series.attachAxis(self._axis_y)
            self._series[key] = series
        return self._series[key]

    def set_data(self, data, window=None):
        """{系列名: [(分, 値)]} でグラフを描き直す。window [秒] があれば右端を現在にする。"""
        filled = {key: val for key, val in data.items() if val}
        if not filled:
            return
        span = window / 60 if window else max(1.0, max(v[-1][0] for v in filled.values()))
        unit, scale = time_unit(span)
        for key, series in self._series.items():
            series.setVisible(key in filled)
        for key in sorted(filled):
            points = filled[key]
            step = max(1, len(points) // MAX_CHART_POINTS)
            self._get_series(key).replace(
                [QPointF(x * scale, y) for x, y in points[::step]])
        x_step = nice_step(span * scale, X_TICKS)
        if window:
            self._axis_x.setTitleText(f"現在からの時間 [{unit}]")
            set_ticks(self._axis_x, -span * scale, 0.0, x_step)
        else:
            self._axis_x.setTitleText(f"経過時間 [{unit}]")
            set_ticks(self._axis_x, 0.0, math.ceil(span * scale / x_step) * x_step, x_step)
        if not self._y_range:
            self._fit_y([y for val in filled.values() for _x, y in val])
        self._chart.legend().setVisible(len(filled) > 1)

    def _fit_y(self, values):
        """縦軸を値の範囲に合わせ、目盛りを切りのよい値にする。"""
        low, high = min(values), max(values)
        if self._y_floor is not None:
            low = max(self._y_floor, low)
        step = nice_step(max(high - low, 1.0 if self._integer else 0.1), Y_TICKS,
                         self._integer)
        lower = math.floor(low / step) * step
        upper = math.ceil(high / step) * step
        if upper - lower < step * 1.5:
            upper += step
        set_ticks(self._axis_y, lower, upper, step)

    def clear(self):
        """全系列を消す。"""
        self._chart.removeAllSeries()
        self._series = {}


def used_ranks(points):
    """履歴に現れた測位モードの順位を昇順で返す。"""
    return sorted({int(y) for _x, y in points})


class ModeChart(LineChart):
    """測位モードの履歴。縦軸には実際に現れたモードだけを並べる。"""

    def __init__(self):
        self._category = QCategoryAxis()
        self._category.setLabelsPosition(
            QCategoryAxis.AxisLabelsPosition.AxisLabelsPositionOnValue)
        super().__init__("測位モード", "", (-0.5, 0.5), self._category)
        self._ranks = []

    def set_data(self, data, window=None):
        """測位モードの順位を、軸の並びの番号に置き換えて描く。"""
        points = [p for val in data.values() for p in val]
        if not points:
            return
        ranks = used_ranks(points)
        if ranks != self._ranks:
            self._set_labels(ranks)
        index = {rank: pos for pos, rank in enumerate(ranks)}
        super().set_data({key: [(x, index[int(y)]) for x, y in val]
                          for key, val in data.items()}, window)

    def _set_labels(self, ranks):
        """軸のラベルを作り直す。"""
        for label in self._category.categoriesLabels():
            self._category.remove(label)
        for pos, rank in enumerate(ranks):
            self._category.append(MODE_LABELS[rank], pos)
        self._category.setRange(-0.5, len(ranks) - 0.5)
        self._ranks = ranks

    def clear(self):
        """全系列とラベルを消す。"""
        super().clear()
        self._set_labels([])


def cn0_series(history, window=None):
    """C/N0 の履歴を分単位の系列にする。window [秒] があれば現在からの時間にする。"""
    filled = [val for val in history.values() if val]
    if not filled:
        return {}
    latest = max(val[-1][0] for val in filled)
    origin = latest if window else min(val[0][0] for val in filled)
    oldest = latest - window if window else origin
    return {key: [((t - origin) / 60, v) for t, v in val if t >= oldest]
            for key, val in history.items()}


def _offsets(points, start):
    """位置の履歴を平均位置からの東西・南北・高さの差 [m] にする。"""
    fixes = [(t, p) for t, p in points if p.latitude is not None]
    if not fixes:
        return {}
    lat0 = sum(p.latitude for _t, p in fixes) / len(fixes)
    lon0 = sum(p.longitude for _t, p in fixes) / len(fixes)
    hgt0 = sum(p.height for _t, p in fixes) / len(fixes)
    scale = math.radians(EARTH_RADIUS)
    return {
        "東西": [((t - start) / 60, (p.longitude - lon0) * scale * math.cos(math.radians(lat0)))
               for t, p in fixes],
        "南北": [((t - start) / 60, (p.latitude - lat0) * scale) for t, p in fixes],
        "高さ": [((t - start) / 60, p.height - hgt0) for t, p in fixes],
    }


def pvt_series(points, window=None):
    """測位履歴をグラフの系列に分ける。位置は表示範囲の平均からの差だけを使う。"""
    if not points:
        return {"mode": {}, "offset": {}, "accuracy": {}, "nrsv": {}}
    start = points[0][0]
    if window:
        start = points[-1][0]
        points = [point for point in points if point[0] >= start - window]

    def series(field):
        return [((t - start) / 60, getattr(p, field)) for t, p in points
                if getattr(p, field) is not None]

    return {
        "mode": {"測位モード": [((t - start) / 60, MODE_RANKS.get(p.mode, 0))
                           for t, p in points]},
        "offset": _offsets(points, start),
        "accuracy": {"水平": series("haccuracy"), "垂直": series("vaccuracy")},
        "nrsv": {"使用衛星数": series("nrsv")},
    }


class SkyPlot(QWidget):
    """追尾中の衛星の配置図。中心が天頂、外周が地平線、上が北。"""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(300, 300)
        self._positions = {}

    def set_positions(self, positions):
        """{衛星名: (方位角, 仰角)} を設定して再描画する。"""
        self._positions = positions
        self.update()

    def paintEvent(self, _event):  # pylint: disable=invalid-name
        """配置図を描く。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(self.width() / 2, self.height() / 2)
        radius = min(self.width(), self.height()) / 2 - 24
        painter.setPen(QPen(QColor("#b0b0b0")))
        for elevation in (0, 30, 60):
            size = radius * (90 - elevation) / 90
            painter.drawEllipse(center, size, size)
        painter.drawLine(center + QPointF(0, -radius), center + QPointF(0, radius))
        painter.drawLine(center + QPointF(-radius, 0), center + QPointF(radius, 0))
        for label, x_dir, y_dir in (("N", 0, -1), ("E", 1, 0), ("S", 0, 1), ("W", -1, 0)):
            point = center + QPointF(x_dir * (radius + 12), y_dir * (radius + 12))
            painter.drawText(QRectF(point.x() - 10, point.y() - 10, 20, 20),
                             Qt.AlignmentFlag.AlignCenter, label)
        for name, (azimuth, elevation) in sorted(self._positions.items()):
            if azimuth is None or elevation is None or elevation < 0:
                continue
            distance = radius * (90 - elevation) / 90
            angle = math.radians(azimuth)
            point = center + QPointF(distance * math.sin(angle), -distance * math.cos(angle))
            painter.setBrush(QColor(SYSTEM_COLORS.get(name[0], "#000000")))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(point, 5, 5)
            painter.setPen(QPen(QColor("#303030")))
            painter.drawText(point + QPointF(7, 4), name)
