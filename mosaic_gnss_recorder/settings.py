"""受信機設定の読み取りとコマンドの組み立て、設定画面。

変更するのは Current だけで、Boot には保存しない。
"""
# pylint: disable=no-member  # Qt のシグナルは pylint から見えない
import re
from collections import namedtuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QVBoxLayout, QWidget)

Setting = namedtuple("Setting", "label key command selector fields")
Preset = namedtuple("Preset", "label rover systems signals e6_usage")

ROVER_MODES = ("StandAlone", "StandAlone+SBAS", "StandAlone+PPP", "StandAlone+SBAS+PPP",
               "StandAlone+SBAS+DGNSS", "StandAlone+SBAS+DGNSS+RTK", "all")
ALL_ROVER_MODES = frozenset(
    ("standalone", "sbas", "dgnss", "rtkfloat", "rtkfixed", "ppp"))
MASKS = ("0", "5", "10", "15", "20", "25", "30")
ON_OFF = ("on", "off")
SETTINGS = (
    Setting("測位モード", "PVTMode", "setPVTMode", None,
            (("Mode", ("Rover", "Static")), ("RoverMode", ROVER_MODES))),
    Setting("仰角マスク(追尾)[°]", "ElevationMask:Tracking", "setElevationMask", "Tracking",
            (("Mask", MASKS),)),
    Setting("仰角マスク(測位)[°]", "ElevationMask:PVT", "setElevationMask", "PVT",
            (("Mask", MASKS),)),
    Setting("受信機ダイナミクス", "ReceiverDynamics", "setReceiverDynamics", None,
            (("Level", ("High", "Moderate", "Low")),
             ("Motion", ("Static", "Quasistatic", "Pedestrian", "Automotive", "RaceCar",
                         "HeavyMachinery", "UAV", "Unlimited")))),
    Setting("マルチパス抑制", "MultipathMitigation", "setMultipathMitigation", None,
            (("Code", ON_OFF), ("Carrier", ON_OFF))),
    Setting("SBAS 補正", "SBASCorrections", "setSBASCorrections", None,
            (("Satellite", ("auto", "EGNOS", "WAAS", "MSAS", "GAGAN", "SDCM", "BDSBAS",
                            "KASS", "ANGA", "SouthPAN")),
             ("SISMode", ("Test", "Operational")))),
)
SIGNALS = ("GPSL1CA", "GPSL2PY", "GPSL2C", "GPSL5", "GPSL1C", "GLOL1CA", "GLOL2P", "GLOL2CA",
           "GLOL3", "GALE1BC", "GALE6BC", "GALE5a", "GALE5b", "GEOL1", "GEOL5", "BDSB1I",
           "BDSB2I", "BDSB3I", "BDSB1C", "BDSB2a", "BDSB2b", "QZSL1CA", "QZSL2C", "QZSL5",
           "QZSL6", "QZSL1C", "QZSL1S", "QZSL1CB", "NAVICL5")
SYSTEMS = {"GPS": "G", "GLONASS": "R", "GALILEO": "E", "SBAS": "S", "BEIDOU": "C",
           "QZSS": "J", "NAVIC": "I"}
HAS_SIGNALS = ("GALE1BC", "GALE6BC", "GALE5a", "GALE5b")
HAS_ROVER = "StandAlone+SBAS+PPP"
PRESETS = (
    Preset("やりたい測位を選択", None, (), (), False),
    Preset("Galileo HAS PPP", HAS_ROVER, ("GALILEO",), HAS_SIGNALS, True),
    Preset("Galileo HAS PPP + L6 記録", HAS_ROVER, ("GALILEO", "QZSS"),
           HAS_SIGNALS + ("QZSL1CA", "QZSL6"), True),
    Preset("単独測位", "StandAlone", (), (), False),
    Preset("SBAS", "StandAlone+SBAS", (), (), False),
    Preset("RTK(補正データの入力が別途必要)", "StandAlone+SBAS+DGNSS+RTK", (), (), False),
)
READ_COMMANDS = ("getPVTMode", "getElevationMask", "getReceiverDynamics",
                 "getMultipathMitigation", "getSBASCorrections", "getSatelliteTracking",
                 "getSignalTracking", "getSignalUsage")
REPLY_RE = re.compile(r"^\s*(PVTMode|ElevationMask|ReceiverDynamics|MultipathMitigation"
                      r"|SBASCorrections|SatelliteTracking|SignalTracking|SignalUsage),\s*(.*)$")
SELECTOR_REPLY = "ElevationMask"
SIGNAL_COLUMNS = 5
RESTART_COMMANDS = ("setSignalTracking", "setSatelliteTracking")


