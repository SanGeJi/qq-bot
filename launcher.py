"""
智能启动器
自动启动 NapCatQQ，等它就绪后再启动 QQ Bot。
双击这个文件即可一键启动。
"""

import os
import socket
import subprocess
import sys
import threading
import time

# 加载 .env 文件（让 NAPCTAT_DIR 等环境变量生效）
DOTENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(DOTENV_FILE):
    with open(DOTENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and val:
                os.environ.setdefault(key, val)

NAPCTAT_DIR = os.environ.get("NAPCTAT_DIR", "").strip() or os.path.join(os.path.dirname(os.path.abspath(__file__)), "napcat")
NAPCTAT_BAT = os.path.join(NAPCTAT_DIR, "napcat.bat")
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_MAIN = os.path.join(BOT_DIR, "main.py")
WS_PORT = 6700
TIMEOUT = 90  # 等待 NapCat 最长时间（秒）


def print_step(step: str):
    print(f"  >> {step}")


def check_port(port: int, host: str = "127.0.0.1") -> bool:
    """检测端口是否已开放。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        result = s.connect_ex((host, port))
        s.close()
        return result == 0
    except Exception:
        return False


def wait_for_port(port: int, timeout: int) -> bool:
    """等待端口开放，最多等 timeout 秒。"""
    print_step(f"等待 NapCat WebSocket (port {port}) ...")
    start = time.time()
    while time.time() - start < timeout:
        if check_port(port):
            elapsed = int(time.time() - start)
            print_step(f"NapCat 已就绪（耗时 {elapsed} 秒）")
            return True
        time.sleep(2)
    return False


def start_napcat() -> subprocess.Popen | None:
    """启动 NapCatQQ，返回进程对象。"""
    if not os.path.exists(NAPCTAT_BAT):
        print(f"[错误] 找不到 NapCat: {NAPCTAT_BAT}")
        return None

    print_step("启动 NapCatQQ ...")
    try:
        proc = subprocess.Popen(
            f'cmd /c start "NapCatQQ" "{NAPCTAT_BAT}"',
            cwd=NAPCTAT_DIR,
            shell=True,
        )
        return proc
    except Exception as e:
        print(f"[错误] 启动 NapCat 失败: {e}")
        return None


def start_bot():
    """启动 Python 机器人。"""
    print_step("启动 Python 机器人 ...")
    os.chdir(BOT_DIR)
    try:
        subprocess.run([sys.executable, BOT_MAIN], check=True)
    except KeyboardInterrupt:
        print("\n[退出] 用户中断")
    except Exception as e:
        print(f"[错误] 机器人异常退出: {e}")
        input("按 Enter 键关闭 ...")


def main():
    print("=" * 50)
    print("  QQ DeepSeek Bot - 智能启动器")
    print("=" * 50)
    print()

    # 1. 检查端口是否已被占用（NapCat 可能已经在运行）
    if check_port(WS_PORT):
        print_step("检测到 NapCat 已在运行，直接启动机器人 ...")
        print()
        start_bot()
        return

    # 2. 启动 NapCat
    napcat_proc = start_napcat()
    if napcat_proc is None:
        input("按 Enter 键关闭 ...")
        return

    print()
    napcat_ready = wait_for_port(WS_PORT, TIMEOUT)

    if not napcat_ready:
        print(f"[警告] NapCat 在 {TIMEOUT} 秒内未就绪")
        print("  可能原因：")
        print("  1. NapCat 正在等待扫码登录")
        print(f"     请打开 http://127.0.0.1:6099/webui 扫码")
        print(f"  2. NapCat 启动失败，请检查 {NAPCTAT_DIR} 目录")
        resp = input("\n是否继续启动机器人？(y/n): ").strip().lower()
        if resp != 'y':
            print("[退出]")
            input("按 Enter 键关闭 ...")
            return

    print()
    start_bot()

    print()
    print("[提示] NapCat 仍在后台运行")
    print("如需完全退出，请关闭 NapCat 窗口")


if __name__ == "__main__":
    main()
