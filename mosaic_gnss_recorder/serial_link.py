"""受信データの流れ(実機の COM ポート / 記録ファイルの再生)を担当する。"""
import queue
import re
import threading
import time
from collections import deque

import serial
from serial.tools import list_ports

from .logger import SessionLogger
from .sbf_parser import L6Message, Pvt, SbfStream
from .state import MonitorState

BAUDRATE = 115200
COMMAND_TIMEOUT = 3.0
PROMPT_RE = re.compile(r"\b(COM\d|USB\d|IP\d\d|NTR\d|IPS\d|IPR\d)>")
PRODUCT_RE = re.compile(r'hwplatform product="([^"]+)"')
FIRMWARE_RE = re.compile(r'<firmware version="([^"]+)"')
TEXT_TAIL = 65536
NON_TEXT_RE = re.compile(r"[^ -~\t\r\n]")
MAX_NON_TEXT_RATIO = 0.1
L6_STREAM = "Stream10"
BASE_MESSAGES = "Rinex+PVTGeod+Status+GALRawCNAV"
L6_MESSAGES = "QZSRawL6D+QZSRawL6E"
PROBE_COMMANDS = ("getReceiverCapabilities", "lif,Identification")


def available_ports():
    """使える COM ポートを (デバイス名, 説明) のリストで返す。"""
    return [(p.device, p.description) for p in sorted(list_ports.comports())]


def l6_setup_commands(port_name, l6_capable=True):
    """記録用の設定コマンド。Current のみ変更し Boot には保存しない。"""
    if not l6_capable:
        return [f"setSBFOutput,{L6_STREAM},{port_name},{BASE_MESSAGES},sec1"]
    return ["setSignalTracking,+QZSL6",
            f"setSBFOutput,{L6_STREAM},{port_name},{BASE_MESSAGES}+{L6_MESSAGES},sec1"]


def l6_stop_commands():
    """記録用の SBF 出力を止めるコマンド。"""
    return [f"setSBFOutput,{L6_STREAM},none,none,off"]


class Pipeline:  # pylint: disable=too-many-instance-attributes
    """受信バイト列を解析し、状態・ログ・コンソールへ振り分ける。"""

    def __init__(self):
        self.state = MonitorState()
        self.stream = SbfStream()
        self.console = queue.Queue()
        self.prompt = None
        self.info = {}
        self._logger = None
        self._log_lock = threading.Lock()
        self._text = ""
        self._prompt_seen = threading.Event()

    def process(self, data):
        """受信データを処理する。"""
        blocks, text = self.stream.feed(data)
        for block in blocks:
            decoded = self.state.on_block(block)
            with self._log_lock:
                if self._logger is not None:
                    self._logger.write_block(block)
                    if isinstance(decoded, L6Message):
                        self._logger.write_l6(decoded)
                    elif isinstance(decoded, Pvt):
                        self._logger.write_pvt(decoded)
        # 壊れたブロックの断片などバイナリはコンソールに出さない
        if text and len(NON_TEXT_RE.findall(text)) <= len(text) * MAX_NON_TEXT_RATIO:
            self._on_text(NON_TEXT_RE.sub("", text))

    def _on_text(self, text):
        """コマンド応答を処理する。プロンプトを見たら次を送れるようにする。"""
        self.console.put(text)
        self._text = (self._text + text)[-TEXT_TAIL:]
        matches = list(PROMPT_RE.finditer(self._text))
        if matches:
            self.prompt = matches[-1].group(1)
            self._update_info(self._text)
            self._text = self._text[matches[-1].end():]
            self._prompt_seen.set()

    def _update_info(self, text):
        """応答から機種・ファームウェア・L6 対応を読み取る。"""
        product = PRODUCT_RE.search(text)
        firmware = FIRMWARE_RE.search(text)
        if product:
            self.info["product"] = product.group(1)
        if firmware:
            self.info["firmware"] = firmware.group(1)
        if "ReceiverCapabilities," in text:
            self.info["l6_capable"] = "QZSL6" in text

    def take_prompt(self):
        """プロンプトが来ていれば True を返して印を消す。"""
        seen = self._prompt_seen.is_set()
        self._prompt_seen.clear()
        return seen

    def start_logging(self, folder):
        """記録を始め、SBF のパスを返す。"""
        with self._log_lock:
            if self._logger is None:
                self._logger = SessionLogger(folder)
            return self._logger.sbf_path

    def stop_logging(self):
        """記録を止める。"""
        with self._log_lock:
            if self._logger is not None:
                self._logger.close()
                self._logger = None

    def log_status(self):
        """記録中なら (ファイル名, SBF バイト数, L6 メッセージ数) を返す。"""
        with self._log_lock:
            if self._logger is None:
                return None
            return (self._logger.sbf_path, self._logger.sbf_bytes,
                    self._logger.l6_messages)


class SourceThread(threading.Thread):
    """データ源スレッドの共通部分。"""

    def __init__(self, pipeline):
        super().__init__(daemon=True)
        self.pipeline = pipeline
        self.error = None
        self._stop_event = threading.Event()

    def stop(self):
        """停止を要求して終了を待つ。"""
        self._stop_event.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=3.0)


class SerialSource(SourceThread):
    """COM ポートの受信とコマンド送信を行う。"""

    def __init__(self, pipeline, port, opener=serial.Serial):
        super().__init__(pipeline)
        self.port = port
        self._opener = opener
        self._commands = deque(PROBE_COMMANDS)
        self._lock = threading.Lock()
        self._waiting_until = 0.0

    def send(self, commands):
        """コマンドを送信待ちに追加する。プロンプトを待って 1 つずつ送る。"""
        with self._lock:
            self._commands.extend(commands)

    def _send_next(self, link):
        """前のコマンドが完了していれば次を送る。"""
        now = time.monotonic()
        if self._waiting_until and not self.pipeline.take_prompt():
            if now < self._waiting_until:
                return
            self.pipeline.console.put("\n[応答なし: タイムアウト]\n")
        self._waiting_until = 0.0
        with self._lock:
            if not self._commands:
                return
            command = self._commands.popleft()
        self.pipeline.console.put(f"\n> {command}\n")
        link.write((command + "\r\n").encode("ascii"))
        self._waiting_until = now + COMMAND_TIMEOUT

    def run(self):
        """受信ループ。切断などの例外は error に残して終わる。"""
        try:
            with self._opener(self.port, BAUDRATE, timeout=0.05) as link:
                while not self._stop_event.is_set():
                    self._send_next(link)
                    data = link.read(4096)
                    if data:
                        self.pipeline.process(data)
        except (serial.SerialException, OSError) as exc:
            self.error = str(exc)


class ReplaySource(SourceThread):
    """記録済み SBF を実時間の speed 倍で流す。speed=0 は最大速度。"""

    CHUNK = 1024

    def __init__(self, pipeline, path, speed=1.0):
        super().__init__(pipeline)
        self.path = path
        self.speed = speed
        self.finished = False

    def run(self):
        """ファイルを読み切るか停止要求で終わる。"""
        last_time = None
        try:
            with open(self.path, "rb") as fin:
                while not self._stop_event.is_set():
                    data = fin.read(self.CHUNK)
                    if not data:
                        self.finished = True
                        return
                    self.pipeline.process(data)
                    current = self.pipeline.state.snapshot_time()
                    if self.speed and current and last_time:
                        self._stop_event.wait(min((current - last_time) / self.speed, 1.0))
                    last_time = current
        except OSError as exc:
            self.error = str(exc)
