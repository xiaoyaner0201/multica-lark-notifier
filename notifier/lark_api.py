"""直调飞书开放平台 API 的发送器。

为什么不复用 lark-cli：容器里没有那个二进制，也没有它的凭据目录。
把凭据挂进容器又意味着一份长期凭据落盘在镜像运行时里。直接用
app_id / app_secret 换 tenant_access_token 更适合服务端常驻进程。

只用标准库（urllib），不引第三方依赖——这东西要能丢到任何一台机器直接跑。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from .events import StatusChange
from .lark import render_card

_BASE = "https://open.feishu.cn/open-apis"
# 提前 5 分钟续期：token 有效期 2 小时，不要等到最后一刻才换，
# 否则一次网络抖动就会让正在处理的那条通知失败。
_RENEW_MARGIN_SECONDS = 300


class LarkAPIError(RuntimeError):
    """飞书返回了业务错误。HTTP 200 不代表成功，要看 body 里的 code。"""


def _post_json(url: str, payload: dict[str, Any], token: str = "", timeout: float = 15.0) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 飞书把业务错误也放在响应体里，即使 HTTP 状态码非 200 也要读出来，
        # 否则报错只剩一个数字，排查时完全看不出原因。
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise LarkAPIError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LarkAPIError(f"network error: {exc.reason}") from exc


class TenantTokenCache:
    """tenant_access_token 的缓存与自动续期。

    多个请求可能同时发现 token 过期，用锁避免并发重复换取。
    """

    def __init__(self, app_id: str, app_secret: str, timeout: float = 15.0) -> None:
        if not app_id or not app_secret:
            raise ValueError("app_id and app_secret are required")
        self._app_id = app_id
        self._app_secret = app_secret
        self._timeout = timeout
        self._token = ""
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at - _RENEW_MARGIN_SECONDS:
                return self._token
            data = _post_json(
                f"{_BASE}/auth/v3/tenant_access_token/internal",
                {"app_id": self._app_id, "app_secret": self._app_secret},
                timeout=self._timeout,
            )
            if data.get("code") != 0:
                raise LarkAPIError(
                    f"tenant_access_token failed: code={data.get('code')} msg={data.get('msg')}"
                )
            self._token = data["tenant_access_token"]
            self._expires_at = time.time() + float(data.get("expire", 7200))
            return self._token


class LarkAPISender:
    """以 bot 身份发送卡片，直连开放平台。"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        target: str,
        timeout: float = 15.0,
    ) -> None:
        if not target:
            raise ValueError("target is required (oc_ chat id or ou_ open id)")
        self._tokens = TenantTokenCache(app_id, app_secret, timeout)
        self._target = target
        self._timeout = timeout
        # 收件人类型决定 receive_id_type，写错会报 230001 而不是明确的类型错误。
        self._receive_id_type = "chat_id" if target.startswith("oc_") else "open_id"

    def send(self, change: StatusChange) -> None:
        card = render_card(change)
        url = f"{_BASE}/im/v1/messages?receive_id_type={self._receive_id_type}"
        data = _post_json(
            url,
            {
                "receive_id": self._target,
                "msg_type": "interactive",
                # content 要的是 JSON 字符串，不是对象——传对象会报 230001。
                "content": json.dumps(card, ensure_ascii=False),
            },
            token=self._tokens.get(),
            timeout=self._timeout,
        )
        if data.get("code") != 0:
            raise LarkAPIError(f"send failed: code={data.get('code')} msg={data.get('msg')}")
