#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Link2 Studio - Insta360 Link 2 云台相机综合控制平台 (v2)

架构：
  - ptz_daemon.exe   常驻云台守护进程（DirectShow IAMCameraControl，命令 <10ms）
  - 运动控制器        Python 20Hz 线程：速度控制 + 平滑 goto（插值）
  - ffmpeg dshow     单采集进程，30fps MJPEG，多客户端共享
  - 录像             帧数据 tee 到第二个 ffmpeg 编码进程（不占用摄像头）
  - 配置持久化        studio_config.json（预设位/巡航/设置）

用法：
  python link2_studio.py [端口]
  浏览器打开 http://127.0.0.1:8899
"""
import json
import math
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DAEMON_EXE = ROOT / "ptz_daemon.exe"
FFMPEG = Path(r"C:\Users\Cccong\link2-tools\ffmpeg\bin\ffmpeg.exe")
SNAP_DIR = ROOT / "snapshots"
REC_DIR = ROOT / "recordings"
CONFIG_FILE = ROOT / "studio_config.json"

DSHOW_VIDEO = "Insta360 Link 2"
CAM_FPS = 30

PAN_MIN, PAN_MAX = -145, 145
TILT_MIN, TILT_MAX = -90, 100
ZOOM_MIN, ZOOM_MAX = 100, 400

# ---------------- 配置 ----------------
CONFIG = {
    "presets": {},          # name -> {pan, tilt, zoom}
    "patrol_dwell": 2.0,    # 巡航每个点停留秒数
    "fov": 75.0,            # 点击转向用的水平视场角（度，1x 变焦时）
    "speed": 60,            # 默认运动速度 deg/s
}
CONFIG_LOCK = threading.Lock()


def load_config():
    global CONFIG
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        for k in ("presets", "patrol_dwell", "fov", "speed"):
            if k in data:
                CONFIG[k] = data[k]
    except Exception:
        pass


def save_config():
    with CONFIG_LOCK:
        CONFIG_FILE.write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- PTZ 守护进程封装 ----------------
class PTZDaemon:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.last_start = 0
        self.ranges = None

    def _start(self):
        now = time.monotonic()
        if now - self.last_start < 1.0:
            raise RuntimeError("ptz daemon restarting too fast")
        self.last_start = now
        self.proc = subprocess.Popen(
            [str(DAEMON_EXE)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            cwd=str(ROOT), creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.ranges = self._parse(self._cmd("RANGE", 4.0))
        if len(self.ranges) != 8:
            raise RuntimeError("bad RANGE response")

    def _cmd(self, line, timeout=2.0):
        if self.proc is None or self.proc.poll() is not None:
            self._start()
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        # 简单超时保护：daemon 响应都是立即的
        resp = self.proc.stdout.readline()
        if not resp:
            raise RuntimeError("ptz daemon no response")
        return resp.strip()

    @staticmethod
    def _parse(resp):
        if not resp.startswith("OK"):
            raise RuntimeError(resp or "ptz daemon error")
        return [int(x) for x in resp[2:].split()]

    def command(self, line, timeout=2.0):
        with self.lock:
            try:
                return self._parse(self._cmd(line, timeout))
            except RuntimeError:
                # 进程可能崩溃，标记待重启
                if self.proc and self.proc.poll() is None:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
                self.proc = None
                raise

    def ping(self):
        try:
            self.command("PING")
            return True
        except Exception:
            return False

    def get(self):
        v = self.command("GET")
        return {"pan": v[0], "tilt": v[1], "zoom": v[2]}

    def set(self, pan, tilt):
        pan = max(PAN_MIN, min(PAN_MAX, int(pan)))
        tilt = max(TILT_MIN, min(TILT_MAX, int(tilt)))
        v = self.command(f"SET {pan} {tilt}")
        return {"pan": v[0], "tilt": v[1], "zoom": v[2]}

    def zoom(self, z):
        z = max(ZOOM_MIN, min(ZOOM_MAX, int(z)))
        self.command(f"ZOOM {z}")
        return z

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write("QUIT\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


PTZ = PTZDaemon()


# ---------------- 运动控制器（20Hz） ----------------
class MotionController(threading.Thread):
    """速度控制 + 平滑 goto 统一控制器。所有云台运动经由此线程下发。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.vel = (0.0, 0.0)          # deg/s
        self.target = None             # (pan, tilt)
        self.speed = 60.0              # deg/s
        self.pos = None                # 控制器跟踪的位置（命令值）
        self.done_event = threading.Event()
        self.done_event.set()
        self._stop = False

    def run(self):
        while not self._stop:
            t0 = time.monotonic()
            try:
                with self.lock:
                    vel = self.vel
                    target = self.target
                    speed = self.speed
                if self.pos is None:
                    try:
                        self.pos = [PTZ.get()["pan"], PTZ.get()["tilt"]]
                    except Exception:
                        time.sleep(0.5)
                        continue
                if target is not None:
                    dx = target[0] - self.pos[0]
                    dy = target[1] - self.pos[1]
                    dist = math.hypot(dx, dy)
                    dt = 0.05
                    step = speed * dt
                    if dist <= step or dist == 0:
                        self.pos = [target[0], target[1]]
                        with self.lock:
                            self.target = None
                        PTZ.set(*self.pos)
                        self.done_event.set()
                    else:
                        self.pos[0] += dx / dist * step
                        self.pos[1] += dy / dist * step
                        PTZ.set(int(self.pos[0]), int(self.pos[1]))
                elif vel != (0.0, 0.0):
                    dt = 0.05
                    self.pos[0] = max(PAN_MIN, min(PAN_MAX, self.pos[0] + vel[0] * dt))
                    self.pos[1] = max(TILT_MIN, min(TILT_MAX, self.pos[1] + vel[1] * dt))
                    PTZ.set(int(self.pos[0]), int(self.pos[1]))
            except Exception:
                pass  # daemon 暂时不可用，下一轮重试
            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, 0.05 - elapsed))

    def _resync(self):
        """从硬件读取真实当前位置（消除控制器内部位置漂移）"""
        try:
            cur = PTZ.get()
            self.pos = [cur["pan"], cur["tilt"]]
        except Exception:
            pass

    def set_vel(self, pan_vel, tilt_vel):
        with self.lock:
            self._resync()
            self.vel = (float(pan_vel), float(tilt_vel))
            self.target = None
            self.done_event.clear()

    def goto(self, pan, tilt, speed=None, wait=False):
        pan = max(PAN_MIN, min(PAN_MAX, int(pan)))
        tilt = max(TILT_MIN, min(TILT_MAX, int(tilt)))
        with self.lock:
            self._resync()
            if speed:
                self.speed = float(speed)
            self.vel = (0.0, 0.0)
            self.target = (pan, tilt)
            self.done_event.clear()
        if wait:
            self.done_event.wait(timeout=30)

    def stop_motion(self):
        with self.lock:
            self.vel = (0.0, 0.0)
            self.target = None
        self.done_event.set()

    def state(self):
        with self.lock:
            moving = self.target is not None or self.vel != (0.0, 0.0)
            tgt = list(self.target) if self.target else None
            return {"moving": moving, "target": tgt, "speed": self.speed}


MOTION = MotionController()


# ---------------- 人脸视觉服务（检测/识别/跟踪） ----------------
FACE = None
try:
    from face_tracker import FaceService

    def _face_get_frame():
        with CAMERA_LOCK:
            if LATEST_FRAME is None:
                return None
            return LATEST_FRAME, 0, 0

    FACE = FaceService(
        get_frame=_face_get_frame,
        get_position=PTZ.get,
        set_vel=lambda pv, tv: MOTION.set_vel(pv, tv),
        stop_motion=MOTION.stop_motion,
        fov_h=CONFIG["fov"],
        on_log=lambda msg: print(f"[face] {msg}"),
    )
