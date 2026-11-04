"""外部の測位エンジン(MALIB の MADOCA-PPP、CLASLIB の CLAS)を動かす。

エンジンと補助データは engines/ に置く(engines/build_engines.ps1 で作る)。
リアルタイム(MADOCA: malib_rt、CLAS: clas_rt)はこのアプリの TCP 配信を入力にして、
解を記録フォルダの .pos に書く。
CLAS 後処理は記録済みの SBF を RINEX と .l6 に変換してから CLASLIB の rnx2rtkp で処理する。
"""
import datetime
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter, deque, namedtuple

from . import kml_export
from . import sbf_parser as sp

EngineSolution = namedtuple("EngineSolution", "time quality nsat latitude longitude height")
PosRow = namedtuple("PosRow", "tow wnc mode nrsv haccuracy latitude longitude height")

# RTKLIB の解の品質 Q
QUALITY_NAMES = {0: "解なし", 1: "FIX", 2: "Float", 5: "単独", 6: "PPP"}
CLAS_MODE_NAMES = {1: "CLAS FIX", 2: "CLAS Float", 5: "Standalone"}
HISTORY_SECONDS = 86400
STOP_TIMEOUT = 5.0
SBF2RIN_CANDIDATES = (r"C:\Program Files\Septentrio\RxTools\bin\sbf2rin.exe",
                      r"C:\Program Files (x86)\Septentrio\RxTools\bin\sbf2rin.exe")
# sbf2rin のコード名を CLAS のバイアスが対象とするコードへ読み替える(違うと衛星が捨てられる)
CLAS_CODE_RENAME = {"G": {"2L": "2X", "5Q": "5X"},
                    "E": {"1C": "1X", "5Q": "5X", "7Q": "7X"},
                    "J": {"2L": "2X", "5Q": "5X"}}
OBS_TYPES = "SYS / # / OBS TYPES"
END_OF_HEADER = "END OF HEADER"
GPS_EPOCH = datetime.datetime(1980, 1, 6)
WEEK_SECONDS = 604800
# 移動体 / 静止で変える設定(ひな形の値を上書きする)。2026-11-03 の走行と 10/31 の静止で決めた。
# CLAS 移動体: 加速度ノイズを車の加減速に合わせ、車載アンテナの低い C/N0 でも衛星を捨てない。
#   misc-floatcnt(Float がこの回数続いたらフィルタをやり直す)は大きい方が走行での FIX 精度が良い。
# CLAS 静止: CLASLIB のサンプル(静止観測向け)の値。後処理では最初から正しく FIX する。
# MADOCA 静止: 位置を 1 点として推定する ppp-static の方が収束が速い。
MOTIONS = {"moving": "移動体", "static": "静止"}
MOTION_OPTIONS = {
    "CLAS": {
        "moving": {"misc-floatcnt": "120", "pos1-snrmask_r": "off",
                   "stats-prnaccelh": "3.0", "stats-prnaccelv": "1.0"},
        "static": {"misc-floatcnt": "15", "pos1-snrmask_r": "on",
                   "stats-prnaccelh": "0.2", "stats-prnaccelv": "0.1"},
    },
    "MADOCA": {"moving": {"pos1-posmode": "ppp-kine"}, "static": {"pos1-posmode": "ppp-static"}},
}


def engine_root():
    """engines フォルダのパス。exe 版は同梱先、開発時はリポジトリ直下。"""
    if getattr(sys, "frozen", False):
        return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)), "engines")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engines")


def render(template, values):
    """設定のひな形の @KEY@ を置き換える。"""
    for key, value in values.items():
        template = template.replace(f"@{key}@", str(value))
    return template


def set_options(text, values):
    """RTKLIB 形式の設定文字列の「名前 = 値」を values で置き換える。行末のコメントは残す。"""
    lines = text.splitlines(keepends=True)
    for name, value in values.items():
        for i, line in enumerate(lines):
            key, sep, rest = line.partition("=")
            if sep and key.strip() == name:
                comment = rest[rest.index("#"):] if "#" in rest else "\n"
                lines[i] = f"{key}={value:<20} {comment}"
                break
        else:
            raise ValueError(f"設定に {name} がありません")
    return "".join(lines)


