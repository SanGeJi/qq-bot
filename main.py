#!/usr/bin/env python3
"""
QQ DeepSeek Bot - 基于 OneBot v11 协议的 QQ 机器人

功能：
- 支持 DeepSeek 官方 API 和 OpenAI 兼容 API（中转）
- 私聊自动回复，群聊 @ 机器人回复
- 每个私聊/群聊独立会话上下文
- /重置会话 指令清空当前会话历史

使用方式：
1. 安装依赖：pip install -r requirements.txt
2. 编辑 config.json 填入 API 密钥和配置
3. 启动 go-cqhttp，确保 WebSocket 服务已开启
4. 运行：python main.py
"""

import logging
import os
import signal
import sys

# Windows 控制台编码修复
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

# 将项目目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import load_config
from config import fetch_available_models, pick_default_model
from session import SessionManager
from llm_client import LLMClient
from state import BotState
from user_profiles import UserProfileManager
from qqbot import QQBot


def setup_logging():
    """配置日志输出到控制台和文件。"""
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")

    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


def validate_config(config: dict) -> bool:
    """验证配置是否有效。"""
    api_key = config.get("api_key", "")
    api_base = config.get("api_base", "")

    if not api_key:
        print("[ERROR] 请在 config.json 中配置 api_key")
        print("        或设置环境变量 QQBOT_API_KEY")
        return False

    if not api_base:
        print("[ERROR] 请在 config.json 中配置 api_base")
        print("        或设置环境变量 QQBOT_API_BASE")
        return False

    return True


def main():
    setup_logging()
    logger = logging.getLogger("main")

    print("=" * 55)
    print("  QQ DeepSeek Bot")
    print("=" * 55)

    # 加载配置
    config = load_config()
    project_dir = os.path.dirname(os.path.abspath(__file__))
    logger.info(f"项目目录: {project_dir}")

    # 验证
    if not validate_config(config):
        sys.exit(1)

    logger.info(f"平台: {config.get('name', '')}")
    logger.info(f"API 地址: {config['api_base']}")
    logger.info(f"WebSocket 地址: {config['ws_address']}")
    logger.info(f"模型: {config.get('model') or '(待获取)'}")

    # 兼容新旧配置，取主人列表
    raw_admin = config.get("admin_qqs", config.get("admin_qq", 0))
    if isinstance(raw_admin, int):
        admin_qqs = [raw_admin] if raw_admin else []
    elif isinstance(raw_admin, list):
        admin_qqs = raw_admin
    else:
        admin_qqs = []

    if admin_qqs:
        logger.info(f"主人列表: {', '.join(str(a) for a in admin_qqs)}")
    else:
        logger.info("未设置主人")

    # 初始化各模块
    protected_admin_qqs = config.get("protected_admin_qqs", [])

    session_manager = SessionManager(
        system_prompt=config["system_prompt"],
        max_history=config["max_history"],
        admin_qqs=admin_qqs,
        protected_admin_qqs=protected_admin_qqs,
    )

    # 启动时自动获取模型（如果未指定）
    if not config.get("model"):
        print("\n[配置] 未指定模型，正在从 API 获取可用模型列表 ...")
        models = fetch_available_models(config["api_base"], config["api_key"])
        if models:
            default_model = pick_default_model(models)
            config["model"] = default_model
            logger.info(f"找到 {len(models)} 个可用模型")
            logger.info(f"自动选择模型: {default_model}")
            print(f"[配置] 找到 {len(models)} 个模型，自动选择: {default_model}")
            print("  (启动后用 /model 命令查看完整列表并切换)")
        else:
            logger.warning("无法获取模型列表，将使用 config.json 中指定的模型")

    llm_client = LLMClient(config)

    bot_state = BotState()

    user_profiles = UserProfileManager()
    logger.info(f"已加载 {user_profiles.count()} 个用户档案")

    bot = QQBot(config, llm_client, session_manager, bot_state, user_profiles)

    # 处理退出信号
    def signal_handler(sig, frame):
        logger.info("收到退出信号，正在停止...")
        bot.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("\n[Bot] 机器人已启动，等待消息中...")
    print("   (按 Ctrl+C 停止)\n")

    try:
        bot.run()
    except KeyboardInterrupt:
        pass
    finally:
        bot.stop()
        logger.info("机器人已停止")


if __name__ == "__main__":
    main()
