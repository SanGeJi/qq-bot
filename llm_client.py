"""
LLM API 客户端
兼容 DeepSeek 官方 API 和 OpenAI 格式的中转 API。
"""

import base64
import logging
import time
from typing import List, Dict, Optional

import requests

logger = logging.getLogger(__name__)


class LLMClient:
    """支持 DeepSeek / OpenAI 兼容 API 的聊天客户端。"""

    def __init__(self, config: dict):
        self.provider = config.get("provider", "openai")
        self.api_key = config.get("api_key", "")
        self.api_base = config.get("api_base", "").rstrip("/")

        self.model = config.get("model", "deepseek-chat")
        self.max_tokens = config.get("max_tokens", 2048)
        self.temperature = config.get("temperature", 0.7)

        # 共用 HTTP session（连接复用）
        self._http = requests.Session()
        self._http.headers.update({
            "Authorization": f"Bearer {self.api_key}" if self.api_key else "",
            "Content-Type": "application/json" if self.api_key else "",
        })

        if self.api_key:
            logger.info(
                f"LLM 客户端初始化: base={self.api_base}, model={self.model}"
            )

    def switch_profile(self, profile: dict):
        """
        运行时切换 API 配置（provider / api_key / api_base / model）。

        立即生效，不需要新建客户端实例。
        """
        self.provider = profile.get("provider", "openai")
        self.api_key = profile.get("api_key", self.api_key)
        self.api_base = profile.get("api_base", self.api_base).rstrip("/")
        self.model = profile.get("model", self.model)

        # 更新 HTTP 会话的 Authorization 头
        self._http.headers.update({
            "Authorization": f"Bearer {self.api_key}",
        })

        logger.info(
            f"已切换配置: provider={self.provider}, "
            f"base={self.api_base}, model={self.model}"
        )

    def chat(self, messages: List[Dict[str, str]], retry: int = 1,
             max_tokens: int | None = None, temperature: float | None = None) -> str:
        """
        发送聊天请求并返回回复文本。

        Args:
            messages: 消息列表（含 system 消息）
            retry: 网络错误时的重试次数
            max_tokens: 覆盖默认的 max_tokens（None 则使用配置值）
            temperature: 覆盖默认的 temperature（None 则使用配置值）

        Returns:
            AI 回复文本

        Raises:
            Exception: API 调用失败时抛出
        """
        url = f"{self.api_base}/chat/completions"

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
            "temperature": temperature if temperature is not None else self.temperature,
            "stream": False,
        }

        last_error = None

        for attempt in range(1 + retry):
            try:
                logger.debug(
                    f"LLM 请求 (attempt {attempt+1}): "
                    f"{len(messages)} 条消息, model={self.model}"
                )

                resp = self._http.post(url, json=payload, timeout=60)

                if resp.status_code == 200:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    logger.debug(f"LLM 响应: {len(content)} 字符")
                    return content

                # 非 200 错误
                error_msg = f"API 错误 (HTTP {resp.status_code})"
                try:
                    detail = resp.json()
                    error_msg += f": {detail.get('error', {}).get('message', detail)}"
                except Exception:
                    error_msg += f": {resp.text[:300]}"

                # 4xx 错误（鉴权、限流等）不重试
                if 400 <= resp.status_code < 500:
                    raise Exception(error_msg)

                # 5xx 错误可以重试
                last_error = Exception(error_msg)
                logger.warning(f"{error_msg}，将重试...")

            except requests.exceptions.Timeout:
                last_error = Exception("API 请求超时，请稍后再试")
                logger.warning(f"请求超时 (attempt {attempt+1})")
            except requests.exceptions.ConnectionError as e:
                last_error = Exception(f"无法连接 API 服务器: {url}")
                logger.warning(f"连接失败 (attempt {attempt+1}): {e}")

            # 重试前等待（指数退避）
            if attempt < retry:
                wait = 2 ** attempt
                logger.info(f"将在 {wait} 秒后重试...")
                time.sleep(wait)

        raise last_error or Exception("未知 API 错误")

    def fetch_models(self, timeout: int = 10) -> List[str]:
        """
        从 API 获取可用模型列表。

        Returns:
            模型 ID 列表（仅保留聊天类模型）

        Raises:
            Exception: 请求失败时抛出
        """
        url = f"{self.api_base}/models"
        try:
            # 不用 self._http（带 Content-Type: application/json 的共享 Session），
            # 部分中转站对带 JSON Content-Type 的 GET 请求处理异常导致超时
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=timeout,
            )
            if resp.status_code != 200:
                raise Exception(f"获取模型列表失败 (HTTP {resp.status_code})")

            data = resp.json()
            all_models = [item["id"] for item in data.get("data", [])]

            # 返回全部模型（多供应商中转站可能包含各类模型名）
            return sorted(all_models)

        except requests.exceptions.Timeout:
            raise Exception("获取模型列表超时")
        except requests.exceptions.ConnectionError:
            raise Exception(f"无法连接 {url}")

    # ------------------------------------------------------------------
    #  多模态（图片）支持
    # ------------------------------------------------------------------

    def quick_judge(self, system_prompt: str, user_prompt: str,
                    timeout: int = 15) -> bool:
        """
        快速判断：只用极少 token 让 LLM 回答是/否。

        Args:
            system_prompt: 人设提示（精简版）
            user_prompt: 包含上下文+当前消息+判断问题
            timeout: 请求超时（秒）

        Returns:
            True 表示应该回复，False 表示不回复
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        try:
            reply = self.chat(
                messages, retry=0,
                max_tokens=2, temperature=0.1,
            )
            # 只要回答中包含 "会" 且不包含 "不会"，就认为应该回复
            return "会" in reply and "不会" not in reply
        except Exception:
            # 判断失败时保守处理：不回复（避免在群里太聒噪）
            return False

    def download_image_as_base64(self, url: str) -> str:
        """
        下载图片并转换为 base64 数据 URL。

        注意：QQ 图片 URL（gchat.qpic.cn）过期很快，且可能有防盗链，
        这里使用独立 HTTP 请求（不带 API Authorization 头）并模拟浏览器。

        Args:
            url: 图片 URL（如 QQ 的 CQ 码中的 url）

        Returns:
            data:image/xxx;base64,... 格式的数据 URL
            下载失败时返回空字符串
        """
        # 尝试多种方式下载
        strategies = [
            ("浏览器头", self._make_browser_download),
            ("纯 requests", self._make_plain_download),
            ("跳过 SSL", lambda u: _fallback_download(u)),
        ]

        last_error = None
        for name, func in strategies:
            try:
                result = func(url)
                if result:
                    return result
            except Exception as e:
                last_error = e
                logger.debug(f"下载策略 [{name}] 失败: {e}")

        logger.warning(f"图片下载失败（已尝试 {len(strategies)} 种方式）: {url[:80]}... 错误: {last_error}")
        return ""

    def _make_browser_download(self, url: str) -> str | None:
        """策略1：模拟浏览器下载"""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Referer": "https://qun.qq.com/",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=8)
        resp.raise_for_status()
        return self._bytes_to_data_url(resp)

    def _make_plain_download(self, url: str) -> str | None:
        """策略2：最简单的请求，无特殊头"""
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        return self._bytes_to_data_url(resp)

    def _bytes_to_data_url(self, resp: requests.Response) -> str | None:
        """将 requests Response 转为 base64 data URL"""
        content_type = resp.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            logger.warning(f"返回类型不是图片 (Content-Type: {content_type})，跳过")
            logger.debug(f"返回内容前200字节: {resp.content[:200]}")
            return None

        if "png" in content_type:
            ext = "png"
        elif "gif" in content_type:
            ext = "gif"
        elif "webp" in content_type:
            ext = "webp"
        elif "bmp" in content_type:
            ext = "bmp"
        else:
            ext = "jpeg"

        b64 = base64.b64encode(resp.content).decode("utf-8")
        logger.info(f"图片下载成功: {len(resp.content)} 字节 -> data:image/{ext};base64,... ({len(b64)} 字符)")
        return f"data:image/{ext};base64,{b64}"


def _fallback_download(url: str) -> str | None:
    """备用方式：使用 urllib（跳过 SSL 验证）"""
    import ssl
    import urllib.request
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
    )
    with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
        data = resp.read()
        content_type = resp.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            return None
        ext = content_type.split("/")[-1] if "/" in content_type else "jpeg"
        b64 = base64.b64encode(data).decode("utf-8")
        return f"data:image/{ext};base64,{b64}"