except Exception as _face_err:
    print(f"[face] 模块不可用: {_face_err}")


# ---------------- 巡航线程 ----------------
class PatrolThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self._stop = False
        self.active = False

    def run(self):
        self.active = True
        try:
            while not self._stop:
                with CONFIG_LOCK:
                    presets = dict(CONFIG["presets"])
                    dwell = float(CONFIG["patrol_dwell"])
                if not presets:
                    time.sleep(1)
                    continue
                for name, p in presets.items():
                    if self._stop:
                        return
                    MOTION.goto(p["pan"], p["tilt"], wait=True)
                    t0 = time.monotonic()
                    while not self._stop and time.monotonic() - t0 < dwell:
                        time.sleep(0.1)
        finally:
            self.active = False

    def stop(self):
        self._stop = True


PATROL = None


def patrol_status():
    return {"active": PATROL is not None and PATROL.active}


# ---------------- 视频采集 ----------------
JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"

CAM_SIZE = "1280x720"
CAMERA_PROCESS = None
CAMERA_THREAD = None
CAMERA_LOCK = threading.Lock()
FRAME_COND = threading.Condition(CAMERA_LOCK)
LATEST_FRAME = None
LATEST_FRAME_TIME = 0
FRAME_COUNT = 0
CAMERA_ERROR = ""


class Recorder:
    """把 MJPEG 帧 tee 到 ffmpeg 编码进程，不额外占用摄像头。"""

    def __init__(self):
        self.proc = None
        self.queue = None
        self.writer = None
        self.file = None
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            if self.proc:
                return {"recording": True, "file": str(self.file)}
            REC_DIR.mkdir(exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            encoders = [
                (["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"], ".mp4"),
                (["-c:v", "mpeg4", "-q:v", "5"], ".mp4"),
                (["-c", "copy"], ".mjpeg"),
            ]
            for enc, ext in encoders:
                out = REC_DIR / f"rec_{stamp}{ext}"
                cmd = [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
                       "-f", "mjpeg", "-framerate", str(CAM_FPS), "-i", "pipe:0"] + enc + [str(out)]
                try:
                    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
                except Exception:
                    continue
                time.sleep(1.0)
                if proc.poll() is not None:
                    continue
                self.proc = proc
                self.file = out
                self.queue = __import__("queue").Queue(maxsize=120)
                self.writer = threading.Thread(target=self._writer, daemon=True)
                self.writer.start()
                return {"recording": True, "file": str(out)}
            return {"error": "no encoder available"}

    def _writer(self):
        q = self.queue
        proc = self.proc
        while True:
            frame = q.get()
            if frame is None:
                break
            try:
                proc.stdin.write(frame)
            except Exception:
                break
        try:
            proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def feed(self, frame):
        if self.proc and self.queue is not None:
            try:
                if self.queue.full():
                    self.queue.get_nowait()  # 丢旧帧保实时
                self.queue.put_nowait(frame)
            except Exception:
                pass

    def stop(self):
        with self.lock:
            if not self.proc:
                return {"recording": False}
            q, proc, f = self.queue, self.proc, self.file
            self.proc = None
            self.queue = None
        try:
            q.put(None)
        except Exception:
            pass
        # 等待 writer 收尾
        t0 = time.monotonic()
        while time.monotonic() - t0 < 6 and proc.poll() is None:
            time.sleep(0.2)
        return {"recording": False, "file": str(f)}

    @property
    def recording(self):
        return self.proc is not None


RECORDER = Recorder()


def open_camera_process():
    cmd = [
        str(FFMPEG),
        "-hide_banner", "-loglevel", "error",
        "-f", "dshow",
        "-rtbufsize", "64M",
        "-video_size", CAM_SIZE,
        "-framerate", str(CAM_FPS),
        "-i", f"video={DSHOW_VIDEO}",
        "-an",
        "-q:v", "3",
        "-f", "mjpeg",
        "pipe:1",
    ]
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    deadline = time.monotonic() + 8.0
    data = b""
    while time.monotonic() < deadline and proc.poll() is None:
        chunk = proc.stdout.read(4096)
        if chunk:
            data += chunk
            if JPEG_SOI in data and JPEG_EOI in data:
                return proc, data
        else:
            time.sleep(0.03)
    try:
        err = proc.stderr.read().decode("utf-8", "replace").strip()
    except Exception:
        err = "camera open failed"
    proc.terminate()
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
    raise RuntimeError(err or "camera open failed")


def camera_worker():
    global CAMERA_PROCESS, CAMERA_ERROR, LATEST_FRAME, LATEST_FRAME_TIME, FRAME_COUNT
    while True:
        try:
            proc, buffered = open_camera_process()
            with CAMERA_LOCK:
                CAMERA_PROCESS = proc
                CAMERA_ERROR = ""
                FRAME_COND.notify_all()

            def combined():
                yield buffered
                while True:
                    chunk = proc.stdout.read(16384)
                    if not chunk:
                        break
                    yield chunk

            data = b""
            for chunk in combined():
                data += chunk
                while True:
                    start = data.find(JPEG_SOI)
                    if start < 0:
                        data = data[-2:]
                        break
                    end = data.find(JPEG_EOI, start + 2)
                    if end < 0:
                        data = data[start:]
                        break
                    end += 2
                    frame = data[start:end]
                    data = data[end:]
                    with CAMERA_LOCK:
                        LATEST_FRAME = frame
                        LATEST_FRAME_TIME = time.time()
                        FRAME_COUNT += 1
                        FRAME_COND.notify_all()
                    RECORDER.feed(frame)
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception as err:
            with CAMERA_LOCK:
                CAMERA_PROCESS = None
                CAMERA_ERROR = str(err)
                FRAME_COND.notify_all()
            time.sleep(1)


def ensure_camera():
    global CAMERA_THREAD
    with CAMERA_LOCK:
        alive = CAMERA_THREAD is not None and CAMERA_THREAD.is_alive()
        if not alive:
            CAMERA_THREAD = threading.Thread(target=camera_worker, daemon=True)
            CAMERA_THREAD.start()


def set_quality(size):
    global CAM_SIZE, CAMERA_PROCESS
    if size not in ("1280x720", "1920x1080"):
        raise ValueError("bad size")
    with CAMERA_LOCK:
        CAM_SIZE = size
        if CAMERA_PROCESS and CAMERA_PROCESS.poll() is None:
            CAMERA_PROCESS.terminate()  # worker 会用新尺寸重开
    return {"size": size}


def camera_status():
    ensure_camera()
    with CAMERA_LOCK:
        return {
            "source": DSHOW_VIDEO if CAMERA_PROCESS else "",
            "error": CAMERA_ERROR,
            "frames": FRAME_COUNT,
            "size": CAM_SIZE,
            "fps": CAM_FPS,
            "age": round(time.time() - LATEST_FRAME_TIME, 2) if LATEST_FRAME_TIME else None,
            "recording": RECORDER.recording,
        }


def stream_frames(handler, multipart):
    ensure_camera()
    handler.send_response(200)
    if multipart:
        handler.send_header("content-type", "multipart/x-mixed-replace; boundary=frame")
    else:
        handler.send_header("content-type", "application/octet-stream")
    handler.send_header("cache-control", "no-store")
    handler.send_header("connection", "close")
    handler.end_headers()

    last_sent = 0
    try:
        while True:
            with CAMERA_LOCK:
                FRAME_COND.wait_for(
                    lambda: (LATEST_FRAME is not None and LATEST_FRAME_TIME != last_sent) or CAMERA_ERROR,
                    timeout=5,
                )
                if CAMERA_ERROR and LATEST_FRAME is None:
                    raise RuntimeError(CAMERA_ERROR)
                jpg = LATEST_FRAME
                last_sent = LATEST_FRAME_TIME
            if not jpg:
                continue
            if multipart:
                handler.wfile.write(b"--frame\r\n")
                handler.wfile.write(b"Content-Type: image/jpeg\r\n")
                handler.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode("ascii"))
                handler.wfile.write(jpg)
                handler.wfile.write(b"\r\n")
            else:
                handler.wfile.write(jpg)
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass


