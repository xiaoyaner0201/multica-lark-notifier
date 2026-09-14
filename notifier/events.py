"""从 Multica 的 issue.status_changed 事件里取出要推送的内容。

payload 形状来自上游 server/internal/handler/issue.go 的 EventIssueUpdated
发布点：顶层是一个 map，`issue` 是完整的 IssueResponse，另有 status_changed
与 prev_status 等变更标志。

这里只做纯函数：解析 + 判定 + 渲染。网络与配置留给调用方，
这样每条规则都能单独测，不用起 HTTP 服务。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

# 要推送的状态。用 status_category 而非 status 判定：自定义状态的 key 可能是
# in_review_2 这种不透明句柄（上游 MUL-6243 起支持 per-workspace 自定义状态），
# 但它会继承 7 个内置分类之一，category 才是稳定的语义面。
NOTIFY_CATEGORIES = frozenset({"blocked", "in_review"})

CATEGORY_LABELS = {
    "blocked": "已阻塞",
    "in_review": "待审阅",
}

CATEGORY_COLORS = {
    "blocked": "red",
    "in_review": "orange",
}


@dataclass(frozen=True)
class StatusChange:
    """一次值得推送的状态变更。"""

    issue_id: str
    identifier: str
    title: str
    description: str
    status: str
    status_category: str
    status_display: str
    prev_status: str
    workspace_id: str
    url: str

    @property
    def label(self) -> str:
        return CATEGORY_LABELS.get(self.status_category, self.status_category)

    @property
    def color(self) -> str:
        return CATEGORY_COLORS.get(self.status_category, "blue")


def _text(source: dict[str, Any], key: str) -> str:
    value = source.get(key)
    return value if isinstance(value, str) else ""


def build_issue_url(app_url: str, workspace_slug: str, identifier: str) -> str:
    """拼 Issue 详情页地址。

    前端路由是 /{workspaceSlug}/issues/{id}，且 IssueDetailRoute 会把 UUID
    重写成 identifier，所以直接用 identifier 拼，用户点开地址栏就是 PROJ-128。
    """
    base = app_url.rstrip("/")
    return f"{base}/{quote(workspace_slug)}/issues/{quote(identifier)}"


def extract(payload: Any, app_url: str, workspace_slug: str) -> StatusChange | None:
    """从事件 payload 中取出待推送变更；不该推送时返回 None。

    宽进严出：payload 来自外部，任何字段都可能缺失或类型不对，
    一律当作「不推送」而不是抛异常——一次畸形投递不该让整条通道失败。
    """
    if not isinstance(payload, dict):
        return None

    # 平台对 issue.updated 和 issue.status_changed 派发同一个 payload，
    # 只有后者带 status_changed=true。订阅两个事件时靠这个标志去重。
    if payload.get("status_changed") is not True:
        return None

    issue = payload.get("issue")
    if not isinstance(issue, dict):
        return None

    status = _text(issue, "status")
    # status_category 带 omitempty：缺失表示该端点没解析分类，
    # 上游注释明确要求此时回退到 status，而不是当成空分类。
    category = _text(issue, "status_category") or status
    if category not in NOTIFY_CATEGORIES:
        return None

    identifier = _text(issue, "identifier")
    issue_id = _text(issue, "id")
    if not identifier or not issue_id:
        return None

    # 自定义状态把人取的名字放在 status_name，key 可能不可读；
    # 内置状态该字段为空串，此时用分类标签。
    status_name = _text(issue, "status_name")
    status_display = status_name or CATEGORY_LABELS.get(category, status)

    return StatusChange(
        issue_id=issue_id,
        identifier=identifier,
        title=_text(issue, "title") or "(无标题)",
        # description 在 IssueResponse 里是可空指针，JSON 里可能是 null。
        # _text 已处理非字符串的情况，这里拿到的要么是正文要么是空串。
        description=_text(issue, "description"),
        status=status,
        status_category=category,
        status_display=status_display,
        prev_status=_text(payload, "prev_status"),
        workspace_id=_text(issue, "workspace_id"),
        url=build_issue_url(app_url, workspace_slug, identifier),
    )
