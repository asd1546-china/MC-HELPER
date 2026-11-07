# -*- coding: utf-8 -*-
"""
MC_RUN —— 用 minecraft_launcher_lib 启动 PCL2 实例的脚本
依赖库已随工程放在 ./lib 目录（自带，可移植）。

特性：
  * 默认测试实例：航空山谷（NeoForge，需 Java 21）
  * 【自动内存检测 + 优化】根据物理内存 / 可用内存 / CPU 核数，
    自动算出最适合整合包的 -Xmx / -Xms 与 G1GC 调优参数，
    并在最终命令里强制去重，保证优化参数一定生效。

用法：
  python launch_mc.py                      # 用顶部 INSTANCE 配置直接启动
  python launch_mc.py --run               # 真正启动（覆盖 DRY_RUN）
  python launch_mc.py --dry               # 强制只打印命令不启动
  python launch_mc.py --select            # 弹出菜单，手动选实例
  python launch_mc.py -i 航空山谷          # 直接指定实例（--instance 简写 -i）
  python launch_mc.py --xmx 6             # 手动指定最大内存 6GB（覆盖自动）
  python launch_mc.py --xms 4             # 手动指定初始内存 4GB
"""
import os
import sys
import argparse
import subprocess

# ===== 把工程自带的库目录加进搜索路径（让脚本不依赖外部已装的 mll）=====
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_DIR, "MC_RUN/lib"))

import minecraft_launcher_lib as mll

# ======================= 配置区（按需修改）=======================
MINECRAFT_DIR = r"G:\new word"          # PCL2 的 .minecraft 根目录
INSTANCE      = "航空山谷"               # PCL2 实例名（同时就是版本 id）
GAME_DIR      = os.path.join(MINECRAFT_DIR, "versions", INSTANCE)  # PCL2 版本隔离目录

# Java 路径：NeoForge 1.21.1 需要 Java 21。留空 "" 则使用 PATH 里的 java
JAVA_PATH     = r"C:\Program Files\BellSoft\LibericaJDK-21\bin\java.exe"

USERNAME      = "Steve"                 # 离线模式显示名（进游戏后可在设置改）
DRY_RUN       = False                    # True=只打印命令不进游戏；False=直接启动游戏

# ---- 内存优化 ----
# None = 自动检测并优化；填数字（单位 GB）则强制使用，例如 FORCE_XMX_GB = 6
FORCE_XMX_GB  = None
FORCE_XMS_GB  = None
MODDED        = True                    # 整合包（含大量 mod）→ True，原版 → False
PRE_TOUCH     = True                    # 启动时一次性占满内存，消除运行期分配卡顿（启动略慢）

# ---- 第三方账号（外置登录 / authlib-injector）----
# 如果你要连第三方认证服务器的多人游戏，把下面三项填好并把开关打开。
# 纯单人/离线游玩保持 False 即可。
USE_AUTHLIB_INJECTOR = False
AUTHLIB_JAR = r""                       # 例如 G:\authlib-injector.jar
AUTH_SERVER  = r""                      # 例如 https://你的认证服务器/api/authlib-injector
# ===============================================================


# ======================= 自动内存检测 + 优化 =======================
def _system_memory_windows():
    """通过 kernel32.GlobalMemoryStatusEx 取物理内存（Windows 免依赖）"""
    try:
        import ctypes
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
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return stat.ullTotalPhys, stat.ullAvailPhys
    except Exception:
        pass
    return 0, 0


def get_physical_memory():
    """返回 (总物理内存字节, 当前可用物理内存字节)。优先 psutil，回退到系统 API。"""
    # 1) psutil（最准，含实时可用内存）
    try:
        import psutil
        vm = psutil.virtual_memory()
        return vm.total, vm.available
    except Exception:
        pass
    # 2) Windows 原生 API
    total, avail = _system_memory_windows()
    if total:
        return total, avail
    # 3) Linux / macOS 回退（只能取到总量，可用按总量估算）
    try:
        if hasattr(os, "sysconf"):
            ps = os.sysconf("SC_PAGE_SIZE")
            pn = os.sysconf("SC_PHYS_PAGES")
            if ps and pn:
                return ps * pn, ps * pn
    except Exception:
        pass
    return 0, 0


def _gb(byte_val):
    return byte_val / (1024 ** 3)


