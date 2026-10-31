"""mosaic_gnss_recorder の解析・集計・保存・通信のテスト。"""
import binascii
import csv
import math
import os
import struct
import sys
import tempfile

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

# pylint: disable=wrong-import-position
from mosaic_gnss_recorder import app, charts, sbf_parser as sp  # noqa: E402
from mosaic_gnss_recorder.logger import SessionLogger  # noqa: E402
from mosaic_gnss_recorder.serial_link import (  # noqa: E402
    Pipeline, SerialSource, l6_setup_commands)
from mosaic_gnss_recorder.state import MonitorState  # noqa: E402


def wrap(number, body):
    """本体に SBF ヘッダを付ける。"""
    body += b"\0" * ((-(len(body) + 8)) % 4)
    head = struct.pack("<HH", number, len(body) + 8)
    return b"$@" + struct.pack("<H", binascii.crc_hqx(head + body, 0)) + head + body


def l6_block(number=sp.BLOCK_L6D, tow=1000, parity=1, fill=0x55):
    """L6 ブロックを作る。"""
    payload = b"\x1a\xcf\xfc\x1d" + bytes([fill]) * 248
    words = struct.unpack(">63I", payload)
    body = struct.pack("<IHBBBBBB", tow, 2441, 187, parity, 2, 1, 0, 63)
    return wrap(number, body + struct.pack("<63I", *words))


def test_stream_handles_split_blocks_and_text():
    """チャンク境界で分かれたブロックと、前後の文字列を分離する。"""
    stream = sp.SbfStream()
    data = b"$R: ok\r\nUSB1>" + l6_block() + b"tail"
    results = [stream.feed(data[i:i + 7]) for i in range(0, len(data), 7)]
    assert sum(len(r[0]) for r in results) == 1
    assert "".join(r[1] for r in results) == "$R: ok\r\nUSB1>tail"


def test_stream_rejects_bad_crc():
    """CRC 不一致のブロックは捨てて数える。"""
    bad = bytearray(l6_block())
    bad[-1] ^= 0xFF
    stream = sp.SbfStream()
    blocks, _ = stream.feed(bytes(bad) + l6_block())
    assert len(blocks) == 1
    assert stream.crc_errors == 1


def test_sv_name():
    """衛星番号の変換。未定義は None。"""
    assert sp.sv_name(187) == "J07"
    assert sp.sv_name(71) == "E01"
    assert sp.sv_name(223) == "C41"
    assert sp.sv_name(0) is None


def test_decode_meas_epoch_cn0_rules():
    """L2P は +10 しない。拡張信号番号と無効値を扱う。"""
    sb1 = bytearray(20)
    sb1[1], sb1[2], sb1[15], sb1[19] = 2, 5, 80, 2
    sb2a = bytearray(16)
    sb2a[0], sb2a[2], sb2a[5] = 31, 100, 1 << 3
    sb2b = bytearray(16)
    sb2b[0], sb2b[2] = 3, 255
    body = struct.pack("<IHBBBBBB", 5000, 2441, 1, 20, 16, 0x80, 0, 0)
    body += bytes(sb1 + sb2a + sb2b)
    epoch = sp.decode_meas_epoch(body)
    assert epoch.scrambled
    assert epoch.measurements[0].cn0 == 20.0
    assert epoch.measurements[1].signal == 33
    assert epoch.measurements[1].cn0 == 35.0
    assert epoch.measurements[2].cn0 is None


def pvt_block(mode=10, lat=0.6, lon=2.4):
    """PVTGeodetic ブロックを作る。"""
    body = bytearray(88)
    struct.pack_into("<IHBB", body, 0, 550930000, 2441, mode, 0)
    struct.pack_into("<ddd", body, 8, lat, lon, 72.5)
    struct.pack_into("<ff", body, 36, 3.0, 4.0)
    body[66] = 9
    struct.pack_into("<HH", body, 82, 48, 120)
    return wrap(sp.BLOCK_PVT_GEODETIC, bytes(body))


def test_decode_pvt_position_and_do_not_use():
    """緯度経度は度に換算し、Do-Not-Use は None にする。"""
    state = MonitorState()
    state.on_block(sp.SbfStream().feed(pvt_block())[0][0])
    pvt = state.snapshot()["pvt"]
    assert pvt.latitude == pytest.approx(math.degrees(0.6))
    assert pvt.speed == pytest.approx(5.0)
    assert pvt.haccuracy == 0.48
    invalid = sp.SbfStream().feed(pvt_block(mode=0, lat=-2e10, lon=-2e10))[0][0]
    state.on_block(invalid)
    assert state.snapshot()["pvt"].latitude is None
    assert len(state.snapshot()["pvt_history"]) == 2


