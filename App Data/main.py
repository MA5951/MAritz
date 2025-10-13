import sys
import time
import csv
import tempfile
import math
import bisect
import json
import atexit
import threading
from pathlib import Path
from enum import Enum
from typing import SupportsBytes
import shutil  # <-- added

# ---- Optional global hotkeys (works even when tray is hidden) ----
try:
    import keyboard  # pip install keyboard
    _HAS_KEYBOARD = True
except Exception:
    _HAS_KEYBOARD = False

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QPushButton, QFileDialog,
    QLabel, QVBoxLayout, QHBoxLayout, QGraphicsView, QGraphicsScene,
    QSystemTrayIcon, QMenu
)
from PySide6.QtCore import QObject, QThread, Signal, QTimer, Qt
from PySide6.QtGui import (
    QPalette, QColor, QPen, QBrush, QPainter, QFont, QIcon, QAction, QPixmap
)

import struct

from backend_client_py import BackendProcess


# ---------- asset path (works in PyInstaller + source) ----------
def _asset_path(name: str) -> str:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).parent
    return str((base / name).resolve())


# ---------- WPILOG parsing ----------
floatStruct = struct.Struct("<f")
doubleStruct = struct.Struct("<d")
kControlStart, kControlFinish, kControlSetMetadata = 0, 1, 2

class StartRecordData:
    __slots__ = ("entry","name","type","metadata")
    def __init__(self, entry, name, type, metadata):
        self.entry, self.name, self.type, self.metadata = entry, name, type, metadata

class DataLogRecord:
    __slots__ = ("entry","timestamp","data")
    def __init__(self, entry:int, timestamp:int, data:SupportsBytes):
        self.entry, self.timestamp, self.data = entry, timestamp, data
    def isControl(self): return self.entry==0
    def isStart(self):    return self.isControl() and len(self.data)>=17 and self.data[0]==kControlStart
    def isFinish(self):   return self.isControl() and len(self.data)==5 and self.data[0]==kControlFinish
    def isSetMetadata(self): return self.isControl() and len(self.data)>=9 and self.data[0]==kControlSetMetadata
    def getStartData(self):
        d=self.data
        entry=int.from_bytes(d[1:5],"little")
        name,pos=self._readInnerString(5)
        typ,pos =self._readInnerString(pos)
        meta,_  =self._readInnerString(pos)
        return StartRecordData(entry,name,typ,meta)
    def getFinishEntry(self):
        return int.from_bytes(self.data[1:5],"little")
    def getMetadataData(self):
        buf=self.data
        eid=int.from_bytes(buf[1:5],"little")
        ln =int.from_bytes(buf[5:9],"little")
        meta=buf[9:9+ln].decode("utf-8")
        return eid, meta
    def _readInnerString(self,pos):
        ln=int.from_bytes(self.data[pos:pos+4],"little")
        end=pos+4+ln
        return self.data[pos+4:end].decode("utf-8"), end
    def getBoolean(self): return bool(self.data[0])
    def getInteger(self): return int.from_bytes(self.data,"little",signed=True)
    def getFloat(self):   return floatStruct.unpack(self.data)[0]
    def getDouble(self):  return doubleStruct.unpack(self.data)[0]
    def getString(self):  return self.data.decode("utf-8")
    def getRaw(self):     return self.data.hex()
    def getBooleanArray(self): return ",".join(str(bool(b)) for b in self.data)
    def getIntegerArray(self):
        cnt=len(self.data)//8
        vals=[int.from_bytes(self.data[i*8:(i+1)*8],"little",signed=True) for i in range(cnt)]
        return ",".join(map(str,vals))
    def getFloatArray(self):
        cnt=len(self.data)//4
        vals=struct.unpack("<"+"f"*cnt,self.data)
        return ",".join(f"{v:.6g}" for v in vals)
    def getDoubleArray(self):
        cnt=len(self.data)//8
        vals=struct.unpack("<"+"d"*cnt,self.data)
        return ",".join(f"{v:.6g}" for v in vals)
    def getStringArray(self):
        size=int.from_bytes(self.data[0:4],"little")
        arr=[]; pos=4
        for _ in range(size):
            ln=int.from_bytes(self.data[pos:pos+4],"little"); pos+=4
            s=self.data[pos:pos+ln].decode("utf-8"); pos+=ln
            arr.append(s)
        return ",".join(arr)

