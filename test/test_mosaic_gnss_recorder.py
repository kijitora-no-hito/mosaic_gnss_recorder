"""mosaic_gnss_recorder の解析・集計・保存・通信のテスト。"""
import binascii
import csv
import math
import os
import socket
import struct
import sys
import tempfile
import time
from xml.etree import ElementTree

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

# pylint: disable=wrong-import-position
from mosaic_gnss_recorder import (  # noqa: E402
    app, charts, engines, kml_export, sbf_parser as sp, settings)
from mosaic_gnss_recorder.logger import SessionLogger  # noqa: E402
from mosaic_gnss_recorder.serial_link import (  # noqa: E402
    Pipeline, SerialSource, l6_setup_commands)
from mosaic_gnss_recorder.state import MonitorState  # noqa: E402
from mosaic_gnss_recorder.tcp_server import SbfTcpServer  # noqa: E402

KML_NAMESPACE = "{http://www.opengis.net/kml/2.2}"


def wrap(number, body):
    """本体に SBF ヘッダを付ける。"""
    body += b"\0" * ((-(len(body) + 8)) % 4)
    head = struct.pack("<HH", number, len(body) + 8)
    return b"$@" + struct.pack("<H", binascii.crc_hqx(head + body, 0)) + head + body


def l6_block(number=sp.BLOCK_L6D, tow=1000, parity=1, fill=0x55, svid=187):
    """L6 ブロックを作る。"""
    payload = b"\x1a\xcf\xfc\x1d" + bytes([fill]) * 248
    words = struct.unpack(">63I", payload)
    body = struct.pack("<IHBBBBBB", tow, 2441, svid, parity, 2, 1, 0, 63)
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


def test_kml_splits_runs_and_shares_next_point():
    """モードの切れ目で次の点を含め、線がつながる。"""
    blocks = sp.SbfStream().feed(pvt_block() + pvt_block() + pvt_block(mode=1))[0]
    first, second, third = (sp.decode_pvt(b.raw[sp.HEADER_SIZE:]) for b in blocks)
    assert kml_export.split_runs([first, second, third]) == [(10, [first, second, third]),
                                                             (1, [third])]


def test_kml_time_removes_leap_seconds():
    """GPS 時刻を UTC に直す。"""
    pvt = sp.decode_pvt(sp.SbfStream().feed(pvt_block())[0][0].raw[sp.HEADER_SIZE:])
    assert kml_export.utc_time(pvt).isoformat() == "2026-10-24T09:01:52+00:00"


def test_kml_export_separates_modes_and_skips_no_position(tmp_path):
    """モード別に出力し、位置の無いエポックは除く。"""
    source = tmp_path / "a.sbf"
    source.write_bytes(pvt_block() + pvt_block() + pvt_block(mode=1)
                       + pvt_block(mode=0, lat=-2e10, lon=-2e10))
    output = tmp_path / "a.kml"
    assert kml_export.export([str(source)], str(output), "a&b") == 3
    root = ElementTree.parse(output).getroot()
    assert len(list(root.iter(f"{KML_NAMESPACE}Point"))) == 3
    assert len(list(root.iter(f"{KML_NAMESPACE}LineString"))) == 1
    names = [element.text for element in root.iter(f"{KML_NAMESPACE}name")]
    assert "a&b" in names and "PPP 点 (2)" in names and "Standalone 点 (1)" in names


def test_kml_export_without_position_is_error(tmp_path):
    """位置が無ければファイルを作らない。"""
    source = tmp_path / "a.sbf"
    source.write_bytes(pvt_block(mode=0, lat=-2e10, lon=-2e10))
    output = tmp_path / "a.kml"
    with pytest.raises(ValueError):
        kml_export.export([str(source)], str(output), "a")
    assert not output.exists()


def receive_exactly(client, size):
    """size バイト受け取るまで読む。"""
    data = b""
    while len(data) < size:
        chunk = client.recv(size - len(data))
        assert chunk
        data += chunk
    return data


def wait_clients(server, count):
    """接続数が count になるまで待つ。"""
    deadline = time.monotonic() + 3.0
    while server.client_count != count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.client_count == count