def recommend_memory(total_gb, available_gb, modded=True, cores=1):
    """
    根据内存规模给出最优 -Xmx（单位 GB，浮点），并返回“为何如此”的原因。
    模型要点：
      * 给系统预留一定内存，避免把机器拖到卡死；
      * 整合包 JVM 自身还有 metaspace / 线程栈 / 本地代码等额外开销；
      * G1GC 在大堆上停顿风险上升，设一个甜点上限；
      * 若当前可用内存不足，自动下调并提示。
    """
    # 系统预留（OS + 后台程序）
    os_reserve = 3.0 if total_gb >= 8 else (2.0 if total_gb >= 4 else 1.0)
    # JVM 非堆开销（整合包更吃这块）
    jvm_overhead = 1.5 if modded else 0.8
    # 甜点上限：再大对 MC 提升有限，反而增加 GC 停顿
    cap = 12.0 if modded else 8.0

    base = total_gb - os_reserve - jvm_overhead
    xmx = min(base, cap)
    reason = ""
    if xmx >= cap - 0.001:
        reason = f"已达甜点上限 {cap:.0f}GB（再大对 MC 帮助有限，反而增加 GC 停顿）"
    else:
        reason = f"按总量推算：{total_gb:.1f}GB - 系统预留 {os_reserve:.0f}GB - JVM 开销 {jvm_overhead:.1f}GB"

    xmx = max(xmx, 2.0)  # 无论如何不低于 2GB，否则整合包必崩

    # 尊重“当前可用”，避免启动时过度承诺导致分配失败
    avail_safe = available_gb * 0.85
    if xmx > avail_safe:
        xmx = max(2.0, avail_safe)
        reason = f"受当前可用内存限制（仅 {available_gb:.1f}GB），已安全下调以防分配失败"

    # 取整到 0.5GB，干净利落
    xmx = round(xmx * 2) / 2
    return xmx, reason


