"""读取 COM7 串口输出并保存, 用于烧录后验证。
用法: python verify_serial.py [秒数, 默认12]
"""
import sys
import time

import serial

PORT = "COM7"
BAUD = 115200
DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0

out = []
try:
    ser = serial.Serial(PORT, BAUD, timeout=1)
except Exception as e:
    print("OPEN_FAIL:", e)
    sys.exit(1)

ser.reset_input_buffer()
# 通过 RTS 复位板子, 抓取完整启动输出 (标准 devkit: RTS->EN, DTR->IO0)
try:
    ser.dtr = False
    ser.rts = True
    time.sleep(0.1)
    ser.rts = False
    time.sleep(0.2)
except Exception:
    pass

t0 = time.time()
while time.time() - t0 < DUR:
    try:
        n = ser.in_waiting
        data = ser.read(n if n > 0 else 1)
    except Exception as e:
        out.append("\nREAD_ERR: " + str(e))
        break
    if data:
        out.append(data.decode(errors="replace"))
ser.close()

text = "".join(out)
open("serial_out.txt", "w", encoding="utf-8").write(text)
print("BYTES=%d" % len(text.encode("utf-8", "replace")))
print(text)
