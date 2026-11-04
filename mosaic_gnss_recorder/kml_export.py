"""記録済みの SBF を、測位モードで色分けした KML に変換する。

モードごとのフォルダに分けるので、地図ソフト側で表示を切り替えられる。
"""
import datetime
from xml.sax.saxutils import escape

from . import sbf_parser as sp

GPS_EPOCH = datetime.datetime(1980, 1, 6, tzinfo=datetime.timezone.utc)
LEAP_SECONDS = 18
JST = datetime.timezone(datetime.timedelta(hours=9))
ICON = "http://maps.google.com/mapfiles/kml/shapes/shaded_dot.png"
LINE_WIDTH = 3
ICON_SCALE = 0.4
# KML の色は aabbggrr
MODE_KML_COLORS = {1: "ff2020e0", 2: "ffc040c0", 4: "ff40c040", 5: "ffc0c000",
                   6: "ff00c8ff", 10: "ffe07000"}
OTHER_COLOR = "ff808080"


def mode_name(mode, names=None):
    """測位モードの表示名を返す。names で受信機以外の解(CLAS など)の名前を与えられる。"""
    if names and mode in names:
        return names[mode]
    return sp.PVT_MODES.get(mode, f"Mode {mode}")


def utc_time(pvt):
    """GPS 週と TOW から UTC 時刻を返す。"""
    return GPS_EPOCH + datetime.timedelta(weeks=pvt.wnc, seconds=pvt.tow - LEAP_SECONDS)


def coordinates(pvt):
    """KML の座標文字列を返す。"""
    return f"{pvt.longitude:.8f},{pvt.latitude:.8f},{pvt.height:.2f}"


def split_runs(pvts):
    """同じモードの連続を (モード, 点列) に分ける。線がつながるよう次の点も含める。"""
    runs = []
    for pvt in pvts:
        if runs and runs[-1][0] == pvt.mode:
            runs[-1][1].append(pvt)
            continue
        if runs:
            runs[-1][1].append(pvt)
        runs.append((pvt.mode, [pvt]))
    return runs


def style_xml(mode):
    """モードの線と点のスタイルを返す。"""
    color = MODE_KML_COLORS.get(mode, OTHER_COLOR)
    return (f'<Style id="mode{mode}"><IconStyle><color>{color}</color>'
            f"<scale>{ICON_SCALE}</scale><Icon><href>{ICON}</href></Icon></IconStyle>"
            f"<LabelStyle><scale>0</scale></LabelStyle>"
            f"<LineStyle><color>{color}</color><width>{LINE_WIDTH}</width></LineStyle></Style>")


def point_xml(pvt, names=None):
    """1 エポックの目印を返す。"""
    when = utc_time(pvt)
    hacc = "-" if pvt.haccuracy is None else f"{pvt.haccuracy:.2f} m"
    text = (f"{when.astimezone(JST):%H:%M:%S} JST / {mode_name(pvt.mode, names)} / "
            f"{pvt.nrsv} 機 / HAcc {hacc}")
    return (f"<Placemark><description>{escape(text)}</description>"
            f"<TimeStamp><when>{when:%Y-%m-%dT%H:%M:%SZ}</when></TimeStamp>"
            f"<styleUrl>#mode{pvt.mode}</styleUrl>"
            f"<Point><coordinates>{coordinates(pvt)}</coordinates></Point></Placemark>")


def line_xml(mode, points):
    """同じモードの連続区間の線を返す。"""
    text = " ".join(coordinates(p) for p in points)
    return (f"<Placemark><styleUrl>#mode{mode}</styleUrl><LineString><tessellate>1</tessellate>"
            f"<coordinates>{text}</coordinates></LineString></Placemark>")


def folder_xml(name, items):
    """フォルダを返す。"""
    return f"<Folder><name>{escape(name)}</name>{''.join(items)}</Folder>"


def build_kml(title, pvts, names=None):
    """測位結果から KML の全文を返す。"""
    modes = sorted({p.mode for p in pvts}, key=lambda m: mode_name(m, names))
    runs = [run for run in split_runs(pvts) if len(run[1]) > 1]
    lines = [folder_xml(f"{mode_name(m, names)} 線",
                        [line_xml(m, points) for mode, points in runs if mode == m])
             for m in modes]
    points = []
    for mode in modes:
        items = [point_xml(p, names) for p in pvts if p.mode == mode]
        points.append(folder_xml(f"{mode_name(mode, names)} 点 ({len(items)})", items))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            f"<name>{escape(title)}</name>{''.join(style_xml(m) for m in modes)}"
            f"{folder_xml('測位モード別の線', lines)}{folder_xml('測位モード別の点', points)}"
            "</Document></kml>\n")


def load_pvts(paths):
    """SBF から、位置が入っている測位結果を時刻順に読む。"""
    pvts = []
    for path in paths:
        with open(path, "rb") as fin:
            blocks = sp.SbfStream().feed(fin.read())[0]
        decoded = (sp.decode_pvt(b.raw[sp.HEADER_SIZE:]) for b in blocks
                   if b.number == sp.BLOCK_PVT_GEODETIC)
        pvts.extend(p for p in decoded if p.latitude is not None)
    return sorted(pvts, key=lambda p: (p.wnc, p.tow))


def export(paths, output, title):
    """SBF を KML に変換して、書き出した点数を返す。位置が無ければ ValueError。"""
    pvts = load_pvts(paths)
    if not pvts:
        raise ValueError("位置の入った PVTGeodetic がありません。")
    with open(output, "w", encoding="utf-8") as fout:
        fout.write(build_kml(title, pvts))
    return len(pvts)