def test_tcp_server_forwards_only_valid_blocks():
    """CRC を通った SBF ブロックだけを配信し、文字列は送らない。"""
    server = SbfTcpServer(port=0)
    try:
        with socket.create_connection(("127.0.0.1", server.port)) as client:
            wait_clients(server, 1)
            pipeline = Pipeline()
            pipeline.server = server
            bad = bytearray(pvt_block())
            bad[-1] ^= 0xFF
            pipeline.process(b"$R: ok\r\nUSB1>" + bytes(bad) + pvt_block())
            assert receive_exactly(client, len(pvt_block())) == pvt_block()
            client.settimeout(0.2)
            with pytest.raises(socket.timeout):
                client.recv(1)
    finally:
        server.close()


def test_tcp_server_drops_closed_client():
    """切断したクライアントは送信時に外す。"""
    server = SbfTcpServer(port=0)
    try:
        client = socket.create_connection(("127.0.0.1", server.port))
        wait_clients(server, 1)
        client.close()
        deadline = time.monotonic() + 3.0
        while server.client_count and time.monotonic() < deadline:
            server.send(b"x" * 65536)
        assert server.client_count == 0
    finally:
        server.close()


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


def test_parse_reply_reads_settings():
    """応答の行から設定値を取り出す。仰角マスクは種類ごとに分ける。"""
    text = ("$R: gpm\r\n  PVTMode, Rover, StandAlone+RTK, auto\r\n"
            "$R: gem\r\n  ElevationMask, Tracking, 5\r\n  ElevationMask, PVT, 10\r\n"
            "  Other, 1\r\n")
    assert settings.parse_reply(text) == {
        "PVTMode": ["Rover", "StandAlone+RTK", "auto"],
        "ElevationMask:Tracking": ["5"], "ElevationMask:PVT": ["10"]}


def test_match_choice_ignores_order_and_expands_aliases():
    """同じ意味の値は同じ選択肢になる。無い値はそのまま返す。"""
    modes = settings.ROVER_MODES
    assert settings.match_choice("SBAS+StandAlone", modes) == "StandAlone+SBAS"
    assert settings.match_choice("StandAlone+SBAS+DGNSS+RTKFloat+RTKFixed", modes) \
        == "StandAlone+SBAS+DGNSS+RTK"
    assert settings.match_choice("StandAlone+SBAS+DGNSS+RTKFloat+RTKFixed+PPP", modes) == "all"
    assert settings.match_choice("DGNSS", modes) == "DGNSS"


def test_changed_commands_only_when_value_differs():
    """変えていない項目は送らない。仰角マスクは種類を付けて送る。"""
    pvt, tracking = settings.SETTINGS[0], settings.SETTINGS[1]
    assert not settings.changed_commands(pvt, ["Rover", "SBAS+StandAlone"],
                                         ["Rover", "StandAlone+SBAS"])
    assert settings.changed_commands(pvt, ["Rover", "StandAlone"],
                                     ["Rover", "StandAlone+PPP"]) \
        == ["setPVTMode,Rover,StandAlone+PPP"]
    assert settings.changed_commands(tracking, ["5"], ["10"]) \
        == ["setElevationMask,Tracking,10"]


def test_signal_commands_send_full_list():
    """信号は有効なものを全部並べて 1 回で送る。空にはしない。"""
    assert settings.signal_commands({"GPSL1CA"}, {"GPSL5", "GPSL1CA"}) \
        == ["setSignalTracking,GPSL1CA+GPSL5"]
    assert not settings.signal_commands({"GPSL1CA"}, {"GPSL1CA"})
    assert not settings.signal_commands({"GPSL1CA"}, set())


def test_enabled_systems_from_satellite_list():
    """衛星の並びから衛星系を判定する。"""
    assert settings.enabled_systems(["G01+G02+S120+J02"]) == {"GPS", "SBAS", "QZSS"}
    assert settings.enabled_systems(["all"]) == set(settings.SYSTEMS)


def test_system_commands_are_differences():
    """衛星系は変えたものだけ +/- で送る。"""
    assert settings.system_commands({"GPS", "SBAS"}, {"GPS", "QZSS"}) \
        == ["setSatelliteTracking,+QZSS", "setSatelliteTracking,-SBAS"]


