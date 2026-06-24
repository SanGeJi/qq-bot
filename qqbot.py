"""
QQ Bot 核心模块
基于 OneBot v11 协议的 WebSocket 客户端。
支持私聊和群聊 @ 触发，每个会话独立维护上下文。
"""

import html
import json
import logging
import os
import queue
import re
import threading
import time
from typing import Optional

import websocket

logger = logging.getLogger(__name__)

# QQ 单条消息最大长度（go-cqhttp 默认限制）
MAX_MSG_LENGTH = 4500


class QQBot:
    """QQ 机器人，通过 OneBot v11 WebSocket 协议连接 go-cqhttp。"""

    def __init__(self, config: dict, llm_client, session_manager, bot_state, user_profiles=None):
        self.config = config
        self.llm = llm_client
        self.sessions = session_manager
        self.bot_state = bot_state
        self.user_profiles = user_profiles  # UserProfileManager 实例

        self.ws_address = config["ws_address"]
        self.access_token = config.get("access_token", "")
        self.bot_qq: Optional[int] = None  # 机器人的 QQ 号，从事件中获取
        # 管理员列表：支持单个 admin_qq 兼容旧的配置方式
        raw_admin = config.get("admin_qqs", config.get("admin_qq", 0))
        if isinstance(raw_admin, int):
            self.admin_qqs: list[int] = [raw_admin] if raw_admin else []
        elif isinstance(raw_admin, list):
            self.admin_qqs = raw_admin
        else:
            self.admin_qqs = []
        self.protected_admin_qqs = config.get("protected_admin_qqs", [])
        self.start_time: float = time.time()             # 启动时间戳
        self.presets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets")

        # 从已保存的状态中恢复配置档 / 模型
        saved_profile = self.bot_state.get_profile()
        if saved_profile and config.get("profiles", {}).get(saved_profile):
            profile = config["profiles"][saved_profile]
            self.llm.switch_profile(profile)
            self.config.update(profile)
            logger.info(f"已恢复保存的配置档: {saved_profile} -> {profile.get('model')}")
        else:
            saved_model = self.bot_state.get_model()
            if saved_model:
                self.llm.model = saved_model
                self.config["model"] = saved_model
                logger.info(f"已恢复保存的模型: {saved_model}")
        # 恢复已保存的预设
        saved_preset = self.bot_state.get_preset()
        if saved_preset:
            preset_path = os.path.join(self.presets_dir, f"{saved_preset}.txt")
            if os.path.exists(preset_path):
                with open(preset_path, "r", encoding="utf-8") as f:
                    preset_prompt = f.read().strip()
                if preset_prompt:
                    self.config["system_prompt"] = preset_prompt
                    self.sessions.update_system_prompt(preset_prompt)
                    logger.info(f"已恢复保存的预设: {saved_preset} ({len(preset_prompt)} 字符)")

        self.ws: Optional[websocket.WebSocketApp] = None
        self._running = False
        # 发送队列：LLM 线程将待发消息放入队列，主线程轮询发送
        self._send_queue: queue.Queue = queue.Queue()

        # 用于通知主线程有新消息待发送
        self._send_event = threading.Event()

        # 忙碌锁：自由聊天模式下防止并发回复
        self._busy_lock = threading.Lock()

        # 自由聊天消息队列
        self._free_chat_queue: queue.Queue = queue.Queue()

        # 自由聊天判断所用的精简人设提示词
        self._judgment_prompt = (
            "【你的身份】你是群里的聊天机器人助手。"
            "你在群里潜水看消息，只有来感觉了才冒泡说话。\n"
            "【回复判断标准】\n"
            "- 有人@你、明显在跟你说话 → 一定会回\n"
            "- 话题你感兴趣（动漫、游戏、八卦、吐槽）→ 大概率回\n"
            "- 有人说了很离谱很好笑的话 → 忍不住要吐槽\n"
            "- 群里冷了半天有人抛话题 → 可能会接\n"
            "- 普通闲聊、客套、跟你无关 → 不会回\n"
            "你只能回答一个字：会 或 不会。不要解释，不要多说。"
        )

        # OneBot API 动作调用（请求/响应匹配）
        self._action_id = 0
        self._action_lock = threading.Lock()
        self._action_events: dict[str, threading.Event] = {}
        self._action_results: dict[str, dict] = {}

        # 模型列表缓存（TTL 5 分钟）
        self._model_cache: list[str] | None = None
        self._model_cache_time: float = 0
        self._model_cache_ttl: float = 300

        # 主动冒泡：偶尔在群里自己说话
        self._proactive_enabled: bool = config.get("proactive_chat", True)
        self._proactive_min_s: int = config.get("proactive_min_interval", 300)
        self._proactive_max_s: int = config.get("proactive_max_interval", 1800)
        self._proactive_last: dict[int, float] = {}  # group_id -> 上次主动发言时间
        self._proactive_cooldown: int = config.get("proactive_group_cooldown", 600)
        self._proactive_lock = threading.Lock()       # 防止并发冒泡

    # ------------------------------------------------------------------
    #  工具方法
    # ------------------------------------------------------------------

    HELP_TEXT = """可用命令：
  /预设 [预设名]         - 加载预设文件夹中的提示词
  /菜单 或 /help  - 显示此帮助
  /重置会话       - 清空当前对话上下文

管理员命令：
  /本群关闭                - 关闭当前群聊的机器人
  /本群开启                - 开启当前群聊的机器人
  /本群自由聊天开启        - 开启自由聊天（无需@，自动回复）
  /本群自由聊天关闭        - 关闭自由聊天（需@才回复）
  /全局私信关闭            - 关闭所有人的私聊（管理员除外）
  /私信全局开启            - 开启全局私聊
  /状态                    - 查看机器人运行状态
  /model [模型名]          - 查看或切换模型
	  /切换 或 /switch [配置档] - 查看或切换平台配置（含 API 地址和密钥）
  /用户列表                - 查看所有交互过的用户（QQ号、昵称、交互次数）
  /用户信息 [QQ号]         - 查看指定用户的详细信息"""

    def _uptime_str(self) -> str:
        """返回可读的运行时长。"""
        elapsed = int(time.time() - self.start_time)
        h, m = divmod(elapsed // 60, 60)
        d, h = divmod(h, 24)
        if d:
            return f"{d}天{h}小时{m}分钟"
        if h:
            return f"{h}小时{m}分钟"
        return f"{m}分钟"

    def _status_text(self) -> str:
        """生成机器人状态报告（管理员专用）。"""
        stats = self.sessions.stats()
        priv = "已开启" if self.bot_state.is_private_chat_enabled() else "已关闭"
        acquired = self._busy_lock.acquire(blocking=False)
        try:
            return (
                f"=== 机器人状态 ===\n"
                f"QQ: {self.bot_qq or '未知'}\n"
                f"运行时间: {self._uptime_str()}\n"
                f"活跃会话: {stats['active_1h']} / 总计 {stats['total']}\n"
                f"私聊全局: {priv}\n"
                f"模型: {self.config.get('model', '?')}\n"
                f"API: {self.config.get('provider', '?')}\n"
                f"忙碌: {'是' if not acquired else '否'}"
            )
        finally:
            if acquired:
                self._busy_lock.release()

    def _handle_model_cmd(self, text: str, msg_type: str, user_id: int, group_id: Optional[int]):
        """处理 /model 命令：从 API 获取可用模型列表并切换（带缓存）。"""
        parts = text.split(maxsplit=1)

        # ---- 获取模型列表（带缓存） ----
        now = time.time()
        if self._model_cache and (now - self._model_cache_time) < self._model_cache_ttl:
            models = self._model_cache
            logger.debug(f"模型列表命中缓存 ({len(models)} 个)")
        else:
            try:
                models = self.llm.fetch_models()
                self._model_cache = models
                self._model_cache_time = now
            except Exception as e:
                if self._model_cache:
                    logger.warning(f"获取模型列表失败，使用过期缓存: {e}")
                    models = self._model_cache
                else:
                    self._enqueue_send(msg_type, user_id, group_id, f"获取模型列表失败: {e}")
                    return

        if not models:
            self._enqueue_send(msg_type, user_id, group_id, "API 未返回任何可用模型")
            return

        # ---- 仅 /model → 显示 ----
        if len(parts) == 1:
            current = self.llm.model
            reply = (
                f"当前模型: {current}\n"
                f"可用模型:\n"
            )
            # 分多行显示（每行 4 个）
            for i in range(0, len(models), 4):
                reply += "  " + ", ".join(models[i:i+4]) + "\n"
            reply += f"用法: /model <模型名>"
            self._enqueue_send(msg_type, user_id, group_id, reply.strip())
            return

        # ---- /model <模型名> → 切换 ----
        requested = parts[1].strip()
        if requested not in models:
            reply = f"未知模型: {requested}\n可用: {', '.join(models[:10])}{'...' if len(models) > 10 else ''}"
            self._enqueue_send(msg_type, user_id, group_id, reply)
            return

        self.llm.model = requested
        self.config["model"] = requested
        self.bot_state.set_model(requested)
        self._enqueue_send(msg_type, user_id, group_id, f"已切换模型为: {requested}")
        logger.info(f"[管理员] 切换模型: {requested}")

    def _handle_preset_cmd(self, text: str, msg_type: str, user_id: int, group_id: Optional[int]):
        """处理 /预设 命令：加载预设文件夹中的 txt 文件作为 system_prompt。"""
        if not os.path.isdir(self.presets_dir):
            self._enqueue_send(msg_type, user_id, group_id, "预设文件夹不存在: " + self.presets_dir)
            return

        parts = text.split(None, 1)
        preset_name = parts[1].strip() if len(parts) > 1 else ""

        # ---- /预设（无参数）→ 列出可用预设 ----
        if not preset_name:
            presets = []
            for fn in sorted(os.listdir(self.presets_dir)):
                if fn.endswith(".txt"):
                    presets.append(fn[:-4])
            if not presets:
                self._enqueue_send(msg_type, user_id, group_id, "预设文件夹为空，请放入 .txt 文件")
                return
            current = self.bot_state.get_preset() or "默认"
            reply = f"当前预设: {current}\n可用预设:\n  " + "\n  ".join(presets)
            reply += f"\n\n用法: /预设 <预设名>"
            self._enqueue_send(msg_type, user_id, group_id, reply)
            return

        # ---- /预设 <预设名> → 加载预设 ----
        preset_path = os.path.join(self.presets_dir, f"{preset_name}.txt")
        if not os.path.exists(preset_path):
            self._enqueue_send(msg_type, user_id, group_id, f"预设文件不存在: {preset_name}.txt")
            return

        with open(preset_path, "r", encoding="utf-8") as f:
            prompt = f.read().strip()

        if not prompt:
            self._enqueue_send(msg_type, user_id, group_id, f"预设文件为空: {preset_name}.txt")
            return

        # 更新 system_prompt 并清空所有会话
        self.config["system_prompt"] = prompt
        self.sessions.update_system_prompt(prompt)
        self.bot_state.set_preset(preset_name)
        self._enqueue_send(msg_type, user_id, group_id, f"已加载预设: {preset_name}（{len(prompt)} 字符）")
        logger.info(f"[管理员] 加载预设: {preset_name} ({len(prompt)} 字符)")

    def _handle_switch_cmd(self, text: str, msg_type: str, user_id: int, group_id: Optional[int]):
        """处理 /切换 或 /switch 命令：查看或切换平台配置档。"""
        profiles = self.config.get("profiles", {})
        current_profile = self.bot_state.get_profile()

        parts = text.split(maxsplit=1)
        # 仅 /switch → 显示可用配置档
        if len(parts) == 1:
            lines = [f"当前配置档: {current_profile or '（默认）'}"]
            lines.append(f"当前模型: {self.llm.model}")
            lines.append(f"API: {self.llm.api_base}")
            lines.append("")
            lines.append("可用配置档:")
            for name in profiles:
                p = profiles[name]
                marker = " ← 当前" if name == current_profile else ""
                lines.append(f"  {name}: {p.get('provider')} / {p.get('model')}{marker}")
            lines.append("")
            lines.append("用法: /switch <配置档名>")
            self._enqueue_send(msg_type, user_id, group_id, "\n".join(lines))
            return

        # /switch <name> → 切换
        requested = parts[1].strip()
        if requested not in profiles:
            available = "、".join(profiles.keys())
            self._enqueue_send(msg_type, user_id, group_id, f"未知配置档: {requested}\n可用: {available}")
            return

        profile = dict(profiles[requested])  # 复制一份，不修改原配置
        # 如果配置档未填 api_key，从主配置自动补上
        if not profile.get("api_key"):
            provider = profile.get("provider", "openai")
            fallback_key = self.config.get(f"{provider}_api_key", "")
            if fallback_key:
                profile["api_key"] = fallback_key
                logger.info(f"配置档 [{requested}] 未填 api_key，自动使用主配置的 {provider}_api_key")
        self.llm.switch_profile(profile)
        self.config.update(profile)
        self.bot_state.set_profile(requested)
        self.bot_state.set_model(profile.get("model", ""))

        # 清空所有会话（避免旧会话中的多模态格式被新模型拒绝）
        self.sessions.clear_all()
        self._enqueue_send(
            msg_type, user_id, group_id,
            f"已切换至「{requested}」\n"
            f"提供商: {profile.get('provider')}\n"
            f"模型: {profile.get('model')}\n"
            f"地址: {profile.get('api_base')}\n"
            f"（已清空所有会话历史）"
        )
        logger.info(f"[管理员] 切换配置档: {requested} -> {profile.get('model')}，已清空会话")

    def _handle_user_list(self, msg_type: str, user_id: int, group_id: Optional[int]):
        """处理 /用户列表 命令：查看所有交互过的用户。"""
        if not self.user_profiles:
            self._enqueue_send(msg_type, user_id, group_id, "用户档案功能未启用")
            return
        profiles = self.user_profiles.get_all_profiles()
        if not profiles:
            self._enqueue_send(msg_type, user_id, group_id, "暂无用户记录")
            return
        parts = [f"共 {len(profiles)} 个用户："]
        for i, p in enumerate(profiles[:20], 1):
            parts.append(f"{i}. {p.nickname}({p.user_id}) — {p.mention_count}次")
        if len(profiles) > 20:
            parts.append(f"...还有 {len(profiles) - 20} 个")
        self._enqueue_send(msg_type, user_id, group_id, "\n".join(parts))

    def _handle_user_info(self, text: str, msg_type: str, user_id: int, group_id: Optional[int]):
        """处理 /用户信息 [QQ号] 命令。"""
        if not self.user_profiles:
            self._enqueue_send(msg_type, user_id, group_id, "用户档案功能未启用")
            return
        parts = text.split()
        if len(parts) < 2:
            self._enqueue_send(msg_type, user_id, group_id, "用法：/用户信息 [QQ号]")
            return
        try:
            target = int(parts[1])
        except ValueError:
            self._enqueue_send(msg_type, user_id, group_id, "QQ号格式错误")
            return
        profile = self.user_profiles.get_profile(target)
        if not profile:
            self._enqueue_send(msg_type, user_id, group_id, f"未找到用户 {target}")
            return
        from datetime import datetime
        first_seen = datetime.fromtimestamp(profile.first_seen).strftime("%Y-%m-%d %H:%M")
        last_active = datetime.fromtimestamp(profile.last_active).strftime("%Y-%m-%d %H:%M")
        reply = (
            f"用户信息：\n"
            f"昵称：{profile.nickname}\n"
            f"QQ：{profile.user_id}\n"
            f"首次互动：{first_seen}\n"
            f"最后活跃：{last_active}\n"
            f"互动次数：{profile.mention_count}"
        )
        self._enqueue_send(msg_type, user_id, group_id, reply)

    def _build_ws_url(self) -> str:
        """构造完整的 WebSocket URL（含 access_token）。"""
        url = self.ws_address
        if self.access_token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={self.access_token}"
        return url

    @staticmethod
    def _session_id(msg_type: str, user_id: int, group_id: Optional[int] = None) -> str:
        """生成会话 ID：私聊为 p_用户QQ，群聊为 g_群号。"""
        if msg_type == "private":
            return f"p_{user_id}"
        return f"g_{group_id}"

    def _is_at_me(self, message: str) -> bool:
        """检查消息是否 @ 了机器人。"""
        if self.bot_qq:
            return f"[CQ:at,qq={self.bot_qq}]" in message
        return False

    @staticmethod
    def _parse_message(raw_msg: str) -> tuple[str, list[str], list[str]]:
        """
        解析原始消息，分离纯文本、图片 URL 和图片 file。

        Returns:
            (text, image_urls, image_files) — text 为纯文本，
            image_urls 为所有图片的 url 参数，image_files 为 file 参数
        """
        images_url = []
        images_file = []
        for match in re.finditer(r'\[CQ:image,([^\]]+)\]', raw_msg):
            params = {}
            for part in match.group(1).split(','):
                if '=' in part:
                    key, value = part.split('=', 1)
                    params[key.strip()] = value.strip()
            if 'url' in params and params['url']:
                # HTML 解码（CQ 码中的 URL 可能包含 &amp; 等实体）
                images_url.append(html.unescape(params['url']))
            if 'file' in params and params['file']:
                images_file.append(params['file'])

        text = re.sub(r"\[CQ:.*?\]", "", raw_msg).strip()
        return text, images_url, images_file

    @staticmethod
    def _make_local_img_urls(file_id: str, ws_address: str) -> list[str]:
        """
        根据 go-cqhttp 地址构造本地图片访问 URL。
        go-cqhttp 默认在 HTTP 端口提供 /data/images/ 静态文件服务。
        """
        # ws://host:port → host:port
        host_port = ws_address.replace("ws://", "").replace("wss://", "")
        if ":" in host_port:
            host, port = host_port.rsplit(":", 1)
        else:
            host, port = host_port, "6700"

        candidates = []
        # 同端口尝试
        candidates.append(f"http://{host}:{port}/data/images/{file_id}")
        # go-cqhttp 默认 HTTP API 端口通常比 WS 端口小 1000
        http_port = max(int(port) - 1000, 1)
        if str(http_port) != port:
            candidates.append(f"http://{host}:{http_port}/data/images/{file_id}")
        return candidates

    @staticmethod
    def _split_long_message(text: str) -> list:
        """
        将超长消息按句子边界分割为多条。
        返回分割后的消息列表。
        """
        if len(text) <= MAX_MSG_LENGTH:
            return [text]

        parts = []
        remaining = text
        while remaining:
            if len(remaining) <= MAX_MSG_LENGTH:
                parts.append(remaining)
                break

            # 依次尝试在句号、换行、逗号处分割
            cut = -1
            for sep in ("。", "！", "？", "\n", ".", "!", "?", "，", "；", "、", "…"):
                idx = remaining.rfind(sep, 0, MAX_MSG_LENGTH)
                if idx > cut:
                    cut = idx

            if cut <= 0:
                cut = MAX_MSG_LENGTH
            else:
                cut += 1  # 包含分隔符本身

            parts.append(remaining[:cut])
            remaining = remaining[cut:]

        return parts

    # ------------------------------------------------------------------
    #  消息处理
    # ------------------------------------------------------------------

    def _handle_message(self, data: dict):
        """处理一条消息事件。"""
        msg_type = data.get("message_type")
        user_id = data.get("user_id")
        group_id = data.get("group_id")
        raw_msg = data.get("raw_message", "")

        # 提取发送者信息（用于身份识别）
        sender = data.get("sender", {})
        # 优先使用群名片（card），其次全局昵称（nickname）
        sender_nickname = sender.get("card", "") or sender.get("nickname", "") or f"用户{user_id}"

        # 更新用户档案
        if self.user_profiles:
            self.user_profiles.get_or_create(user_id, sender_nickname)

        # 过滤：仅处理私聊和群聊消息
        if msg_type not in ("private", "group"):
            return

        # 群聊消息：检查 @ 或 自由聊天模式
        if msg_type == "group":
            is_at_me = self._is_at_me(raw_msg)
            is_free_chat = self.bot_state.is_group_free_chat(group_id)
            if not is_at_me and not is_free_chat:
                return

        # ===== 解析纯文本 + 图片 =====
        text, image_urls, image_files = self._parse_message(raw_msg)

        sid = self._session_id(msg_type, user_id, group_id)
        is_admin = user_id in self.admin_qqs

        # ==============================================================
        #  公共命令（所有人可用，不受开关限制）
        # ==============================================================
        if text in ("/菜单", "/help", "/帮助"):
            self._enqueue_send(msg_type, user_id, group_id, self.HELP_TEXT)
            return

        # ==============================================================
        #  管理员命令（优先处理，不受开关限制）
        # ==============================================================
        if is_admin:
            if text == "/状态":
                self._enqueue_send(msg_type, user_id, group_id, self._status_text())
                return
            if text == "/本群关闭" and msg_type == "group":
                self.bot_state.set_group_enabled(group_id, False)
                self._enqueue_send(msg_type, user_id, group_id, "本群已关闭，不再响应消息。")
                logger.info(f"[管理员] 关闭群 {group_id}")
                return
            if text == "/本群开启" and msg_type == "group":
                self.bot_state.set_group_enabled(group_id, True)
                self._enqueue_send(msg_type, user_id, group_id, "本群已开启。")
                logger.info(f"[管理员] 开启群 {group_id}")
                return
            if text == "/本群自由聊天开启" and msg_type == "group":
                self.bot_state.set_group_free_chat(group_id, True)
                self._enqueue_send(msg_type, user_id, group_id, "本群自由聊天已开启，无需 @ 我也会回复。")
                logger.info(f"[管理员] 开启群 {group_id} 自由聊天")
                return
            if text == "/本群自由聊天关闭" and msg_type == "group":
                self.bot_state.set_group_free_chat(group_id, False)
                self._enqueue_send(msg_type, user_id, group_id, "本群自由聊天已关闭，需要 @ 我才会回复。")
                logger.info(f"[管理员] 关闭群 {group_id} 自由聊天")
                return
            if text == "/全局私信关闭":
                self.bot_state.set_private_chat_enabled(False)
                self._enqueue_send(msg_type, user_id, group_id, "全局私聊已关闭，仅管理员可私信。")
                logger.info(f"[管理员] 关闭全局私聊")
                return
            if text == "/私信全局开启":
                self.bot_state.set_private_chat_enabled(True)
                self._enqueue_send(msg_type, user_id, group_id, "全局私聊已开启。")
                logger.info(f"[管理员] 开启全局私聊")
                return
            if text.startswith("/model"):
                self._handle_model_cmd(text, msg_type, user_id, group_id)
                return
            if text.startswith("/切换") or text.startswith("/switch"):
                self._handle_switch_cmd(text, msg_type, user_id, group_id)
                return
            if text == "/用户列表":
                self._handle_user_list(msg_type, user_id, group_id)
                return
            if text.startswith("/预设"):
                self._handle_preset_cmd(text, msg_type, user_id, group_id)
                return
            if text.startswith("/用户信息"):
                self._handle_user_info(text, msg_type, user_id, group_id)
                return

        # ==============================================================
        #  权限检查
        # ==============================================================
        if msg_type == "private":
            if not self.bot_state.is_private_chat_enabled() and not is_admin:
                return
        elif msg_type == "group":
            if not self.bot_state.is_group_enabled(group_id):
                return

        # ==============================================================
        #  /重置会话（所有人可用）
        # ==============================================================
        if text == "/重置会话":
            self.sessions.reset_session(sid)
            self._enqueue_send(msg_type, user_id, group_id, "会话已重置，我们可以重新开始对话了。")
            logger.info(f"[{sid}] 会话已重置")
            return

        # 无文字内容的消息（纯 CQ 码，如大表情）不处理图片
        if not text and image_urls:
            logger.info(f"[{sid}] 纯 CQ 码消息（无文字），跳过图片处理: {raw_msg[:100]}")
            return
        if not text and not image_urls:
            return

        # ==============================================================
        #  自由聊天模式（未被 @）：进入消息队列统一判断
        # ==============================================================
        is_at_me = self._is_at_me(raw_msg)
        in_free_chat = (msg_type == "group" and self.bot_state.is_group_free_chat(group_id))
        if in_free_chat and not is_at_me:
            # 只有纯文本才进队列；带图片的跳过（避免下载成本）
            self._free_chat_queue.put({
                "msg_type": msg_type,
                "user_id": user_id,
                "group_id": group_id,
                "text": text,
                "sender_nickname": sender_nickname,
                "sid": sid,
            })
            logger.debug(f"[{sid}] 自由聊天消息已入队: {text[:60]}")
            return

        # ==============================================================
        #  正常 LLM 对话（@ 触发 或 私聊，支持多模态图片）
        # ==============================================================
        # 自由聊天被 @ 时：统一入队处理，由队列线程管理忙碌锁
        if in_free_chat and is_at_me:
            self._free_chat_queue.put({
                "msg_type": msg_type,
                "user_id": user_id,
                "group_id": group_id,
                "text": text,
                "sender_nickname": sender_nickname,
                "sid": sid,
                "is_at": True,
                "image_urls": image_urls,
                "image_files": image_files,
            })
            logger.info(f"[{sid}] @消息入队（自由聊天模式）")
            return

        log_text = text[:80] if text else "(图片消息)"
        logger.info(f"[{sid}] 收到消息: {log_text}" +
                    (f" + {len(image_urls)} 张图片" if image_urls else ""))

        session = self.sessions.get_session(sid)

        threading.Thread(
            target=self._process_llm,
            args=(session, msg_type, user_id, group_id,
                  text, image_urls, image_files, sender_nickname),
            daemon=True,
        ).start()

    # ------------------------------------------------------------------
    #  自由聊天消息队列处理
    # ------------------------------------------------------------------

    _STOP_KEYWORDS = [
        "别说了", "闭嘴", "安静", "别回了", "停下", "别吵", "别bb",
        "不要说了", "别刷屏", "别叫", "别闹", "消停", "别说话",
        "少说两句", "别叭叭", "别发了", "住口", "收声",
    ]

    def _should_stay_quiet(self, session, current_text: str) -> bool:
        """根据最近聊天上下文判断是否应该闭嘴。

        Returns True 表示应该保持沉默（不回消息）。
        """
        messages = session.messages
        if not messages:
            return False

        # ---- 被明确叫停：最近 5 条消息里有人让机器人闭嘴 ----
        for msg in messages[-5:]:
            content = msg["content"]
            if isinstance(content, list):
                content = "".join(
                    p.get("text", "") for p in content if p.get("type") == "text"
                )
            content = str(content)
            for kw in self._STOP_KEYWORDS:
                if kw in content:
                    logger.info(f"[{session.session_id}] 检测到停止信号「{kw}」，保持沉默")
                    return True

        # ---- 被无视：最后连续 2 条都是自己发的，说明没人理 ----
        recent_roles = [msg["role"] for msg in messages[-3:]]
        if len(recent_roles) >= 2 and recent_roles[-2:] == ["assistant", "assistant"]:
            logger.info(f"[{session.session_id}] 连续自言自语，可能被无视，保持沉默")
            return True

        return False

    def _process_queued_free_chat(self, item: dict):
        """处理队列中的一条自由聊天消息：先用 quick_judge 预判断，再决定是否完整回复。"""
        msg_type = item["msg_type"]
        user_id = item["user_id"]
        group_id = item["group_id"]
        text = item["text"]
        sender_nickname = item["sender_nickname"]
        sid = item["sid"]
        is_at = item.get("is_at", False)
        image_urls = item.get("image_urls", [])
        image_files = item.get("image_files", [])

        # ---- @ 消息不受任何限制 ----
        if not is_at:
            # 检查是否被叫停或被无视
            session = self.sessions.get_session(sid)
            if self._should_stay_quiet(session, text):
                return

            # ---- quick_judge：极低成本预判断是否值得回复 ----
            recent_context = ""
            msgs = session.messages
            if msgs:
                recent = msgs[-6:]  # 最近 6 条作为上下文
                lines = []
                for m in recent:
                    content = m["content"]
                    if isinstance(content, list):
                        content = "".join(
                            p.get("text", "") for p in content if p.get("type") == "text"
                        )
                    role = m["role"]
                    prefix = "群友" if role == "user" else "你"
                    lines.append(f"{prefix}: {str(content)[:120]}")
                recent_context = "\n".join(lines)

            judge_prompt = (
                f"【最近聊天】\n{recent_context}\n\n"
                f"【新消息】{sender_nickname} 说: {text[:200]}\n\n"
                f"你应该回复这条消息吗？"
            )

            try:
                should_reply = self.llm.quick_judge(self._judgment_prompt, judge_prompt, timeout=10)
            except Exception:
                # quick_judge 失败时保守处理：不回复
                should_reply = False

            if not should_reply:
                logger.debug(f"[{sid}] quick_judge 判定不回复: {text[:60]}")
                return

            logger.info(f"[{sid}] quick_judge 判定回复: {text[:60]}")

        # ---- 获取忙碌锁 ----
        if not self._busy_lock.acquire(blocking=False):
            logger.debug(f"[{sid}] 忙碌中，队列消息跳过")
            return

        session = self.sessions.get_session(sid)
        try:
            self._process_llm(
                session, msg_type, user_id, group_id,
                text=text, image_urls=image_urls,
                image_files=image_files,
                sender_nickname=sender_nickname,
            )
        except Exception as e:
            sanitized = self._sanitize_error(e)
            logger.error(f"[{sid}] 队列消息处理失败: {sanitized}")
        finally:
            self._busy_lock.release()

    # ------------------------------------------------------------------
    #  发送消息
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_error(error: Exception) -> str:
        """脱敏错误信息，隐藏 API key 等敏感内容。"""
        msg = str(error)
        # 隐藏 API key 格式的字符串
        msg = re.sub(r'sk-[a-zA-Z0-9]{20,}', 'sk-****', msg)
        msg = re.sub(r'[Bb]earer\s+sk-[a-zA-Z0-9]{20,}', 'Bearer sk-****', msg)
        # 截断过长信息
        if len(msg) > 120:
            msg = msg[:120] + '...'
        return msg

    def _process_llm(self, session, msg_type: str, user_id: int, group_id: Optional[int],
                     text: str = "", image_urls: list[str] | None = None,
                     image_files: list[str] | None = None,
                     sender_nickname: str = ""):
        """在后台线程中调用 LLM 并发送回复（支持多模态图片）。

        注意：本方法不管理忙碌锁，锁由调用方（_process_queued_free_chat）统一管理。
        """
        try:
            # ---- 下载图片并转 base64 ----
            image_base64_list: list[str] = []
            if image_urls:
                logger.info(f"[{session.session_id}] 正在下载 {len(image_urls)} 张图片...")
                for i, url in enumerate(image_urls):
                    logger.info(f"[{session.session_id}] 图片{i+1} URL: {url[:120]}")
                    b64 = self.llm.download_image_as_base64(url)
                    # URL 下载失败 → 尝试 get_image API 获取 file_uuid
                    if not b64 and image_files and i < len(image_files):
                        file_id = image_files[i]
                        logger.info(f"[{session.session_id}] 尝试 get_image API: {file_id}")
                        result = self._call_action("get_image", {"file": file_id}, timeout=8)
                        logger.info(f"[{session.session_id}] get_image 返回: {result}")
                        if result and result.get("status") == "ok":
                            data = result.get("data", {})
                            logger.info(f"[{session.session_id}] get_image data 字段: {list(data.keys())}")
                            # NapCatQQ: 用 file_uuid 构造带签名的下载链接
                            file_uuid = data.get("file_uuid", "")
                            if file_uuid:
                                signed_url = f"https://multimedia.nt.qq.com.cn/download?appid=1406&fileid={file_uuid}"
                                logger.info(f"[{session.session_id}] 尝试带签名 URL ({len(file_uuid)} 字符)")
                                b64 = self.llm.download_image_as_base64(signed_url)
                                if b64:
                                    logger.info(f"[{session.session_id}] 签名 URL 下载成功")
                            # 标准 OneBot: file_path 直接读磁盘
                            file_path = data.get("file_path", "")
                            if not b64 and file_path:
                                b64 = self._read_image_from_disk(file_path)
                        # 全盘搜索兜底
                        if not b64:
                            logger.info(f"[{session.session_id}] 尝试全盘搜索: {file_id}")
                            found = self._search_image_on_disk(file_id)
                            if found:
                                b64 = self._read_image_from_disk(found)
                    if b64:
                        image_base64_list.append(b64)
                if image_base64_list:
                    logger.info(f"[{session.session_id}] 图片下载完成 {len(image_base64_list)}/{len(image_urls)}")
                else:
                    logger.warning(f"[{session.session_id}] 所有图片下载失败，仅发送文本")

            # ---- 将用户消息加入会话 ----
            is_group = (msg_type == "group")
            session.add_message(
                "user", text,
                user_nickname=sender_nickname,
                user_id=user_id,
                is_group=is_group,
                images=image_base64_list if image_base64_list else None,
            )

            # ---- 判断每个主人是否已经和 AI 对话过 ----
            owner_chat_status = {}
            if self.user_profiles:
                for qq in self.admin_qqs:
                    profile = self.user_profiles.get_profile(qq)
                    owner_chat_status[qq] = profile is not None and profile.mention_count > 0

            # ---- 调用 LLM（带自动切换兜底） ----
            messages = session.get_messages(owner_chat_status, current_user_id=user_id)
            reply = self._call_llm_with_fallback(session, messages, msg_type, user_id, group_id)
            # 去掉模型模仿身份标签格式在开头加的 [昵称]/[昵称(QQ)] 前缀，
            # 先清理再入历史，避免坏习惯被一轮轮自我强化
            reply = self._strip_self_prefix(reply)
            session.add_message("assistant", reply)
            # 检测回复中的图片链接，转成 CQ 码图片
            final_reply = self._process_reply_images(reply, session)
            self._enqueue_send(msg_type, user_id, group_id, final_reply)
            logger.info(f"[{session.session_id}] 已回复 {len(reply)} 字符")
        except Exception as e:
            sanitized = self._sanitize_error(e)
            logger.error(f"[{session.session_id}] LLM 错误: {sanitized}")
            self._enqueue_send(
                msg_type, user_id, group_id,
                f"处理消息时出错了: {sanitized}",
            )
    def _switch_to_profile(self, name: str) -> bool:
        """按名称切换到指定配置档，成功返回 True。"""
        profiles = self.config.get("profiles", {})
        if name not in profiles:
            return False
        profile = dict(profiles[name])
        if not profile.get("api_key"):
            provider = profile.get("provider", "openai")
            fallback_key = self.config.get(f"{provider}_api_key", "")
            if fallback_key:
                profile["api_key"] = fallback_key
        self.llm.switch_profile(profile)
        self.config.update(profile)
        self.bot_state.set_profile(name)
        self.bot_state.set_model(profile.get("model", ""))
        logger.info(f"自动切换至配置档: {name} -> {profile.get('model')}")
        return True

    def _call_llm_with_fallback(self, session, messages, msg_type, user_id, group_id) -> str:
        """
        调用 LLM，失败时按 fallback_chain 自动切换并重试。
        全部失败则抛出最后一个异常。
        """
        fallback_chain = self.config.get("fallback_chain", [])
        if not fallback_chain:
            return self.llm.chat(messages)

        tried = set()
        last_error = None

        for attempt in range(len(fallback_chain) + 1):
            try:
                return self.llm.chat(messages)
            except Exception as e:
                last_error = e
                current_profile = self.bot_state.get_profile()
                logger.warning(
                    f"[{session.session_id}] {self.llm.model} 调用失败: {self._sanitize_error(e)}"
                )
                next_profile = None
                for name in fallback_chain:
                    if name not in tried and name != current_profile:
                        next_profile = name
                        break
                if not next_profile:
                    break
                tried.add(current_profile)
                session.reset()
                self._switch_to_profile(next_profile)
                logger.info(f"[{session.session_id}] 自动切换至 [{next_profile}]，重试中...")

        raise last_error or Exception("所有 API 均不可用")


    # 机器人自我昵称（用于识别并剥离模型自加的身份标签前缀）。
    # 这些来自全局人设，所有群通用；其它群若用了不同自称，可在
    # config.json 顶层加 "self_aliases": ["别名1", "别名2"] 扩展。
    _SELF_ALIASES = set()

    def _self_aliases(self) -> set[str]:
        aliases = set(self._SELF_ALIASES)
        for a in (self.config.get("self_aliases") or []):
            a = str(a).strip().lower()
            if a:
                aliases.add(a)
        return aliases

    def _strip_self_prefix(self, reply: str) -> str:
        """剥离模型模仿 [昵称(QQ号)] 格式、在自己回复开头加的身份标签。

        仅当标签里的昵称是机器人自己的别名，或括号里的 QQ 号等于机器人 QQ 时才剥离，
        避免误删正常以 [ 开头的内容（如别的群友标签、正常括号）。最多连续剥离 3 层。
        多群通用：别名来自全局人设，QQ 号兜底匹配本机，与具体群无关。
        """
        if not reply:
            return reply
        aliases = self._self_aliases()
        bot_qq = str(self.bot_qq) if self.bot_qq else None
        for _ in range(3):
            m = re.match(r'^\s*\[\s*([^\]\(]{0,20}?)\s*(?:\(\s*(\d{5,})\s*\))?\s*\]\s*', reply)
            if not m:
                break
            name = (m.group(1) or "").strip().lower()
            qq = m.group(2)
            if name in aliases or (bot_qq and qq == bot_qq):
                reply = reply[m.end():]
                continue
            break
        return reply

    def _process_reply_images(self, reply: str, session) -> str:
        """
        检测 LLM 回复中的图片链接，转换为 QQ CQ 码图片。
        支持格式：
          - Markdown: ![alt](url)
          - 直接 URL: https://...jpg/png/gif/webp
          - base64 data: data:image/...;base64,...

        Returns:
            含 CQ 码的消息文本，若无图片则返回原文本
        """
        import re as _re
        import os as _os
        import base64 as _b64

        # 搜索所有图片 URL（Markdown 格式 + 直接 URL + base64）
        img_pattern = _re.compile(
            r'!\[.*?\]\((https?://[^\s)]+)\)'  # ![alt](url)
            r'|(?<![\w)])(https?://[^\s]+\.(?:jpg|jpeg|png|gif|webp)(?:\?[^\s]*)?)'  # direct URL
            r'|(data:image/[a-z]+;base64,[a-zA-Z0-9+/=]+)',  # base64 data
            _re.IGNORECASE
        )

        matches = list(img_pattern.finditer(reply))
        if not matches:
            return reply

        logger.info(f"[{session.session_id}] 回复中发现 {len(matches)} 个图片链接，尝试转换...")

        cq_tags = []
        last_end = 0
        for m in matches:
            url = m.group(1) or m.group(2) or m.group(3) or m.group(0)
            if not url:
                continue

            cq = None
            # base64 data 直接用
            if url.startswith('data:'):
                cq = f'[CQ:image,file={url}]'
            else:
                # 下载 URL 图片转 base64
                try:
                    b64 = self.llm.download_image_as_base64(url)
                    if b64:
                        cq = f'[CQ:image,file={b64}]'
                    else:
                        logger.warning(f"[{session.session_id}] 下载图片失败，保留链接: {url[:60]}...")
                except Exception as e:
                    logger.warning(f"[{session.session_id}] 图片处理失败: {e}")

            if cq:
                cq_tags.append((m.start(), m.end(), cq))

        # 构建最终消息：用 CQ 码替换图片链接，保留周围文字
        if not cq_tags:
            return reply

        result_parts = []
        last_end = 0
        for start, end, cq in cq_tags:
            if start > last_end:
                result_parts.append(reply[last_end:start])
            result_parts.append(cq)
            last_end = end
        if last_end < len(reply):
            result_parts.append(reply[last_end:])

        final = ''.join(result_parts)
        logger.info(f"[{session.session_id}] 已将 {len(cq_tags)} 个图片转为 QQ 图片消息")
        return final

    #  发送消息
    # ------------------------------------------------------------------

    def _censor_owner_info(self, message: str, user_id: int | None = None) -> str:
        """
        后置过滤：将消息中所有保密主人 QQ 号替换为隐藏文本。
        如果消息是发给保密主人本人，则不拦截（让本人看到自己的身份）。
        使用正则 \b 边界避免误匹配其他数字的子串。
        """
        protected = getattr(self, 'protected_admin_qqs', None) or []
        if not protected:
            return message
        # 发给保密主人本人时不拦截
        if user_id and user_id in protected:
            return message
        censored = message
        for qq in protected:
            qq_str = str(qq)
            censored = re.sub(r'\b' + re.escape(qq_str) + r'\b', '******', censored)
        if censored != message:
            logger.warning(f"[安全过滤] 已拦截消息中暴露的保密主人 QQ 号")
        return censored

    def _enqueue_send(self, msg_type: str, user_id: int, group_id: Optional[int], message: str):
        """将待发送消息放入队列（线程安全）。
        进入队列前经过安全过滤，防止主人信息泄露。
        """
        message = self._censor_owner_info(message, user_id=user_id)
        self._send_queue.put((msg_type, user_id, group_id, message))
        self._send_event.set()

    def _send_single(self, action: str, params: dict):
        """发送一条 WebSocket 请求（从主线程调用）。"""
        if not self.ws:
            logger.warning("WebSocket 未连接，无法发送消息")
            return

        payload = json.dumps({"action": action, "params": params}, ensure_ascii=False)
        try:
            self.ws.send(payload)
        except Exception as e:
            logger.error(f"发送消息失败: {e}")

    def _call_action(self, action: str, params: dict, timeout: float = 10) -> dict | None:
        """
        发送 OneBot API 动作并等待响应（线程安全）。

        通过 WebSocket 发送带 echo 的请求，等待服务端返回匹配的响应。
        """
        with self._action_lock:
            self._action_id += 1
            echo = f"q_{self._action_id}"
            event = threading.Event()
            self._action_events[echo] = event

        payload = json.dumps({
            "action": action,
            "params": params,
            "echo": echo,
        }, ensure_ascii=False)
        try:
            self.ws.send(payload)
        except Exception as e:
            self._action_events.pop(echo, None)
            logger.error(f"API 请求发送失败: {e}")
            return None

        event.wait(timeout=timeout)

        result = self._action_results.pop(echo, None)
        self._action_events.pop(echo, None)

        if result is None:
            logger.warning(f"API 动作 {action} 超时（{timeout}秒）")
        return result

    @staticmethod
    def _read_image_from_disk(file_path: str) -> str | None:
        """
        从本地磁盘读取图片文件并转为 base64 data URL。

        Args:
            file_path: 文件的绝对路径

        Returns:
            data:image/xxx;base64,... 或 None
        """
        import base64 as _b64
        try:
            with open(file_path, "rb") as f:
                data = f.read()
            ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else "jpeg"
            if ext in ("jpg", "jpeg"):
                ext = "jpeg"
            elif ext == "png":
                ext = "png"
            elif ext == "gif":
                ext = "gif"
            elif ext == "webp":
                ext = "webp"
            else:
                ext = "jpeg"
            b64 = _b64.b64encode(data).decode("utf-8")
            logger.info(f"本地图片读取成功: {file_path} ({len(data)} 字节)")
            return f"data:image/{ext};base64,{b64}"
        except Exception as e:
            logger.warning(f"本地图片读取失败 {file_path}: {e}")
            return None

    @staticmethod
    def _search_image_on_disk(file_id: str) -> str | None:
        """
        在常见 NapCat 数据目录中搜索图片文件。

        Args:
            file_id: 图片文件名（如 xxx.image 或 xxx.jpg）

        Returns:
            文件的绝对路径，未找到返回 None
        """
        import os as _os
        # 当前项目目录
        p = _os.path.join("data", "images", file_id)
        if _os.path.isfile(p):
            return _os.path.abspath(p)

        # NapCat 安装目录
        base = _os.path.expanduser("~")
        search_dirs = [
            # NapCat 安装目录下的缓存
            "D:/napcat/napcat/cache",
            # 常见用户数据目录
            _os.path.join(base, "AppData", "Roaming", "NapCatQQ", "data", "images"),
            _os.path.join(base, "AppData", "Local", "NapCatQQ", "data", "images"),
            _os.path.join(base, "Documents", "NapCatQQ", "data", "images"),
            _os.path.join(base, ".config", "napcat", "data", "images"),
            # QQNT 默认图片目录
            _os.path.join(base, "Documents", "Tencent Files"),
            _os.path.join(base, "AppData", "Local", "Tencent"),
        ]
        for d in search_dirs:
            if _os.path.isdir(d):
                # 先搜当前目录
                fp = _os.path.join(d, file_id)
                if _os.path.isfile(fp):
                    logger.info(f"在磁盘找到图片: {fp}")
                    return _os.path.abspath(fp)
                # 再搜下一层子目录（如 Tencent Files 下的每个 QQ 目录）
                try:
                    for entry in _os.scandir(d):
                        if entry.is_dir():
                            fp2 = _os.path.join(entry.path, file_id)
                            if _os.path.isfile(fp2):
                                logger.info(f"在磁盘找到图片: {fp2}")
                                return _os.path.abspath(fp2)
                            # 再进一层（Image 子目录）
                            try:
                                for sub in _os.scandir(entry.path):
                                    if sub.is_dir():
                                        fp3 = _os.path.join(sub.path, file_id)
                                        if _os.path.isfile(fp3):
                                            logger.info(f"在磁盘找到图片: {fp3}")
                                            return _os.path.abspath(fp3)
                            except PermissionError:
                                continue
                except PermissionError:
                    continue

        return None

    def _send_msg(self, msg_type: str, user_id: int, group_id: Optional[int], message: str):
        """发送一条 QQ 消息，自动处理分割和 @ 回复。"""
        if msg_type == "private":
            action = "send_private_msg"
            base_params: dict = {"user_id": user_id}
        else:
            action = "send_group_msg"
            # 群聊中 @ 提问者
            message = f"[CQ:at,qq={user_id}] {message}"
            base_params = {"group_id": group_id}

        parts = self._split_long_message(message)
        for i, part in enumerate(parts):
            params = {**base_params, "message": part}
            self._send_single(action, params)
            if i > 0:
                time.sleep(0.3)  # 多条消息间隔，防止过快

    # ------------------------------------------------------------------
    #  WebSocket 事件处理器
    # ------------------------------------------------------------------

    def _on_open(self, ws):
        logger.info(f"✅ WebSocket 已连接: {self.ws_address}")
        # 连接成功后立即对所有自由聊天群冒泡（后台线程，不阻塞 WS）
        if self._proactive_enabled and self.bot_state._group_free_chat:
            threading.Thread(target=self._try_proactive_speak, daemon=True).start()

    def _on_message(self, ws, raw: str):
        """收到 WebSocket 消息（JSON 格式的 OneBot 事件 / API 响应）。"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        # ---- 优先匹配 API 动作的响应（基于 echo 字段） ----
        if "echo" in data:
            echo = data["echo"]
            if echo in self._action_events:
                self._action_results[echo] = data
                self._action_events[echo].set()
            return

        # ---- 普通事件 ----
        # 从任何事件中捕获机器人自身的 QQ 号
        if "self_id" in data and data["self_id"]:
            self.bot_qq = data["self_id"]

        post_type = data.get("post_type")

        if post_type == "meta_event":
            # 心跳 / 生命周期事件，忽略
            return

        if post_type == "message":
            self._handle_message(data)

    def _on_error(self, ws, error):
        logger.error(f"WebSocket 错误: {error}")

    def _on_close(self, ws, status, msg):
        logger.info(f"WebSocket 已断开 (status={status}): {msg}")

    # ------------------------------------------------------------------
    #  发送队列轮询
    # ------------------------------------------------------------------

    def _process_send_queue(self):
        """处理发送队列中的所有消息。"""
        while True:
            try:
                msg_type, user_id, group_id, message = self._send_queue.get_nowait()
                self._send_msg(msg_type, user_id, group_id, message)
            except queue.Empty:
                break

    # ------------------------------------------------------------------
    #  主动冒泡
    # ------------------------------------------------------------------

    def _try_proactive_speak(self):
        """在自由聊天开启的群里主动说话 —— 每个符合条件的群都发。"""
        # 防止并发冒泡（_on_open 和定时循环可能同时触发）
        if not self._proactive_lock.acquire(blocking=False):
            logger.debug("主动冒泡: 上一轮尚未完成，跳过")
            return
        try:
            self._try_proactive_speak_locked()
        except Exception as e:
            logger.error(f"主动冒泡异常: {e}")
        finally:
            self._proactive_lock.release()

    def _try_proactive_speak_locked(self):
        """主动冒泡核心逻辑（调用方已持有 _proactive_lock）。"""
        now = time.time()

        # 找出候选群：自由聊天开启 + 冷却已过
        candidates = []
        skipped_cooldown = 0
        for group_id_str, enabled in self.bot_state._group_free_chat.items():
            if not enabled:
                continue
            try:
                group_id = int(group_id_str)
            except ValueError:
                continue
            if now - self._proactive_last.get(group_id, 0) < self._proactive_cooldown:
                skipped_cooldown += 1
                continue
            sid = f"g_{group_id}"
            session = self.sessions.get_session(sid)
            candidates.append((group_id, session))

        logger.info(
            f"主动冒泡: 自由聊天群共 {len(self.bot_state._group_free_chat)} 个, "
            f"候选 {len(candidates)} 个"
            + (f" (跳过 {skipped_cooldown} 个冷却中)" if skipped_cooldown else "")
        )

        if not candidates:
            if int(now / 1800) != int(getattr(self, '_proactive_logged_half_hour', 0) / 1800):
                self._proactive_logged_half_hour = now
            return

        spoke_count = 0
        for group_id, session in candidates:
            msgs = session.messages

            # ---- 构建上下文 prompt ----
            if msgs:
                recent = msgs[-8:]
                lines = []
                for m in recent:
                    content = m["content"]
                    if isinstance(content, list):
                        content = "".join(
                            p.get("text", "") for p in content if p.get("type") == "text"
                        )
                    role = m["role"]
                    label = "群友" if role == "user" else "助手"
                    lines.append(f"{label}: {str(content)[:150]}")
                context = "\n".join(lines)

                prompt = (
                    f"【最近聊天记录】\n{context}\n\n"
                    f"【你的任务】你刚才一直在看群但不说话。现在决定要不要说句话。\n"
                    f"- 如果群里在聊你感兴趣的话题（动漫、游戏、八卦、吐槽），可以插一句\n"
                    f"- 如果群里很安静没人说话，你也可以冒个泡说自己在干嘛\n"
                    f"- 如果最近的话题你完全插不上嘴，或者刚说过话不久，就闭嘴\n"
                    f"- 要说话就说一句自然的、像真人随口说的话（不超过60字）\n"
                    f"你只能说一句话，或者如果觉得不该说话就回答「不发言」。不要解释。"
                )
            else:
                prompt = (
                    f"【场景】群聊里一直没人说话，很安静。\n"
                    f"【你的任务】作为群聊助手，随口冒个泡打破沉默。\n"
                    f"- 可以说说自己在干嘛（刚睡醒/在刷B站/打游戏输了/逛小红书看到好玩的）\n"
                    f"- 可以吐槽一句（好无聊/周一好烦/又饿了）\n"
                    f"- 自然、简短、像真人随口碎碎念（不超过60字）\n"
                    f"你只能说一句话。不要解释，不要加「不发言」。"
                )

            logger.info(f"主动冒泡: 群 {group_id} 调用 LLM 生成发言...")
            try:
                reply = self.llm.chat(
                    [{"role": "system", "content": prompt}],
                    retry=0, max_tokens=80, temperature=0.9,
                )
                reply = reply.strip()
                skip_guard = not msgs
                if not reply:
                    logger.info(f"主动冒泡: 群 {group_id} LLM 返回空，跳过")
                    continue
                if not skip_guard and "不发言" in reply:
                    logger.info(f"主动冒泡: 群 {group_id} LLM 决定不发言，跳过")
                    continue

                reply = self._strip_self_prefix(reply)
                if not reply:
                    logger.info(f"主动冒泡: 群 {group_id} 发言被前缀剥离后为空，跳过")
                    continue

                self._proactive_last[group_id] = now

                # 检查 WebSocket 状态
                if not self.ws:
                    logger.warning(f"主动冒泡: 群 {group_id} WS 已断开，无法发送: {reply[:40]}")
                    continue

                logger.info(f"主动冒泡: 群 {group_id} 准备发送: {reply[:60]}")
                parts = self._split_long_message(reply)
                for i, part in enumerate(parts):
                    self._send_single("send_group_msg", {"group_id": group_id, "message": part})
                    logger.debug(f"主动冒泡: 群 {group_id} 已发送第 {i+1}/{len(parts)} 段")
                    if i > 0:
                        time.sleep(0.3)

                session.add_message("assistant", reply)
                logger.info(f"主动冒泡成功 ✅: 群 {group_id} → {reply[:60]}")
                spoke_count += 1

                # 群之间小间隔，避免 API 限流
                time.sleep(1.5)
            except Exception as e:
                logger.warning(f"主动冒泡失败 ❌: 群 {group_id} → {e}")

        if spoke_count > 0:
            logger.info(f"主动冒泡完成: 共同步 {spoke_count}/{len(candidates)} 个群")

    # ------------------------------------------------------------------
    #  主循环
    # ------------------------------------------------------------------

    def run(self):
        """启动机器人，持续运行直到被停止。"""
        self._running = True
        reconnect_delay = 1  # 初始重连延迟（秒）

        # 启动发送队列处理线程（轮询 _send_event）
        def _sender_loop():
            while self._running:
                self._send_event.wait(timeout=0.5)
                self._send_event.clear()
                self._process_send_queue()

        sender_thread = threading.Thread(target=_sender_loop, daemon=True)
        sender_thread.start()

        # 定时清理过期会话（每 6 小时）
        def _cleanup_loop():
            while self._running:
                time.sleep(6 * 3600)
                self.sessions.cleanup_old(max_age_hours=24)

        cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True)
        cleanup_thread.start()

        # 自由聊天消息队列处理线程
        def _free_chat_loop():
            while self._running:
                try:
                    item = self._free_chat_queue.get(timeout=1)
                    self._process_queued_free_chat(item)
                except queue.Empty:
                    continue
                except Exception as e:
                    logger.error(f"自由聊天队列处理异常: {e}")

        free_chat_thread = threading.Thread(target=_free_chat_loop, daemon=True)
        free_chat_thread.start()

        # 主动冒泡线程（定时触发）
        import random as _random

        def _proactive_loop():
            while self._running:
                interval = _random.randint(self._proactive_min_s, self._proactive_max_s)
                for _ in range(interval):
                    if not self._running:
                        break
                    time.sleep(1)
                if not self._running:
                    break
                if self._proactive_enabled:
                    try:
                        self._try_proactive_speak()
                    except Exception as e:
                        logger.error(f"主动冒泡异常: {e}")

        proactive_thread = threading.Thread(target=_proactive_loop, daemon=True)
        proactive_thread.start()

        while self._running:
            try:
                url = self._build_ws_url()
                logger.info(f"正在连接 WebSocket: {url}")

                self.ws = websocket.WebSocketApp(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )

                # run_forever 阻塞直到断开连接
                self.ws.run_forever(
                    ping_interval=30,
                    ping_timeout=10,
                )

                # 连接断开后清理
                self.ws = None
                reconnect_delay = min(reconnect_delay * 2, 30)

                if self._running:
                    logger.info(f"将在 {reconnect_delay} 秒后重连...")
                    time.sleep(reconnect_delay)

            except KeyboardInterrupt:
                self.stop()
                break
            except Exception as e:
                logger.error(f"运行时错误: {e}")
                if self._running:
                    time.sleep(reconnect_delay)

        logger.info("机器人已停止")

    def stop(self):
        """停止机器人。"""
        logger.info("正在停止机器人...")
        self._running = False
        # 持久化用户档案
        if self.user_profiles:
            try:
                self.user_profiles.save()
                logger.info("用户档案已保存")
            except Exception as e:
                logger.error(f"保存用户档案失败: {e}")
        if self.ws:
            self.ws.close()
            self.ws = None