def snapshot():
    ensure_camera()
    with CAMERA_LOCK:
        jpg = LATEST_FRAME
    if not jpg:
        raise RuntimeError("no frame yet")
    SNAP_DIR.mkdir(exist_ok=True)
    name = f"snap_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
    (SNAP_DIR / name).write_bytes(jpg)
    return {"url": f"/snapshots/{name}", "bytes": len(jpg)}


# ---------------- 业务逻辑 ----------------
def aim(dx, dy):
    """点击转向：dx/dy 为画面归一化偏移（-0.5~0.5），按当前 FOV 与 zoom 换算角度。"""
    try:
        pos = PTZ.get()
    except Exception as err:
        raise RuntimeError(f"ptz unavailable: {err}")
    zoom = max(100, pos.get("zoom", 100))
    with CONFIG_LOCK:
        fov_h = float(CONFIG["fov"])
    fov_h_eff = fov_h / (zoom / 100.0)
    fov_v_eff = 2 * math.degrees(math.atan(math.tan(math.radians(fov_h_eff) / 2) * 9 / 16))
    pan = pos["pan"] + dx * fov_h_eff * 1.15
    tilt = pos["tilt"] - dy * fov_v_eff * 1.15
    MOTION.goto(pan, tilt)
    return {"pan": int(pan), "tilt": int(tilt), "fov_h_eff": round(fov_h_eff, 1)}


