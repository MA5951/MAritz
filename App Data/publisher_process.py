
import sys, time, csv, threading, json

try:
    from ntcore import NetworkTableInstance
except ImportError:
    NetworkTableInstance = None

PERIOD = 0.020  # 20 ms

class Publisher:
    def __init__(self):
        self.inst = None
        self.table = None
        self.frames = []     # list[dict[str,(type,value,meta)]]
        self.idx = 0
        self.start = time.perf_counter()
        self.playing = False
        self.publishing = False
        self.exit = False
        self.lock = threading.Lock()

    def set_server(self, host, port):
        if NetworkTableInstance is None:
            print("ERR ntcore not installed", flush=True); return
        if self.inst:
            self.inst.stopClient()
        self.inst = NetworkTableInstance.getDefault()
        self.inst.setServer(host, int(port))
        try:
            self.inst.setUpdateRate(PERIOD)
        except Exception:
            pass
        self.inst.startClient4("MAritzPyProc")
        self.table = self.inst.getTable("Replay")

    def _coalesce(self, path):
        rows = []
        with open(path, newline="", encoding="utf-8") as f:
            r = csv.reader(f); header = next(r, None)
            for row in r:
                try:
                    ts = float(row[0])
                except:
                    continue
                if ts > 1000.0:
                    continue
                key, tp, val = row[1], row[2], row[3]
                meta = row[4] if len(row)>=5 else ""
                rows.append((ts, key, tp, val, meta))
        rows.sort(key=lambda x: x[0])
        if not rows:
            self.frames = []
            return
        last_ts = rows[-1][0]
        nframes = int(last_ts / PERIOD) + 1
        frames = [dict() for _ in range(nframes)]
        for ts, key, tp, val, meta in rows:
            fi = int(ts / PERIOD)
            frames[fi][key] = (tp, val, meta)  # last one wins within frame
        self.frames = frames
        self.idx = 0
        self.start = time.perf_counter()

    def load_csv(self, path):
        try:
            self._coalesce(path)
            print("OK", flush=True)
        except Exception:
            print("ERR", flush=True)

    def seek(self, t):
        with self.lock:
            t = max(0.0, min(t, 1000.0))
            self.idx = int(t / PERIOD)
            self.start = time.perf_counter() - t

    def play(self):
        with self.lock:
            base = self.idx * PERIOD
            self.start = time.perf_counter() - base
            self.playing = True

    def pause(self):
        with self.lock:
            self.playing = False

    def stop(self):
        with self.lock:
            self.playing = False
            self.idx = 0
            self.start = time.perf_counter()

    def set_publish(self, on):
        with self.lock:
            self.publishing = on

    def _put(self, key, tp, val, meta):
        if not self.table:
            return
        e = self.table.getEntry(key)
        if tp == "boolean":
            e.setBoolean(val in ("True","true","1","t","T"))
        elif tp in ("int64","float","double"):
            try: e.setDouble(float(val))
            except: e.setDouble(0.0)
        elif tp == "string":
            e.setString(val)
        elif tp == "boolean[]":
            arr = [x in ("True","true","1","t","T") for x in val.split(",")] if val else []
            e.setBooleanArray(arr)
        elif tp in ("int64[]","float[]","double[]"):
            nums = []
            if val:
                for x in val.split(","):
                    try: nums.append(float(x))
                    except: nums.append(0.0)
            e.setDoubleArray(nums)
        elif tp == "string[]":
            e.setStringArray(val.split(",") if val else [])
        elif tp == "raw":
            # Expect meta to be a JSON dict with at least {"type":"struct:Pose2d", "schema":"..."}
            type_str = None
            if meta:
                try:
                    m = json.loads(meta)
                    type_str = m.get("type", None)
                except Exception:
                    type_str = None
            if not type_str:
                type_str = "raw"  # fallback
            try:
                b = bytes.fromhex(val)
            except Exception:
                b = b""
            try:
                e.setRaw(b, type_str)
            except Exception:
                # fallback to plain raw if type fails
                try:
                    e.setRaw(b)
                except Exception:
                    pass
        else:
            e.setString(val)

    def run(self):
        next_wake = time.perf_counter()
        prev_frame = {}
        while not self.exit:
            now = time.perf_counter()
            delay = next_wake - now
            if delay > 0.002:
                time.sleep(delay - 0.001)
            while True:
                now = time.perf_counter()
                if now >= next_wake:
                    break
            with self.lock:
                playing = self.playing
                publishing = self.publishing
                idx = self.idx
                start = self.start
            if playing and self.frames:
                elapsed = now - start
                target_idx = min(int(elapsed / PERIOD), len(self.frames)-1)
                while idx <= target_idx and idx < len(self.frames):
                    if publishing:
                        frame = self.frames[idx]
                        for k,(tp,val,meta) in frame.items():
                            if prev_frame.get(k) != (tp,val,meta):
                                self._put(k, tp, val, meta)
                        for k in list(prev_frame.keys()):
                            if k not in frame:
                                prev_frame.pop(k)
                        prev_frame = frame
                        try:
                            self.inst.flush()
                        except Exception:
                            pass
                    idx += 1
                with self.lock:
                    self.idx = idx
                    if self.idx >= len(self.frames):
                        self.playing = False
            next_wake += PERIOD
            behind = now - next_wake
            if behind > PERIOD:
                missed = int(behind / PERIOD)
                next_wake += missed * PERIOD

def main():
    pub = Publisher()
    threading.Thread(target=pub.run, daemon=True).start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        cmd, *rest = line.split(" ", 1)
        try:
            if cmd == "SET_SERVER":
                host, port = rest[0].split()
                pub.set_server(host, int(port)); print("OK", flush=True)
            elif cmd == "LOAD_CSV":
                path = rest[0]
                if (path.startswith('"') and path.endswith('"')) or (path.startswith("'") and path.endswith("'")):
                    path = path[1:-1]
                pub.load_csv(path)  # prints OK/ERR
            elif cmd == "SEEK":
                pub.seek(float(rest[0])); print("OK", flush=True)
            elif cmd == "PLAY":
                pub.play(); print("OK", flush=True)
            elif cmd == "PAUSE":
                pub.pause(); print("OK", flush=True)
            elif cmd == "STOP":
                pub.stop(); print("OK", flush=True)
            elif cmd == "PUBLISH_ON":
                pub.set_publish(True); print("OK", flush=True)
            elif cmd == "PUBLISH_OFF":
                pub.set_publish(False); print("OK", flush=True)
            elif cmd == "QUIT":
                pub.exit = True; print("BYE", flush=True); break
            else:
                print("ERR", flush=True)
        except Exception:
            print("ERR", flush=True)

if __name__ == "__main__":
    main()
