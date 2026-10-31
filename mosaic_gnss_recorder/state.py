"""デコード結果を集計し、GUI へ渡す状態を保持する。"""
import struct
import threading
from collections import defaultdict, deque

from . import sbf_parser as sp

HISTORY_LENGTH = 14400
RECENT_LENGTH = 60
WEEK_SECONDS = 604800
HISTORY_SIGNALS = {27: "L6", 6: "L1CA"}
QZSS_SIGNALS = {6: "L1CA", 7: "L2C", 26: "L5", 27: "L6"}
BANDS = ("L1", "L2", "L5", "E6/L6/B3")
SIGNAL_BANDS = {
    0: 0, 1: 0, 5: 0, 6: 0, 8: 0, 9: 0, 13: 0, 17: 0, 24: 0, 28: 0, 32: 0, 33: 0, 37: 0, 38: 0,
    2: 1, 3: 1, 7: 1, 10: 1, 11: 1,
    4: 2, 12: 2, 14: 2, 15: 2, 20: 2, 21: 2, 22: 2, 25: 2, 26: 2, 29: 2, 34: 2, 39: 2,
    19: 3, 27: 3, 30: 3,
}


class L6Stat:
    """チャネル・衛星ごとの L6 受信統計。"""

    def __init__(self):
        self.total = 0
        self.good = 0
        self.rs_sum = 0
        self.recent = deque(maxlen=RECENT_LENGTH)

    def add(self, msg):
        """メッセージを 1 つ集計する。"""
        self.total += 1
        self.good += msg.parity
        self.rs_sum += msg.rs_count
        self.recent.append(msg.parity)

    def row(self):
        """表示用の値を dict で返す。"""
        recent = 100.0 * sum(self.recent) / len(self.recent) if self.recent else 0.0
        return {"total": self.total, "good": self.good,
                "rate": 100.0 * self.good / self.total if self.total else 0.0,
                "recent": recent,
                "rs_mean": self.rs_sum / self.total if self.total else 0.0}


class MonitorState:  # pylint: disable=too-many-instance-attributes
    """受信スレッドが更新し、GUI が snapshot() で読む状態。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._pvt = None
        self._epoch_time = None
        self._scrambled = False
        self._qzss = {}
        self._positions = {}
        self._tracked = set()
        self._signal_cn0 = {}
        self._sat_bands = {}
        self._pvt_history = deque(maxlen=HISTORY_LENGTH)
        self._l6 = defaultdict(L6Stat)
        self._history = defaultdict(lambda: deque(maxlen=HISTORY_LENGTH))
        self._blocks = 0
        self._decode_errors = 0

    def on_block(self, block):
        """SBF ブロックを 1 つ取り込み、Pvt か L6Message ならそれを返す。"""
        with self._lock:
            self._blocks += 1
            try:
                return self._dispatch(block)
            except (struct.error, IndexError):
                self._decode_errors += 1
                return None

    def _dispatch(self, block):
        """ブロック番号に応じてデコードする。"""
        body = block.raw[sp.HEADER_SIZE:]
        if block.number == sp.BLOCK_PVT_GEODETIC:
            self._pvt = sp.decode_pvt(body)
            self._pvt_history.append(
                (self._pvt.wnc * WEEK_SECONDS + self._pvt.tow, self._pvt))
            return self._pvt
        if block.number == sp.BLOCK_MEAS_EPOCH:
            self._on_meas(sp.decode_meas_epoch(body))
        elif block.number == sp.BLOCK_CHANNEL_STATUS:
            channels = sp.decode_channel_status(body)
            self._positions = {p.svid: p for p in channels}
            self._tracked = {p.svid for p in channels if p.tracking}
        elif block.number in (sp.BLOCK_L6D, sp.BLOCK_L6E):
            msg = sp.decode_l6(block.number, body)
            self._l6[(msg.channel, msg.svid)].add(msg)
            return msg
        return None

    def _on_meas(self, epoch):
        """1 エポックぶんの C/N0 を反映する。"""
        time_sec = epoch.wnc * WEEK_SECONDS + epoch.tow
        self._epoch_time = time_sec
        self._scrambled = epoch.scrambled
        qzss = defaultdict(dict)
        by_signal = defaultdict(dict)
        bands = defaultdict(dict)
        for meas in epoch.measurements:
            name = sp.sv_name(meas.svid)
            if meas.cn0 is None or name is None or meas.signal not in sp.SIGNAL_NAMES:
                continue
            by_signal[sp.SIGNAL_NAMES[meas.signal]][name] = meas.cn0
            if meas.signal in SIGNAL_BANDS:
                band = BANDS[SIGNAL_BANDS[meas.signal]]
                bands[name][band] = max(meas.cn0, bands[name].get(band, 0.0))
            if name.startswith("J"):
                if meas.signal in QZSS_SIGNALS:
                    qzss[meas.svid][QZSS_SIGNALS[meas.signal]] = meas.cn0
                if meas.signal in HISTORY_SIGNALS:
                    key = f"{name} {HISTORY_SIGNALS[meas.signal]}"
                    self._history[key].append((time_sec, meas.cn0))
        self._qzss = dict(qzss)
        self._signal_cn0 = dict(by_signal)
        self._sat_bands = dict(bands)

    def snapshot_time(self):
        """最新エポックの時刻(週番号込みの秒)を返す。"""
        with self._lock:
            return self._epoch_time

    def snapshot(self):
        """GUI 表示用に現在の状態を複製して返す。"""
        with self._lock:
            return {
                "pvt": self._pvt,
                "epoch_time": self._epoch_time,
                "scrambled": self._scrambled,
                "blocks": self._blocks,
                "decode_errors": self._decode_errors,
                "qzss": {sp.sv_name(svid): dict(sig) for svid, sig in self._qzss.items()},
                "elevations": {sp.sv_name(p.svid): p.elevation
                               for p in self._positions.values() if sp.sv_name(p.svid)},
                "positions": {sp.sv_name(p.svid): (p.azimuth, p.elevation)
                              for p in self._positions.values() if sp.sv_name(p.svid)},
                "tracked": {sp.sv_name(p.svid): (p.azimuth, p.elevation)
                            for p in self._positions.values()
                            if p.svid in self._tracked and sp.sv_name(p.svid)},
                "sat_bands": {name: dict(val) for name, val in self._sat_bands.items()},
                "pvt_history": list(self._pvt_history),
                "l6": {(ch, sp.sv_name(svid)): stat.row()
                       for (ch, svid), stat in self._l6.items()},
                "signals": {name: list(sats.values())
                            for name, sats in self._signal_cn0.items()},
                "history": {key: list(val) for key, val in self._history.items()},
            }
