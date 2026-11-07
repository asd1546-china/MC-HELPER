"""
硬件仪表盘 · 纯标准库版（零扩展库、零 pip install）

- 自定义控件: Dial 继承 tkinter.Canvas, 用 create_arc 自己画出圆环仪表
- CPU  : ctypes 调 kernel32.GetSystemTimes 算差值(不装 psutil)
- 内存 : ctypes 调 kernel32.GlobalMemoryStatusEx
- GPU  : Windows 性能计数器(PowerShell), Intel Iris Xe 核显也能读
- 采样放在后台线程, 界面主线程不卡顿

用法:
    python gpu_dashboard.py            # 打开仪表盘窗口
    python gpu_dashboard.py --once     # 不开窗, 终端打印一次采样
"""

import ctypes
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk

# ---------------------------------------------------------------- 数据采集

class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong),
                ("dwHighDateTime", ctypes.c_ulong)]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


_kernel32 = ctypes.windll.kernel32
_prev_cpu = None


def _ft_value(ft):
    """把 FILETIME 高低 32 位拼成一个整数"""
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def read_cpu():
    """用系统累计时间差算 CPU 占用率, 第一次调用返回 0.0(建立基线)"""
    global _prev_cpu
    idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
    if not _kernel32.GetSystemTimes(ctypes.byref(idle),
                                    ctypes.byref(kernel),
                                    ctypes.byref(user)):
        return 0.0
    cur = (_ft_value(idle), _ft_value(kernel) + _ft_value(user))
    if _prev_cpu is None:
        _prev_cpu = cur
        return 0.0
    d_idle = cur[0] - _prev_cpu[0]
    d_total = cur[1] - _prev_cpu[1]
    _prev_cpu = cur
    if d_total <= 0:
        return 0.0
    return max(0.0, min(100.0, (1 - d_idle / d_total) * 100))


def read_memory():
    """返回 (占用百分比, 已用GB, 总量GB)"""
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    if not _kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        return 0.0, 0.0, 0.0
    used = (m.ullTotalPhys - m.ullAvailPhys) / 1024 ** 3
    return float(m.dwMemoryLoad), used, m.ullTotalPhys / 1024 ** 3


PS_SCRIPT = r'''
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
$inv = [System.Globalization.CultureInfo]::InvariantCulture
$paths = @(
  '\GPU Engine(*)\Utilization Percentage',
  '\GPU Adapter Memory(*)\Shared Usage',
  '\GPU Adapter Memory(*)\Dedicated Usage'
)
$r = Get-Counter -Counter $paths
if ($null -eq $r) { 'ERROR:GPU 计数器不可用(需 Windows 10/11)'; exit }
$eng = @{}; $shared = 0.0; $ded = 0.0
foreach ($s in $r.CounterSamples) {
  $v = [double]$s.CookedValue
  if ($s.Path -like '*GPU Engine*') {
    if ($s.InstanceName -match 'engtype_([a-zA-Z0-9]+)') {
      $k = $matches[1].ToLower()
      if (-not $eng.ContainsKey($k)) { $eng[$k] = 0.0 }
      $eng[$k] += $v
    }
  } elseif ($s.Path -like '*Shared Usage*')  { $shared += $v }
  elseif ($s.Path -like '*Dedicated Usage*') { $ded += $v }
}
foreach ($k in ($eng.Keys | Sort-Object)) {
  if ($eng[$k] -gt 0.01) { 'ENG:' + $k + '=' + $eng[$k].ToString('F1', $inv) }
}
'MEM:shared=' + ($shared / 1MB).ToString('F1', $inv)
'MEM:ded=' + ($ded / 1MB).ToString('F1', $inv)
'''

ENG_CN = {
    "3d": "3D", "copy": "Copy", "compute": "Compute",
    "videodecode": "解码", "videoencode": "编码",
    "videoprocessing": "视频处理", "legacyoverlay": "叠层",
}