class DataLogIterator:
    __slots__ = ("buf","pos")
    def __init__(self, buf, pos): self.buf, self.pos = buf, pos
    def __iter__(self): return self
    def __next__(self):
        if self.pos+4>len(self.buf): raise StopIteration
        head=self.buf[self.pos]
        eL=(head&0x3)+1; sL=((head>>2)&0x3)+1; tL=((head>>4)&0x7)+1
        hdr=1+eL+sL+tL
        if self.pos+hdr>len(self.buf): raise StopIteration
        entry=sum(self.buf[self.pos+1+i]<<(8*i) for i in range(eL))
        size =sum(self.buf[self.pos+1+eL+i]<<(8*i) for i in range(sL))
        ts   =sum(self.buf[self.pos+1+eL+sL+i]<<(8*i) for i in range(tL))
        data =self.buf[self.pos+hdr:self.pos+hdr+size]
        self.pos+=hdr+size
        return DataLogRecord(entry,ts,data)

class DataLogReader:
    __slots__ = ("buf",)
    def __init__(self, buf): self.buf=buf
    def __iter__(self):
        hdr_sz=int.from_bytes(self.buf[8:12],"little")
        return DataLogIterator(self.buf,12+hdr_sz)

class ConvertWorker(QObject):
    finished=Signal(str)
    def __init__(self, filepath:Path):
        super().__init__(); self.filepath=filepath
    def run(self):
        buf=self.filepath.read_bytes()
        reader=DataLogReader(buf)
        entries,rows={},[]
        for rec in reader:
            if rec.isStart():
                sd=rec.getStartData(); entries[sd.entry]=sd
            elif rec.isFinish():
                entries.pop(rec.getFinishEntry(),None)
            elif rec.isSetMetadata():
                eid,meta=rec.getMetadataData()
                if eid in entries: entries[eid].metadata=meta
            elif rec.isControl():
                continue
            else:
                sd=entries.get(rec.entry)
                if not sd: continue
                ts=rec.timestamp/1e6; tp=sd.type
                try:
                    if   tp=="boolean":     val=rec.getBoolean()
                    elif tp=="int64":       val=rec.getInteger()
                    elif tp=="float":       val=rec.getFloat()
                    elif tp=="double":      val=rec.getDouble()
                    elif tp=="string":      val=rec.getString()
                    elif tp=="boolean[]":   val=rec.getBooleanArray()
                    elif tp=="int64[]":     val=rec.getIntegerArray()
                    elif tp=="float[]":     val=rec.getFloatArray()
                    elif tp=="double[]":    val=rec.getDoubleArray()
                    elif tp=="string[]":    val=rec.getStringArray()
                    else:                   val=rec.getRaw()
                except:
                    val=""
                rows.append((f"{ts:.6f}",sd.name,tp,str(val), sd.metadata if sd.metadata else ""))
        tmp=tempfile.NamedTemporaryFile(delete=False,suffix=".csv",
                                        mode="w",newline="",encoding="utf-8")
        w=csv.writer(tmp); w.writerow(("timestamp","key","type","value","meta")); w.writerows(rows)
        path=tmp.name; tmp.close()

        # --- persist a copy next to the original log for debugging ---
        try:
            dest = self.filepath.with_suffix(".csv")
            if dest.exists():
                # avoid overwrite: add epoch timestamp
                dest = dest.with_name(f"{dest.stem}_{int(time.time())}.csv")
            shutil.copyfile(path, dest)
            print(f"[convert] Saved CSV copy: {dest}")
        except Exception as e:
            print(f"[convert] Failed to save CSV copy: {e}")

        self.finished.emit(path)


