"""决定一条状态变更该通知谁。

hook payload 只带 creator_id / assignee_id，不带订阅者列表，而「被 @ 的人」
只存在于 issue_subscriber 表（reason='mentioned'）。所以收件人要现查库。

两条来源合并：
  payload 里的 creator / assignee —— 零查询，但只有两个人
  issue_subscriber 表          —— 覆盖 mentioned / commenter / manual 等

agent 类型一律排除：它们没有邮箱，也不该收飞书。生产数据里 agent 订阅者
（creator|agent 358 条、assignee|agent 288 条）远多于真人，不过滤会白查一堆。
"""

from __future__ import annotations

import logging
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class RecipientError(RuntimeError):
    """收件人解析失败。"""


@dataclass(frozen=True)
class Recipient:
    """一个待通知的人。"""

    user_id: str
    email: str
    # 为什么收到这条：creator / assignee / mentioned / commenter ...
    # 同一个人可能因多个原因入选，这里保留首个命中的原因用于排查。
    reason: str


@dataclass
class RecipientPolicy:
    """哪些订阅原因要通知。

    默认覆盖「和这条 issue 有真实关系」的人。autopilot / delegated 这类
    由系统或流程产生的订阅不默认发——它们数量大且未必是人在关注。
    """

    reasons: frozenset[str] = field(
        default_factory=lambda: frozenset({"creator", "assignee", "mentioned", "commenter", "manual"})
    )
    # 兜底上限：一条变更最多通知多少人。防止某个 issue 被大量 @ 之后
    # 一次状态变更引发几十条私聊。超出时记日志并截断，而不是静默全发。
    max_recipients: int = 20


class RecipientResolver:
    """查出一条 issue 该通知哪些人。

    走 Multica 自己的 API 而不是直连数据库：回调服务不该持有生产库凭据，
    而且 API 会随上游演进保持语义，SQL 不会。
    """

    def __init__(self, api_base: str, token: str, policy: RecipientPolicy | None = None) -> None:
        if not api_base:
            raise RecipientError("api_base 不能为空")
        self._api_base = api_base.rstrip("/")
        self._token = token
        self._policy = policy or RecipientPolicy()

    def resolve(self, issue: dict, workspace_id: str) -> list[Recipient]:
        """返回该通知的人，已去重、已排除 agent、已截断到上限。"""
        found: dict[str, Recipient] = {}

        # 先收 payload 里现成的两个，省一次查询也保证 creator/assignee
        # 在订阅表异常时仍能拿到。
        for key, reason in (("creator", "creator"), ("assignee", "assignee")):
            uid = _member_id(issue, key)
            if uid and uid not in found:
                found[uid] = Recipient(user_id=uid, email="", reason=reason)

        issue_id = str(issue.get("id") or "").strip()
        if issue_id:
            try:
                for r in self._fetch_subscribers(issue_id, workspace_id):
                    prev = found.get(r.user_id)
                    # payload 来的 creator/assignee 没有邮箱，只有 user_id；
                    # 订阅表这条才带邮箱。用 setdefault 会让占位记录留下来，
                    # 导致 creator/assignee 永远解析不出 open_id、静默收不到通知。
                    # 所以补全邮箱，但保留先前更准确的 reason。
                    if prev is None:
                        found[r.user_id] = r
                    elif not prev.email and r.email:
                        found[r.user_id] = Recipient(
                            user_id=prev.user_id, email=r.email, reason=prev.reason
                        )
            except RecipientError as exc:
                # 订阅表查不到不该让整条通知失败：creator/assignee 照发，
                # 少通知几个人好过一个都收不到。
                logger.warning("订阅者查询失败，仅通知 creator/assignee: %s", exc)

        out = list(found.values())
        no_email = [r.reason for r in out if not r.email]
        if no_email:
            # 没邮箱就换不出 open_id，这些人收不到。留痕，否则是静默丢通知。
            logger.warning(
                "issue %s 有 %d 个收件人缺邮箱（%s），将无法投递",
                issue.get("identifier"), len(no_email), ",".join(sorted(set(no_email))),
            )
        if len(out) > self._policy.max_recipients:
            logger.warning(
                "issue %s 收件人 %d 人超过上限 %d，已截断",
                issue.get("identifier"), len(out), self._policy.max_recipients,
            )
            out = out[: self._policy.max_recipients]
        return out

    def _fetch_subscribers(self, issue_id: str, workspace_id: str) -> list[Recipient]:
        url = (
            f"{self._api_base}/api/workspaces/{urllib.parse.quote(workspace_id)}"
            f"/issues/{urllib.parse.quote(issue_id)}/subscribers"
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                import json

                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RecipientError(f"订阅者接口调用失败: {exc}") from exc

        rows = data if isinstance(data, list) else data.get("subscribers") or data.get("data") or []
        out: list[Recipient] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            # agent 没有邮箱也不该收飞书，这里就排掉，不留到后面再过滤。
            if str(row.get("user_type") or row.get("type") or "").strip() != "member":
                continue
            if row.get("unsubscribed_at"):
                continue
            reason = str(row.get("reason") or "").strip()
            if reason and reason not in self._policy.reasons:
                continue
            uid = str(row.get("user_id") or row.get("id") or "").strip()
            if not uid:
                continue
            out.append(
                Recipient(
                    user_id=uid,
                    email=str(row.get("email") or "").strip(),
                    reason=reason or "subscriber",
                )
            )
        return out


def _member_id(issue: dict, prefix: str) -> str:
    """取 creator/assignee 的 user_id，agent 返回空串。

    payload 用 <prefix>_type 区分人和 agent。只有 member 才是人。
    """
    if str(issue.get(f"{prefix}_type") or "").strip() != "member":
        return ""
    return str(issue.get(f"{prefix}_id") or "").strip()


def debug_target_override() -> str | None:
    """调试期把所有通知重定向到一个人。

    值为飞书 open_id。设置后不再按收件人分发，全部发给这一个人，
    卡片里仍标注原本该收到的人，便于确认解析结果正确。

    留这个开关是因为「按人分发」一旦发错，错的是同事的私聊，
    撤不回来。先重定向观察几天比直接放开稳妥。
    """
    value = os.environ.get("NOTIFIER_DEBUG_TARGET", "").strip()
    return value or None