def read_gpu():
    """返回 (峰值占用, 引擎明细字典, 共享显存MB, 专用显存MB, 错误)"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # 不闪黑框
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_SCRIPT],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", creationflags=flags,
    ).stdout
    eng, shared, ded, err = {}, 0.0, 0.0, None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("ERROR:"):
            err = line[6:]
        elif line.startswith("ENG:"):
            k, v = line[4:].split("=", 1)
            eng[k] = min(float(v), 100.0)   # 多进程求和可能超 100, 封顶
        elif line.startswith("MEM:shared="):
            shared = float(line.split("=", 1)[1])
        elif line.startswith("MEM:ded="):
            ded = float(line.split("=", 1)[1])
    peak = max(eng.values()) if eng else 0.0
    return peak, eng, shared, ded, err


def sample():
    """一次完整采样, 返回字典
    刻意先查慢的 GPU: 这段 1~1.5 秒正好当 CPU 的采样窗口, 差值才准
    """
    gpu, eng, shared, ded, err = read_gpu()
    cpu = read_cpu()
    mem_pct, mem_used, mem_total = read_memory()
    return {
        "cpu": cpu, "mem_pct": mem_pct,
        "mem_used": mem_used, "mem_total": mem_total,
        "gpu": gpu, "eng": eng, "shared": shared,
        "ded": ded, "err": err,
    }


# ---------------------------------------------------------------- 自定义控件

class Dial(tk.Canvas):
    """自己画的圆环仪表控件 —— 这就是"自定义控件": 不靠现成组件, 自己重写绘制"""

    BG = "#f7f8fa"
    TRACK = "#e4e7ec"

    def __init__(self, master, title, size=180):
        super().__init__(master, width=size, height=size,
                         bg=self.BG, highlightthickness=0)
        self.size = size
        self.title = title
        self.value = 0.0
        self.note = ""
        self._draw()

    def set(self, value, note=""):
        self.value = max(0.0, min(100.0, float(value)))
        self.note = note
        self._draw()

    @staticmethod
    def _color(v):
        if v >= 80:
            return "#d83b3b"   # 红: 满载
        if v >= 50:
            return "#e8912d"   # 橙: 有点忙
        return "#2ea043"       # 绿: 悠闲

    def _draw(self):
        self.delete("all")
        s, pad = self.size, 18
        box = (pad, pad, s - pad, s - pad)
        # 底环 (extent 用 -359.9 而不是 -360, tkinter 对整圈更稳)
        self.create_arc(*box, start=90, extent=-359.9, style="arc",
                        outline=self.TRACK, width=13)
        # 前景弧: 从正上方顺时针推进
        extent = -min(self.value * 3.6, 359.9)
        self.create_arc(*box, start=90, extent=extent, style="arc",
                        outline=self._color(self.value), width=13)
        self.create_text(s / 2, s / 2 - 10, text=f"{self.value:.0f}%",
                         font=("Segoe UI", 24, "bold"), fill="#1f2328")
        self.create_text(s / 2, s / 2 + 18, text=self.title,
                         font=("Microsoft YaHei", 10), fill="#57606a")
        if self.note:
            self.create_text(s / 2, s - 12, text=self.note,
                             font=("Microsoft YaHei", 9), fill="#8b949e")


# ---------------------------------------------------------------- 主窗口

class Dashboard:
    def __init__(self, root, interval=2.5):
        self.root = root
        self.q = queue.Queue(maxsize=1)
        self.stop = threading.Event()

        root.title("硬件仪表盘")
        root.configure(bg=Dial.BG)
        root.resizable(False, False)

        tk.Label(root, text="硬 件 仪 表 盘", font=("Microsoft YaHei", 14, "bold"),
                 bg=Dial.BG, fg="#1f2328").pack(pady=(16, 2))
        tk.Label(root, text="Intel Iris Xe · PowerShell 性能计数器 · 纯标准库",
                 font=("Microsoft YaHei", 9), bg=Dial.BG, fg="#8b949e").pack()

        row = tk.Frame(root, bg=Dial.BG)
        row.pack(padx=18, pady=12)
        self.d_cpu = Dial(row, "CPU")
        self.d_mem = Dial(row, "内存")
        self.d_gpu = Dial(row, "GPU")
        for i, d in enumerate((self.d_cpu, self.d_mem, self.d_gpu)):
            d.grid(row=0, column=i, padx=8)

        self.status = tk.Label(root, text="正在采样…",
                               font=("Microsoft YaHei", 9),
                               bg=Dial.BG, fg="#57606a", wraplength=520,
                               justify="center")
        self.status.pack(pady=(4, 16))

        read_cpu()   # 先建立基线, 这样第一帧就能显示真实 CPU 而不是 0%

        # 后台采样线程: PowerShell 查询较慢, 绝不能放进 UI 线程
        threading.Thread(target=self._worker, args=(interval,),
                         daemon=True).start()
        root.after(150, self._poll)
        root.protocol("WM_DELETE_WINDOW", self._close)

    def _worker(self, interval):
        while not self.stop.is_set():
            t0 = time.time()
            try:
                data = sample()
            except Exception as e:          # 别让后台线程悄悄死掉
                data = {"err": str(e)}
            if self.q.full():
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    pass
            self.q.put(data)
            self.stop.wait(max(0.5, interval - (time.time() - t0)))

    def _poll(self):
        try:
            data = self.q.get_nowait()
        except queue.Empty:
            self.root.after(150, self._poll)
            return

        if data.get("err"):
            self.status.config(text=f"读取失败: {data['err']}")
        else:
            self.d_cpu.set(data["cpu"])
            self.d_mem.set(data["mem_pct"],
                           f"{data['mem_used']:.1f} / {data['mem_total']:.1f} GB")
            self.d_gpu.set(data["gpu"], f"共享显存 {data['shared']:.0f} MB")
            detail = "  ".join(
                f"{ENG_CN.get(k, k)} {v:.1f}%"
                for k, v in sorted(data["eng"].items())
            )
            self.status.config(
                text=f"CPU {data['cpu']:.1f}%   |   GPU 引擎: {detail or '空闲'}   |   "
                     f"专用显存 {data['ded']:.0f} MB"
            )
        self.root.after(150, self._poll)

    def _close(self):
        self.stop.set()
        self.root.destroy()


def main_monitor():
    if "--once" in sys.argv:
        read_cpu()          # 预热基线
        time.sleep(0.4)
        d = sample()
        if d["err"]:
            print("GPU 读取失败:", d["err"])
        else:
            detail = " ".join(f"{ENG_CN.get(k, k)}={v:.1f}%"
                              for k, v in sorted(d["eng"].items()))
            print(f"CPU {d['cpu']:.1f}% | 内存 {d['mem_used']:.2f}/"
                  f"{d['mem_total']:.1f}GB ({d['mem_pct']:.1f}%) | "
                  f"GPU {d['gpu']:.1f}% [{detail}] | "
                  f"共享显存 {d['shared']:.0f}MB")
        return

    root = tk.Tk()
    Dashboard(root)
    root.mainloop()


if __name__ == "__main__":
    main()