def test_presets_use_known_items():
    """プリセットの値は画面の選択肢にあるものだけを使う。"""
    for preset in settings.PRESETS[1:]:
        assert preset.rover in settings.ROVER_MODES
        assert set(preset.systems) <= set(settings.SYSTEMS)
        assert set(preset.signals) <= set(settings.SIGNALS)


def test_usage_commands_only_when_e6_data_is_unused():
    """E6 の航法データが使われていないときだけ送る。"""
    assert settings.usage_commands(["GPSL1CA", "GALE1BC"]) == ["setSignalUsage,,+GALE6BC"]
    assert not settings.usage_commands(["GPSL1CA", "GALE1BC+GALE6BC"])
    assert not settings.usage_commands([])


def test_restarts_tracking():
    """追尾が止まるコマンドを見分ける。"""
    assert settings.restarts_tracking(["setSignalTracking,GPSL1CA"])
    assert not settings.restarts_tracking(["setElevationMask,PVT,10"])


def test_pipeline_keeps_settings_capability_and_error():
    """プロンプトまでの応答から設定、対応信号、エラーを読む。"""
    pipeline = Pipeline()
    pipeline.process(b"$R: grc\r\n  ReceiverCapabilities, Main, GPSL1CA+QZSL6, DSK1, RAIM, 50, 50"
                     b"\r\nUSB1> ")
    pipeline.process(b"$R: gem\r\n  ElevationMask, PVT, 10\r\nUSB1> ")
    pipeline.process(b"$R? setPVTMode: Invalid argument!\r\nUSB1> ")
    assert pipeline.info["signals"] == ("GPSL1CA", "QZSL6")
    assert pipeline.settings == {"ElevationMask:PVT": ["10"]}
    assert pipeline.info["error"] == "setPVTMode: Invalid argument!"


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


def test_parse_stat_reads_solution_and_no_solution():
    """malib_rt の状態行を読む。解が無ければ位置は None。"""
    line = "STAT 2026/11/02 15:58:40.0 6 7 35.123456789 139.123456789 50.1234 186 0"
    solution = engines.parse_stat(line)
    assert solution.quality == 6 and solution.nsat == 7
    assert solution.latitude == pytest.approx(35.123456789)
    empty = engines.parse_stat("STAT 2000/01/01 00:00:00.0 0 0 0 0 0 3 0")
    assert empty.latitude is None
    assert engines.parse_stat("START x") is None
    assert engines.quality_text(solution) == "PPP / 7 機"


def test_madoca_config_fills_placeholders(tmp_path):
    """設定のひな形のポート・出力・アンテナを置き換え、@ を残さない。"""
    engine = engines.MadocaEngine()
    conf, pos = engine.write_config(28785, str(tmp_path), "20261103_000000")
    with open(conf, encoding="utf-8") as fin:
        text = fin.read()
    assert "@" not in text
    assert "inpstr1-path       =localhost:28785" in text
    assert f"outstr1-path       ={pos}" in text
    assert "inpstr1-format     =sbf" in text


def test_madoca_engine_reads_status_lines(tmp_path):
    """エンジンの標準出力から最新の解と履歴を取り、止められる。"""
    fake = tmp_path / "fake_engine.py"
    fake.write_text("import sys, time\n"
                    "print('START', sys.argv[1], flush=True)\n"
                    "print('STAT 2026/11/02 15:58:40.0 6 7 35.0 139.0 50.0 1 0', flush=True)\n"
                    "time.sleep(30)\n", encoding="ascii")
    engine = engines.MadocaEngine(command=[sys.executable, str(fake)])
    engine.start(28785, str(tmp_path))
    deadline = time.monotonic() + 10.0
    while engine.snapshot()[0] is None and time.monotonic() < deadline:
        time.sleep(0.05)
    latest, history = engine.snapshot()
    assert latest.quality == 6 and len(history) == 1
    engine.stop()
    assert not engine.running


