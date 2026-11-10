# YiBao · 桌面机器人

桌面机器人项目工作区，包含三部分：Insta360 Link 2 云台相机的视觉跟踪控制程序、ESP32-S3 舵机控制固件，以及外壳三维模型与渲染素材。

## 目录结构

```
ESP32_other/
├─ esp32s3_servo_ctrl/           ESP-IDF 工程（ESP32-S3-N16R8 硬件自检固件）
│  ├─ main/main.c                摇杆/按键/压力/ICM-20602/舵机自检，串口 115200 输出
│  ├─ CMakeLists.txt
│  └─ build_idf.py, verify_serial.py
└─ ESP32_状态报告_2026-08-29.md  本地 IDF 环境与串口/板子状态记录

insta360/开发文件/
├─ win-ptz/                      Insta360 Link 2 云台控制（Windows）
│  ├─ link2_studio.py            综合控制平台：Web 控制台 + 云台运动控制 + 录像
│  ├─ face_tracker.py            YuNet 检测 / SFace 识别 / P 控制跟踪
│  ├─ ptz_server_win.py          云台 HTTP 服务
│  ├─ ptz_daemon.c               DirectShow IAMCameraControl 守护进程（<10ms 响应）
│  ├─ ptz_dshow.c                DirectShow 云台控制 CLI
│  └─ models/                    face_detection_yunet.onnx、face_recognition_sface.onnx
└─ 交接文档.md                    硬件限制、SDK 盘点、环境搭建与落地结论

image/                           外壳渲染图、三视图、STL
```

## 运行环境

- **固件**：ESP-IDF v5.5.4 + ESP32-S3（USB-UART CH343，COM7，115200）
- **云台程序**：Windows 10/11，Python 3.13（opencv-python-headless ≥ 4.6），FFmpeg（含 dshow），MinGW-w64（编译 C 守护进程）
- **三维模型**：SolidWorks 2024

## 未入库内容

为保证仓库可用体积，以下内容通过 `.gitignore` 排除，仅在本地保留：

| 内容 | 体积 | 原因 |
| --- | --- | --- |
| `insta360/赛事SDK包/`、`赛事SDK包（Windows+Linux）.rar`、`Link2SDK-拓展协议版/` | ~2 GB（单文件最大 758 MB） | 影石官方 SDK 安装包，超 GitHub 100 MB 单文件限制，请向厂商获取 |
| `model/` | ~1 GB | SolidWorks 源文件（一代/二代/三代外壳） |
| `build/`、`__pycache__`、`*.exe`、录制与快照 | — | 构建产物与运行期数据 |

相机硬件限制与 SDK 选型结论见 [`insta360/开发文件/交接文档.md`](insta360/开发文件/交接文档.md)；固件与串口状态见 [`ESP32_other/ESP32_状态报告_2026-08-29.md`](ESP32_other/ESP32_状态报告_2026-08-29.md)。
