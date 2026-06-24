"""
用户档案管理
持久化存储用户信息（QQ号、昵称等），实现按 QQ 号区分用户。
"""

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

PROFILE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_profiles.json")


class UserProfile:
    """单个用户的档案。"""

    def __init__(self, user_id: int, nickname: str):
        self.user_id = user_id
        self.nickname = nickname
        self.first_seen = time.time()
        self.last_active = time.time()
        self.mention_count = 1

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "nickname": self.nickname,
            "first_seen": self.first_seen,
            "last_active": self.last_active,
            "mention_count": self.mention_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UserProfile":
        profile = cls(data["user_id"], data["nickname"])
        profile.first_seen = data.get("first_seen", time.time())
        profile.last_active = data.get("last_active", time.time())
        profile.mention_count = data.get("mention_count", 1)
        return profile

    def __repr__(self) -> str:
        return f"User({self.nickname}, {self.user_id})"


class UserProfileManager:
    """管理所有用户档案，持久化到 JSON 文件。"""

    def __init__(self):
        self._profiles: dict[int, UserProfile] = {}
        self._load()

    # ------------------------------------------------------------------
    #  持久化
    # ------------------------------------------------------------------

    def _load(self):
        if not os.path.exists(PROFILE_FILE):
            return
        try:
            with open(PROFILE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for uid_str, info in data.items():
                self._profiles[int(uid_str)] = UserProfile.from_dict(info)
            logger.info(f"已加载 {len(self._profiles)} 个用户档案")
        except Exception as e:
            logger.warning(f"加载用户档案失败: {e}")

    def _save(self):
        try:
            data = {str(uid): p.to_dict() for uid, p in self._profiles.items()}
            with open(PROFILE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存用户档案失败: {e}")

    # ------------------------------------------------------------------
    #  增／删／改／查
    # ------------------------------------------------------------------

    def get_or_create(self, user_id: int, nickname: str) -> UserProfile:
        """获取或创建用户档案。nickname 仅在创建时设置，后续会随 QQ 昵称自动更新。"""
        if user_id in self._profiles:
            profile = self._profiles[user_id]
            changed = False
            # 更新昵称（QQ 昵称可能变化）
            if nickname and profile.nickname != nickname:
                old_name = profile.nickname
                profile.nickname = nickname
                logger.info(f"用户 {user_id} 昵称更新: {old_name} -> {nickname}")
                changed = True
            profile.last_active = time.time()
            profile.mention_count += 1
            if changed or profile.mention_count % 5 == 0:
                self._save()
            return profile
        profile = UserProfile(user_id, nickname or str(user_id))
        self._profiles[user_id] = profile
        self._save()
        logger.info(f"新用户: {profile.nickname}({user_id})")
        return profile

    def get_profile(self, user_id: int) -> Optional[UserProfile]:
        """获取用户档案，不存在则返回 None。"""
        return self._profiles.get(user_id)

    def get_nickname(self, user_id: int) -> str:
        """获取用户昵称，不存在则返回 QQ 号字符串。"""
        profile = self._profiles.get(user_id)
        return profile.nickname if profile else str(user_id)

    def delete_profile(self, user_id: int) -> bool:
        """删除用户档案。"""
        if user_id in self._profiles:
            del self._profiles[user_id]
            self._save()
            logger.info(f"已删除用户档案: {user_id}")
            return True
        return False

    def get_all_profiles(self) -> list[UserProfile]:
        """获取所有用户档案，按最后活跃时间降序排列。"""
        return sorted(
            self._profiles.values(),
            key=lambda p: p.last_active,
            reverse=True,
        )

    def count(self) -> int:
        return len(self._profiles)

    def save(self):
        """公开保存接口，供优雅退出时调用。"""
        self._save()