def test_pvt_is_saved_to_csv(tmp_path):
    """測位結果を CSV に保存する。無効な座標は空欄にする。"""
    pipeline = Pipeline()
    pipeline.start_logging(str(tmp_path))
    pipeline.process(pvt_block() + pvt_block(mode=0, lat=-2e10, lon=-2e10))
    pipeline.stop_logging()
    path = next(tmp_path.glob("*_pvt.csv"))
    with open(path, encoding="utf-8", newline="") as fin:
        rows = list(csv.DictReader(fin))
    assert rows[0]["mode"] == "PPP"
    assert float(rows[0]["latitude_deg"]) == pytest.approx(math.degrees(0.6))
    assert rows[1]["latitude_deg"] == ""


def test_setup_commands_for_receiver_without_l6():
    """QZSL6 が無い受信機には L6 の設定を送らない。"""
    commands = l6_setup_commands("USB1", l6_capable=False)
    assert len(commands) == 1
    assert "QZSRawL6D" not in commands[0] and "PVTGeod" in commands[0]


def test_unknown_satellite_is_not_in_positions():
    """名前の決まらない衛星番号は衛星配置の表示に渡さない。"""
    sat = bytearray(12)
    sat[0], sat[8] = 62, 30
    sat2 = bytearray(12)
    sat2[0], sat2[8] = 187, 48
    body = struct.pack("<IHBBBBBB", 1000, 2441, 2, 12, 8, 0, 0, 0) + bytes(sat + sat2)
    state = MonitorState()
    state.on_block(sp.SbfStream().feed(wrap(sp.BLOCK_CHANNEL_STATUS, body))[0][0])
    assert list(state.snapshot()["positions"]) == ["J07"]


def test_sky_shows_only_tracked_satellites():
    """探索中のチャネルは衛星配置に出さない。同じ衛星の別チャネルが追尾中なら出す。"""
    def channel(svid, status):
        sat = bytearray(12)
        sat[0], sat[8], sat[9] = svid, 30, 1
        return bytes(sat) + struct.pack("<BBHHH", 0, 0, status, 0, 0)

    sats = channel(1, 0x0001) + channel(2, 0x0031) + channel(2, 0x0001)
    body = struct.pack("<IHBBBBBB", 1000, 2441, 3, 12, 8, 0, 0, 0) + sats
    state = MonitorState()
    state.on_block(sp.SbfStream().feed(wrap(sp.BLOCK_CHANNEL_STATUS, body))[0][0])
    snap = state.snapshot()
    assert list(snap["tracked"]) == ["G02"]
    assert sorted(snap["positions"]) == ["G01", "G02"]


def test_cn0_series_slides_with_window():
    """表示幅を指定すると、最新を 0 とする範囲だけが残る。"""
    history = {"J02 L6": [(0.0, 40.0), (100.0, 41.0), (400.0, 42.0)],
               "J03 L6": [(50.0, 30.0)]}
    series = charts.cn0_series(history, 300.0)
    assert series["J02 L6"] == [(-5.0, 41.0), (0.0, 42.0)]
    assert series["J03 L6"] == []
    assert charts.cn0_series(history)["J02 L6"][0] == (0.0, 40.0)


def test_pvt_series_uses_mean_of_window():
    """位置の差は表示幅の中の平均を基準にする。"""
    def pvt(height):
        return sp.Pvt(0, 0, 1, 0, 8, 1.0, 2.0, 0.6, 2.4, height, 0.0)

    points = [(0.0, pvt(100.0)), (600.0, pvt(10.0)), (660.0, pvt(14.0))]
    series = charts.pvt_series(points, 300.0)
    assert series["offset"]["高さ"] == [(-1.0, -2.0), (0.0, 2.0)]
    assert [x for x, _y in series["nrsv"]["使用衛星数"]] == [-1.0, 0.0]


def test_default_folder_of_frozen_app_is_outside_install_dir(monkeypatch):
    """exe 版の保存先はカレントディレクトリに依存しない。"""
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.chdir(tempfile.gettempdir())
    folder = app.default_folder()
    assert folder.endswith("mosaic_gnss_recorder")
    assert not folder.startswith(os.getcwd())


def test_default_folder_from_python_is_logs():
    """Python 起動のときはカレントディレクトリの logs。"""
    assert app.default_folder() == os.path.join(os.getcwd(), "logs")


def test_time_unit_follows_record_length():
    """記録の長さで時間軸の単位が変わる。"""
    assert charts.time_unit(1.0)[0] == "秒"
    assert charts.time_unit(30.0)[0] == "分"
    assert charts.time_unit(150.0)[0] == "時間"


