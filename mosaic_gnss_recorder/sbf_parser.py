"""SBF のストリーム分離と、主要なブロックのデコード。

ブロック定義は mosaic-G5 Firmware v1.1.0 Reference Guide の 4 章に従う。
"""
import binascii
import math
import struct
from collections import namedtuple

BLOCK_MEAS_EPOCH = 4027
BLOCK_PVT_GEODETIC = 4007
BLOCK_CHANNEL_STATUS = 4013
BLOCK_L6D = 4270
BLOCK_L6E = 4271

HEADER_SIZE = 8
MAX_BLOCK_SIZE = 16384
L6_MESSAGE_SIZE = 250
DO_NOT_USE_FLOAT = -2e10

SIGNAL_NAMES = {
    0: "GPS L1CA", 1: "GPS L1P", 2: "GPS L2P", 3: "GPS L2C", 4: "GPS L5",
    5: "GPS L1C", 6: "QZS L1CA", 7: "QZS L2C", 8: "GLO L1CA", 9: "GLO L1P",
    10: "GLO L2P", 11: "GLO L2CA", 12: "GLO L3", 13: "BDS B1C", 14: "BDS B2a",
    15: "NavIC L5", 17: "GAL E1", 19: "GAL E6", 20: "GAL E5a", 21: "GAL E5b",
    22: "GAL E5", 23: "LBand", 24: "SBAS L1", 25: "SBAS L5", 26: "QZS L5",
    27: "QZS L6", 28: "BDS B1I", 29: "BDS B2I", 30: "BDS B3I", 32: "QZS L1C",
    33: "QZS L1S", 34: "BDS B2b", 37: "NavIC L1", 38: "QZS L1CB", 39: "QZS L5S",
}
# C/N0 の換算で +10 dB を加えない信号(GPS L1P / L2P)
SIGNALS_WITHOUT_CN0_OFFSET = (1, 2)

PVT_MODES = {
    0: "No PVT", 1: "Standalone", 2: "DGNSS", 3: "Fixed", 4: "RTK Fixed",
    5: "RTK Float", 6: "SBAS", 7: "MB RTK Fixed", 8: "MB RTK Float", 10: "PPP",
}

Block = namedtuple("Block", "number revision raw")
Pvt = namedtuple("Pvt", "tow wnc mode error nrsv haccuracy vaccuracy "
                 "latitude longitude height speed")
Measurement = namedtuple("Measurement", "svid signal antenna cn0")
MeasEpoch = namedtuple("MeasEpoch", "tow wnc scrambled measurements")
L6Message = namedtuple("L6Message", "tow wnc svid channel parity rs_count data")
SatPosition = namedtuple("SatPosition", "svid azimuth elevation tracking")


def sv_name(svid):
    """SBF の衛星番号を RINEX 形式の表記にする。未定義なら None。"""
    ranges = (
        (1, 37, "G", 0), (38, 61, "R", 37), (63, 68, "R", 38), (71, 106, "E", 70),
        (120, 140, "S", 100), (141, 180, "C", 140), (181, 190, "J", 180),
        (191, 197, "I", 190), (198, 215, "S", 157), (216, 222, "I", 208),
        (223, 245, "C", 182), (250, 251, "G", 212),
    )
    for low, high, prefix, offset in ranges:
        if low <= svid <= high:
            return f"{prefix}{svid - offset:02d}"
    return None


class SbfStream:  # pylint: disable=too-few-public-methods
    """受信バイト列を SBF ブロックと、それ以外の文字列(コマンド応答)に分ける。"""

    def __init__(self):
        self._buffer = bytearray()
        self.crc_errors = 0

    def feed(self, data):
        """data を追加し、(ブロックのリスト, 文字列) を返す。"""
        self._buffer.extend(data)
        blocks = []
        text = bytearray()
        pos = 0
        while pos is not None:
            pos = self._step(pos, blocks, text)
        return blocks, bytes(text).decode("ascii", errors="replace")

    def _step(self, pos, blocks, text):
        """pos から 1 ブロックぶん処理し、次の位置を返す。続きを待つときは None。"""
        buf = self._buffer
        sync = buf.find(b"$@", pos)
        if sync < 0:
            # 末尾の '$' は次の受信で同期語になる可能性があるので残す
            end = len(buf) - 1 if buf.endswith(b"$") else len(buf)
            text.extend(buf[pos:end])
            del buf[:end]
            return None
        text.extend(buf[pos:sync])
        pos = sync
        if len(buf) - pos < HEADER_SIZE:
            del buf[:pos]
            return None
        crc, block_id, length = struct.unpack_from("<HHH", buf, pos + 2)
        if length < HEADER_SIZE or length % 4 or length > MAX_BLOCK_SIZE:
            text.extend(buf[pos:pos + 2])
            return pos + 2
        if len(buf) - pos < length:
            del buf[:pos]
            return None
        raw = bytes(buf[pos:pos + length])
        if binascii.crc_hqx(raw[4:], 0) != crc:
            self.crc_errors += 1
            text.extend(raw[:2])
            return pos + 2
        blocks.append(Block(block_id & 0x1FFF, block_id >> 13, raw))
        return pos + length


