"""
配置管理模块
加载和保存 JSON 配置文件。新格式只需 3 个字段，也兼容旧格式。

三种配置方式（优先级从低到高）：
1. config.json — 新格式: name / api_key / api_base / model(?)
2. .env 文件 — 自动加载
3. 系统环境变量
"""

import json, os, re, logging, requests, time

logger = logging.getLogger(__name__)


# ============================================================
#  路径 & 常量
# ============================================================

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DOTENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

ENV_MAP = {
    "name":         "QQBOT_NAME",
    "api_key":      "QQBOT_API_KEY",
    "api_base":     "QQBOT_API_BASE",
    "model":        "QQBOT_MODEL",
    "ws_address":   "QQBOT_WS_ADDRESS",
    "access_token": "QQBOT_ACCESS_TOKEN",
    "max_tokens":   "QQBOT_MAX_TOKENS",
    "temperature":  "QQBOT_TEMPERATURE",
    "admin_qqs":    "QQBOT_ADMIN_QQS",
    # 兼容旧环境变量名
    "_deepseek_api_key": "QQBOT_DEEPSEEK_API_KEY",
    "_openai_api_key":   "QQBOT_OPENAI_API_KEY",
    "_deepseek_api_base":"QQBOT_DEEPSEEK_API_BASE",
    "_openai_api_base":  "QQBOT_OPENAI_API_BASE",
}
DEFAULT_CONFIG = {
    # ---- 必填（只需这 3 个）----
    "name":     "",           # 平台名称（随便取）
    "api_key":  "",           # API 密钥
    "api_base": "",           # API 地址（如 https://api.deepseek.com）

    # ---- 可选 ----
    "model": "",              # 模型名，留空则自动从 API 获取

    # ---- OneBot 连接 ----
    "ws_address": "ws://127.0.0.1:6700",
    "access_token": "",

    # ---- 生成参数 ----
    "max_tokens": 2048,
    "temperature": 0.7,

    # ---- 多模态 ----
    "multimodal_models": [
        "gpt-4o", "gpt-4.", "gpt-5", "claude-3", "claude-4",
        "gemini", "deepseek-v4", "deepseek-vl",
        "qwen-vl", "qwen2-vl", "glm-4v", "internvl", "llava", "cogvlm"
    ],

    # ---- 人设 ----
    "system_prompt": "你是一个友好的 QQ 群聊助手。回复风格自然随意，像朋友聊天一样。回复简短，友善但不做作。群聊中 @ 你时才回复。如实说明自己是聊天机器人，不假装人类。"
,
    "max_history": 40,

    # ---- 管理员 ----
    "admin_qqs": [],
    "protected_admin_qqs": [],

    # ---- 故障切换链 & 配置档 ----
    "fallback_chain": [],
    "profiles": {},

    # ---- 兼容旧字段（内部使用） ----
    "provider": "openai",
}


# ============================================================
#  .env 自动加载
# ============================================================

def _load_dotenv():
    """自动加载项目目录下的 .env 文件到 os.environ（不覆盖已存在的环境变量）。"""
    if not os.path.exists(DOTENV_FILE):
        return
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


# ============================================================
#  归一化 & 兼容
# ============================================================

def _normalize(config: dict) -> dict:
    """把旧格式 / 环境变量映射到统一的 api_key / api_base。"""
    # ---- 旧 provider-特定的 key → 统一 api_key ----
    for provider in ("deepseek", "openai"):
        old_key = config.get(f"{provider}_api_key", "")
        if old_key and not config.get("api_key"):
            config["api_key"] = old_key
        old_base = config.get(f"{provider}_api_base", "")
        if old_base and not config.get("api_base"):
            config["api_base"] = old_base

    # ---- 环境变量兼容：旧 QQBOT_DEEPSEEK_API_KEY 等 ----
    for env_key, key in [
        ("QQBOT_DEEPSEEK_API_KEY",  "api_key"),
        ("QQBOT_OPENAI_API_KEY",    "api_key"),
        ("QQBOT_DEEPSEEK_API_BASE", "api_base"),
        ("QQBOT_OPENAI_API_BASE",   "api_base"),
    ]:
        val = os.environ.get(env_key, "")
        if val and not config.get(key):
            config[key] = val

    # ---- 清理 URL 尾部斜杠 ----
    if config.get("api_base"):
        config["api_base"] = config["api_base"].rstrip("/")

    return config


# ============================================================
#  加载 / 保存
# ============================================================

def _apply_env_overrides(config: dict) -> dict:
    """用环境变量覆盖配置（环境变量优先级更高）。"""
    for key, env_var in ENV_MAP.items():
        if key.startswith("_"):
            continue
        if env_var in os.environ:
            raw = os.environ[env_var]
            if key in ("max_tokens",):
                try: config[key] = int(raw)
                except ValueError: pass
            elif key in ("temperature",):
                try: config[key] = float(raw)
                except ValueError: pass
            elif key in ("admin_qqs",):
                try: config[key] = [int(x.strip()) for x in raw.split(",") if x.strip()]
                except ValueError: pass
            else:
                config[key] = raw
    return config


def load_config() -> dict:
    """加载配置：.env → config.json → 系统环境变量。"""
    _load_dotenv()

    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            user_config = json.load(f)
        config = {**DEFAULT_CONFIG, **user_config}
    else:
        save_config(DEFAULT_CONFIG)
        print(f"[配置] 已创建默认配置文件: {CONFIG_FILE}")
        print("[配置] 请编辑 config.json 填入 name / api_key / api_base")
        return dict(DEFAULT_CONFIG)

    config = _normalize(config)
    config = _apply_env_overrides(config)
    config = _normalize(config)                # 环境变量覆盖后再归一一次
    return config


def save_config(config: dict):
    """保存配置到 JSON 文件。"""
    clean = {k: v for k, v in config.items() if k in DEFAULT_CONFIG}
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)


# ============================================================
#  模型自动获取
# ============================================================

def fetch_available_models(api_base: str, api_key: str, timeout: int = 10) -> list:
    """从 API 拉取可用模型列表。失败返回空列表。"""
    url = f"{api_base.rstrip('/')}/models"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        models = [item["id"] for item in data.get("data", [])]
        return sorted(models)
    except Exception:
        return []


def pick_default_model(models: list) -> str:
    """从模型列表中智能选择一个默认值。"""
    if not models:
        return ""
    # 排除非聊天模型
    candidates = [m for m in models if "embed" not in m.lower()
                  and "moderate" not in m.lower()
                  and "tts" not in m.lower()
                  and "whisper" not in m.lower()
                  and "dall" not in m.lower()]
    if not candidates:
        candidates = models
    # 优先选含 chat 的
    chat = [m for m in candidates if "chat" in m.lower()]
    return chat[0] if chat else candidates[0]