# ---------- timeline ----------
class TimelineView(QGraphicsView):
    positionClicked = Signal(float)
    def __init__(self, duration, parent=None):
        super().__init__(parent)
        self.duration=duration; self.segments=[]; self.cursor_x=0.0
        self.setScene( QGraphicsScene(self) )
        self.setRenderHints(self.renderHints()|QPainter.Antialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self._draw_segments()
    def set_segments(self, segments):
        self.segments=segments; self._draw_segments()
    def _draw_segments(self):
        sc=self.scene(); sc.clear()
        w=max(800,int(self.duration*100))
        sc.setSceneRect(0,0,w,40)
        for s,e,st in self.segments:
            if s>1000: continue
            ex=min(e,1000)
            x0=s*100; width=(ex-s)*100
            color=QColor(200,0,0) if st=="estop" else \
                  QColor(80,80,80) if st=="disabled" else \
                  QColor(0,200,0) if st=="autonomous" else \
                  QColor(0,0,200)
            sc.addRect(x0,0,width,40,QPen(Qt.NoPen),QBrush(color))
        pen = QPen(Qt.white)
        pen.setWidth(1)
        pen.setJoinStyle(Qt.RoundJoin)
        sc.addRect(0,0,w,40,pen)
    def wheelEvent(self, ev):
        dx=ev.angleDelta().x(); dy=ev.angleDelta().y()
        if dx:
            sb=self.horizontalScrollBar(); sb.setValue(sb.value()-dx)
            return
        factor=1.2**(dy/120) if dy else 1.0
        cur=self.transform().m11()
        sw=self.sceneRect().width(); vw=self.viewport().width()
        min_s=vw/sw if sw>0 else 1.0
        new=cur*factor
        if new<min_s: factor=min_s/cur
        self.scale(factor,1); self.viewport().update()
    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            p = ev.position()
            pt = self.mapToScene(p.x(), p.y())
            ts = max(0, min(pt.x()/100, self.duration, 1000))
            self.positionClicked.emit(ts)
        super().mousePressEvent(ev)
    def update_cursor(self, t):
        if t>1000: return
        self.cursor_x=t*100
        self.ensureVisible(self.cursor_x,0,50,40)
        self.viewport().update()
    def drawForeground(self, painter, rect):
        painter.save(); painter.resetTransform()
        pen=QPen(Qt.white); pen.setWidth(1); painter.setPen(pen)
        font=QFont(); font.setPixelSize(10); painter.setFont(font)
        vw=self.viewport().width(); vh=self.viewport().height()
        left=self.mapToScene(0,0).x()/100; right=self.mapToScene(vw,0).x()/100
        span=right-left; target=span/10
        nice=[1,2,5,10,20,30,60,120,300,600]
        interval=next((n for n in nice if n>=target),nice[-1])
        first=math.floor(left/interval)*interval
        t=first
        while t<=right:
            if 0<=t<=1000:
                x=self.mapFromScene(t*100,0).x()
                painter.drawLine(x,vh-20,x,vh-5)
                painter.drawText(x+2,vh-22,f"{int(t)}s")
            t+=interval
        pen=QPen(Qt.white,2); pen.setCosmetic(True); painter.setPen(pen)
        x=self.mapFromScene(self.cursor_x,0).x()
        painter.drawLine(x,0,x,vh)
        painter.restore()


# ---------- controller ----------
class Controller(QObject):
    loaded=Signal(int,float)
    segmentsChanged=Signal(list)
    progressChanged=Signal(int,int)
    elapsedChanged=Signal(float)

    def __init__(self):
        super().__init__()
        self.csv_path=None
        self.log=[]; self.timestamps=[]
        self.idx=0; self.start_time=0.0
        self.is_publishing=False
        self.segments=[]

        self.timer=QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.setInterval(4)
        self.timer.timeout.connect(self._tick)

        self.nt_host = "127.0.0.1"
        self.nt_port = 5810

        if not hasattr(self, "backend") or self.backend is None:
            from backend_client import BackendProcess as BE
            self.backend = BE()

    def open_log(self, parent):
        path,_=QFileDialog.getOpenFileName(parent, "Open WPILog","","WPILog Files (*.wpilog);;All Files (*)")
        if not path: return
        self.worker=ConvertWorker(Path(path))
        self.thread=QThread()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(self._on_converted)
        self.worker.finished.connect(self.thread.quit)
        self.thread.start()

    def _on_converted(self, csv_path):
        self.csv_path=csv_path
        self.log=[]
        with open(csv_path,newline="",encoding="utf-8") as f:
            r=csv.reader(f); header=next(r)
            for row in r:
                try: ts=float(row[0])
                except: continue
                if ts>1000: continue
                meta = row[4] if len(row)>=5 else ""
                self.log.append((ts,row[1],row[2],row[3],meta))
        self.log.sort(key=lambda x:x[0])
        self.timestamps=[r[0] for r in self.log]
        total=len(self.log); duration=self.timestamps[-1] if total else 1.0

        # --- Build segments ---
        segs=[]
        # First try DS: flags (your original behavior)
        has_ds = any(k.startswith("DS:") for _,k,_,_,_ in self.log)
        if has_ds:
            flags={"enabled":False,"autonomous":False,"estop":False}
            def state():
                if flags["estop"]: return "estop"
                if not flags["enabled"]: return "disabled"
                if flags["autonomous"]: return "autonomous"
                return "teleop"
            cur=state(); st=0.0
            for ts,key,_,val,_ in self.log:
                if key.startswith("DS:"):
                    f=key.split("DS:")[1]
                    if f in flags:
                        flags[f]=(val=="True")
                        ns=state()
                        if ns!=cur:
                            segs.append((st,ts,cur)); cur=ns; st=ts
            segs.append((st,duration,cur))
        else:
            # Fallback: decode NT:/FMSInfo/FMSControlData using your mapping:
            # 51 -> autonomous, 49 -> teleop, 50/0/NaN/invalid -> disabled

            def _to_int_or_none(s: str):
                try:
                    # accepts "49", "49.0", " 50 ", etc.
                    return int(float(s))
                except Exception:
                    return None

            def decode_state(mask: int | None) -> str:
                if mask == 51:
                    return "autonomous"
                if mask == 49:
                    return "teleop"
                # 50, 0, None/NaN/invalid -> disabled
                return "disabled"

            # Collect rows (keep order); include even invalid ones so we can mark transitions when they become valid
            fms_rows = [(ts, _to_int_or_none(val))
                        for ts, key, _, val, _ in self.log
                        if key == "NT:/FMSInfo/FMSControlData"]

            if not fms_rows:
                # No way to infer; assume disabled for entire duration
                segs = [(0.0, duration, "disabled")]
            else:
                # Start explicitly as DISABLED until first valid transition shows up
                cur_state = "disabled"
                st = 0.0
                for ts, mask in fms_rows:
                    ns = decode_state(mask)
                    if ns != cur_state:
                        if ts > st:
                            segs.append((st, ts, cur_state))
                        cur_state = ns
                        st = ts
                # tail
                if duration > st:
                    segs.append((st, duration, cur_state))


        self.segments=segs

        self.loaded.emit(total,duration)
        self.segmentsChanged.emit(segs)

        try:
            self.backend.start(self.nt_host, self.nt_port, self.csv_path)
        except Exception as e:
            print("Backend preload failed:", e)


    def toggle_publish(self):
        self.is_publishing = not self.is_publishing
        try:
            if self.is_publishing:
                resp = self.backend.pub_on()
                if resp != "OK":
                    raise RuntimeError(f"Backend replied: {resp}")
                if self.timestamps:
                    cur_t = self.timestamps[self.idx] if self.idx < len(self.timestamps) else 0.0
                    self.backend.seek(cur_t)
                if self.timer.isActive():
                    self.backend.play()
            else:
                self.backend.pub_off()
        except Exception as e:
            print('Failed to toggle publish:', e)
            self.is_publishing = False

    def toggle_replay(self):
        if not self.log: return
        if not self.timer.isActive():
            base=self.timestamps[self.idx] if self.idx<len(self.timestamps) else 0.0
            self.start_time=time.perf_counter()-base
            self.timer.start()
            try:
                if self.is_publishing: self.backend.play()
            except Exception: pass
        else:
            self.timer.stop()
            try:
                if self.is_publishing: self.backend.pause()
            except Exception: pass

    def seek(self, t):
        self.idx=bisect.bisect_left(self.timestamps,t)
        self.start_time=time.perf_counter()-t
        try:
            if self.is_publishing: self.backend.seek(t)
        except Exception: pass

    def _tick(self):
        now=time.perf_counter()-self.start_time
        total=len(self.log)
        while self.idx<total and self.log[self.idx][0]<=now:
            self.idx+=1
        self.progressChanged.emit(self.idx,total)
        self.elapsedChanged.emit(now)


# ---------- tray window ----------
class TrayWindow(QWidget):
    def __init__(self, ctrl, full_win):
        super().__init__(None, Qt.Tool)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_StyledBackground)
        self.setFixedSize(600,100)
        self.setStyleSheet("QWidget { background-color: rgba(33, 34, 34, 230); }")

        self.ctrl, self.full = ctrl, full_win

        btn_open   = QPushButton('Open Log')
        btn_open.clicked.connect(self._on_open)
        self.btn_replay = QPushButton('Play'); self.btn_replay.setEnabled(False)
        self.btn_replay.clicked.connect(self._on_toggle_replay)
        self.btn_pub    = QPushButton('Start Broadcast'); self.btn_pub.setEnabled(False)
        self.btn_pub.clicked.connect(self._on_toggle_pub)
        btn_full   = QPushButton('Full App'); btn_full.clicked.connect(full_win.show)
        btn_close  = QPushButton('✕'); btn_close.setFixedSize(20,20)
        btn_close.clicked.connect(self.hide)
        btn_close.setStyleSheet('background:transparent; color:white;')

        self.lbl_status  = QLabel('Log not loaded'); self.lbl_status.setStyleSheet('color:white')
        self.lbl_elapsed = QLabel('0.00s'); self.lbl_elapsed.setStyleSheet('color:white')

        self.timeline = TimelineView(1.0)
        self.timeline.positionClicked.connect(lambda ts: (ctrl.seek(ts), self.timeline.update_cursor(ts)))

        top = QHBoxLayout()
        top.addWidget(btn_open); top.addWidget(self.btn_replay); top.addWidget(self.btn_pub)
        top.addWidget(btn_full); top.addSpacing(15); top.addWidget(self.lbl_status)
        top.addSpacing(15); top.addWidget(self.lbl_elapsed); top.addStretch(); top.addWidget(btn_close)
        top.setContentsMargins(5,5,5,5)

        layout = QVBoxLayout(self)
        layout.addLayout(top); layout.addWidget(self.timeline)
        layout.setContentsMargins(2,2,2,2); self.setLayout(layout)

        ctrl.loaded.connect(self._on_loaded)
        ctrl.segmentsChanged.connect(self.timeline.set_segments)
        ctrl.elapsedChanged.connect(self._on_elapsed)
        ctrl.progressChanged.connect(lambda *_: self._update_play_status())
        ctrl.elapsedChanged.connect(lambda e: self.timeline.update_cursor(min(e, self.timeline.duration)))

    def _on_open(self):
        self.lbl_status.setText('Loading log'); self.lbl_status.setStyleSheet('color:orange')
        self.ctrl.open_log(self)

    def _on_loaded(self, total, dur):
        self.btn_replay.setEnabled(True); self.btn_pub.setEnabled(True)
        self.timeline.duration = dur; self.timeline.set_segments(self.ctrl.segments)
        self.timeline.resetTransform()
        view_w = self.timeline.viewport().width()
        scene_w = self.timeline.sceneRect().width()
        if scene_w > 0:
            min_scale = view_w / scene_w
            self.timeline.scale(min_scale, 1.0)
        self.timeline.update_cursor(0.0)
        self.lbl_status.setText('Ready'); self.lbl_status.setStyleSheet('color:white')
        self.lbl_elapsed.setText('0.00s')
        self._update_play_status(); self._update_pub_status()

    def _on_toggle_replay(self):
        self.ctrl.toggle_replay(); self._update_play_status()

    def _on_toggle_pub(self):
        self.ctrl.toggle_publish(); self._update_pub_status()

    def _update_play_status(self):
        running = self.ctrl.timer.isActive()
        self.btn_replay.setText('Stop Replay' if running else 'Play')
        if running:
            self.btn_replay.setStyleSheet('background-color: #53c268; color: white;')
        else:
            self.btn_replay.setStyleSheet('background-color: #cf4e4e; color: white;')
        if self.lbl_status.text() not in ('Loading log',):
            self.lbl_status.setText('Running' if running else 'Ready')

    def _update_pub_status(self):
        on = self.ctrl.is_publishing
        self.btn_pub.setText('Stop Broadcast' if on else 'Start Broadcast')
        if on:
            self.btn_pub.setStyleSheet('background-color: #53c268; color: white;')
        else:
            self.btn_pub.setStyleSheet('background-color: #cf4e4e; color: white;')

    def _on_elapsed(self, e): self.lbl_elapsed.setText(f'{e:.2f}s')

    def showEvent(self, event):
        full = QApplication.primaryScreen().geometry()
        avail = QApplication.primaryScreen().availableGeometry()
        tb_height = full.height() - avail.height()
        margin = 10
        x = avail.x() + avail.width() - self.width() - margin
        y = full.height() - tb_height - self.height() - margin
        self.move(x, y)
        super().showEvent(event)