def parse_reply(text):
    """コマンド応答から設定値を {キー: [値, ...]} で取り出す。"""
    found = {}
    for line in text.splitlines():
        match = REPLY_RE.match(line)
        if not match:
            continue
        name, fields = match.group(1), [f.strip() for f in match.group(2).split(",")]
        if name == SELECTOR_REPLY:
            name, fields = f"{name}:{fields[0]}", fields[1:]
        found[name] = fields
    return found


def canonical(value):
    """+ で結んだ値を、順序と大文字小文字によらず比べられる形にする。"""
    tokens = {token.lower() for token in value.split("+")}
    if "rtk" in tokens:
        tokens = (tokens - {"rtk"}) | {"rtkfloat", "rtkfixed"}
    return ALL_ROVER_MODES if "all" in tokens else frozenset(tokens)


def match_choice(value, choices):
    """value と同じ意味の選択肢を返す。無ければ value のまま返す。"""
    return next((c for c in choices if canonical(c) == canonical(value)), value)


def changed_commands(setting, current, chosen):
    """プルダウンの値が現在値と違うときの set コマンドを返す。"""
    if [canonical(v) for v in current] == [canonical(v) for v in chosen]:
        return []
    args = ([setting.selector] if setting.selector else []) + list(chosen)
    return [f"{setting.command},{','.join(args)}"]


def enabled_signals(fields, capable=SIGNALS):
    """SignalTracking の応答から有効な信号を返す。"""
    tokens = fields[0].split("+") if fields else []
    return set(capable) if "all" in tokens else set(tokens)


def signal_commands(current, chosen):
    """信号の選択が変わったときの set コマンドを返す。"""
    if not chosen or set(chosen) == set(current):
        return []
    others = sorted(set(chosen) - set(SIGNALS))
    return ["setSignalTracking," + "+".join(
        [name for name in SIGNALS if name in chosen] + others)]


def enabled_systems(fields):
    """SatelliteTracking の応答から、1 機でも有効な衛星系を返す。"""
    tokens = fields[0].split("+") if fields else []
    if "all" in tokens:
        return set(SYSTEMS)
    return {name for name, prefix in SYSTEMS.items()
            if name in tokens or any(re.fullmatch(prefix + r"\d+", t) for t in tokens)}


def system_commands(current, chosen):
    """衛星系の選択が変わったときの set コマンドを返す。"""
    return ([f"setSatelliteTracking,+{name}" for name in SYSTEMS
             if name in chosen and name not in current]
            + [f"setSatelliteTracking,-{name}" for name in SYSTEMS
               if name in current and name not in chosen])


def usage_commands(fields):
    """E6 の航法データを使っていないときの set コマンドを返す。"""
    if len(fields) < 2 or "GALE6BC" in fields[1].split("+"):
        return []
    return ["setSignalUsage,,+GALE6BC"]


def restarts_tracking(commands):
    """追尾に影響するコマンドが含まれていれば真を返す。"""
    return any(command.startswith(RESTART_COMMANDS) for command in commands)


