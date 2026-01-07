
import subprocess, sys, threading
from pathlib import Path

class BackendProcess:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()

    def start(self, host: str, port: int, csv_path: str | None = None):
        if self.proc and self.proc.poll() is None:
            return
        # locate script path for dev; use flag for frozen exe
        script = str(Path(__file__).parent / "publisher_process.py")
        if getattr(sys, 'frozen', False):
            # We are running as PyInstaller exe: re-run same exe in backend mode
            args = [sys.executable, '--publisher']
            creationflags = 0
            if os.name == 'nt':
                creationflags = 0x08000000  # CREATE_NO_WINDOW (avoid flashing console)
        else:
            args = [sys.executable, script]
            creationflags = 0

        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            creationflags=creationflags
        )
        threading.Thread(target=self._drain, daemon=True).start()
        self.set_server(host, port)
        if csv_path:
            self.load_csv(csv_path)

    def _drain(self):
        if not self.proc:
            return
        for line in self.proc.stderr:
            sys.stderr.write("[backend] " + line)

    def _send(self, cmd: str):
        if not self.proc or self.proc.poll() is not None:
            raise RuntimeError("Backend not running")
        with self.lock:
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()
            return self.proc.stdout.readline().strip()

    def set_server(self, host: str, port: int): return self._send(f"SET_SERVER {host} {port}")
    def load_csv(self, path: str): return self._send(f"LOAD_CSV {path}")
    def seek(self, t: float): return self._send(f"SEEK {t}")
    def play(self): return self._send("PLAY")
    def pause(self): return self._send("PAUSE")
    def stop(self): return self._send("STOP")
    def pub_on(self): return self._send("PUBLISH_ON")
    def pub_off(self): return self._send("PUBLISH_OFF")
    def quit(self):
        try: self._send("QUIT")
        except Exception: pass
        if self.proc:
            self.proc.terminate()
            self.proc = None