class FullWindow(QMainWindow):
    def __init__(self, ctrl):
        super().__init__()
        self.ctrl = ctrl
        self.setWindowTitle('MAritz')
        self.setGeometry(200,200,900,200)
        p = QPalette()
        p.setColor(QPalette.Window, QColor(53,53,53))
        p.setColor(QPalette.WindowText, Qt.white)
        p.setColor(QPalette.Base, QColor(25,25,25))
        p.setColor(QPalette.Text, Qt.white)
        p.setColor(QPalette.Button, QColor(53,53,53))
        p.setColor(QPalette.ButtonText, Qt.white)
        QApplication.setPalette(p)

        btn_open = QPushButton('Open Log')
        btn_open.clicked.connect(lambda: ctrl.open_log(self))
        self.btn_replay = QPushButton('Play'); self.btn_replay.setEnabled(False)
        self.btn_replay.clicked.connect(lambda: (ctrl.toggle_replay(), self._update()))
        self.btn_pub = QPushButton('Start Broadcast'); self.btn_pub.setEnabled(False)
        self.btn_pub.clicked.connect(lambda: (ctrl.toggle_publish(), self._update_pub()))
        btn_back = QPushButton('Back to Tray'); btn_back.clicked.connect(self.hide)

        self.timeline = TimelineView(1.0)
        self.timeline.positionClicked.connect(lambda ts: (
            ctrl.seek(ts), self.timeline.update_cursor(ts), self._update_progress(ts)
        ))

        self.lbl_progress = QLabel('0/0')
        self.lbl_elapsed  = QLabel('0.00s')

        top = QHBoxLayout()
        top.addWidget(btn_open); top.addWidget(self.btn_replay); top.addWidget(self.btn_pub)
        top.addWidget(btn_back); top.addWidget(self.lbl_progress); top.addWidget(self.lbl_elapsed)

        layout = QVBoxLayout()
        layout.addLayout(top); layout.addWidget(self.timeline)

        w = QWidget(); w.setLayout(layout); self.setCentralWidget(w)

        ctrl.loaded.connect(lambda t,d: (
            self.btn_replay.setEnabled(True),
            self.btn_pub.setEnabled(True),
            setattr(self.timeline, 'duration', d),
            self.timeline.set_segments(ctrl.segments),
            self._update_progress(0)
        ))
        ctrl.progressChanged.connect(lambda i, tot: self.lbl_progress.setText(f'{i}/{tot}'))
        ctrl.elapsedChanged.connect(lambda e: self.lbl_elapsed.setText(f'{e:.2f}s'))
        ctrl.elapsedChanged.connect(lambda e: self.timeline.update_cursor(min(e,self.timeline.duration)))

    def _update(self):
        running = self.ctrl.timer.isActive()
        self.btn_replay.setText('Stop Replay' if running else 'Play')
        if running:
            self.btn_replay.setStyleSheet('background-color: #53c268; color: white;')
        else:
            self.btn_replay.setStyleSheet('background-color: #cf4e4e; color: white;')

    def _update_pub(self):
        on = self.ctrl.is_publishing
        self.btn_pub.setText('Stop Broadcast' if on else 'Start Broadcast')
        if on:
            self.btn_pub.setStyleSheet('background-color: #53c268; color: white;')
        else:
            self.btn_pub.setStyleSheet('background-color: #cf4e4e; color: white;')

    def _update_progress(self, ts):
        idx = bisect.bisect_left(self.ctrl.timestamps, ts)
        tot = len(self.ctrl.log)
        self.lbl_progress.setText(f'{idx}/{tot}')
        self.lbl_elapsed.setText(f'{ts:.2f}s')


