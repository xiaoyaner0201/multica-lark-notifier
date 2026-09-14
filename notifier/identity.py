"""把 Multica 的用户身份解析成飞书 open_id。

两条信道，A 主 B 兜底：

A. 飞书通讯录按邮箱反查（contact/v3/users/batch_get_id）
   人员变动自动跟上，不需要维护。但依赖两件事：bot 申请了
   contact:user.id:readonly，且当事人在飞书绑定的邮箱与 Multica
   注册邮箱一致。

B. 本地覆盖表
   A 查不到、或查到的人不对时的兜底。实测 47 个注册用户里有 4 个
   用外部邮箱（非 @example.com），这些人 A 永远查不到；还有一类
   更隐蔽：飞书绑定邮箱与 Multica 注册邮箱不是同一个，A 会返回
   空结果或者错的人。

B 优先于 A：覆盖表是人工确认过的，比通讯录的自动匹配更可信。
顺序反过来的话，一个绑错邮箱的人会被 A 静默解析成别人 —— 把
issue 通知发给错的同事，比不发更糟。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

LARK_HOST = "https://open.feishu.cn"
BATCH_LIMIT = 50  # 飞书 batch_get_id 单次上限


class IdentityError(RuntimeError):
    """身份解析失败，且失败原因不该被当成“这个人没有飞书账号”。"""


@dataclass
class Resolution:
    """一次解析的结果，带来源，便于审计谁是被兜底表救回来的。"""

    email: str
    open_id: str | None
    source: str  # "override" | "directory" | "unresolved"
    note: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.open_id)


@dataclass
class IdentityResolver:
    """邮箱 -> open_id。

    Args:
        app_id / app_secret: 飞书自建应用凭据。
        override_path: 覆盖表 JSON 路径。缺失不是错误 —— 全靠通讯录也能跑，
            只是那 4 个外部邮箱用户收不到。
        cache_ttl: 通讯录结果缓存秒数。人员变动不频繁，默认 1 小时；
            覆盖表不进缓存，改完即时生效。
    """

    app_id: str
    app_secret: str
    override_path: Path | None = None
    cache_ttl: float = 3600.0

    _token: str = field(default="", init=False, repr=False)
    _token_expiry: float = field(default=0.0, init=False, repr=False)
    _cache: dict[str, tuple[float, str | None]] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # ---- 覆盖表 ----

    def _overrides(self) -> dict[str, str]:
        """每次读盘而不缓存：改完覆盖表不该需要重启服务。

        文件小（几十行），读盘成本远低于一次误投递的代价。
        """
        if not self.override_path or not self.override_path.exists():
            return {}
        try:
            raw = json.loads(self.override_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 覆盖表坏了要响，不能静默退化成“只用 A”——那正是这张表要防的情况。
            raise IdentityError(f"覆盖表无法解析: {self.override_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise IdentityError(f"覆盖表应为 JSON 对象: {self.override_path}")
        return {str(k).strip().lower(): str(v).strip() for k, v in raw.items() if v}

    # ---- 飞书通讯录 ----

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expiry:
                return self._token
            body = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode()
            req = urllib.request.Request(
                f"{LARK_HOST}/open-apis/auth/v3/tenant_access_token/internal",
                data=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                raise IdentityError(f"获取 tenant_access_token 失败: {exc}") from exc
            if payload.get("code") != 0:
                raise IdentityError(f"获取 tenant_access_token 失败: {payload.get('msg')}")
            self._token = payload["tenant_access_token"]
            # 提前 5 分钟过期，避免边界上拿到刚失效的 token。
            self._token_expiry = time.time() + max(payload.get("expire", 7200) - 300, 60)
            return self._token

    def _query_directory(self, emails: list[str]) -> dict[str, str]:
        """按邮箱批量查 open_id。查不到的邮箱不会出现在返回里。"""
        if not emails:
            return {}
        found: dict[str, str] = {}
        for start in range(0, len(emails), BATCH_LIMIT):
            chunk = emails[start : start + BATCH_LIMIT]
            url = (
                f"{LARK_HOST}/open-apis/contact/v3/users/batch_get_id"
                f"?{urllib.parse.urlencode({'user_id_type': 'open_id'})}"
            )
            req = urllib.request.Request(
                url,
                data=json.dumps({"emails": chunk}).encode(),
                headers={
                    "Authorization": f"Bearer {self._access_token()}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                # 通讯录不可用不代表这些人没账号，交给调用方决定是否重试。
                raise IdentityError(f"查询飞书通讯录失败: {exc}") from exc

            code = payload.get("code")
            if code == 99991672:
                raise IdentityError(
                    "bot 缺少 contact:user.id:readonly 权限，无法按邮箱反查 open_id。"
                    "在飞书开放平台为应用申请该权限后重试，或先用覆盖表兜底。"
                )
            if code != 0:
                raise IdentityError(f"查询飞书通讯录失败: {code} {payload.get('msg')}")

            for item in (payload.get("data") or {}).get("user_list") or []:
                email = (item.get("email") or "").strip().lower()
                open_id = item.get("user_id") or item.get("open_id")
                if email and open_id:
                    found[email] = open_id
        return found

    # ---- 对外 ----

    def resolve(self, emails: list[str]) -> list[Resolution]:
        """把一批邮箱解析成 open_id。

        覆盖表优先，其余走通讯录。通讯录不可用时不抛给调用方，而是把
        那批人标成 unresolved —— 一个人查不到不该让整批通知发不出去。
        """
        normalized = [e.strip().lower() for e in emails if e and e.strip()]
        if not normalized:
            return []

        overrides = self._overrides()
        results: list[Resolution] = []
        need_lookup: list[str] = []

        now = time.time()
        for email in normalized:
            if email in overrides:
                results.append(Resolution(email, overrides[email], "override"))
                continue
            cached = self._cache.get(email)
            if cached and now < cached[0]:
                open_id = cached[1]
                results.append(
                    Resolution(email, open_id, "directory" if open_id else "unresolved",
                               "" if open_id else "通讯录中查不到（缓存）")
                )
                continue
            need_lookup.append(email)

        if need_lookup:
            try:
                found = self._query_directory(need_lookup)
            except IdentityError as exc:
                logger.warning("通讯录查询失败，本批降级为未解析: %s", exc)
                found = {}
                for email in need_lookup:
                    results.append(Resolution(email, None, "unresolved", str(exc)))
            else:
                expiry = time.time() + self.cache_ttl
                for email in need_lookup:
                    open_id = found.get(email)
                    self._cache[email] = (expiry, open_id)
                    results.append(
                        Resolution(email, open_id, "directory" if open_id else "unresolved",
                                   "" if open_id else "通讯录中查不到该邮箱")
                    )

        order = {e: i for i, e in enumerate(normalized)}
        results.sort(key=lambda r: order.get(r.email, 0))
        return results

    def audit(self, emails: list[str]) -> dict[str, list[str]]:
        """按来源归类，供每日巡检用。

        返回 unresolved 名单是这个方法存在的理由：它就是需要人工补进
        覆盖表的那批人。
        """
        buckets: dict[str, list[str]] = {"override": [], "directory": [], "unresolved": []}
        for item in self.resolve(emails):
            buckets[item.source].append(item.email)
        return buckets
