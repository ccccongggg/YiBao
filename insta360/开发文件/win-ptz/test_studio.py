#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Link2 Studio 功能自动化测试"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8899"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return json.loads(r.read())


def post(path, payload=None):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def show(label, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label} {detail}")


# 1. 平滑 goto 插值
print("== 平滑 goto（speed=40）==")
post("/api/goto", {"pan": 60, "tilt": 20, "speed": 40})
samples = []
for _ in range(8):
    p = get("/api/position")
    samples.append((p["pan"], p["tilt"], p["moving"]))
    time.sleep(0.25)
for s in samples:
    print(f"   pan={s[0]:>4} tilt={s[1]:>4} moving={s[2]}")
mono = all(
    samples[i][0] <= samples[i + 1][0] + 1 for i in range(len(samples) - 1)
    if samples[i][2]
)
final = samples[-1]
show("平滑到位", final[0] == 60 and final[1] == 20 and not final[2], f"终点 {final[0]},{final[1]}")
show("插值递进（非瞬跳）", mono and samples[0][0] != 60)

# 2. 变焦
r = post("/api/zoom", {"zoom": 200})
show("变焦 2x", r.get("zoom") == 200, str(r))
r = post("/api/zoom", {"zoom": 100})
show("变焦回 1x", r.get("zoom") == 100, str(r))

# 3. 点击转向
r = post("/api/aim", {"x": 0.2, "y": 0.0})
show("点击转向", "pan" in r, str(r))

# 4. 预设位
post("/api/goto", {"pan": 0, "tilt": 0})
time.sleep(1.5)
r = post("/api/preset/save", {"name": "正前"})
show("保存预设", r.get("ok"), str(r))
post("/api/goto", {"pan": 40, "tilt": -20})
time.sleep(1.5)
r = post("/api/preset/save", {"name": "右下"})
presets = get("/api/presets")
show("预设列表", set(presets["presets"].keys()) == {"正前", "右下"}, str(list(presets["presets"].keys())))

# 5. 巡航
r = post("/api/patrol/start")
show("启动巡航", r.get("active") is True, str(r))
time.sleep(4)
st = get("/api/status")
show("巡航中状态", st.get("patrol") is True, f"patrol={st['patrol']}")
r = post("/api/patrol/stop")
show("停止巡航", r.get("active") is False)

# 6. 截图
r = post("/api/snapshot")
show("截图", "url" in r, str(r))
if "url" in r:
    with urllib.request.urlopen(BASE + r["url"], timeout=5) as f:
        data = f.read()
    show("截图文件可访问", data[:2] == b"\xff\xd8", f"{len(data)} bytes JPEG")

# 7. 录像 4 秒
r = post("/api/record", {"on": True})
show("开始录像", r.get("recording") is True, str(r))
time.sleep(4)
r = post("/api/record", {"on": False})
show("停止录像", r.get("recording") is False, str(r))
if "file" in r:
    from pathlib import Path
    p = Path(r["file"])
    show("录像文件", p.exists() and p.stat().st_size > 10000, f"{p.name} {p.stat().st_size if p.exists() else 0} bytes")

# 8. 视频流帧率
t0 = time.time()
n = 0
with urllib.request.urlopen(BASE + "/video.raw", timeout=10) as f:
    buf = b""
    while time.time() - t0 < 2.0:
        chunk = f.read(65536)
        if not chunk:
            break
        buf += chunk
        n += chunk.count(b"\xff\xd8\xff\xe0") + chunk.count(b"\xff\xd8\xff\xe1")
fps = n / (time.time() - t0)
show("视频流帧率", fps >= 20, f"{fps:.1f} fps")

# 9. 预设删除 & 复位
post("/api/preset/delete", {"name": "右下"})
presets = get("/api/presets")
show("删除预设", "右下" not in presets["presets"])
post("/api/goto", {"pan": 0, "tilt": 0})
post("/api/zoom", {"zoom": 100})
print("\n测试完成")