# --------- Global hotkey Qt bridge ---------
class HotkeyBridge(QObject):
    toggleReplay = Signal()
    toggleBroadcast = Signal()
    openLog = Signal()


_hotkey_thread = None

def start_global_hotkeys(bridge: HotkeyBridge):
    if not _HAS_KEYBOARD:
        # still run app; just no hotkeys
        print("[hotkeys] 'keyboard' package not found; global hotkeys disabled.")
        return
    def _worker():
        keyboard.add_hotkey('alt+r+s', bridge.toggleReplay.emit)
        keyboard.add_hotkey('alt+r+b', bridge.toggleBroadcast.emit)
        keyboard.add_hotkey('alt+r+o', bridge.openLog.emit)
        keyboard.wait()
    global _hotkey_thread
    _hotkey_thread = threading.Thread(target=_worker, daemon=True)
    _hotkey_thread.start()

def stop_global_hotkeys():
    if _HAS_KEYBOARD:
        try: keyboard.unhook_all_hotkeys()
        except Exception: pass


# ---------- main ----------
def main():
    app = QApplication.instance() or QApplication(sys.argv)

    # Controller + windows
    ctrl     = Controller()
    full     = FullWindow(ctrl)
    tray_win = TrayWindow(ctrl, full)

    # Load icons (once)
    ICON_BW     = QIcon(_asset_path("icon_bw.ico"))
    ICON_NORMAL = QIcon(_asset_path("icon.ico"))
    ICON_GREEN  = QIcon(_asset_path("icon_green.ico"))

    # App/window icons (static)
    app.setWindowIcon(ICON_NORMAL)
    full.setWindowIcon(ICON_NORMAL)
    tray_win.setWindowIcon(ICON_NORMAL)

    # Tray
    tray = QSystemTrayIcon(ICON_BW if not ctrl.log else ICON_NORMAL, app)
    tray.setToolTip('MAritz')

    # De-stuttered icon swapping
    class TrayState(Enum):
        NO_LOG = 0
        LOADED_IDLE = 1
        PLAYING = 2

    def compute_state() -> TrayState:
        if not ctrl.log:
            return TrayState.NO_LOG
        return TrayState.PLAYING if ctrl.timer.isActive() else TrayState.LOADED_IDLE

    _current_state = {"state": None}
    _debounce = QTimer()
    _debounce.setSingleShot(True)
    _debounce.setInterval(150)  # ms

    def _apply_icon_for(state: TrayState):
        if _current_state["state"] == state:
            return
        _current_state["state"] = state
        if state == TrayState.NO_LOG:
            tray.setIcon(ICON_BW)
        elif state == TrayState.LOADED_IDLE:
            tray.setIcon(ICON_NORMAL)
        else:
            tray.setIcon(ICON_GREEN)

    def _debounce_tick():
        _apply_icon_for(compute_state())

    _debounce.timeout.connect(_debounce_tick)

    def schedule_update():
        if not _debounce.isActive():
            _debounce.start()

    # initial icon
    _apply_icon_for(compute_state())

    # update on events that actually change state
    ctrl.loaded.connect(lambda *_: schedule_update())

    # patch play/publish UI updates to also schedule tray update
    orig_play = tray_win._update_play_status
    def _upd_play():
        orig_play()
        schedule_update()
    tray_win._update_play_status = _upd_play

    orig_pub = tray_win._update_pub_status
    def _upd_pub():
        orig_pub()
        # If you ever map publish state to an icon, call schedule_update() here.
    tray_win._update_pub_status = _upd_pub

    # menu + show
    menu = QMenu()
    show_action = QAction('Show Controls')
    show_action.triggered.connect(tray_win.show)
    exit_action = QAction('Exit')
    exit_action.triggered.connect(app.quit)
    menu.addAction(show_action); menu.addAction(exit_action)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda r: tray_win.show() if r==QSystemTrayIcon.Trigger else None)
    tray.show()

    # ---- Global hotkeys (work even when tray hidden) ----
    bridge = HotkeyBridge()
    bridge.toggleReplay.connect(tray_win._on_toggle_replay)
    bridge.toggleBroadcast.connect(tray_win._on_toggle_pub)
    bridge.openLog.connect(tray_win._on_open)

    start_global_hotkeys(bridge)
    atexit.register(stop_global_hotkeys)

    sys.exit(app.exec())


if __name__ == '__main__':
    if '--publisher' in sys.argv:
        import multiprocessing
        multiprocessing.freeze_support()
        from publisher_process import main as _publisher_main
        _publisher_main()
        raise SystemExit(0)

    import multiprocessing
    multiprocessing.freeze_support()

    # allow `from backend_client import BackendProcess` in Controller
    import backend_client_py as _bc
    sys.modules['backend_client'] = _bc

    main()