class SettingsPanel(QScrollArea):  # pylint: disable=too-many-instance-attributes
    """現在の設定をプルダウンで変更して送信する画面。"""

    def __init__(self, on_read, on_send):
        super().__init__()
        self._on_send = on_send
        self._shown = None
        self._settings = {}
        self._combos = {}
        self._labels = {}
        self._systems = {}
        self._signals = {}
        self._use_e6 = False
        body = QWidget()
        layout = QVBoxLayout(body)
        row = QHBoxLayout()
        self.read_button = QPushButton("現在値を読み込む")
        self.read_button.clicked.connect(on_read)
        self.send_button = QPushButton("変更を送信")
        self.send_button.clicked.connect(self._send)
        self.status_label = QLabel("Current のみ変更します。Boot には保存しません。")
        self.preset_box = QComboBox()
        self.preset_box.addItems([preset.label for preset in PRESETS])
        self.preset_box.currentIndexChanged.connect(self._apply_preset)
        row.addWidget(self.preset_box)
        row.addWidget(self.read_button)
        row.addWidget(self.send_button)
        row.addWidget(self.status_label, 1)
        layout.addLayout(row)
        layout.addWidget(self._build_combos())
        layout.addWidget(self._build_checks("衛星系(setSatelliteTracking)", SYSTEMS,
                                            self._systems))
        signal_group = self._build_checks("信号(setSignalTracking)", SIGNALS, self._signals)
        self._signal_grid = signal_group.layout()
        layout.addWidget(signal_group)
        layout.addStretch(1)
        self.setWidget(body)
        self.setWidgetResizable(True)

    def _build_combos(self):
        """プルダウンの欄を作る。"""
        group = QGroupBox("測位と追尾")
        grid = QGridLayout(group)
        for col, text in enumerate(("項目", "現在値", "変更後")):
            grid.addWidget(QLabel(text), 0, col)
        for row, setting in enumerate(SETTINGS, 1):
            grid.addWidget(QLabel(setting.label), row, 0)
            self._labels[setting.key] = QLabel("—")
            grid.addWidget(self._labels[setting.key], row, 1)
            combos = []
            box = QHBoxLayout()
            for _name, choices in setting.fields:
                combo = QComboBox()
                combo.addItems(choices)
                combo.setEnabled(False)
                combos.append(combo)
                box.addWidget(combo)
            self._combos[setting.key] = combos
            grid.addLayout(box, row, 2)
        grid.setColumnStretch(2, 1)
        return group

    @staticmethod
    def _build_checks(title, names, boxes):
        """チェックボックスの欄を作る。"""
        group = QGroupBox(title)
        grid = QGridLayout(group)
        for name in names:
            boxes[name] = QCheckBox(name)
            boxes[name].setEnabled(False)
        SettingsPanel._place(grid, boxes.values())
        return group

    @staticmethod
    def _place(grid, boxes):
        """チェックボックスを左上から詰めて並べる。"""
        for index, box in enumerate(boxes):
            grid.addWidget(box, index // SIGNAL_COLUMNS, index % SIGNAL_COLUMNS,
                           Qt.AlignmentFlag.AlignLeft)

    def update_from(self, values, capable, error=None):
        """受信機から読んだ値を画面に反映する。値が変わったときだけ入力を置き換える。"""
        if error:
            self.status_label.setText(f"受信機のエラー: {error}")
        if self._shown == (values, capable):
            return
        self._shown = (dict(values), capable)
        self._settings = dict(values)
        self.preset_box.setCurrentIndex(0)
        for setting in SETTINGS:
            self._show_setting(setting, values.get(setting.key))
        signals = values.get("SignalTracking")
        systems = values.get("SatelliteTracking")
        self._place(self._signal_grid,
                    [box for name, box in self._signals.items()
                     if not capable or name in capable])
        for name, box in self._signals.items():
            box.setVisible(not capable or name in capable)
            box.setEnabled(signals is not None)
            box.setChecked(name in enabled_signals(signals or [], capable or SIGNALS))
        for name, box in self._systems.items():
            box.setEnabled(systems is not None)
            box.setChecked(name in enabled_systems(systems or []))

    def _apply_preset(self, index):
        """目的に関係する項目を選択済みにする。送信はしない。"""
        preset = PRESETS[index]
        self._use_e6 = preset.e6_usage
        if preset.rover is None:
            return
        self._combos["PVTMode"][0].setCurrentText("Rover")
        self._combos["PVTMode"][1].setCurrentText(preset.rover)
        for name in preset.systems:
            self._systems[name].setChecked(True)
        missing = [name for name in preset.signals if self._signals[name].isHidden()]
        for name in preset.signals:
            self._signals[name].setChecked(True)
        text = f"{preset.label} を選択しました。[変更を送信] で反映します。"
        if missing:
            text = f"この受信機は {'、'.join(missing)} に対応していません。"
        self.status_label.setText(text)

    def _show_setting(self, setting, fields):
        """1 項目の現在値とプルダウンを更新する。"""
        count = len(setting.fields)
        known = fields is not None and len(fields) >= count
        self._labels[setting.key].setText(", ".join(fields[:count]) if known else "—")
        for index, combo in enumerate(self._combos[setting.key]):
            combo.setEnabled(known)
            if known:
                text = match_choice(fields[index], setting.fields[index][1])
                if combo.findText(text) < 0:
                    combo.addItem(text)
                combo.setCurrentText(text)

    def commands(self):
        """画面の入力と現在値の差を set コマンドにして返す。"""
        result = []
        for setting in SETTINGS:
            fields = self._settings.get(setting.key)
            if fields is None or not self._combos[setting.key][0].isEnabled():
                continue
            chosen = [combo.currentText() for combo in self._combos[setting.key]]
            result += changed_commands(setting, fields[:len(chosen)], chosen)
        systems = self._settings.get("SatelliteTracking")
        if systems is not None:
            chosen = {n for n, box in self._systems.items() if box.isChecked()}
            result += system_commands(enabled_systems(systems), chosen)
        signals = self._settings.get("SignalTracking")
        if signals is not None:
            current = enabled_signals(signals)
            hidden = current - {n for n, box in self._signals.items() if not box.isHidden()}
            chosen = {n for n, box in self._signals.items()
                      if box.isChecked() and not box.isHidden()}
            result += signal_commands(current, chosen | hidden)
        if self._use_e6:
            result += usage_commands(self._settings.get("SignalUsage", []))
        return result

    def _send(self):
        """変更があれば送信を依頼する。"""
        commands = self.commands()
        if commands:
            self._on_send(commands)
        else:
            self.status_label.setText("変更はありません。")
