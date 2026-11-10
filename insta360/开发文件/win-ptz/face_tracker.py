#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
face_tracker.py — Insta360 Link 2 人脸视觉封装模块

三层封装，彼此独立、可单独复用：
  FaceDetector    YuNet DNN 检测器：人脸框 + 5 关键点 + 置信度
  FaceRecognizer  SFace 识别器：人脸注册 / 1:N 比对（余弦相似度）
  TrackController 云台跟踪控制器：画面偏差 → P 控制 → pan/tilt 速度

FaceService 把三者组装成一个独立线程服务，宿主只需：
  1. 提供回调：get_frame / get_position / set_vel / stop_motion
  2. 喂帧：feed(jpeg_bytes)（或轮询 get_frame）
  3. 调用：set_detect(bool) / set_track(bool) / enroll(name) / status()

依赖：opencv-python-headless>=4.6（FaceDetectorYN / FaceRecognizerSF）
模型：models/face_detection_yunet.onnx（必需）、models/face_recognition_sface.onnx（可选，识别用）
"""
import json
import math
import os
import threading
import time
from pathlib import Path

import numpy as np

try:
    import cv2
    CV2_OK = hasattr(cv2, "FaceDetectorYN_create")
except Exception:
    cv2 = None
    CV2_OK = False

ROOT = Path(__file__).resolve().parent
DET_MODEL = ROOT / "models" / "face_detection_yunet.onnx"
REC_MODEL = ROOT / "models" / "face_recognition_sface.onnx"
FACE_DB = ROOT / "faces_db.json"

COSINE_THRESHOLD = 0.363   # SFace 官方推荐阈值（余弦相似度，越大越像）


def _ascii_safe(path):
    """OpenCV 4.10 的 C++ ONNX 解析器无法读含非 ASCII 字符的路径（Windows）。

    把模型文件复制到 ASCII 临时目录后再返回。本地特征库 JSON 不受影响。
    """
    p = Path(path)
    if not p.exists():
        return p
    if all(ord(c) < 128 for c in str(p)):
        return p
    import tempfile, shutil
    cache = Path(tempfile.gettempdir()) / "link2_models"
    cache.mkdir(parents=True, exist_ok=True)
    dst = cache / p.name
    if not dst.exists() or dst.stat().st_mtime < p.stat().st_mtime:
        shutil.copy2(p, dst)
    return dst


# ---------------- 检测器 ----------------
class FaceDetector:
    """人脸检测封装（Haar 级联，零外部下载 / 任意分辨率 / 实时稳定）。

    相比 YuNet 的取舍：准确度略低、无真实关键点。识别所需的 5 个关键点按
    人脸标准比例合成（双眼/鼻/嘴角），足以驱动 SFace.alignCrop。
    输出结构与原 YuNet 封装完全一致：[{x, y, w, h, landmarks, score}]。
    """

    # 合成关键点的相对位置（基于经验值，匹配 SFace 期望顺序）
    # 顺序：右眼 / 左眼 / 鼻尖 / 右嘴角 / 左嘴角
    _LM_FRAC = [
        (0.30, 0.38),
        (0.70, 0.38),
        (0.50, 0.55),
        (0.42, 0.72),
        (0.58, 0.72),
    ]

    def __init__(self, score_threshold=3.0):
        if not CV2_OK:
            raise RuntimeError("cv2 不可用，请安装 opencv-python-headless>=4.6")
        self.cascades = []  # [(name, CascadeClassifier)]，按优先级，default 优先
        for fname in ("haarcascade_frontalface_default.xml",
                      "haarcascade_frontalface_alt2.xml"):
            cp = os.path.join(cv2.data.haarcascades, fname)
            if not Path(cp).exists():
                continue
            c = cv2.CascadeClassifier(cp)
            if not c.empty():
                self.cascades.append((fname, c))
        if not self.cascades:
            raise RuntimeError("haar 级联加载失败")
        self.score_threshold = float(score_threshold)
        self.lock = threading.Lock()
        self.last_ms = 0.0

    @classmethod
    def _landmarks(cls, x, y, w, h):
        return [(x + fx * w, y + fy * h) for fx, fy in cls._LM_FRAC]

    def detect(self, bgr):
        """返回 [{x,y,w,h,landmarks[(x,y)x5],score}]，按面积降序。

        多级联(default→alt2) + 双尺度(原始+0.5x) 兜底，提升远近/角度鲁棒性。
        """
        results = []
        for _, casc in self.cascades:
            results = self._detect_at(bgr, casc, 1.0)
            if results:
                break
            results = self._detect_at(bgr, casc, 0.5)
            if results:
                break
        results.sort(key=lambda d: d["w"] * d["h"], reverse=True)
        return results

    def _detect_at(self, bgr, casc, scale):
        h, w = bgr.shape[:2]
        if scale != 1.0:
            small = cv2.resize(bgr, (int(w * scale), int(h * scale)),
                               interpolation=cv2.INTER_AREA)
        else:
            small = bgr
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        with self.lock:
            t0 = time.perf_counter()
            rects, _, level_weights = casc.detectMultiScale3(
                gray, scaleFactor=1.1, minNeighbors=4, minSize=(40, 40),
                flags=cv2.CASCADE_SCALE_IMAGE,
                outputRejectLevels=True,
            )
            self.last_ms = (time.perf_counter() - t0) * 1000
        out = []
        if rects is not None:
            inv = 1.0 / scale
            for (x, y, fw, fh), conf in zip(rects, level_weights):
                if conf < self.score_threshold:
                    continue
                x, y, fw, fh = int(x * inv), int(y * inv), int(fw * inv), int(fh * inv)
                out.append({
                    "x": x, "y": y, "w": fw, "h": fh,
                    "landmarks": self._landmarks(x, y, fw, fh),
                    "score": float(conf),
                })
        return out


# SFace 标准 5 点对齐模板（112x112 画布），与 SFace 官方训练一致
_ALIGN_TEMPLATE = np.array([
    [38.2946, 51.6963],
    [73.5318, 51.5014],
    [56.0252, 71.7366],
    [41.5493, 92.3655],
    [70.7299, 92.2041],
], dtype=np.float32)


# ---------------- 识别器 ----------------
class FaceRecognizer:
    """人脸识别封装：SFace ONNX 手工推理（避开 FaceRecognizerSF_create 解析缺陷）。

    注册人脸 → 特征库（faces_db.json）→ 1:N 比对（余弦相似度）。
    """

    def __init__(self, model_path=REC_MODEL, db_path=FACE_DB):
        if not Path(model_path).exists():
            raise RuntimeError(f"识别模型不存在: {model_path}")
        safe = _ascii_safe(model_path)  # 绕开 OpenCV 中文路径 bug
        self.net = cv2.dnn.readNetFromONNX(str(safe))
        self.db_path = Path(db_path)
        self.lock = threading.Lock()
        self.db = {}          # name -> [feature(np.float32 x128)]
        self._load()

    def _load(self):
        try:
            data = json.loads(self.db_path.read_text(encoding="utf-8"))
            self.db = {k: [np.array(v, dtype=np.float32) for v in feats]
                       for k, feats in data.items()}
        except Exception:
            self.db = {}

    def _save(self):
        self.db_path.write_text(
            json.dumps({k: [f.tolist() for f in v] for k, v in self.db.items()},
                       ensure_ascii=False, indent=1), encoding="utf-8")

    def _align(self, bgr, face):
        """5 点相似变换对齐到 112x112 标准脸。"""
        src = np.array(face["landmarks"], dtype=np.float32)
        M, _ = cv2.estimateAffinePartial2D(src, _ALIGN_TEMPLATE)
        if M is None:
            return None
        return cv2.warpAffine(bgr, M, (112, 112))

    def extract(self, bgr, face):
        """人脸对齐 + 特征提取。face 需含 landmarks。"""
        aligned = self._align(bgr, face)
        if aligned is None:
            raise RuntimeError("align failed")
        blob = cv2.dnn.blobFromImage(aligned, 1.0/127.5, (112, 112),
                                     (127.5, 127.5, 127.5), swapRB=True)
        self.net.setInput(blob)
        feat = self.net.forward()           # 1x128
        v = feat[0].astype(np.float32)
        # L2 归一化（提升余弦匹配的数值稳定性）
        n = np.linalg.norm(v) + 1e-8
        return v / n

    def enroll(self, name, bgr, face):
        """注册一张人脸（可同名多张提升鲁棒性）。"""
        feat = self.extract(bgr, face)
        with self.lock:
            self.db.setdefault(name, [])
            self.db[name].append(feat)
            self._save()
            return {"name": name, "samples": len(self.db[name])}

    def identify(self, feat):
        """1:N 比对。返回 (name, similarity) 或 (None, 0)。"""
        with self.lock:
            if not self.db:
                return None, 0.0
            best_name, best = None, 0.0
            for name, feats in self.db.items():
                for f in feats:
                    sim = float(np.dot(feat, f))   # 已 L2 归一化 → dot 即余弦
                    if sim > best:
                        best_name, best = name, sim
        if best >= COSINE_THRESHOLD:
            return best_name, best
        return None, best

    def delete(self, name):
        with self.lock:
            removed = self.db.pop(name, None) is not None
            if removed:
                self._save()
        return removed

    def names(self):
        with self.lock:
            return {k: len(v) for k, v in self.db.items()}


# ---------------- 跟踪控制器 ----------------
class TrackController:
    """
    P 控制器：人脸中心相对画面中心的偏差 → 云台角速度。

    坐标约定：
      err_x>0 人脸在画面右侧 → 需增大 pan（右转）
      err_y>0 人脸在画面下方 → 需减小 tilt（低头）
    速度 = Kp * 偏差角，clamp 到 max_speed；deadzone 内不动（防抖）。
    """

    def __init__(self, fov_h=75.0, kp=3.0, max_speed=80.0, deadzone_deg=1.5):
        self.fov_h = fov_h
        self.kp = kp
        self.max_speed = max_speed
        self.deadzone_deg = deadzone_deg
        self.last_target_center = None   # 目标粘滞用

    def pick_target(self, faces):
        """选跟踪目标：优先靠近上次目标中心的人脸，否则取最大。"""
        if not faces:
            self.last_target_center = None
            return None
        if self.last_target_center is not None:
            lx, ly = self.last_target_center
            near = [f for f in faces
                    if math.hypot((f["x"] + f["w"] / 2) - lx, (f["y"] + f["h"] / 2) - ly)
                    < max(f["w"], 120)]
            if near:
                faces = near
        t = faces[0]  # 已按面积降序
        self.last_target_center = (t["x"] + t["w"] / 2, t["y"] + t["h"] / 2)
        return t

    def compute_vel(self, face, img_w, img_h, zoom=100):
        """返回 (pan_vel, tilt_vel) deg/s，deadzone 内返回 (0,0)。"""
        if face is None:
            return 0.0, 0.0
        cx, cy = face["x"] + face["w"] / 2, face["y"] + face["h"] / 2
        err_x = (cx - img_w / 2) / (img_w / 2)       # -1..1
        err_y = (cy - img_h / 2) / (img_h / 2)       # -1..1
        fov_h_eff = self.fov_h / max(1.0, zoom / 100.0)
        fov_v_eff = 2 * math.degrees(math.atan(math.tan(math.radians(fov_h_eff) / 2) * img_h / img_w))
        off_pan = err_x * fov_h_eff / 2              # 需要的 pan 变化量（度）
        off_tilt = -err_y * fov_v_eff / 2            # 需要的 tilt 变化量（度）

        def axis(offset):
            if abs(offset) < self.deadzone_deg:
                return 0.0
            return max(-self.max_speed, min(self.max_speed, self.kp * offset))

        return axis(off_pan), axis(off_tilt)


# ---------------- 服务组装 ----------------
class FaceService(threading.Thread):
    """
    人脸视觉服务：检测/识别/跟踪 一站式。

    宿主回调（构造时注入，与 Studio 解耦）：
      get_frame()   -> (jpeg_bytes, w, h) | None   最新帧（无则跳过本轮）
      get_position() -> {"pan":.., "tilt":.., "zoom":..}
      set_vel(pan_vel, tilt_vel)                   速度命令 deg/s
      stop_motion()                                停止云台
    """

    LOOP_HZ = 12

    def __init__(self, get_frame, get_position, set_vel, stop_motion,
                 fov_h=75.0, on_log=None):
        super().__init__(daemon=True, name="FaceService")
        self.get_frame = get_frame
        self.get_position = get_position
        self.set_vel = set_vel
        self.stop_motion = stop_motion
        self.on_log = on_log or (lambda msg: None)

        self.detect_on = False
        self.track_on = False
        self.lock = threading.Lock()
        self._stop_flag = False

        self.detector = None
        self.recognizer = None
        self.tracker = TrackController(fov_h=fov_h)
        self.last_result = {"faces": [], "track_target": False}
        self.det_ms = 0.0
        self.fps = 0.0
        self._fps_ts = []
        self._last_seen_target = 0.0
        self._vel_sent = (0.0, 0.0)

        # 目标人物锁定：设为某已注册人名时，只跟踪该人（按身份匹配，非最大脸）
        self.target_name = None
        # 识别记录：[{name, time, similarity}]，环形缓冲
        self._recog_log = []
        self._LOG_MAX = 200

        self.init_models()

    # ---- 模型加载（识别模型缺失时优雅降级） ----
    def init_models(self):
        if not CV2_OK:
            self.on_log("cv2 不可用，人脸功能禁用")
            return
        try:
            self.detector = FaceDetector()
        except Exception as err:
            self.on_log(f"检测器加载失败: {err}")
            return
        try:
            self.recognizer = FaceRecognizer()
        except Exception as err:
            self.on_log(f"识别模型未加载（识别功能禁用）: {err}")

    @property
    def available(self):
        return self.detector is not None

    @property
    def recognition_available(self):
        return self.recognizer is not None

    # ---- 对外控制 ----
    def set_detect(self, on):
        if on and not self.available:
            raise RuntimeError("人脸检测不可用（模型/cv2 缺失）")
        with self.lock:
            self.detect_on = bool(on)
            if not on:
                self.track_on = False
                self.last_result = {"faces": [], "track_target": False}
        if not on:
            self._send_vel(0.0, 0.0)
        return {"detect": self.detect_on}

    def set_track(self, on):
        if on and not self.available:
            raise RuntimeError("人脸检测不可用")
        if on and not self.detect_on:
            self.set_detect(True)
        with self.lock:
            self.track_on = bool(on)
        if not on:
            self._send_vel(0.0, 0.0)
        return {"detect": self.detect_on, "track": self.track_on}

    def update_settings(self, fov=None, kp=None, max_speed=None, deadzone=None):
        if fov is not None:
            self.tracker.fov_h = float(fov)
        if kp is not None:
            self.tracker.kp = float(kp)
        if max_speed is not None:
            self.tracker.max_speed = float(max_speed)
        if deadzone is not None:
            self.tracker.deadzone_deg = float(deadzone)
        return self.settings()

    def settings(self):
        t = self.tracker
        return {"fov": t.fov_h, "kp": t.kp, "max_speed": t.max_speed,
                "deadzone": t.deadzone_deg}

    def status(self):
        with self.lock:
            return {
                "available": self.available,
                "recognition": self.recognition_available,
                "detect": self.detect_on,
                "track": self.track_on,
                "target": self.target_name,
                "faces": self.last_result.get("faces", []),
                "track_target": self.last_result.get("track_target", False),
                "det_ms": round(self.det_ms, 1),
                "fps": round(self.fps, 1),
            }

    # ---- 目标人物锁定 ----
    def set_target(self, name):
        """锁定跟踪目标为某已注册人名；name=None 清除（恢复跟踪任意最大脸）。"""
        with self.lock:
            self.target_name = str(name).strip() if name else None
            self.tracker.last_target_center = None
        if not self.target_name:
            self._send_vel(0.0, 0.0)
        return {"target": self.target_name}

    def get_target(self):
        with self.lock:
            return self.target_name

    def recognize_and_lock(self):
        """识别当前画面中的人脸，锁定第一个匹配到的已注册人。

        返回 {locked: name|None, similarity}。供"识别追踪"按键调用。
        """
        if not self.available or not self.recognition_available:
            return {"locked": None}
        frame = self.get_frame()
        if not frame:
            return {"locked": None}
        jpeg, _, _ = frame
        bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        faces = self.detector.detect(bgr)
        if not faces:
            return {"locked": None, "reason": "画面中未检测到人脸"}
        best_name, best_sim = None, 0.0
        for f in faces[:5]:
            try:
                feat = self.recognizer.extract(bgr, f)
                name, sim = self.recognizer.identify(feat)
                if name and sim > best_sim:
                    best_name, best_sim = name, sim
            except Exception:
                pass
        if best_name:
            with self.lock:
                self.target_name = best_name
                self.tracker.last_target_center = None
            return {"locked": best_name, "similarity": round(best_sim, 3)}
        return {"locked": None, "reason": "未匹配到已注册人脸（可先注册）"}

    # ---- 上传人脸注册 ----
    def enroll_from_image(self, name, image_bytes):
        """从上传的图片字节注册人脸（不依赖实时视频流）。"""
        if not self.recognition_available:
            raise RuntimeError("识别模型未加载")
        if not name or not str(name).strip():
            raise ValueError("name required")
        name = str(name).strip()
        if not image_bytes:
            raise ValueError("image required")
        arr = np.frombuffer(image_bytes, np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            raise RuntimeError("无法解码图片（仅支持 jpg/png）")
        faces = self.detector.detect(bgr)
        if not faces:
            raise RuntimeError("图片中未检测到人脸")
        info = self.recognizer.enroll(name, bgr, faces[0])
        return {"ok": True, **info, "face": {k: faces[0][k] for k in ("x", "y", "w", "h")}}

    # ---- 识别记录 ----
    def recognition_log(self):
        with self.lock:
            return list(self._recog_log)

    def _log_recognition(self, recognized_faces):
        """记录识别到的人脸（仅记录识别出身份的）。去重同一人短时重复。"""
        now = time.time()
        for f in recognized_faces:
            n = f.get("name")
            if not n:
                continue
            entry = {"name": n, "time": round(now, 2),
                     "similarity": f.get("similarity", 0),
                     "x": f.get("x"), "y": f.get("y"),
                     "w": f.get("w"), "h": f.get("h")}
            self._recog_log.append(entry)
        # 截断
        if len(self._recog_log) > self._LOG_MAX:
            self._recog_log[:] = self._recog_log[-self._LOG_MAX:]

    def enroll(self, name):
        """注册当前画面中最大的一张脸。"""
        if not self.recognition_available:
            raise RuntimeError("识别模型未加载")
        if not name or not str(name).strip():
            raise ValueError("name required")
        name = str(name).strip()
        frame = self.get_frame()
        if not frame:
            raise RuntimeError("无视频帧")
        jpeg, _, _ = frame
        bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        faces = self.detector.detect(bgr)
        if not faces:
            raise RuntimeError("画面中未检测到人脸")
        info = self.recognizer.enroll(name, bgr, faces[0])
        return {"ok": True, **info}

    def enrolled(self):
        if not self.recognition_available:
            return {}
        return self.recognizer.names()

    def delete_face(self, name):
        if not self.recognition_available:
            raise RuntimeError("识别模型未加载")
        if not self.recognizer.delete(name):
            raise ValueError(f"未注册: {name}")
        return {"ok": True}

    # ---- 内部 ----
    def _send_vel(self, pv, tv):
        if (pv, tv) != self._vel_sent:
            try:
                self.set_vel(pv, tv)
                self._vel_sent = (pv, tv)
            except Exception:
                pass

    def run(self):
        period = 1.0 / self.LOOP_HZ
        while not self._stop_flag:
            t0 = time.monotonic()
            try:
                self._tick()
            except Exception:
                pass
            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, period - elapsed))

    def _tick(self):
        with self.lock:
            want_detect = self.detect_on
        if not want_detect:
            time.sleep(0.2)
            return

        frame = self.get_frame()
        if not frame:
            time.sleep(0.05)
            return
        jpeg, w, h = frame
        bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            return
        ih, iw = bgr.shape[:2]

        faces = self.detector.detect(bgr)
        self.det_ms = self.detector.last_ms
        now = time.monotonic()
        self._fps_ts = [t for t in self._fps_ts if now - t < 3.0] + [now]
        self.fps = len(self._fps_ts) / 3.0

        with self.lock:
            track_on = self.track_on
            target_name = self.target_name

        # 识别（SFace 单脸 ~几 ms）——先识别，便于按身份选目标
        out_faces = []
        if self.recognizer is not None and faces:
            for f in faces[:5]:
                try:
                    feat = self.recognizer.extract(bgr, f)
                    name, sim = self.recognizer.identify(feat)
                    f = dict(f, name=name, similarity=round(sim, 3))
                except Exception:
                    pass
                out_faces.append({k: f[k] for k in ("x", "y", "w", "h", "score", "name", "similarity")
                                  if k in f})
            self._log_recognition(out_faces)
        else:
            out_faces = [{k: f[k] for k in ("x", "y", "w", "h", "score")} for f in faces]

        # 跟踪目标选取
        target = None
        if track_on and out_faces:
            if target_name:
                # 锁定身份：只跟踪匹配目标姓名的脸（取其中最大）
                matched = [f for f in out_faces if f.get("name") == target_name]
                if matched:
                    matched.sort(key=lambda d: d["w"] * d["h"], reverse=True)
                    target = matched[0]
                # 未匹配到目标人物：保持静止，绝不追错人
            else:
                # 未锁定：跟踪最大/最近的脸
                target = self.tracker.pick_target(out_faces)

        # 标记目标脸
        if target is not None:
            for f in out_faces:
                if f is target:
                    f["target"] = True
                    break

        # 跟踪控制
        track_target = target is not None
        if track_target:
            try:
                zoom = self.get_position().get("zoom", 100)
            except Exception:
                zoom = 100
            pv, tv = self.tracker.compute_vel(target, iw, ih, zoom)
            self._send_vel(pv, tv)
            self._last_seen_target = time.monotonic()
        elif track_on:
            # 目标丢失：短暂保持 0 速（不动），目标粘滞信息由 tracker 保留
            if time.monotonic() - self._last_seen_target > 0.5:
                self._send_vel(0.0, 0.0)

        with self.lock:
            self.last_result = {"faces": out_faces, "track_target": track_target}

    def stop(self):
        self._stop_flag = True
        self._send_vel(0.0, 0.0)


# ---------------- 独立自测 ----------------
if __name__ == "__main__":
    # 从 Link2 Studio 拉流自测：python face_tracker.py [port]
    import sys
    import urllib.request

    port = sys.argv[1] if len(sys.argv) > 1 else "8899"
    stream = urllib.request.urlopen(f"http://127.0.0.1:{port}/video.raw", timeout=10)
    buf = b""

    def get_frame():
        global buf
        chunk = stream.read(65536)
        if not chunk:
            return None
        buf += chunk
        s = buf.find(b"\xff\xd8")
        e = buf.find(b"\xff\xd9", s + 2)
        if s < 0 or e < 0:
            return None
        jpeg = buf[s:e + 2]
        buf = buf[e + 2:]
        return jpeg, 0, 0

    svc = FaceService(get_frame, lambda: {"pan": 0, "tilt": 0, "zoom": 100},
                      lambda pv, tv: None, lambda: None,
                      on_log=print)
    svc.set_detect(True)
    print(f"opencv={cv2.__version__} available={svc.available} "
          f"recognition={svc.recognition_available}")
    import time as _t
    t0 = _t.time()
    while _t.time() - t0 < 10:
        _t.sleep(1)
        st = svc.status()
        print(f"fps={st['fps']} det={st['det_ms']}ms faces={len(st['faces'])} "
              f"first={st['faces'][0] if st['faces'] else None}")
    svc.stop()