def parse_stat(line):
    """malib_rt の状態行を EngineSolution にする。状態行でなければ None。"""
    cols = line.split()
    if len(cols) < 9 or cols[0] != "STAT":
        return None
    try:
        quality, nsat = int(cols[3]), int(cols[4])
        lat, lon, height = float(cols[5]), float(cols[6]), float(cols[7])
    except ValueError:
        return None
    if quality == 0:
        lat = lon = height = None
    return EngineSolution(f"{cols[1]} {cols[2]}", quality, nsat, lat, lon, height)


def quality_text(solution):
    """解の状態の表示文字列。"""
    if solution is None:
        return "待機中"
    name = QUALITY_NAMES.get(solution.quality, f"Q={solution.quality}")
    return f"{name} / {solution.nsat} 機"


def _creation_flags():
    """コンソール窓を出さず、Ctrl+Break を送れるようにする。"""
    if os.name != "nt":
        return 0
    return subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW


class RealtimeEngine:  # pylint: disable=too-many-instance-attributes
    """リアルタイム測位エンジンを起動し、標準出力の状態行を読む。

    サブクラスで EXE、write_config、arguments を決める。
    """

    EXE = ""
    NAME = ""

    def __init__(self, root=None, command=None):
        self.root = root or engine_root()
        self._command = command
        self.process = None
        self.pos_path = None
        self.latest = None
        self.history = deque()
        self.messages = deque(maxlen=50)
        self._lock = threading.Lock()
        self._thread = None

    def write_config(self, port, folder, stamp, motion="moving"):
        """設定ファイルを書き、(設定, pos) のパスを返す。motion は MOTIONS のキー。"""
        raise NotImplementedError

    def arguments(self, conf, pos, port):
        """エンジンに渡す引数。"""
        raise NotImplementedError

    def start(self, port, folder, motion="moving"):
        """TCP 配信のポートを入力にして起動する。pos のパスを返す。motion は MOTIONS のキー。"""
        if self.running:
            return self.pos_path
        os.makedirs(folder, exist_ok=True)
        conf, self.pos_path = self.write_config(port, folder,
                                                time.strftime("%Y%m%d_%H%M%S"), motion)
        command = self._command or [os.path.join(self.root, "bin", self.EXE)]
        self.latest = None
        self.history.clear()
        self.process = subprocess.Popen(  # pylint: disable=consider-using-with
            command + self.arguments(conf, self.pos_path, port),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="ascii", errors="replace", creationflags=_creation_flags())
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()
        return self.pos_path

    def _read(self):
        """状態行を読み続ける。"""
        for line in self.process.stdout:
            solution = parse_stat(line)
            if solution is None:
                self.messages.append(line.rstrip())
                continue
            now = time.monotonic()
            with self._lock:
                self.latest = solution
                if solution.latitude is not None:
                    self.history.append((now, solution))
                while self.history and now - self.history[0][0] > HISTORY_SECONDS:
                    self.history.popleft()

    def snapshot(self):
        """(最新の解, 履歴のコピー) を返す。"""
        with self._lock:
            return self.latest, list(self.history)

    @property
    def running(self):
        """エンジンが動いているか。"""
        return self.process is not None and self.process.poll() is None

    def stop(self):
        """Ctrl+Break で止め、応答が無ければ強制終了する。"""
        if not self.running:
            return
        try:
            self.process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
            self.process.wait(timeout=STOP_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait(timeout=STOP_TIMEOUT)
        if self._thread is not None:
            self._thread.join(timeout=STOP_TIMEOUT)


class MadocaEngine(RealtimeEngine):
    """MALIB の MADOCA-PPP(malib_rt)。TCP 配信の SBF の L6E を使う。"""

    EXE = "malib_rt.exe"
    NAME = "MADOCA"

    def write_config(self, port, folder, stamp, motion="moving"):
        """設定ファイルを書き、(設定, pos) のパスを返す。"""
        with open(os.path.join(self.root, "templates", "madoca_rt.conf"),
                  encoding="utf-8") as fin:
            template = fin.read()
        base = os.path.join(os.path.abspath(folder), f"madoca_{stamp}")
        conf, pos = base + ".conf", base + ".pos"
        text = render(template, {"PORT": port, "POS": pos,
                                 "ATX": os.path.join(self.root, "data", "igs20_2423.atx")})
        text = set_options(text, MOTION_OPTIONS[self.NAME][motion])
        with open(conf, "x", encoding="utf-8") as fout:
            fout.write(text)
        return conf, pos

    def arguments(self, conf, pos, port):
        """malib_rt は設定ファイルだけを受け取る(ポートと pos は設定に書く)。"""
        return [conf]


class ClasRtEngine(RealtimeEngine):
    """CLASLIB の CLAS(clas_rt)。TCP 配信の SBF の L6D を使う。"""

    EXE = "clas_rt.exe"
    NAME = "CLAS"

    def write_config(self, port, folder, stamp, motion="moving"):
        """設定ファイルを書き、(設定, pos) のパスを返す。"""
        base = os.path.join(os.path.abspath(folder), f"clas_{stamp}")
        conf, pos = base + ".conf", base + ".pos"
        with open(conf, "x", encoding="utf-8") as fout:
            fout.write(clas_config(self.root, motion))
        return conf, pos

    def arguments(self, conf, pos, port):
        """設定、TCP 配信のポート、出力先を渡す。"""
        return ["-k", conf, "-p", str(port), "-o", pos]


def find_sbf2rin():
    """RxTools の sbf2rin.exe を探す。無ければ None。"""
    for path in SBF2RIN_CANDIDATES:
        if os.path.exists(path):
            return path
    return shutil.which("sbf2rin")


def rename_obs_codes(lines):
    """RINEX 3 観測ファイルのヘッダの観測コードを CLAS 用に読み替えた行を返す。"""
    system = None
    in_header = True
    for line in lines:
        if in_header and line[60:].startswith(OBS_TYPES):
            if line[0] != " ":
                system = line[0]
            table = CLAS_CODE_RENAME.get(system, {})
            codes = [code[0] + table.get(code[1:], code[1:]) for code in line[7:60].split()]
            body = "".join(f" {code}" for code in codes)
            line = f"{line[:6]}{body:<54}{line[60:]}"
        elif in_header and line[60:].startswith(END_OF_HEADER):
            in_header = False
        yield line


def extract_l6d(blocks):
    """L6D を、RS 復号に最も多く成功した衛星の分だけ時刻順の bytes で返す。

    欠けの後は次のサブフレーム先頭まで捨てる(sbf_parser.L6Continuity)。
    """
    messages = [sp.decode_l6(b.number, b.raw[sp.HEADER_SIZE:]) for b in blocks
                if b.number == sp.BLOCK_L6D]
    messages = [m for m in messages if m is not None]
    if not any(m.parity for m in messages):
        return b""
    svid = Counter(m.svid for m in messages if m.parity).most_common(1)[0][0]
    gate = sp.L6Continuity()
    return b"".join(m.data for m in messages if m.svid == svid and gate.accept(m))


def parse_pos(path):
    """RTKLIB の pos(GPST の週秒または日時)を PosRow のリストにする。"""
    rows = []
    with open(path, encoding="ascii", errors="replace") as fin:
        for line in fin:
            if line.startswith("%") or not line.strip():
                continue
            cols = line.split()
            if "/" in cols[0]:
                when = datetime.datetime.strptime(f"{cols[0]} {cols[1]}",
                                                  "%Y/%m/%d %H:%M:%S.%f")
                seconds = (when - GPS_EPOCH).total_seconds()
                wnc, tow = int(seconds // WEEK_SECONDS), seconds % WEEK_SECONDS
            else:
                wnc, tow = int(cols[0]), float(cols[1])
            lat, lon, height = (float(c) for c in cols[2:5])
            sdn, sde = float(cols[7]), float(cols[8])
            rows.append(PosRow(tow, wnc, int(cols[5]), int(cols[6]),
                               (sdn * sdn + sde * sde) ** 0.5, lat, lon, height))
    return rows


def clas_config(root, motion="moving"):
    """CLAS の設定ファイルの中身(ひな形にデータのパスと移動体/静止の値を入れたもの)。"""
    with open(os.path.join(root, "templates", "clas_kinematic.conf"), encoding="utf-8") as fin:
        text = render(fin.read(), {"DATA": os.path.join(root, "data")})
    return set_options(text, MOTION_OPTIONS["CLAS"][motion])


class ClasJob(threading.Thread):  # pylint: disable=too-many-instance-attributes
    """記録済み SBF を CLAS で後処理する。終わると result か error が入る。"""

    # pylint: disable-next=too-many-arguments
    def __init__(self, sbf_paths, output_base, root=None, sbf2rin=None, *, motion="moving"):
        super().__init__(daemon=True)
        self.sbf_paths = list(sbf_paths)
        self.output_base = output_base
        self.motion = motion
        self.root = root or engine_root()
        self.sbf2rin = sbf2rin or find_sbf2rin()
        self.step = "準備中"
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self._process()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            self.error = str(exc)

    def _convert(self, work):
        """SBF をつないで RINEX(観測・航法)と L6D に変換し、そのパスを返す。"""
        if self.sbf2rin is None:
            raise RuntimeError("sbf2rin.exe が見つかりません。RxTools をインストールしてください。")
        joined = os.path.join(work, "input.sbf")
        with open(joined, "wb") as fout:
            for path in self.sbf_paths:
                with open(path, "rb") as fin:
                    shutil.copyfileobj(fin, fout)
        self.step = "RINEX に変換中"
        obs, nav = os.path.join(work, "input.obs"), os.path.join(work, "input.nav")
        for args in (["-o", obs, "-s", "-D", "-R304"], ["-o", nav, "-nP", "-R304"]):
            subprocess.run([self.sbf2rin, "-f", joined] + args, check=True,
                           capture_output=True, creationflags=_creation_flags())
        clas_obs = os.path.join(work, "input_clas.obs")
        with open(obs, encoding="ascii", errors="replace") as fin, \
                open(clas_obs, "w", encoding="ascii", newline="\n") as fout:
            fout.writelines(rename_obs_codes(fin))
        self.step = "L6D を取り出し中"
        with open(joined, "rb") as fin:
            l6d = extract_l6d(sp.SbfStream().feed(fin.read())[0])
        if not l6d:
            raise RuntimeError("L6D(CLAS)が記録されていません。受信設定で QZSL6 を追尾してください。")
        l6_path = os.path.join(work, "input.l6")
        with open(l6_path, "wb") as fout:
            fout.write(l6d)
        return clas_obs, nav, l6_path

    def _process(self):
        """変換して CLAS で解き、(pos, kml, 件数の辞書) を返す。"""
        work = self.output_base + "_clas_work"
        os.makedirs(work, exist_ok=True)
        obs, nav, l6_path = self._convert(work)
        conf = os.path.join(work, "clas.conf")
        with open(conf, "w", encoding="utf-8") as fout:
            fout.write(clas_config(self.root, self.motion))
        self.step = "CLAS で測位中"
        pos = os.path.abspath(self.output_base + "_clas.pos")
        # CLASLIB の Windows 版は / 区切りのパスを読めないので、絶対パスを \ で渡す
        bin_dir = os.path.join(self.root, "bin")
        subprocess.run([os.path.join(bin_dir, "clas_rnx2rtkp.exe"), "-ti", "1", "-k",
                        os.path.abspath(conf), os.path.abspath(obs), os.path.abspath(nav),
                        os.path.abspath(l6_path), "-o", pos],
                       check=True, cwd=bin_dir, capture_output=True,
                       creationflags=_creation_flags())
        if not os.path.exists(pos):
            raise RuntimeError("CLAS の結果が作られませんでした。")
        rows = [r for r in parse_pos(pos) if r.latitude is not None]
        if not rows:
            raise RuntimeError("CLAS の解がありません。")
        kml = self.output_base + "_clas.kml"
        with open(kml, "w", encoding="utf-8") as fout:
            fout.write(kml_export.build_kml(os.path.basename(self.output_base) + " CLAS", rows,
                                            CLAS_MODE_NAMES))
        counts = Counter(CLAS_MODE_NAMES.get(r.mode, f"Q={r.mode}") for r in rows)
        self.step = "完了"
        return pos, kml, dict(counts)