def test_set_options_replaces_value_and_keeps_comment():
    """設定の値だけを置き換え、コメントと他の行は残す。無い名前はエラー。"""
    text = "pos1-posmode       =ppp-kine   # mode\nmisc-floatcnt =15\nother =1 # x\n"
    result = engines.set_options(text, {"pos1-posmode": "ppp-static", "misc-floatcnt": "120"})
    lines = result.splitlines()
    assert lines[0].startswith("pos1-posmode       =ppp-static") and lines[0].endswith("# mode")
    assert lines[1].split("=")[1].strip() == "120"
    assert lines[2] == "other =1 # x"
    with pytest.raises(ValueError):
        engines.set_options(text, {"missing": "1"})


@pytest.mark.parametrize("motion", ["moving", "static"])
def test_engine_configs_follow_motion(tmp_path, motion):
    """移動体 / 静止で CLAS の floatcnt と MADOCA の測位モードが変わる。"""
    def value(path, name):
        with open(path, encoding="utf-8") as fin:
            for line in fin:
                key, _, rest = line.partition("=")
                if key.strip() == name:
                    return rest.split("#")[0].strip()
        return None
    clas_conf, _ = engines.ClasRtEngine().write_config(28785, str(tmp_path), "c", motion)
    madoca_conf, _ = engines.MadocaEngine().write_config(28785, str(tmp_path), "m", motion)
    expected = engines.MOTION_OPTIONS["CLAS"][motion]["misc-floatcnt"]
    assert value(clas_conf, "misc-floatcnt") == expected
    expected = "ppp-kine" if motion == "moving" else "ppp-static"
    assert value(madoca_conf, "pos1-posmode") == expected


def test_clas_rt_config_and_arguments(tmp_path):
    """CLAS リアルタイムの設定はデータのパスを埋め、ポートと出力は引数で渡す。"""
    engine = engines.ClasRtEngine()
    conf, pos = engine.write_config(28785, str(tmp_path), "20261103_000000")
    with open(conf, encoding="utf-8") as fin:
        text = fin.read()
    assert "@" not in text
    assert os.path.join(engine.root, "data", "clas_grid.def") in text
    assert pos.endswith("clas_20261103_000000.pos")
    assert engine.arguments(conf, pos, 28785) == ["-k", conf, "-p", "28785", "-o", pos]


def test_clas_rt_engine_passes_port_and_output(tmp_path):
    """起動時にポートと pos を渡し、状態行を読む。"""
    fake = tmp_path / "fake_clas.py"
    fake.write_text("import sys, time\n"
                    "print('START', ' '.join(sys.argv[1:]), flush=True)\n"
                    "print('STAT 2026/11/02 15:58:40.0 1 12 35.0 139.0 50.0 1 0 199', flush=True)\n"
                    "time.sleep(30)\n", encoding="ascii")
    engine = engines.ClasRtEngine(command=[sys.executable, str(fake)])
    pos = engine.start(28790, str(tmp_path))
    deadline = time.monotonic() + 10.0
    while engine.snapshot()[0] is None and time.monotonic() < deadline:
        time.sleep(0.05)
    latest, _ = engine.snapshot()
    assert engines.quality_text(latest) == "FIX / 12 機"
    assert f"-p 28790 -o {pos}" in engine.messages[0]
    engine.stop()
    assert not engine.running


def obs_header(text, label):
    """60 桁目にラベルを置いた RINEX ヘッダ行を作る。"""
    return f"{text:<60}{label}\n"


def test_rename_obs_codes_for_clas():
    """CLAS 用に観測コードを読み替え、継続行と他の衛星系・データ部は変えない。"""
    lines = [obs_header("E    5 C1C L1C C5Q L5Q C7Q", engines.OBS_TYPES),
             obs_header("       L7Q", engines.OBS_TYPES),
             obs_header("R    2 C1C L1C", engines.OBS_TYPES),
             obs_header("", engines.END_OF_HEADER),
             obs_header("G    2 C2L L2L", engines.OBS_TYPES)]
    result = list(engines.rename_obs_codes(lines))
    assert result[0][:60].split() == ["E", "5", "C1X", "L1X", "C5X", "L5X", "C7X"]
    assert result[1][:60].split() == ["L7X"]
    assert result[2] == lines[2] and result[4] == lines[4]
    assert all(len(line) == len(source) for line, source in zip(result, lines))


