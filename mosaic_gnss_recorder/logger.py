"""検査済みの SBF ブロックと、L6 メッセージ(.l6)をファイルに保存する。"""
import csv
import os
import time

from . import sbf_parser as sp

FLUSH_INTERVAL = 1.0
CSV_COLUMNS = ("wnc", "tow_s", "mode", "latitude_deg", "longitude_deg", "height_m",
               "speed_mps", "nrsv", "haccuracy_m", "vaccuracy_m")


class SessionLogger:  # pylint: disable=too-many-instance-attributes
    """1 回の記録ぶんのファイルを管理する。スレッド間の排他は呼び出し側で行う。"""

    def __init__(self, folder, prefix="mosaic"):
        os.makedirs(folder, exist_ok=True)
        self.folder = folder
        self.base = f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}"
        self.sbf_path = os.path.join(folder, self.base + ".sbf")
        self._sbf = open(self.sbf_path, "xb")  # pylint: disable=consider-using-with
        csv_path = os.path.join(folder, self.base + "_pvt.csv")
        # pylint: disable-next=consider-using-with
        self._csv_file = open(csv_path, "x", encoding="utf-8", newline="")
        self._csv = csv.writer(self._csv_file)
        self._csv.writerow(CSV_COLUMNS)
        self._l6_files = {}
        self._next_flush = time.monotonic() + FLUSH_INTERVAL
        self.sbf_bytes = 0
        self.l6_messages = 0

    def write_block(self, block):
        """SBF ブロックをそのまま書く。"""
        self._sbf.write(block.raw)
        self.sbf_bytes += len(block.raw)
        if time.monotonic() >= self._next_flush:
            self.flush()

    def write_l6(self, msg):
        """RS 復号に成功した L6 メッセージを衛星・チャネル別の .l6 に書く。"""
        if not msg.parity:
            return
        key = (msg.channel, msg.svid)
        if key not in self._l6_files:
            name = f"{self.base}_{msg.channel}_{sp.sv_name(msg.svid)}.l6"
            path = os.path.join(self.folder, name)
            self._l6_files[key] = open(path, "xb")  # pylint: disable=consider-using-with
        self._l6_files[key].write(msg.data)
        self.l6_messages += 1

    def write_pvt(self, pvt):
        """測位結果を CSV に 1 行書く。"""
        def text(value, digits):
            return "" if value is None else f"{value:.{digits}f}"
        self._csv.writerow((
            pvt.wnc, f"{pvt.tow:.3f}", sp.PVT_MODES.get(pvt.mode, str(pvt.mode)),
            text(pvt.latitude, 9), text(pvt.longitude, 9), text(pvt.height, 3),
            text(pvt.speed, 3), "" if pvt.nrsv is None else pvt.nrsv,
            text(pvt.haccuracy, 2), text(pvt.vaccuracy, 2)))

    def flush(self):
        """バッファをディスクへ書き出す。"""
        self._sbf.flush()
        self._csv_file.flush()
        for fout in self._l6_files.values():
            fout.flush()
        self._next_flush = time.monotonic() + FLUSH_INTERVAL

    def close(self):
        """全ファイルを閉じる。"""
        self._sbf.close()
        self._csv_file.close()
        for fout in self._l6_files.values():
            fout.close()
        self._l6_files.clear()
