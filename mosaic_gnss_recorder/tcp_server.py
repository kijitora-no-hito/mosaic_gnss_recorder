"""受信した SBF ブロックを TCP で外部の測位ソフトへ配信する。

待ち受けは既定で 127.0.0.1(この PC 内)だけ。送れずに溜まったクライアントは切り離す。
"""
import socket
import threading

DEFAULT_PORT = 28785
LOCAL_HOST = "127.0.0.1"
ACCEPT_TIMEOUT = 0.2
MAX_PENDING_BYTES = 1 << 20


class SbfTcpServer:
    """接続中の全クライアントに同じバイト列を送る。"""

    def __init__(self, port=DEFAULT_PORT, host=LOCAL_HOST):
        self._listener = socket.create_server((host, port))
        self._listener.settimeout(ACCEPT_TIMEOUT)
        self.port = self._listener.getsockname()[1]
        self._clients = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        """接続を受け付ける。"""
        while not self._stop_event.is_set():
            try:
                client, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            client.setblocking(False)
            with self._lock:
                self._clients[client] = bytearray()

    def send(self, data):
        """全クライアントへ送る。送り切れない分は次回に回す。"""
        with self._lock:
            for client, pending in list(self._clients.items()):
                pending.extend(data)
                try:
                    sent = client.send(pending)
                except BlockingIOError:
                    sent = 0
                except OSError:
                    self._drop(client)
                    continue
                del pending[:sent]
                if len(pending) > MAX_PENDING_BYTES:
                    self._drop(client)

    def _drop(self, client):
        """クライアントを切り離す。ロックを持った状態で呼ぶ。"""
        self._clients.pop(client, None)
        client.close()

    @property
    def client_count(self):
        """接続中のクライアント数。"""
        with self._lock:
            return len(self._clients)

    def close(self):
        """待ち受けと全接続を閉じる。"""
        self._stop_event.set()
        self._listener.close()
        self._thread.join(timeout=1.0)
        with self._lock:
            for client in list(self._clients):
                self._drop(client)