def test_extract_l6d_uses_most_received_satellite():
    """RS 成功の L6D だけを、最も多い衛星の分だけつなぐ。"""
    data = (l6_block(svid=187, fill=1) + l6_block(svid=187, fill=2)
            + l6_block(svid=187, parity=0) + l6_block(svid=193, fill=3)
            + l6_block(number=sp.BLOCK_L6E, svid=193))
    l6d = engines.extract_l6d(sp.SbfStream().feed(data)[0])
    assert len(l6d) == 2 * sp.L6_MESSAGE_SIZE
    assert l6d[4] == 1 and l6d[sp.L6_MESSAGE_SIZE + 4] == 2


def test_l6_continuity_drops_frames_after_gap_until_subframe_start():
    """欠け(間隔 1.5 s 超、RS 失敗)の後は、サブフレーム先頭(奇数の種別)まで捨てる。"""
    def message(tow, fill, parity=1):
        block = sp.SbfStream().feed(l6_block(tow=tow * 1000, fill=fill, parity=parity))[0][0]
        return sp.decode_l6(block.number, block.raw[sp.HEADER_SIZE:])
    gate = sp.L6Continuity()
    sequence = [(1000, 0x54, 1, False),  # 最初はサブフレーム先頭を待つ
                (1001, 0x55, 1, True), (1002, 0x54, 1, True),
                (1004, 0x54, 1, False),  # 1003 が欠けた
                (1005, 0x55, 1, True),
                (1006, 0x55, 0, False), (1007, 0x54, 1, False),  # RS 失敗も欠け
                (1008, 0x55, 1, True)]
    for tow, fill, parity, expected in sequence:
        assert gate.accept(message(tow, fill, parity)) is expected, tow
    assert gate.dropped == 3  # 1000, 1004, 1007(RS 失敗のフレームは数えない)


def test_extract_l6d_skips_frames_after_gap():
    """CLAS 後処理用の L6D も、欠けの後はサブフレーム先頭まで捨てる。"""
    data = b"".join(l6_block(tow=t * 1000, fill=f)
                    for t, f in ((1000, 0x55), (1001, 0x54), (1003, 0x54), (1004, 0x55)))
    l6d = engines.extract_l6d(sp.SbfStream().feed(data)[0])
    assert len(l6d) == 3 * sp.L6_MESSAGE_SIZE
    assert l6d[2 * sp.L6_MESSAGE_SIZE + 5] == 0x55


def test_clas_pos_to_kml_uses_clas_names(tmp_path):
    """CLAS の pos を読み、CLAS FIX などの名前で KML にする。"""
    pos = tmp_path / "a.pos"
    pos.write_text("% header\n"
                   "2442 556500.000 35.0 139.0 50.0 1 9 0.003 0.004 0.010 0 0 0 0.0 0.0\n"
                   "2442 556501.000 35.0 139.0 50.0 2 9 0.03 0.04 0.10 0 0 0 0.0 0.0\n",
                   encoding="ascii")
    rows = engines.parse_pos(str(pos))
    assert rows[0].haccuracy == pytest.approx(0.005)
    root = ElementTree.fromstring(kml_export.build_kml("a", rows, engines.CLAS_MODE_NAMES))
    names = [element.text for element in root.iter(f"{KML_NAMESPACE}name")]
    assert "CLAS FIX 点 (1)" in names and "CLAS Float 点 (1)" in names


def test_clas_job_reports_missing_sbf2rin(tmp_path, monkeypatch):
    """sbf2rin が無ければ理由を error に入れて終わる。"""
    monkeypatch.setattr(engines, "find_sbf2rin", lambda: None)
    source = tmp_path / "a.sbf"
    source.write_bytes(pvt_block())
    job = engines.ClasJob([str(source)], str(tmp_path / "a"))
    job.run()
    assert "sbf2rin" in job.error and job.result is None


def test_manual_html_has_tables_and_relative_images():
    """マニュアルの markdown を表と相対パスの画像を含む HTML にする。"""
    sys.path.insert(0, os.path.join(ROOT, "docs"))
    import build_manual_pdf  # pylint: disable=import-outside-toplevel,import-error
    html = build_manual_pdf.to_html("| a | b |\n|---|---|\n| 1 | 2 |\n\n![x](images/a.png)\n", "t")
    assert "<table>" in html and '<img alt="x" src="images/a.png"' in html
    assert "<title>t</title>" in html
