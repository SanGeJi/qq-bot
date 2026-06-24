"""
机器人状态管理
持久化存储群开关、私聊开关等状态。
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_state.json")


class BotState:
    """管理群启用/禁用和私聊全局开关的状态（持久化到文件）。"""

    def __init__(self):
        self._group_enabled: dict[str, bool] = {}
        self._group_free_chat: dict[str, bool] = {}
        self._private_chat_enabled: bool = True
        self._current_model: str = ""
        self._current_profile: str = ""
        self._current_preset: str = ""
        self._load()

    # ------------------------------------------------------------------
    #  持久化
    # ------------------------------------------------------------------

    def _load(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._group_enabled = data.get("group_enabled", {})
            self._group_free_chat = data.get("group_free_chat", {})
            self._private_chat_enabled = data.get("private_chat_enabled", True)
            self._current_model = data.get("current_model", "")
            self._current_profile = data.get("current_profile", "")
            self._current_preset = data.get("current_preset", "")
            logger.info(f"已加载状态: {len(self._group_enabled)} 个群开关, {sum(1 for v in self._group_free_chat.values() if v)} 个自由聊天, 模型={self._current_model or '默认'}, 配置档={self._current_profile or '默认'}")
        except Exception as e:
            logger.warning(f"加载状态文件失败: {e}")

    def _save(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "group_enabled": self._group_enabled,
                    "group_free_chat": self._group_free_chat,
                    "private_chat_enabled": self._private_chat_enabled,
                    "current_model": self._current_model,
                    "current_profile": self._current_profile,
                    "current_preset": self._current_preset,
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存状态失败: {e}")

    # ------------------------------------------------------------------
    #  群开关
    # ------------------------------------------------------------------

    def is_group_enabled(self, group_id: int) -> bool:
        """检查群是否已启用。"""
        return self._group_enabled.get(str(group_id), True)

    def set_group_enabled(self, group_id: int, enabled: bool):
        """设置群启用/禁用。"""
        self._group_enabled[str(group_id)] = enabled
        self._save()
        logger.info(f"群 {group_id} -> {'启用' if enabled else '禁用'}")

    # ------------------------------------------------------------------
    #  私聊全局开关
    # ------------------------------------------------------------------

    def is_private_chat_enabled(self) -> bool:
        """检查全局私聊是否已启用。"""
        return self._private_chat_enabled

    def set_private_chat_enabled(self, enabled: bool):
        """设置全局私聊启用/禁用。"""
        self._private_chat_enabled = enabled
        self._save()
        logger.info(f"全局私聊 -> {'启用' if enabled else '禁用'}")

    # ------------------------------------------------------------------
    #  模型切换
    # ------------------------------------------------------------------

    def get_model(self) -> str:
        """获取已保存的自定义模型名（空字符串表示使用配置默认值）。"""
        return self._current_model

    def set_model(self, model: str):
        """保存自定义模型名。"""
        self._current_model = model
        self._save()
        logger.info(f"模型已保存: {model or '默认'}")

    # ------------------------------------------------------------------
    #  配置档切换
    # ------------------------------------------------------------------

    def get_profile(self) -> str:
        """获取已保存的配置档名（空字符串表示未切换）。"""
        return self._current_profile

    def set_profile(self, name: str):
        """保存当前配置档名。"""
        self._current_profile = name
        self._save()
        logger.info(f"配置档已保存: {name or '默认'}")

    def get_preset(self) -> str:
        """获取当前加载的预设名称。"""
        return self._current_preset

    def set_preset(self, name: str):
        """保存当前加载的预设名称。"""
        self._current_preset = name
        self._save()
        logger.info(f"预设已保存: {name or '默认'}")

    # ------------------------------------------------------------------
    #  群自由聊天
    # ------------------------------------------------------------------

    def is_group_free_chat(self, group_id: int) -> bool:
        """检查群是否开启了自由聊天模式。"""
        return self._group_free_chat.get(str(group_id), False)

    def set_group_free_chat(self, group_id: int, enabled: bool):
        """设置群自由聊天模式。"""
        self._group_free_chat[str(group_id)] = enabled
        self._save()
        logger.info(f"群 {group_id} 自由聊天 -> {'开启' if enabled else '关闭'}")