def decode_pvt(body):
    """PVTGeodetic の本体(ヘッダ除く)をデコードする。"""
    tow, wnc, mode, error = struct.unpack_from("<IHBB", body, 0)
    nrsv = body[66]
    hacc, vacc = struct.unpack_from("<HH", body, 82)
    lat, lon, height = struct.unpack_from("<ddd", body, 8)
    v_north, v_east = struct.unpack_from("<ff", body, 36)
    valid = DO_NOT_USE_FLOAT not in (lat, lon)
    speed_valid = DO_NOT_USE_FLOAT not in (v_north, v_east)
    return Pvt(tow / 1000.0, wnc, mode & 0x0F, error, None if nrsv == 255 else nrsv,
               None if hacc == 65535 else hacc / 100.0,
               None if vacc == 65535 else vacc / 100.0,
               math.degrees(lat) if valid else None,
               math.degrees(lon) if valid else None,
               height if valid else None,
               math.hypot(v_north, v_east) if speed_valid else None)


def _signal_number(type_byte, obs_info):
    """Type と ObsInfo から信号番号を得る(31 は拡張番号)。"""
    low = type_byte & 0x1F
    return (obs_info >> 3) + 32 if low == 31 else low


def _cn0(raw, signal):
    """C/N0 の生値を dB-Hz にする。無効値は None。"""
    if raw == 255:
        return None
    return raw * 0.25 + (0 if signal in SIGNALS_WITHOUT_CN0_OFFSET else 10)


def decode_meas_epoch(body):
    """MeasEpoch の本体から衛星・信号ごとの C/N0 を取り出す。"""
    tow, wnc, count, sb1_len, sb2_len, flags = struct.unpack_from("<IHBBBB", body, 0)
    measurements = []
    pos = 12
    for _ in range(count):
        svid = body[pos + 2]
        signal = _signal_number(body[pos + 1], body[pos + 18])
        measurements.append(Measurement(svid, signal, body[pos + 1] >> 5,
                                        _cn0(body[pos + 15], signal)))
        nested = body[pos + 19]
        pos += sb1_len
        for _ in range(nested):
            signal = _signal_number(body[pos], body[pos + 5])
            measurements.append(Measurement(svid, signal, body[pos] >> 5,
                                            _cn0(body[pos + 2], signal)))
            pos += sb2_len
    return MeasEpoch(tow / 1000.0, wnc, bool(flags & 0x80), measurements)


def decode_l6(number, body):
    """QZSRawL6D / QZSRawL6E の本体をデコードする。"""
    tow, wnc, svid, parity, rs_count = struct.unpack_from("<IHBBB", body, 0)
    words = struct.unpack_from("<63I", body, 12)
    data = struct.pack(">63I", *words)[:L6_MESSAGE_SIZE]
    channel = "L6D" if number == BLOCK_L6D else "L6E"
    return L6Message(tow / 1000.0, wnc, svid, channel, bool(parity), rs_count, data)


def is_tracking(status):
    """TrackingStatus の 2 ビット欄のどれかが Tracking(3)なら真を返す。"""
    return any((status >> shift) & 3 == 3 for shift in range(0, 16, 2))


def decode_channel_status(body):
    """ChannelStatus の本体から衛星の方位角・仰角を取り出す。"""
    count, sb1_len, sb2_len = struct.unpack_from("<BBB", body, 6)
    positions = []
    pos = 12
    for _ in range(count):
        svid = body[pos] or struct.unpack_from("<H", body, pos + 2)[0]
        azimuth = struct.unpack_from("<H", body, pos + 4)[0] & 0x1FF
        elevation = struct.unpack_from("<b", body, pos + 8)[0]
        states = range(pos + sb1_len, pos + sb1_len + body[pos + 9] * sb2_len, sb2_len)
        tracking = any(is_tracking(struct.unpack_from("<H", body, state + 2)[0])
                       for state in states)
        positions.append(SatPosition(svid, None if azimuth == 511 else azimuth,
                                     None if elevation == -128 else elevation, tracking))
        pos += sb1_len + body[pos + 9] * sb2_len
    return positions