def test_nice_step_values():
    """目盛り間隔は 1・2・5 系列。整数軸は 1 未満にならない。"""
    assert charts.nice_step(60, 6) == 10
    assert charts.nice_step(150, 6) == 50
    assert charts.nice_step(2.5, 6) == 0.5
    assert charts.nice_step(3, 4, integer=True) == 1.0


def test_label_format_digits():
    """目盛り間隔に応じた桁数になる。"""
    assert charts.label_format(10) == "%.0f"
    assert charts.label_format(1.0) == "%.0f"
    assert charts.label_format(0.5) == "%.1f"
    assert charts.label_format(0.02) == "%.2f"


def test_used_ranks_lists_only_seen_modes():
    """軸に並べるのは現れた測位モードだけ。"""
    assert charts.used_ranks([(0, 4), (1, 1), (2, 4)]) == [1, 4]


def test_state_counts_failed_message():
    """RS 失敗が統計に反映される。"""
    state = MonitorState()
    for parity in (1, 1, 1, 0):
        state.on_block(sp.SbfStream().feed(l6_block(parity=parity))[0][0])
    row = state.snapshot()["l6"][("L6D", "J07")]
    assert row["total"] == 4 and row["good"] == 3 and row["rate"] == 75.0


def test_broken_block_does_not_stop_processing():
    """CRC が正しくても解析できないブロックは数えて読み飛ばす。"""
    state = MonitorState()
    block = sp.SbfStream().feed(wrap(sp.BLOCK_PVT_GEODETIC, bytes(8)))[0][0]
    assert state.on_block(block) is None
    assert state.snapshot()["decode_errors"] == 1


def test_binary_noise_is_not_shown_on_console():
    """文字列でないデータはコンソールに出さず、応答の文字列は出す。"""
    pipeline = Pipeline()
    pipeline.process(bytes(range(128, 200)))
    pipeline.process(b"$R: ok\r\nUSB1>")
    assert pipeline.console.get_nowait() == "$R: ok\r\nUSB1>"
    assert pipeline.console.empty()


def test_logger_skips_failed_message(tmp_path):
    """.l6 には RS 成功分だけを保存し、SBF には全ブロックを保存する。"""
    pipeline = Pipeline()
    path = pipeline.start_logging(str(tmp_path))
    pipeline.process(l6_block(parity=1) + l6_block(parity=0)
                     + l6_block(sp.BLOCK_L6E, parity=1))
    pipeline.stop_logging()
    assert os.path.getsize(path) == 3 * len(l6_block())
    sizes = sorted(p.stat().st_size for p in tmp_path.glob("*.l6"))
    assert sizes == [250, 250]
    assert pipeline.log_status() is None


def test_logger_does_not_overwrite(tmp_path, monkeypatch):
    """同じ名前の既存ファイルは上書きしない。"""
    monkeypatch.setattr("time.strftime", lambda _fmt: "fixed")
    SessionLogger(str(tmp_path)).close()
    with pytest.raises(FileExistsError):
        SessionLogger(str(tmp_path))


class FakePort:
    """コマンドごとに応答を返す疑似シリアル。"""

    def __init__(self):
        self.written = []
        self.replies = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def write(self, data):
        """送信を記録し応答を用意する。"""
        command = data.decode("ascii").strip()
        self.written.append(command)
        caps = "  ReceiverCapabilities, Main, QZSL6, x\r\n" if "Capab" in command else ""
        self.replies.append(f"$R: ok\r\n{caps}USB2>".encode())

    def read(self, _size):
        """用意した応答を 1 つ返す。"""
        return self.replies.pop(0) if self.replies else b""


def wait_written(fake, source, count):
    """count 個のコマンドが送られるまで待つ。"""
    for _ in range(300):
        if len(fake.written) >= count:
            return
        source.join(0.01)


def test_serial_source_sends_after_prompt():
    """プロンプトを待って順に送り、ポート名と L6 対応を読み取る。"""
    fake = FakePort()
    pipeline = Pipeline()
    source = SerialSource(pipeline, "COM9", opener=lambda *a, **k: fake)
    source.start()
    wait_written(fake, source, 2)
    source.join(0.1)
    source.send(l6_setup_commands(pipeline.prompt))
    wait_written(fake, source, 4)
    source.stop()
    assert fake.written[0] == "getReceiverCapabilities"
    assert fake.written[2] == "setSignalTracking,+QZSL6"
    assert "Stream10,USB2," in fake.written[3]
    assert pipeline.info["l6_capable"] is True
