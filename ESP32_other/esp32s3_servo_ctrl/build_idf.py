"""idf.py wrapper: 在 Git Bash (MSYSTEM 环境下) 正常调用 ESP-IDF 的 idf.py。
用法: python build_idf.py <idf.py 参数...>   例如: python build_idf.py build
"""
import os
import runpy
import sys

# idf.py 检测到 MSYSTEM 就拒绝运行, 这里强制剔除
os.environ.pop("MSYSTEM", None)

os.environ.setdefault("IDF_PATH", r"D:\Espressif_ESP32_IDF_5.5\frameworks\esp-idf-v5.5.4")
os.environ.setdefault("IDF_PYTHON_ENV_PATH", r"D:\Espressif_ESP32_IDF_5.5\python_env\idf5.5_py3.11_env")
os.environ.setdefault("IDF_TOOLS_PATH", r"D:\Espressif_ESP32_IDF_5.5")
os.environ.setdefault("ESP_ROM_ELF_DIR", r"D:\Espressif_ESP32_IDF_5.5\tools\esp-rom-elfs\20241011" + os.sep)

TOOL_DIRS = [
    r"D:\Espressif_ESP32_IDF_5.5\tools\cmake\3.30.2\bin",
    r"D:\Espressif_ESP32_IDF_5.5\tools\ninja\1.12.1",
    r"D:\Espressif_ESP32_IDF_5.5\tools\ccache\4.12.1\ccache-4.12.1-windows-x86_64",
    r"D:\Espressif_ESP32_IDF_5.5\tools\xtensa-esp-elf\esp-14.2.0_20260121\xtensa-esp-elf\bin",
    r"D:\Espressif_ESP32_IDF_5.5\tools\riscv32-esp-elf\esp-14.2.0_20260121\riscv32-esp-elf\bin",
]
os.environ["PATH"] = os.pathsep.join(TOOL_DIRS) + os.pathsep + os.environ.get("PATH", "")

IDF_PY = os.path.join(os.environ["IDF_PATH"], "tools", "idf.py")
sys.path.insert(0, os.path.dirname(IDF_PY))  # 让 python_version_checker / idf_py_actions 可导入
sys.argv = [IDF_PY] + sys.argv[1:]
runpy.run_path(IDF_PY, run_name="__main__")
