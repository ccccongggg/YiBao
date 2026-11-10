#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ptz_server_win.py - Insta360 Link 2 Web 控制台（Windows 版）

移植自官方 macOS 版 ptz_server.py：
  - 视频采集：ffmpeg -f dshow（DirectShow）
  - 云台控制：ptz_dshow.exe（DirectShow IAMCameraControl，单位=度）
  - 视频推流：MJPEG (multipart/x-mixed-replace)，单 ffmpeg 进程多浏览器共享

用法：
  python ptz_server_win.py [端口]
  浏览器打开 http://127.0.0.1:8787

仅依赖 Python 3 标准库 + ffmpeg + 同目录 ptz_dshow.exe。
"""
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
PTZ_TOOL = ROOT / "ptz_dshow.exe"
FFMPEG = Path(r"C:\Users\Cccong\link2-tools\ffmpeg\bin\ffmpeg.exe")

# 单位：度（ptz_dshow.exe 的 IAMCameraControl 天然用度）
PAN_MIN, PAN_MAX = -145, 145
TILT_MIN, TILT_MAX = -90, 100
DEFAULT_STEP = 10          # 度
STEP_MIN, STEP_MAX = 1, 45
PTZ_TIMEOUT = 6

DSHOW_VIDEO = "Insta360 Link 2"

# ---------------- 云台控制（串行化，避免并发 COM 冲突） ----------------
PTZ_LOCK = threading.Lock()


def run_ptz(args):
    proc = subprocess.run(
        [str(PTZ_TOOL)] + [str(a) for a in args],
        cwd=str(ROOT), text=True, capture_output=True, timeout=PTZ_TIMEOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "ptz failed").strip())
    return json.loads(proc.stdout)


def current_position():
    with PTZ_LOCK:
        return run_ptz(["j"])


def set_position(pan, tilt):
    pan = max(PAN_MIN, min(PAN_MAX, int(pan)))
    tilt = max(TILT_MIN, min(TILT_MAX, int(tilt)))
    with PTZ_LOCK:
        return run_ptz([pan, tilt])


def move_position(direction, step):
    if direction == "center":
        return set_position(0, 0)
    pos = current_position()
    pan, tilt = int(pos["pan"]), int(pos["tilt"])
    if direction == "left":
        pan -= step
    elif direction == "right":
        pan += step
    elif direction == "up":
        tilt += step
    elif direction == "down":
        tilt -= step
    else:
        raise ValueError("unknown direction")
    return set_position(pan, tilt)


# ---------------- 视频采集（单 ffmpeg 进程，最新帧共享） ----------------
JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"

CAMERA_PROCESS = None
CAMERA_THREAD = None
CAMERA_LOCK = threading.Lock()
FRAME_COND = threading.Condition(CAMERA_LOCK)
LATEST_FRAME = None
LATEST_FRAME_TIME = 0
FRAME_COUNT = 0
CAMERA_ERROR = ""


def open_camera_process():
    cmd = [
        str(FFMPEG),
        "-hide_banner", "-loglevel", "error",
        "-f", "dshow",
        "-rtbufsize", "64M",
        "-video_size", "1280x720",
        "-framerate", "30",
        "-i", f"video={DSHOW_VIDEO}",
        "-an",
        "-vf", "fps=15",
        "-q:v", "4",
        "-f", "mjpeg",
        "pipe:1",
    ]
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    deadline = time.monotonic() + 8.0   # dshow 打开较慢
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
        proc.wait(timeout=1)
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


def camera_status():
    ensure_camera()
    with CAMERA_LOCK:
        return {
            "source": DSHOW_VIDEO if CAMERA_PROCESS else "",
            "error": CAMERA_ERROR,
            "frames": FRAME_COUNT,
            "age": round(time.time() - LATEST_FRAME_TIME, 2) if LATEST_FRAME_TIME else None,
        }


def stream_video(handler):
    ensure_camera()
    handler.send_response(200)
    handler.send_header("content-type", "multipart/x-mixed-replace; boundary=frame")
    handler.send_header("cache-control", "no-store")
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
            handler.wfile.write(b"--frame\r\n")
            handler.wfile.write(b"Content-Type: image/jpeg\r\n")
            handler.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode("ascii"))
            handler.wfile.write(jpg)
            handler.wfile.write(b"\r\n")
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass


# ---------------- 前端页面 ----------------
HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Insta360 Link 2 云台控制台</title>
  <style>
    :root {
      color-scheme: light dark;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      background: #f6f7f8;
      color: #202124;
    }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; }
    main { width: min(1020px, calc(100vw - 32px)); padding: 28px; box-sizing: border-box; }
    h1 { margin: 0 0 18px; font-size: 24px; font-weight: 700; }
    h1 small { font-size: 13px; font-weight: 400; color: #6b7683; margin-left: 10px; }
    .panel {
      background: #fff; border: 1px solid #d7dce1; border-radius: 8px;
      padding: 22px; box-shadow: 0 12px 34px rgba(0,0,0,.08);
    }
    .layout {
      display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(320px, .65fr);
      gap: 18px; align-items: start;
    }
    .video-wrap {
      overflow: hidden; background: #111418; border: 1px solid #c7cdd4;
      border-radius: 8px; aspect-ratio: 16 / 9; position: relative;
    }
    .video-wrap img { display: block; width: 100%; height: 100%; object-fit: cover; }
    .video-hud {
      position: absolute; left: 10px; bottom: 10px;
      background: rgba(0,0,0,.55); color: #fff; border-radius: 6px;
      font: 12px/1.6 ui-monospace, Menlo, Consolas, monospace;
      padding: 4px 10px; pointer-events: none;
    }
    .grid {
      display: grid; grid-template-columns: repeat(3, minmax(72px, 88px));
      grid-template-rows: repeat(3, 64px); gap: 10px; justify-content: center;
      margin: 18px 0 10px;
    }
    button {
      border: 1px solid #c7cdd4; background: #f7f8fa; color: #202124;
      border-radius: 8px; font-size: 26px; font-weight: 650; cursor: pointer; min-width: 0;
    }
    button:active { transform: translateY(1px); background: #e8edf2; }
    button.primary { background: #1d6f5f; border-color: #1d6f5f; color: #fff; font-size: 15px; }
    .controls { display: grid; gap: 14px; }
    label { display: grid; gap: 8px; font-size: 14px; color: #4b5560; }
    input[type="range"] { width: 100%; }
    .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
    code {
      font: 13px ui-monospace, Menlo, Consolas, monospace;
      background: #eef1f4; padding: 4px 7px; border-radius: 6px;
    }
    .status { min-height: 22px; color: #4b5560; font-size: 14px; }
    .presets { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
    .presets button { font-size: 13px; padding: 8px 4px; }
    .hint { font-size: 12px; color: #8a939e; }
    @media (max-width: 840px) { .layout { grid-template-columns: 1fr; } }
    @media (prefers-color-scheme: dark) {
      :root { background: #111418; color: #eef1f4; }
      .panel { background: #191e24; border-color: #313943; box-shadow: none; }
      .video-wrap { border-color: #3a4450; }
      button { background: #222932; border-color: #3a4450; color: #eef1f4; }
      button:active { background: #2b3440; }
      code { background: #252c35; }
      label, .status { color: #a9b3bd; }
    }
  </style>
</head>
<body>
  <main>
    <h1>Insta360 Link 2 云台控制台 <small>Windows · DirectShow</small></h1>
    <section class="layout">
      <div class="panel">
        <div class="video-wrap">
          <img id="video" src="/video.mjpg" alt="实时画面">
          <div class="video-hud" id="hud">pan --° tilt --°</div>
        </div>
      </div>
      <div class="panel">
        <div class="row">
          <div>当前位置 <code id="pos">读取中...</code></div>
          <button class="primary" id="refresh">刷新</button>
        </div>
        <div class="row">
          <div>视频 <code id="camstat">检测中...</code></div>
        </div>

        <div class="grid" aria-label="云台方向控制">
          <span></span><button data-move="up" title="上">↑</button><span></span>
          <button data-move="left" title="左">←</button><button class="primary" data-move="center">回中</button><button data-move="right" title="右">→</button>
          <span></span><button data-move="down" title="下">↓</button><span></span>
        </div>

        <div class="controls">
          <label>
            步长 <span><code id="stepLabel">10</code>°</span>
            <input id="step" type="range" min="1" max="45" step="1" value="10">
          </label>
          <div class="presets">
            <button data-goto="-90,0">左 90°</button>
            <button data-goto="0,0">正前</button>
            <button data-goto="90,0">右 90°</button>
            <button data-goto="0,-60">俯视</button>
            <button data-goto="0,45">仰视</button>
            <button data-goto="0,90">天花板</button>
          </div>
          <label>
            绝对定位 pan/tilt（度）
            <div class="row">
              <input id="panIn" type="number" min="-145" max="145" value="0" style="width:90px">
              <input id="tiltIn" type="number" min="-90" max="100" value="0" style="width:90px">
              <button id="gotoBtn" style="font-size:14px;padding:6px 14px">转到</button>
            </div>
          </label>
          <div class="status" id="status"></div>
          <div class="hint">提示：键盘 ←↑→↓ 移动，空格回中，PgUp/PgDn 俯仰</div>
        </div>
      </div>
    </section>
  </main>
  <script>
    const posEl = document.getElementById('pos');
    const hudEl = document.getElementById('hud');
    const statusEl = document.getElementById('status');
    const stepEl = document.getElementById('step');
    const stepLabel = document.getElementById('stepLabel');
    const camstatEl = document.getElementById('camstat');

    let busy = false;

    function setStatus(text) { statusEl.textContent = text || ''; }

    async function api(path, options) {
      const res = await fetch(path, options);
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || res.statusText);
      return data;
    }

    function showPosition(d) {
      posEl.textContent = `pan ${d.pan}°, tilt ${d.tilt}°`;
      hudEl.textContent = `pan ${d.pan}° tilt ${d.tilt}°`;
    }

    async function refresh() {
      try {
        showPosition(await api('/api/position'));
        setStatus('');
      } catch (err) { setStatus(err.message); }
    }

    async function refreshCam() {
      try {
        const d = await api('/api/camera');
        if (d.error) { camstatEl.textContent = '错误'; setStatus(d.error); }
        else if (d.frames) { camstatEl.textContent = `推流中 · ${d.frames} 帧`; }
        else camstatEl.textContent = '启动中...';
      } catch (err) { camstatEl.textContent = '未知'; }
    }

    async function move(dir, step) {
      if (busy) return;
      busy = true;
      try {
        setStatus('移动中...');
        const d = await api('/api/move', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({dir, step})
        });
        showPosition(d);
        setStatus('完成');
      } catch (err) { setStatus(err.message); }
      busy = false;
    }

    async function goto(pan, tilt) {
      if (busy) return;
      busy = true;
      try {
        setStatus('转动中...');
        const d = await api('/api/goto', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({pan, tilt})
        });
        showPosition(d);
        setStatus('完成');
      } catch (err) { setStatus(err.message); }
      busy = false;
    }

    stepEl.addEventListener('input', () => stepLabel.textContent = stepEl.value);
    document.getElementById('refresh').addEventListener('click', refresh);
    document.querySelectorAll('button[data-move]').forEach(b => {
      b.addEventListener('click', () => move(b.dataset.move, Number(stepEl.value)));
    });
    document.querySelectorAll('button[data-goto]').forEach(b => {
      b.addEventListener('click', () => {
        const [p, t] = b.dataset.goto.split(',').map(Number);
        goto(p, t);
      });
    });
    document.getElementById('gotoBtn').addEventListener('click', () => {
      goto(Number(document.getElementById('panIn').value), Number(document.getElementById('tiltIn').value));
    });

    document.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT') return;
      const s = Number(stepEl.value);
      const map = {
        ArrowUp: ['up', s], ArrowDown: ['down', s],
        ArrowLeft: ['left', s], ArrowRight: ['right', s],
        PageUp: ['up', 20], PageDown: ['down', 20],
        ' ': ['center', 0],
      };
      if (map[e.key]) { e.preventDefault(); move(...map[e.key]); }
    });

    refresh();
    setInterval(refresh, 1500);
    refreshCam();
    setInterval(refreshCam, 2000);
  </script>
</body>
</html>
"""


# ---------------- HTTP 服务 ----------------
class Handler(BaseHTTPRequestHandler):
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
                stream_video(self)
            except Exception:
                pass
            return
        if path == "/api/position":
            try:
                self.send_json(current_position())
            except Exception as err:
                self.send_json({"error": str(err)}, 500)
            return
        if path == "/api/camera":
            self.send_json(camera_status())
            return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/move":
                payload = self.read_json()
                step = max(STEP_MIN, min(STEP_MAX, int(payload.get("step", DEFAULT_STEP))))
                self.send_json(move_position(str(payload.get("dir", "")), step))
                return
            if path == "/api/goto":
                payload = self.read_json()
                self.send_json(set_position(payload.get("pan", 0), payload.get("tilt", 0)))
                return
        except Exception as err:
            self.send_json({"error": str(err)}, 500)
            return
        self.send_json({"error": "not found"}, 404)


def main():
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    ensure_camera()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"PTZ control UI: http://127.0.0.1:{port}")
    print("按 Ctrl+C 停止")
    server.serve_forever()


if __name__ == "__main__":
    main()