def build_jvm_optimizations(xmx_gb, xms_gb, cores, modded=True):
    """拼出一套整合包友好的 G1GC 优化参数。"""
    # 并行/并发 GC 线程数：按核心数给，封顶避免过多线程争抢
    parallel = max(1, min(cores, 8))
    concurrent = max(1, min(cores // 4, 4))

    # JVM 的 -Xmx/-Xms 不接受小数 GB（如 -Xmx4.5G 会直接报 Invalid maximum heap size），
    # 统一换算成整数 MB 传入，保留 0.5GB 粒度的同时保证参数合法。
    args = [
        f"-Xmx{int(xmx_gb * 1024)}M",
        f"-Xms{int(xms_gb * 1024)}M",
        "-XX:+UseG1GC",
        "-XX:+UnlockExperimentalVMOptions",
        "-XX:G1NewSizePercent=20",
        "-XX:G1ReservePercent=20",
        "-XX:MaxGCPauseMillis=50",
        "-XX:+DisableExplicitGC",           # 屏蔽 MC 偶发的 System.gc()，防卡顿
        f"-XX:ParallelGCThreads={parallel}",
        f"-XX:ConcGCThreads={concurrent}",
    ]
    if PRE_TOUCH:
        args.append("-XX:+AlwaysPreTouch")  # 启动即占满，消除运行期分配抖动
    if modded:
        # 整合包常见：元空间容易涨，给个上限防 OOM
        args.append("-XX:MaxMetaspaceSize=512M")
    return args


def _enforce_single_heap(command, xmx_gb, xms_gb):
    """
    在最终命令里去重 -Xmx/-Xms（移除任何其它来源的同名参数），
    并把我们算出的 -Xmx/-Xms 插到 Java 路径之后、其余 JVM 参数之前，确保生效。
    """
    out = []
    i = 0
    while i < len(command):
        tok = command[i]
        if tok in ("-Xmx", "-Xms"):
            # 形如 "-Xmx 6G" 的分隔开写法的标志位，跳过标志与紧随的值
            i += 1
            if i < len(command) and not command[i].startswith("-"):
                i += 1
            continue
        if tok.startswith("-Xmx") or tok.startswith("-Xms"):
            i += 1
            continue
        out.append(tok)
        i += 1
    # 在 java 可执行文件（index 0）之后插入我们的堆设置
    out[1:1] = [f"-Xmx{int(xmx_gb * 1024)}M", f"-Xms{int(xms_gb * 1024)}M"]
    return out


def _dedupe_path_lists(command):
    """
    去掉 -cp / -p 路径列表里重复的 jar。
    NeoForge 版本 json 常把「原版依赖」和「NeoForge 自带依赖」各列一份，
    mll 忠实拼接后 -cp 出现同路径两次；securejarhandler 的 UnionFileSystem
    用 Collectors.toMap 收集时会抛 "Duplicate key xxx.jar"，游戏直接启动失败。
    按首次出现顺序去重，不改变类加载顺序。
    """
    path_flags = ("-cp", "-classpath", "-p", "--module-path")
    out = list(command)
    for i, tok in enumerate(out):
        flag, eq, inline = tok.partition("=")
        if flag not in path_flags:
            continue
        idx = i if (eq and inline) else i + 1
        if idx >= len(out):
            continue
        raw = inline if (eq and inline) else out[idx]
        seen, uniq = set(), []
        for p in raw.split(os.pathsep):
            key = os.path.normcase(os.path.normpath(p)) if p else p
            if p and key not in seen:
                seen.add(key)
                uniq.append(p)
        out[idx] = (flag + "=" if (eq and inline) else "") + os.pathsep.join(uniq)
    return out


def detect_and_optimize(force_xmx=None, force_xms=None):
    """跑完检测 + 推荐，返回 (jvm_args, info_dict)。"""
    total_b, avail_b = get_physical_memory()
    cores = os.cpu_count() or 1
    total_gb, avail_gb = _gb(total_b), _gb(avail_b)

    if force_xmx is not None:
        xmx = float(force_xmx)
        note = "（手动指定，已覆盖自动检测）"
        reason = "用户手动指定"
    else:
        xmx, reason = recommend_memory(total_gb, avail_gb, MODDED, cores)
        note = "（自动检测优化）"

    # 初始堆：整合包建议 Xms = Xmx，避免运行期堆伸缩带来的卡顿
    if force_xms is not None:
        xms = float(force_xms)
    else:
        xms = xmx

    info = {
        "total_gb": total_gb,
        "avail_gb": avail_gb,
        "cores": cores,
        "xmx": xmx,
        "xms": xms,
        "note": note,
        "reason": reason,
    }
    jvm_args = build_jvm_optimizations(xmx, xms, cores, MODDED)
    return jvm_args, info


# ======================= 游戏实例选择 =======================
def list_instances():
    """
    扫描 MINECRAFT_DIR/versions，返回「可直接启动」的实例名列表。
    判定标准：目录下存在与目录同名的 <实例名>.json（mll 才能构造启动命令）。
    PCL2 的「关联实例 / 服务器配置」没有版本 json，会被自动过滤掉。
    """
    vdir = os.path.join(MINECRAFT_DIR, "versions")
    if not os.path.isdir(vdir):
        return []
    out = []
    for name in sorted(os.listdir(vdir)):
        full = os.path.join(vdir, name)
        if not os.path.isdir(full):
            continue
        if os.path.isfile(os.path.join(full, name + ".json")):
            out.append(name)
    return out


def select_instance():
    """交互式选择实例：打印编号菜单，支持输入序号或直接打实例名，回车用默认。"""
    instances = list_instances()
    if not instances:
        raise SystemExit(
            f"[错误] 在 {MINECRAFT_DIR}\\versions 下没找到任何可启动的实例"
            f"（需要含 <实例名>.json）。"
        )

    print("\n请选择要启动的游戏实例：")
    for i, name in enumerate(instances, 1):
        mark = "  *" if name == INSTANCE else ""
        print(f"  [{i}] {name}{mark}")
    if INSTANCE:
        print("  （* 为脚本默认实例；直接回车即选它）")

    while True:
        try:
            choice = input("输入序号或实例名（回车=默认）: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\n已取消。")
        if choice == "":
            return INSTANCE if INSTANCE in instances else instances[0]
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(instances):
                return instances[idx - 1]
            print("序号超出范围，重试。")
            continue
        if choice in instances:
            return choice
        print("未找到该实例，重试。")


# ======================= 启动命令构造 =======================
def build_command(instance, force_xmx=None, force_xms=None):
    version = instance
    ver_dir = os.path.join(MINECRAFT_DIR, "versions", version)
    if not os.path.isdir(ver_dir):
        raise SystemExit(f"[错误] 找不到版本目录：{ver_dir}\n请检查 MINECRAFT_DIR 与实例名。")
    game_dir = ver_dir  # PCL2 版本隔离目录（mods/config/saves 都在这里）

    java = JAVA_PATH if (JAVA_PATH and os.path.exists(JAVA_PATH)) else "java"

    # 1) 抽取原生库（.dll 等）到实例的 natives 目录，避免启动报缺库
    natives_dir = os.path.join(ver_dir, "natives")
    try:
        mll.natives.extract_natives(version, MINECRAFT_DIR, natives_dir)
        print(f"[OK] 原生库已就绪：{natives_dir}")
    except Exception as e:
        print(f"[警告] 抽取原生库时出现问题（通常可忽略）：{e}")

    # 2) 自动内存检测 + 优化
    jvm_args, mem = detect_and_optimize(force_xmx, force_xms)
    _print_memory_report(mem, jvm_args)

    # 3) 构造启动参数。generate_test_options 会给出合法的离线 uuid/token
    options = mll.utils.generate_test_options()
    options["username"] = USERNAME
    options["gameDirectory"] = game_dir
    options["executablePath"] = java
    options["nativesDirectory"] = natives_dir
    options["jvmArguments"] = jvm_args  # 我们的优化参数（会在命令里去重保证生效）

    # 4) 第三方账号：注入 authlib-injector（-javaagent）
    if USE_AUTHLIB_INJECTOR and AUTHLIB_JAR and AUTH_SERVER:
        if not os.path.exists(AUTHLIB_JAR):
            raise SystemExit(f"[错误] authlib-injector.jar 不存在：{AUTHLIB_JAR}")
        options.setdefault("jvmArguments", []).append(
            f"-javaagent:{AUTHLIB_JAR}={AUTH_SERVER}"
        )

    # 5) 生成完整启动命令
    command = mll.command.get_minecraft_command(version, MINECRAFT_DIR, options)
    # 强制去重 -Xmx/-Xms，确保我们的优化一定生效
    command = _enforce_single_heap(command, mem["xmx"], mem["xms"])
    # 去掉 -cp/-p 里重复的 jar，避免 NeoForge securejarhandler 抛 Duplicate key
    command = _dedupe_path_lists(command)
    return command, java, game_dir


def _print_memory_report(mem, jvm_args):
    line = "=" * 52
    print(line)
    print("  自动内存检测 + JVM 优化报告")
    print(line)
    if mem["total_gb"] > 0:
        print(f"  物理内存总量 : {mem['total_gb']:.1f} GB")
        print(f"  当前可用内存 : {mem['avail_gb']:.1f} GB")
    else:
        print("  （无法读取系统内存，使用保守默认）")
    print(f"  CPU 逻辑核心 : {mem['cores']}")
    print(f"  整合包模式   : {'是 (modded)' if MODDED else '否 (原版)'}")
    print(f"  >>> 分配 -Xmx {mem['xmx']:.1f} GB  -Xms {mem['xms']:.1f} GB  {mem['note']}")
    print(f"      原因：{mem.get('reason', '')}")
    print("-" * 52)
    print("  JVM 优化参数:")
    for a in jvm_args:
        print(f"    {a}")
    print(line + "\n")


# ======================= 入口 =======================
def main_minecraftlauncher():
    parser = argparse.ArgumentParser(description="MC_RUN 启动器（含自动内存优化 + 实例选择）")
    parser.add_argument("--run", action="store_true", help="真正启动游戏（否则只打印命令）")
    parser.add_argument("--dry", action="store_true", help="强制只打印命令不启动")
    parser.add_argument("--xmx", type=float, default=None, help="手动指定最大内存 GB，覆盖自动检测")
    parser.add_argument("--xms", type=float, default=None, help="手动指定初始内存 GB")
    parser.add_argument("--instance", "-i", default=None,
                        help="指定实例名（如 航空山谷）；省略且未配置默认实例则交互选择")
    parser.add_argument("--select", "-s", action="store_true", help="强制弹出实例选择菜单")
    args = parser.parse_args()

    # 解析要启动的实例
    available = list_instances()
    if args.instance:
        instance = args.instance
        if instance not in available:
            raise SystemExit(
                f"[错误] 找不到实例：{instance}\n"
                f"可用实例：{', '.join(available) if available else '（无）'}"
            )
    elif args.select or not INSTANCE:
        instance = select_instance()
    else:
        instance = INSTANCE

    # 优先级：命令行 --xmx > 配置 FORCE_XMX_GB > 自动检测
    force_xmx = args.xmx if args.xmx is not None else FORCE_XMX_GB
    force_xms = args.xms if args.xms is not None else FORCE_XMS_GB

    dry = DRY_RUN
    if args.run:
        dry = False
    if args.dry:
        dry = True

    command, java, game_dir = build_command(instance, force_xmx, force_xms)

    print("===== 启动命令（节选关键段）=====")
    # 完整命令可能很长，这里完整打印
    print(" ".join(command))
    print("==================================\n")

    if dry:
        print("[DRY_RUN] 未真正启动。加 --run 参数即可进游戏（或把脚本顶部 DRY_RUN 设为 False 让默认直接启动）。")
        return

    print("正在启动 Minecraft（实例：%s）..." % instance)
    # cwd 设为实例目录，日志等会落在实例文件夹里（与 PCL2 行为一致）
    subprocess.run(command, cwd=game_dir)


if __name__ == "__main__":
    main_minecraftlauncher()