# ---------------- 前端 ----------------
HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Link2 Studio · Insta360 云台控制平台</title>
<style>
:root {
  --bg: #0f1318; --panel: #171d24; --panel2: #1d252e;
  --border: #2c3641; --text: #e8edf2; --muted: #93a0ad;
  --accent: #22c08a; --accent2: #3d9dfd; --danger: #e5484d;
  --mono: ui-monospace, 'Cascadia Code', Consolas, monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; background: var(--bg); color: var(--text);
  font-family: -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif;
  font-size: 14px;
}
header {
  display: flex; align-items: center; gap: 14px;
  padding: 14px 22px; border-bottom: 1px solid var(--border);
  position: sticky; top: 0; background: rgba(15,19,24,.92); backdrop-filter: blur(8px); z-index: 10;
}
header h1 { margin: 0; font-size: 17px; letter-spacing: .5px; }
header h1 span { color: var(--accent); }
header .chip {
  font-size: 11px; color: var(--muted); border: 1px solid var(--border);
  padding: 2px 8px; border-radius: 20px;
}
header .chip.live { color: var(--accent); border-color: var(--accent); }
.wrap { max-width: 1240px; margin: 0 auto; padding: 18px 22px 60px; }
.layout { display: grid; grid-template-columns: minmax(0, 1fr) 360px; gap: 16px; align-items: start; }
@media (max-width: 980px) { .layout { grid-template-columns: 1fr; } }
.card {
  background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
  padding: 16px; margin-bottom: 16px;
}
.card h2 { margin: 0 0 12px; font-size: 13px; color: var(--muted); font-weight: 600; letter-spacing: 1px; text-transform: uppercase; }
/* 视频区 */
.video-wrap { position: relative; background: #000; border-radius: 8px; overflow: hidden; aspect-ratio: 16/9; }
.video-wrap canvas { display: block; width: 100%; height: 100%; object-fit: contain; cursor: crosshair; }
.hud {
  position: absolute; font: 12px/1.5 var(--mono); color: #fff;
  background: rgba(0,0,0,.55); border-radius: 6px; padding: 4px 10px; pointer-events: none;
}
.hud.tl { top: 10px; left: 10px; }
.hud.tr { top: 10px; right: 10px; text-align: right; }
.hud .rec { color: var(--danger); font-weight: 700; }
.toolbar { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; align-items: center; }
.btn {
  background: var(--panel2); color: var(--text); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 14px; font-size: 13px; cursor: pointer;
  display: inline-flex; align-items: center; gap: 6px; user-select: none;
}
.btn:hover { border-color: var(--accent); }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #06281c; font-weight: 700; }
.btn.danger { background: var(--danger); border-color: var(--danger); color: #fff; }
.btn.sm { padding: 5px 10px; font-size: 12px; }
.btn:active { transform: translateY(1px); }
select, input[type=number], input[type=text] {
  background: var(--panel2); color: var(--text); border: 1px solid var(--border);
  border-radius: 8px; padding: 7px 10px; font-size: 13px;
}
/* 控制面板 */
.grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.dpad { display: grid; grid-template-columns: repeat(3, 56px); grid-template-rows: repeat(3, 50px); gap: 8px; justify-content: center; margin: 10px 0; }
.dpad button {
  background: var(--panel2); color: var(--text); border: 1px solid var(--border);
  border-radius: 8px; font-size: 20px; cursor: pointer;
}
.dpad button:active, .dpad button.holding { background: var(--accent); color: #06281c; border-color: var(--accent); }
.dpad button.center { font-size: 12px; font-weight: 700; }
label.field { display: grid; gap: 6px; margin: 10px 0; color: var(--muted); font-size: 12px; }
label.field .val { color: var(--text); font-family: var(--mono); }
input[type=range] { width: 100%; accent-color: var(--accent); }
.row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.row.between { justify-content: space-between; }
.list { display: grid; gap: 6px; max-height: 220px; overflow-y: auto; }
.list .item {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  background: var(--panel2); border: 1px solid var(--border); border-radius: 8px; padding: 7px 10px;
}
.list .item .coords { font: 11px var(--mono); color: var(--muted); }
.status { min-height: 20px; font-size: 12px; color: var(--muted); margin-top: 8px; }
.status.err { color: var(--danger); }
details.api { margin-top: 10px; }
details.api summary { cursor: pointer; color: var(--muted); font-size: 12px; }
details.api pre {
  background: var(--panel2); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px; font: 11px/1.7 var(--mono); overflow-x: auto; color: #b8c4cf;
}
.hint { font-size: 11px; color: var(--muted); line-height: 1.8; }
.kbd {
  font: 11px var(--mono); background: var(--panel2); border: 1px solid var(--border);
  border-bottom-width: 2px; border-radius: 4px; padding: 0 5px;
}
.empty { color: var(--muted); font-size: 12px; text-align: center; padding: 14px 0; }
</style>
</head>
<body>
<header>
  <h1>Link2 <span>Studio</span></h1>
  <span class="chip" id="chipCam">连接中…</span>
  <span class="chip" id="chipPos">--</span>
  <span class="chip" id="chipPatrol" style="display:none">巡航中</span>
  <span class="chip" id="chipFace" style="display:none">👤 人脸跟踪中</span>
</header>
<div class="wrap">
  <div class="layout">
    <!-- 左：视频 -->
    <div>
      <div class="card">
        <div class="video-wrap">
          <canvas id="cv" width="1280" height="720"></canvas>
          <div class="hud tl" id="hudPos">pan --° tilt --° zoom --x</div>
          <div class="hud tr"><div id="hudFps">-- fps</div><div id="hudRec"></div></div>
        </div>
        <div class="toolbar">
          <button class="btn" id="btnSnap" title="保存当前画面">📷 截图</button>
          <button class="btn" id="btnRec" title="录制 MP4">⏺ 录像</button>
          <button class="btn" id="btnFull" title="全屏">⛶ 全屏</button>
          <span style="flex:1"></span>
          <label style="color:var(--muted);font-size:12px">分辨率</label>
          <select id="selRes"><option value="1280x720">720p</option><option value="1920x1080">1080p</option></select>
        </div>
        <div class="hint" style="margin-top:10px">
          点击画面任意位置，云台转向该点（近似）· 键盘 <span class="kbd">←↑→↓</span> 持续移动 ·
          <span class="kbd">空格</span> 回中 · <span class="kbd">+</span>/<span class="kbd">-</span> 变焦 · <span class="kbd">PgUp/PgDn</span> 俯仰
        </div>
      </div>
      <div class="card">
        <h2>开发者 API</h2>
        <details class="api" open>
          <summary>HTTP 接口一览（可用于二次开发 / 脚本联动）</summary>
          <pre>GET  /api/hello          能力与范围
GET  /api/position       当前 pan/tilt/zoom（度 / zoom 单位）
GET  /api/status         相机/录像/巡航状态
GET  /video.raw          30fps JPEG 裸流
GET  /video.mjpg         MJPEG 流（img 标签可用）
POST /api/goto           {pan, tilt, speed?} 平滑转到绝对角度
POST /api/move           {dir, step} 相对移动
POST /api/vel            {pan_vel, tilt_vel} 速度控制 deg/s
POST /api/zoom           {zoom: 100-400} 变焦
POST /api/aim            {x, y} 点击转向（归一化 -0.5~0.5）
POST /api/preset/save    {name} 保存当前位置为预设
POST /api/preset/delete  {name}
POST /api/patrol/start|stop   巡航（按预设位顺序）
POST /api/snapshot       截图，返回 {url}
POST /api/record         {on: true|false} 录像
POST /api/settings       {fov?, speed?, patrol_dwell?}
POST /api/quality        {size: "1280x720"|"1920x1080"}
GET  /api/face/status    人脸检测/跟踪状态 + 人脸框坐标（含识别姓名）
POST /api/face/detect    {on: true|false} 人脸检测开关
POST /api/face/track     {on: true|false} 人脸跟踪开关（自动追随目标）
POST /api/face/enroll    {name} 注册当前画面中的人脸（识别用）
POST /api/face/delete    {name} 删除已注册人脸
GET  /api/face/list      已注册人脸列表 + 当前锁定目标
GET  /api/face/history   识别记录（最近识别到的人脸）
POST /api/face/target    {name} 锁定某人为跟踪目标（空=清除，跟踪任意）
POST /api/face/recognize_track {on} 识别追踪：识别面前人脸并锁定跟踪
POST /api/face/upload    {name, image: base64} 上传人脸图片注册到档案库
POST /api/face/settings  {fov?, kp?, max_speed?, deadzone?} 跟踪参数</pre>
        </details>
      </div>
    </div>
    <!-- 右：控制 -->
    <div>
      <div class="card">
        <h2>云台控制</h2>
        <label class="field">速度 <span class="val" id="spdLabel">60°/s</span>
          <input id="spd" type="range" min="10" max="180" step="5" value="60">
        </label>
        <div class="dpad">
          <span></span><button data-vel="0,1">↑</button><span></span>
          <button data-vel="-1,0">←</button><button class="center" id="btnCenter">回中</button><button data-vel="1,0">→</button>
          <span></span><button data-vel="0,-1">↓</button><span></span>
        </div>
        <label class="field">Pan <span class="val" id="panLabel">0°</span>
          <input id="panRange" type="range" min="-145" max="145" step="1" value="0">
        </label>
        <label class="field">Tilt <span class="val" id="tiltLabel">0°</span>
          <input id="tiltRange" type="range" min="-90" max="100" step="1" value="0">
        </label>
        <label class="field" id="zoomField" style="display:none">Zoom <span class="val" id="zoomLabel">1.0x</span>
          <input id="zoomRange" type="range" min="100" max="400" step="10" value="100">
        </label>
      </div>
      <div class="card">
        <h2>预设位</h2>
        <div class="row">
          <input type="text" id="presetName" placeholder="预设名称" style="flex:1">
          <button class="btn sm" id="btnPresetSave">保存当前</button>
        </div>
        <div class="list" id="presetList" style="margin-top:10px"><div class="empty">暂无预设位</div></div>
      </div>
      <div class="card">
        <h2>巡航</h2>
        <div class="row between">
          <label class="field" style="margin:0">每点停留 <span class="val" id="dwellLabel">2.0s</span>
            <input id="dwell" type="range" min="0.5" max="10" step="0.5" value="2" style="width:120px">
          </label>
          <button class="btn primary" id="btnPatrol">▶ 开始巡航</button>
        </div>
        <div class="hint" style="margin-top:8px">按预设位列表顺序循环转动。</div>
      </div>
      <div class="card">
        <h2>AI 视觉 · 人脸</h2>
        <button class="btn primary" id="btnRecogTrack" style="width:100%;padding:10px;font-size:15px">🎯 识别追踪面前人脸</button>
        <div class="row" style="margin-top:8px">
          <button class="btn" id="btnFaceDet" style="flex:1">◎ 开启检测</button>
          <button class="btn" id="btnFaceTrack" style="flex:1" disabled>◎ 开启跟踪</button>
        </div>
        <label class="field">跟踪最大速度 <span class="val" id="fSpdLabel">60°/s</span>
          <input id="fSpd" type="range" min="10" max="150" step="5" value="60">
        </label>
        <div class="row">
          <input type="text" id="faceName" placeholder="人名（注册人脸）" style="flex:1">
          <button class="btn sm" id="btnEnroll">注册人脸</button>
        </div>
        <div class="status" id="faceInfo">检测未开启</div>
        <div class="hint" style="margin-top:6px">
          检测=画面叠加人脸框（蓝）；跟踪=云台自动追随目标（绿框）；注册后可识别人名。
        </div>
      </div>
      <div class="card">
        <h2>👥 人脸档案库</h2>
        <div class="hint" style="margin:0 0 8px">已注册人脸可锁定为跟踪目标（只追该人，他人入镜不追）。</div>
        <div class="list" id="faceList"><div class="empty">未注册人脸</div></div>
        <h3 style="margin:14px 0 6px;font-size:13px;color:var(--muted)">上传人脸</h3>
        <div class="row">
          <input type="text" id="uploadName" placeholder="人名" style="flex:1;min-width:0">
        </div>
        <div class="row" style="margin-top:6px">
          <input type="file" id="uploadFile" accept="image/*" style="flex:1;font-size:12px">
          <button class="btn sm" id="btnUpload">上传</button>
        </div>
        <img id="uploadPreview" style="display:none;max-width:100%;margin-top:6px;border-radius:6px">
        <h3 style="margin:14px 0 6px;font-size:13px;color:var(--muted)">识别记录 <span id="logCount" style="font-weight:normal"></span></h3>
        <div class="list" id="faceLog" style="max-height:180px;overflow:auto"><div class="empty">暂无记录</div></div>
      </div>
      <div class="card">
        <h2>设置</h2>
        <label class="field">水平视场角 FOV（点击转向换算用） <span class="val" id="fovLabel">75°</span>
          <input id="fov" type="range" min="40" max="120" step="1" value="75">
        </label>
      </div>
    </div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const state = { zoomSupported: false, presetCount: 0, dragging: null, faces: [], faceStatus: null };

async function api(path, body) {
  const opts = body ? {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(body)} : {};
  const res = await fetch(path, opts);
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || res.statusText);
  return data;
}

/* ---------- 视频：fetch 流 + canvas 渲染 ---------- */
const cv = $('cv'), ctx = cv.getContext('2d');
let fpsCount = 0, fpsTime = performance.now(), latestBmp = null, newBmp = false;

async function startVideo() {
  try {
    const res = await fetch('/video.raw');
    const reader = res.body.getReader();
    let buf = new Uint8Array(0);
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      const nb = new Uint8Array(buf.length + value.length);
      nb.set(buf); nb.set(value, buf.length);
      buf = nb;
      // 解析所有完整 JPEG 帧
      let searched = 0;
      while (true) {
        const s = buf.indexOf(0xFF, searched);
        if (s < 0 || s + 1 >= buf.length) break;
        if (buf[s+1] !== 0xD8) { searched = s + 1; continue; }
        // 找 EOI
        let e = -1;
        for (let i = s + 2; i < buf.length - 1; i++) {
          if (buf[i] === 0xFF && buf[i+1] === 0xD9) { e = i + 2; break; }
        }
        if (e < 0) break;
        const frame = buf.slice(s, e);
        buf = buf.slice(e);
        searched = 0;
        const blob = new Blob([frame], {type: 'image/jpeg'});
        createImageBitmap(blob).then(bmp => {
          if (latestBmp && latestBmp.close) latestBmp.close();
          latestBmp = bmp; newBmp = true;
        });
      }
    }
  } catch (e) { /* 流断开，1s 后重连 */ }
  setTimeout(startVideo, 1000);
}

function render() {
  if (latestBmp && newBmp) {
    if (cv.width !== latestBmp.width || cv.height !== latestBmp.height) {
      cv.width = latestBmp.width; cv.height = latestBmp.height;
    }
    ctx.drawImage(latestBmp, 0, 0);
    newBmp = false;
    fpsCount++;
  }
  drawFaces();
  requestAnimationFrame(render);
}

/* ---------- 人脸视觉：状态轮询 + 画面叠加 ---------- */
function drawFaces() {
  if (!state.faces || !state.faces.length) return;
  ctx.lineWidth = 2;
  ctx.font = 'bold 14px sans-serif';
  for (const f of state.faces) {
    const isTarget = f.target;
    ctx.strokeStyle = isTarget ? '#22c08a' : '#3d9dfd';
    ctx.strokeRect(f.x, f.y, f.w, f.h);
    if (isTarget) {  // 目标脸加十字准星
      ctx.beginPath();
      ctx.moveTo(f.x + f.w/2 - 10, f.y + f.h/2); ctx.lineTo(f.x + f.w/2 + 10, f.y + f.h/2);
      ctx.moveTo(f.x + f.w/2, f.y + f.h/2 - 10); ctx.lineTo(f.x + f.w/2, f.y + f.h/2 + 10);
      ctx.stroke();
    }
    const label = f.name ? `${f.name} ${(f.similarity||0).toFixed(2)}` : `人脸 ${(f.score||0).toFixed(2)}`;
    const tw = ctx.measureText(label).width;
    const ly = Math.max(0, f.y - 20);
    ctx.fillStyle = isTarget ? 'rgba(34,192,138,.85)' : 'rgba(61,157,253,.85)';
    ctx.fillRect(f.x, ly, tw + 10, 18);
    ctx.fillStyle = '#fff';
    ctx.fillText(label, f.x + 5, ly + 14);
  }
}

async function pollFace() {
  try {
    const f = await api('/api/face/status');
    const prevTarget = state.faceStatus && state.faceStatus.target;
    state.faceStatus = f;
    state.faces = (f.detect && f.faces) || [];
    $('btnFaceDet').textContent = f.detect ? '◉ 关闭检测' : '◎ 开启检测';
    $('btnFaceDet').classList.toggle('primary', !!f.detect);
    $('btnFaceTrack').disabled = !f.available || !f.detect;
    $('btnFaceTrack').textContent = f.track ? '◉ 关闭跟踪' : '◎ 开启跟踪';
    $('btnFaceTrack').classList.toggle('danger', !!f.track);
    const rt = $('btnRecogTrack');
    rt.textContent = f.track ? '⏹ 停止识别追踪' : '🎯 识别追踪面前人脸';
    rt.classList.toggle('danger', !!f.track);
    $('chipFace').style.display = f.track ? '' : 'none';
    $('chipFace').textContent = f.target ? `👤 跟踪 ${f.target}` : '👤 人脸跟踪中';
    if (!f.available) {
      $('faceInfo').textContent = '不可用：' + (f.error || '模型未加载');
      $('faceInfo').classList.add('err');
      return;
    }
    $('faceInfo').classList.remove('err');
    const rec = f.recognition ? '' : ' · 识别模型未加载';
    const tgt = f.target ? ` · 锁定 ${f.target}` : (f.track_target ? ' · 跟踪中' : '');
    $('faceInfo').textContent = f.detect
      ? `检测 ${f.det_ms}ms · ${f.fps}fps · 人脸 ${state.faces.length}${tgt}${rec}`
      : '检测未开启';
    if (prevTarget !== f.target) refreshFaceList();  // 目标变更时刷新高亮
  } catch (e) {}
}
setInterval(pollFace, 250);
pollFace();

$('btnFaceDet').addEventListener('click', async () => {
  const st = await api('/api/face/status').catch(() => null);
  try { await api('/api/face/detect', {on: !(st && st.detect)}); pollFace(); }
  catch (e) { showErr(e); }
});
$('btnFaceTrack').addEventListener('click', async () => {
  const st = await api('/api/face/status').catch(() => null);
  try { await api('/api/face/track', {on: !(st && st.track)}); pollFace(); }
  catch (e) { showErr(e); }
});
$('btnRecogTrack').addEventListener('click', async () => {
  const st = await api('/api/face/status').catch(() => null);
  const on = !(st && st.track);
  try {
    const r = await api('/api/face/recognize_track', {on});
    pollFace();
    if (on && r.locked) showFaceInfo(`已锁定跟踪：${r.locked}（相似度 ${(r.similarity||0).toFixed(2)}）`);
    else if (on && !r.locked) showFaceInfo(r.reason || '未识别到已注册人脸，请先注册');
  } catch (e) { showErr(e); }
});
$('fSpd').addEventListener('input', () => $('fSpdLabel').textContent = $('fSpd').value + '°/s');
$('fSpd').addEventListener('change', () =>
  api('/api/face/settings', {max_speed: Number($('fSpd').value)}).catch(showErr));
$('btnEnroll').addEventListener('click', async () => {
  const name = $('faceName').value.trim();
  if (!name) { showErr(new Error('请输入人名')); return; }
  try {
    const r = await api('/api/face/enroll', {name});
    $('faceName').value = '';
    showFaceInfo(`已注册 ${r.name}（样本 ${r.samples}）`);
    refreshFaceList();
  } catch (e) { showErr(e); }
});
function showFaceInfo(msg) {
  const el = $('faceInfo');
  el.textContent = msg; el.classList.remove('err');
  setTimeout(pollFace, 800);
}
async function refreshFaceList() {
  try {
    const data = await api('/api/face/list');
    const list = $('faceList');
    const curTarget = data.target;
    const names = Object.entries(data.faces || {});
    if (!names.length) { list.innerHTML = '<div class="empty">未注册人脸</div>'; return; }
    list.innerHTML = '';
    for (const [name, n] of names) {
      const div = document.createElement('div');
      const active = name === curTarget;
      div.className = 'item' + (active ? ' primary' : '');
      div.innerHTML = `<div><b></b><div class="coords"></div></div>
        <div style="display:flex;gap:4px"><button class="btn sm track"></button><button class="btn sm del">✕</button></div>`;
      div.querySelector('b').textContent = name + (active ? ' · 🎯' : '');
      div.querySelector('.coords').textContent = `${n} 个样本`;
      const tb = div.querySelector('.track');
      tb.textContent = active ? '取消' : '锁定跟踪';
      tb.classList.toggle('primary', active);
      tb.addEventListener('click', async () => {
        try { await api('/api/face/target', {name: active ? '' : name}); pollFace(); refreshFaceList(); }
        catch (e) { showErr(e); }
      });
      div.querySelector('.del').addEventListener('click', async () => {
        try { await api('/api/face/delete', {name}); refreshFaceList(); pollFace(); } catch (e) { showErr(e); }
      });
      list.appendChild(div);
    }
  } catch (e) {}
}
refreshFaceList();

/* ---------- 人脸档案库：上传 + 识别记录 ---------- */
$('uploadFile').addEventListener('change', () => {
  const f = $('uploadFile').files[0];
  const prev = $('uploadPreview');
  if (!f) { prev.style.display = 'none'; return; }
  prev.src = URL.createObjectURL(f);
  prev.style.display = '';
});
$('btnUpload').addEventListener('click', async () => {
  const name = $('uploadName').value.trim();
  const file = $('uploadFile').files[0];
  if (!name) { showErr(new Error('请输入人名')); return; }
  if (!file) { showErr(new Error('请选择图片')); return; }
  const reader = new FileReader();
  reader.onload = async () => {
    const b64 = reader.result;  // data:image/...;base64,xxx
    try {
      const r = await api('/api/face/upload', {name, image: b64});
      showFaceInfo(`上传成功：${r.name}（样本 ${r.samples}）`);
      $('uploadName').value = ''; $('uploadFile').value = '';
      $('uploadPreview').style.display = 'none';
      refreshFaceList();
    } catch (e) { showErr(e); }
  };
  reader.onerror = () => showErr(new Error('读取文件失败'));
  reader.readAsDataURL(file);
});
async function refreshFaceLog() {
  try {
    const d = await api('/api/face/history');
    const log = d.log || [];
    $('logCount').textContent = log.length ? `(${log.length})` : '';
    const el = $('faceLog');
    if (!log.length) { el.innerHTML = '<div class="empty">暂无记录</div>'; return; }
    el.innerHTML = '';
    for (const e of log.slice(-30).reverse()) {
      const div = document.createElement('div');
      div.className = 'item';
      const t = new Date(e.time * 1000).toLocaleTimeString();
      const act = e.name === (state.faceStatus && state.faceStatus.target) ? '🎯' : '👤';
      div.innerHTML = `<div><b>${act} ${e.name}</b><div class="coords">${t} · 相似度 ${(e.similarity||0).toFixed(2)}</div></div>`;
      el.appendChild(div);
    }
  } catch (e) {}
}
setInterval(refreshFaceLog, 1000);
refreshFaceLog();

setInterval(() => {
  const now = performance.now();
  const fps = fpsCount * 1000 / (now - fpsTime);
  $('hudFps').textContent = fps.toFixed(1) + ' fps';
  fpsCount = 0; fpsTime = now;
}, 1000);

/* ---------- 状态轮询 ---------- */
async function poll() {
  try {
    const [pos, st] = await Promise.all([api('/api/position'), api('/api/status')]);
    $('hudPos').textContent = `pan ${pos.pan}° tilt ${pos.tilt}° zoom ${(pos.zoom/100).toFixed(1)}x`;
    $('chipPos').textContent = `${pos.pan}°, ${pos.tilt}°`;
    $('chipCam').textContent = st.error ? '相机错误' : (st.frames ? '● 推流中' : '启动中…');
    $('chipCam').classList.toggle('live', !st.error && !!st.frames);
    $('chipPatrol').style.display = st.patrol ? '' : 'none';
    $('btnPatrol').textContent = st.patrol ? '⏹ 停止巡航' : '▶ 开始巡航';
    $('btnPatrol').classList.toggle('danger', !!st.patrol);
    $('hudRec').innerHTML = st.recording ? '<span class="rec">● REC</span>' : '';
    $('btnRec').textContent = st.recording ? '⏹ 停止录像' : '⏺ 录像';
    $('btnRec').classList.toggle('danger', !!st.recording);
    // 同步滑块（未被拖动时）
    syncRange('panRange', 'panLabel', pos.pan, '°');
    syncRange('tiltRange', 'tiltLabel', pos.tilt, '°');
    if (state.zoomSupported) syncRange('zoomRange', 'zoomLabel', pos.zoom / 100, 'x', 1);
  } catch (e) {}
}
function syncRange(id, labelId, val, unit, fix) {
  const el = $(id);
  if (state.dragging === id) return;
  el.value = val;
  $(labelId).textContent = (fix !== undefined ? val.toFixed(fix) : val) + unit;
}
setInterval(poll, 250);

/* ---------- 控制 ---------- */
function speedVal() { return Number($('spd').value); }
$('spd').addEventListener('input', () => $('spdLabel').textContent = $('spd').value + '°/s');

document.querySelectorAll('.dpad button[data-vel]').forEach(b => {
  const [vx, vy] = b.dataset.vel.split(',').map(Number);
  const start = e => {
    e.preventDefault();
    b.classList.add('holding');
    api('/api/vel', {pan_vel: vx * speedVal(), tilt_vel: vy * speedVal()}).catch(()=>{});
  };
  const stop = () => {
    b.classList.remove('holding');
    api('/api/vel', {pan_vel: 0, tilt_vel: 0}).catch(()=>{});
  };
  b.addEventListener('pointerdown', start);
  b.addEventListener('pointerup', stop);
  b.addEventListener('pointerleave', stop);
  b.addEventListener('pointercancel', stop);
});
window.addEventListener('blur', () => api('/api/vel', {pan_vel:0, tilt_vel:0}).catch(()=>{}));

$('btnCenter').addEventListener('click', () => api('/api/goto', {pan:0, tilt:0}).catch(showErr));

$('panRange').addEventListener('pointerdown', () => state.dragging = 'panRange');
$('panRange').addEventListener('pointerup', () => state.dragging = null);
$('panRange').addEventListener('change', e => api('/api/goto', {pan: Number(e.target.value), tilt: null}).catch(showErr));
$('tiltRange').addEventListener('pointerdown', () => state.dragging = 'tiltRange');
$('tiltRange').addEventListener('pointerup', () => state.dragging = null);
$('tiltRange').addEventListener('change', e => api('/api/goto', {pan: null, tilt: Number(e.target.value)}).catch(showErr));

if ($('zoomField')) {
  $('zoomRange').addEventListener('pointerdown', () => state.dragging = 'zoomRange');
  $('zoomRange').addEventListener('pointerup', () => state.dragging = null);
  $('zoomRange').addEventListener('input', e => {
    $('zoomLabel').textContent = (e.target.value / 100).toFixed(1) + 'x';
  });
  $('zoomRange').addEventListener('change', e => api('/api/zoom', {zoom: Number(e.target.value)}).catch(showErr));
}

/* 点击转向 */
cv.addEventListener('click', e => {
  const r = cv.getBoundingClientRect();
  const x = (e.clientX - r.left) / r.width - 0.5;
  const y = (e.clientY - r.top) / r.height - 0.5;
  api('/api/aim', {x, y}).catch(showErr);
});

/* 键盘 */
const held = {};
function applyHeld() {
  const vx = (held.ArrowRight ? 1 : 0) - (held.ArrowLeft ? 1 : 0);
  const vy = (held.ArrowUp ? 1 : 0) - (held.ArrowDown ? 1 : 0);
  if (state.faceStatus && state.faceStatus.track)  // 手动接管，自动停识别追踪
    api('/api/face/recognize_track', {on: false}).then(pollFace).catch(()=>{});
  if (vx || vy) api('/api/vel', {pan_vel: vx * speedVal(), tilt_vel: vy * speedVal()}).catch(()=>{});
  else api('/api/vel', {pan_vel: 0, tilt_vel: 0}).catch(()=>{});
}
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  if (['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(e.key)) {
    e.preventDefault(); held[e.key] = true; applyHeld();
  } else if (e.key === ' ') { e.preventDefault(); api('/api/goto', {pan:0, tilt:0}).catch(showErr); }
  else if (e.key === 'PageUp') { e.preventDefault(); api('/api/move', {dir:'up', step:15}).catch(showErr); }
  else if (e.key === 'PageDown') { e.preventDefault(); api('/api/move', {dir:'down', step:15}).catch(showErr); }
  else if (e.key === '+' || e.key === '=') { zoomStep(25); }
  else if (e.key === '-') { zoomStep(-25); }
});
document.addEventListener('keyup', e => {
  if (['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(e.key)) { held[e.key] = false; applyHeld(); }
});
async function zoomStep(dz) {
  if (!state.zoomSupported) return;
  try {
    const pos = await api('/api/position');
    const nz = Math.max(100, Math.min(400, pos.zoom + dz));
    await api('/api/zoom', {zoom: nz});
  } catch (e) {}
}

/* ---------- 工具栏 ---------- */
$('btnSnap').addEventListener('click', async () => {
  try {
    const r = await api('/api/snapshot', {});
    const a = document.createElement('a');
    a.href = r.url; a.download = r.url.split('/').pop(); a.click();
  } catch (e) { showErr(e); }
});
$('btnRec').addEventListener('click', () => api('/api/record', {on: null}).catch(showErr));
$('btnFull').addEventListener('click', () => {
  const w = cv.parentElement;
  if (document.fullscreenElement) document.exitFullscreen();
  else w.requestFullscreen && w.requestFullscreen();
});
$('selRes').addEventListener('change', async e => {
  try {
    await api('/api/quality', {size: e.target.value});
    setTimeout(startVideo, 1500);
  } catch (err) { showErr(err); }
});

/* ---------- 预设位 ---------- */
async function refreshPresets() {
  try {
    const data = await api('/api/presets');
    const list = $('presetList');
    state.presetCount = Object.keys(data.presets).length;
    if (!state.presetCount) { list.innerHTML = '<div class="empty">暂无预设位</div>'; return; }
    list.innerHTML = '';
    for (const [name, p] of Object.entries(data.presets)) {
      const div = document.createElement('div');
      div.className = 'item';
      div.innerHTML = `<div><b></b><div class="coords">${p.pan}°, ${p.tilt}°${p.zoom ? ', ' + (p.zoom/100).toFixed(1) + 'x' : ''}</div></div>
        <div class="row"><button class="btn sm go">转到</button><button class="btn sm del">✕</button></div>`;
      div.querySelector('b').textContent = name;
      div.querySelector('.go').addEventListener('click', async () => {
        try { await api('/api/goto', {pan: p.pan, tilt: p.tilt}); if (p.zoom) await api('/api/zoom', {zoom: p.zoom}); } catch (e) { showErr(e); }
      });
      div.querySelector('.del').addEventListener('click', async () => {
        try { await api('/api/preset/delete', {name}); refreshPresets(); } catch (e) { showErr(e); }
      });
      list.appendChild(div);
    }
  } catch (e) {}
}
$('btnPresetSave').addEventListener('click', async () => {
  const name = $('presetName').value.trim();
  if (!name) { showErr(new Error('请输入预设名称')); return; }
  try { await api('/api/preset/save', {name}); $('presetName').value = ''; refreshPresets(); }
  catch (e) { showErr(e); }
});
refreshPresets();

/* ---------- 巡航 ---------- */
$('btnPatrol').addEventListener('click', async () => {
  const st = await api('/api/status');
  try { await api('/api/patrol/' + (st.patrol ? 'stop' : 'start'), {}); }
  catch (e) { showErr(e); }
});
$('dwell').addEventListener('input', () => $('dwellLabel').textContent = Number($('dwell').value).toFixed(1) + 's');
$('dwell').addEventListener('change', () => api('/api/settings', {patrol_dwell: Number($('dwell').value)}).catch(showErr));

/* ---------- 设置 ---------- */
$('fov').addEventListener('input', () => $('fovLabel').textContent = $('fov').value + '°');
$('fov').addEventListener('change', () => api('/api/settings', {fov: Number($('fov').value)}).catch(showErr));

function showErr(e) { console.error(e); }

/* ---------- 初始化 ---------- */
(async function init() {
  try {
    const h = await api('/api/hello');
    if (h.zoom_supported) {
      state.zoomSupported = true;
      $('zoomField').style.display = '';
      $('zoomRange').min = h.zoom_min; $('zoomRange').max = h.zoom_max;
    }
    $('dwell').value = h.settings.patrol_dwell;
    $('dwellLabel').textContent = Number(h.settings.patrol_dwell).toFixed(1) + 's';
    $('fov').value = h.settings.fov;
    $('fovLabel').textContent = h.settings.fov + '°';
    $('spd').value = h.settings.speed;
    $('spdLabel').textContent = h.settings.speed + '°/s';
  } catch (e) {}
  startVideo();
  render();
  poll();
})();
</script>
</body>
</html>
"""


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        return

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("content-length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def send_file(self, path, ctype):
        try:
            body = path.read_bytes()
        except OSError:
            self.send_json({"error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/video.mjpg":
            try:
                stream_frames(self, multipart=True)
            except Exception:
                pass
            return
        if path == "/video.raw":
            try:
                stream_frames(self, multipart=False)
            except Exception:
                pass
            return
        if path == "/api/hello":
            try:
                r = PTZ.ranges if PTZ.ranges else []
                while len(r) < 8:
                    r = PTZ.command("RANGE")
            except Exception as err:
                self.send_json({"error": str(err)}, 500)
                return
            self.send_json({
                "device": DSHOW_VIDEO,
                "pan_min": r[0], "pan_max": r[1],
                "tilt_min": r[2], "tilt_max": r[3],
                "zoom_supported": r[4] > 0,
                "zoom_min": r[4], "zoom_max": r[5],
                "fps": CAM_FPS,
                "settings": CONFIG,
            })
            return
        if path == "/api/position":
            try:
                pos = PTZ.get()
                pos.update(MOTION.state())
                self.send_json(pos)
            except Exception as err:
                self.send_json({"error": str(err)}, 500)
            return
        if path == "/api/status":
            st = camera_status()
            st["patrol"] = patrol_status()["active"]
            self.send_json(st)
            return
        if path == "/api/presets":
            with CONFIG_LOCK:
                self.send_json({"presets": CONFIG["presets"]})
            return
        if path == "/api/face/status":
            if FACE is None:
                self.send_json({"available": False, "error": "face module not loaded"})
            else:
                self.send_json(FACE.status())
            return
        if path == "/api/face/list":
            if FACE is None:
                self.send_json({"error": "face module not loaded"}, 500)
            else:
                self.send_json({"faces": FACE.enrolled(),
                                "recognition": FACE.recognition_available,
                                "target": FACE.get_target()})
            return
        if path == "/api/face/history":
            if FACE is None:
                self.send_json({"error": "face module not loaded"}, 500)
            else:
                self.send_json({"log": FACE.recognition_log()})
            return
        if path.startswith("/snapshots/"):
            name = path.split("/")[-1]
            if not re.fullmatch(r"[\w.-]+", name):
                self.send_json({"error": "bad name"}, 400)
                return
            self.send_file(SNAP_DIR / name, "image/jpeg")
            return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        global PATROL
        path = urlparse(self.path).path
        try:
            if path == "/api/move":
                payload = self.read_json()
                step = max(1, min(45, int(payload.get("step", 10))))
                pos = PTZ.get()
                pan, tilt = pos["pan"], pos["tilt"]
                d = payload.get("dir", "")
                if d == "left": pan -= step
                elif d == "right": pan += step
                elif d == "up": tilt += step
                elif d == "down": tilt -= step
                elif d == "center": pan = tilt = 0
                else:
                    raise ValueError("unknown dir")
                MOTION.goto(pan, tilt)
                self.send_json({"pan": pan, "tilt": tilt})
                return
            if path == "/api/goto":
                payload = self.read_json()
                if payload.get("pan") is None or payload.get("tilt") is None:
                    cur = PTZ.get()
                    pan = payload.get("pan", cur["pan"])
                    tilt = payload.get("tilt", cur["tilt"])
                else:
                    pan, tilt = payload["pan"], payload["tilt"]
                speed = payload.get("speed")
                MOTION.goto(pan, tilt, speed=speed)
                self.send_json({"pan": pan, "tilt": tilt})
                return
            if path == "/api/vel":
                payload = self.read_json()
                MOTION.set_vel(payload.get("pan_vel", 0), payload.get("tilt_vel", 0))
                self.send_json(MOTION.state())
                return
            if path == "/api/zoom":
                payload = self.read_json()
                z = PTZ.zoom(payload.get("zoom", 100))
                self.send_json({"zoom": z})
                return
            if path == "/api/aim":
                payload = self.read_json()
                self.send_json(aim(float(payload.get("x", 0)), float(payload.get("y", 0))))
                return
            if path == "/api/preset/save":
                payload = self.read_json()
                name = str(payload.get("name", "")).strip()
                if not name:
                    raise ValueError("name required")
                pos = PTZ.get()
                with CONFIG_LOCK:
                    CONFIG["presets"][name] = {"pan": pos["pan"], "tilt": pos["tilt"], "zoom": pos["zoom"]}
                save_config()
                self.send_json({"ok": True, "name": name})
                return
            if path == "/api/preset/delete":
                payload = self.read_json()
                name = str(payload.get("name", ""))
                with CONFIG_LOCK:
                    CONFIG["presets"].pop(name, None)
                save_config()
                self.send_json({"ok": True})
                return
            if path == "/api/patrol/start":
                if PATROL and PATROL.active:
                    self.send_json({"ok": True, "already": True})
                    return
                if FACE is not None and FACE.track_on:  # 巡航与人脸跟踪互斥
                    FACE.set_track(False)
                PATROL = PatrolThread()
                PATROL.start()
                self.send_json({"ok": True, "active": True})
                return
            if path == "/api/patrol/stop":
                if PATROL:
                    PATROL.stop()
                    MOTION.stop_motion()
                self.send_json({"ok": True, "active": False})
                return
            if path == "/api/snapshot":
                self.send_json(snapshot())
                return
            if path == "/api/record":
                payload = self.read_json()
                on = payload.get("on")
                if on is None:
                    on = not RECORDER.recording
                self.send_json(RECORDER.start() if on else RECORDER.stop())
                return
            if path == "/api/settings":
                payload = self.read_json()
                with CONFIG_LOCK:
                    if "fov" in payload:
                        CONFIG["fov"] = float(payload["fov"])
                    if "patrol_dwell" in payload:
                        CONFIG["patrol_dwell"] = float(payload["patrol_dwell"])
                    if "speed" in payload:
                        CONFIG["speed"] = float(payload["speed"])
                        MOTION.speed = CONFIG["speed"]
                save_config()
                self.send_json({"ok": True, "settings": CONFIG})
                return
            if path == "/api/quality":
                payload = self.read_json()
                self.send_json(set_quality(payload.get("size", "1280x720")))
                return
            if path == "/api/face/detect":
                if FACE is None:
                    raise RuntimeError("face module not loaded")
                payload = self.read_json()
                self.send_json(FACE.set_detect(bool(payload.get("on", True))))
                return
            if path == "/api/face/track":
                if FACE is None:
                    raise RuntimeError("face module not loaded")
                payload = self.read_json()
                on = bool(payload.get("on", True))
                if on and PATROL and PATROL.active:  # 跟踪与巡航互斥
                    PATROL.stop()
                    MOTION.stop_motion()
                self.send_json(FACE.set_track(on))
                return
            if path == "/api/face/enroll":
                if FACE is None:
                    raise RuntimeError("face module not loaded")
                payload = self.read_json()
                self.send_json(FACE.enroll(payload.get("name")))
                return
            if path == "/api/face/delete":
                if FACE is None:
                    raise RuntimeError("face module not loaded")
                payload = self.read_json()
                self.send_json(FACE.delete_face(payload.get("name")))
                return
            if path == "/api/face/settings":
                if FACE is None:
                    raise RuntimeError("face module not loaded")
                payload = self.read_json()
                self.send_json(FACE.update_settings(
                    fov=payload.get("fov"), kp=payload.get("kp"),
                    max_speed=payload.get("max_speed"),
                    deadzone=payload.get("deadzone")))
                return
            if path == "/api/face/target":
                # 锁定跟踪目标为某已注册人名；name 留空=清除（跟踪任意最大脸）
                if FACE is None:
                    raise RuntimeError("face module not loaded")
                payload = self.read_json()
                name = payload.get("name")
                if name:  # 锁定目标时自动开检测+跟踪
                    FACE.set_detect(True)
                    if PATROL and PATROL.active:
                        PATROL.stop(); MOTION.stop_motion()
                    FACE.set_track(True)
                FACE.set_target(name)
                st = FACE.status()
                self.send_json({"target": st["target"],
                                "detect": st["detect"], "track": st["track"]})
                return
            if path == "/api/face/recognize_track":
                # "识别追踪"按键：识别面前的人并锁定跟踪
                if FACE is None:
                    raise RuntimeError("face module not loaded")
                payload = self.read_json()
                on = bool(payload.get("on", True))
                if on:
                    FACE.set_detect(True)
                    if PATROL and PATROL.active:
                        PATROL.stop(); MOTION.stop_motion()
                    FACE.set_track(True)
                    res = FACE.recognize_and_lock()
                    self.send_json({"on": True, **res})
                else:
                    FACE.set_track(False)
                    FACE.set_target(None)
                    self.send_json({"on": False})
                return
            if path == "/api/face/upload":
                # 上传人脸图片注册到档案库；JSON {name, image: base64(data:...或纯base64)}
                if FACE is None:
                    raise RuntimeError("face module not loaded")
                payload = self.read_json()
                name = payload.get("name")
                img = payload.get("image") or payload.get("data") or ""
                if img.startswith("data:"):
                    img = img.split(",", 1)[1]
                import base64
                try:
                    raw = base64.b64decode(img)
                except Exception:
                    raise RuntimeError("image 不是合法 base64")
                self.send_json(FACE.enroll_from_image(name, raw))
                return
        except Exception as err:
            self.send_json({"error": str(err)}, 500)
            return
        self.send_json({"error": "not found"}, 404)


def main():
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    load_config()
    MOTION.start()
    if FACE is not None:
        FACE.update_settings(fov=CONFIG["fov"])
        FACE.start()
    ensure_camera()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Link2 Studio: http://127.0.0.1:{port}")
    print("Ctrl+C 停止")
    try:
        server.serve_forever()
    finally:
        PTZ.stop()


if __name__ == "__main__":
    main()
