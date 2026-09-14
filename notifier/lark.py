"""飞书发送。

两条通路：

  LarkBotSender   —— 走 lark-cli 的 bot 身份（凭据由 lark-cli 自己管理）。
                     本机部署实际使用的就是这条。
  LarkWebhookSender —— 群自定义机器人 webhook，保留备用。

卡片结构遵循 lark-im skill 的 card-create 工作流（Card 2.0）：
header 三件套 + 状态高亮块（唯一焦点）+ div.fields 字段对 + 跳转按钮。
配色守色环纪律：blocked 用 red 系，in_review 用 orange 系，两者在色环上相邻。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import time
import urllib.request
from typing import Any, Protocol

from .events import StatusChange

# header 主题色 + 高亮块底色。red 与 orange 在色环上相邻，同卡只出现一种。
#
# icon token 必须逐字来自 skill 的 icons.md 枚举，不能按名称规律拼接（拼错不渲染）。
# 彩色枚举里没有 warning 项，所以 blocked 用线性的 warning_outlined 并配 color 上色；
# 线性图标的 color 字段对 standard_icon 生效。
CARD_THEME = {
    "blocked": {
        "template": "red",
        "block_bg": "red-50",
        "accent": "red",
        "tag_color": "red",
        "icon": "warning_outlined",
        "icon_color": "red",
    },
    "in_review": {
        "template": "orange",
        "block_bg": "orange-50",
        "accent": "orange",
        "tag_color": "orange",
        "icon": "approval_colorful",
        "icon_color": "",
    },
}
DEFAULT_THEME = CARD_THEME["in_review"]

# 编号徽章的固定颜色。不能用 neutral/grey——灰色在飞书语义里表示
# 「失效 / 归档」，用在一条要人立刻处理的通知上正好相反。
# carmine 在色环上同时紧邻 red 与 orange，两种主题下都满足邻近色纪律，
# 且作为跨卡片稳定的身份色，不与承担分诊语义的状态标签争色。
IDENTIFIER_TAG_COLOR = "carmine"

# 动态文本必须有上限，否则超长内容会把卡片撑爆（style guide 第 8 条）。
# div 的 lines 属性在 column 内嵌套不被接受，所以改为在渲染前按字符数截断。
#
# MAX_HEADER_CHARS 是整条 header 标题（含 [编号] 前缀与 · 状态后缀）的预算，
# 不是任务名单独的上限；任务名可用多少由 _header_title 扣掉固定部分后算出。
MAX_HEADER_CHARS = 72
MAX_TITLE_CHARS = 60
# 正文全文上限。飞书单卡有整体大小限制，Multica 的 description 可以很长
# （实测 PROJ-128 有 1668 字符），超出部分截断并提示去原任务看。
MAX_BODY_CHARS = 1200


def _clip(text: str, limit: int = MAX_TITLE_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Multica 的 @ 在 Markdown 里是 [@名字](mention://member/<uuid>)。
# mention:// 是它的内部协议，飞书不认识：渲染出来是个蓝色链接，点了什么
# 都不会发生。留着比去掉更糟——用户会以为点了能跳转。
#
# 不转成飞书的 <at> 标签，是因为那需要 open_id，而这里只有 Multica 的 uuid，
# 两边身份不通（正是 identity.py 要解决的问题）。在身份映射可靠之前，
# 退化成纯文本是诚实的做法：保留「谁被 @ 了」这个信息，不伪造可点的假象。
_MENTION_RE = re.compile(r"\[@?([^\]]+?)\]\(mention://[^)]+\)")


def _flatten_mentions(text: str) -> str:
    """把 mention 链接压成纯文本 @名字。"""
    return _MENTION_RE.sub(lambda m: f"@{m.group(1)}", text)


def _body_text(change: StatusChange) -> str:
    """正文区内容：任务描述全文。

    描述为空时退回一句状态说明，避免正文区开天窗。
    """
    body = _flatten_mentions(change.description.strip())
    if not body:
        transition = (
            f"`{change.prev_status}` → `{change.status}`"
            if change.prev_status
            else f"`{change.status}`"
        )
        return f"<font color='grey'>该任务暂无描述。状态：{transition}</font>"
    if len(body) <= MAX_BODY_CHARS:
        return body
    return body[:MAX_BODY_CHARS] + "\n\n<font color='grey'>……内容过长已截断，点击下方按钮查看完整任务</font>"


def _theme(change: StatusChange) -> dict[str, str]:
    return CARD_THEME.get(change.status_category, DEFAULT_THEME)


def _header_icon(theme: dict[str, str]) -> dict[str, Any]:
    """构造 header icon。线性图标带 color，彩色图标自带颜色不再加。"""
    icon: dict[str, Any] = {"tag": "standard_icon", "token": theme["icon"]}
    if theme.get("icon_color"):
        icon["color"] = theme["icon_color"]
    return icon


def _header_title(change: StatusChange) -> str:
    """header 主标题：只放任务名称。

    编号与状态改由 text_tag_list 以标签形式展示，不再拼进标题字符串。
    标题独占预算，超长才截断。
    """
    return _clip(change.title, MAX_TITLE_CHARS)


def _header_tags(change: StatusChange, theme: dict[str, str]) -> list[dict[str, Any]]:
    """header 后缀标签：编号 + 状态。

    text_tag_list 上限 3 个，这里用 2 个。编号用固定的身份色，
    状态用主题色承担分诊语义。
    """
    return [
        {
            "tag": "text_tag",
            "text": {"tag": "plain_text", "content": change.identifier},
            "color": IDENTIFIER_TAG_COLOR,
        },
        {
            "tag": "text_tag",
            "text": {"tag": "plain_text", "content": change.label},
            "color": theme["tag_color"],
        },
    ]


def render_card(change: StatusChange) -> dict[str, Any]:
    """构造 Card 2.0 卡片。

    焦点唯一：状态高亮块是全卡最强视觉块，标题次之，字段对为辅。
    """
    theme = _theme(change)

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "default"},
        "header": {
            # 标题只放任务名；编号与状态用后缀标签（text_tag_list）展示。
            "title": {"tag": "plain_text", "content": _header_title(change)},
            "template": theme["template"],
            "icon": _header_icon(theme),
            "text_tag_list": _header_tags(change, theme),
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 20px 12px",
            "elements": [
                # 正文全文。Multica 的 description 是 Markdown，
                # 飞书 markdown 组件能渲染标题/加粗/列表/代码，直接透传。
                {
                    "tag": "markdown",
                    "content": _body_text(change),
                    "margin": "0px 0px 12px 0px",
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看任务"},
                    "type": "primary_filled",
                    "width": "fill",
                    "behaviors": [{"type": "open_url", "default_url": change.url}],
                },
            ],
        },
    }


def sign_webhook(secret: str, timestamp: int) -> str:
    """飞书群自定义机器人的加签。

    注意与 Multica 的签名算法完全不同：这里把 "<timestamp>\\n<secret>"
    整体当作 HMAC 的 KEY，对空消息体做 HMAC-SHA256 再 base64。
    """
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class Sender(Protocol):
    def send(self, change: StatusChange) -> None: ...


class DryRunSender:
    """只记录不发送。用于不希望产生外部副作用的测试。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, change: StatusChange) -> None:
        self.sent.append(render_card(change))


class LarkBotSender:
    """通过 lark-cli 以 bot 身份发送卡片。

    一律 --as bot：--as user 会以董劭杰本人名义发送，属于冒用身份。
    收件人可以是群（oc_）或个人（ou_）。
    """

    def __init__(self, target: str, cli: str = "lark-cli", timeout: float = 30.0) -> None:
        if not target:
            raise ValueError("target is required (oc_ chat id or ou_ open id)")
        self._target = target
        self._cli = cli
        self._timeout = timeout
        self._flag = "--chat-id" if target.startswith("oc_") else "--user-id"

    def send(self, change: StatusChange) -> None:
        card = render_card(change)
        argv = [
            self._cli, "im", "+messages-send",
            self._flag, self._target,
            "--msg-type", "interactive",
            "--content", json.dumps(card, ensure_ascii=False),
            "--as", "bot",
        ]
        # 继承父进程环境，只叠加静音开关。不能传一个自造的最小 env：
        # lark-cli 要靠 HOME 定位凭据目录，清掉会直接报 not_configured。
        env = dict(os.environ)
        env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
        env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"

        # 用 argv 数组而非 shell 字符串：卡片 JSON 里有大量引号和中文，
        # 经 shell 解析会被撕碎（多层 shell 转义是这条链路的经典故障点）。
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=self._timeout,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(f"lark-cli failed (exit {result.returncode}): {result.stderr[:400]}")

        # lark-cli 的成功判据是 ok==true，不是 code==0：
        # 成功信封根本没有顶层 code 字段，按 code 判断会把成功误判为失败。
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"lark-cli returned non-JSON: {result.stdout[:200]}") from exc
        if envelope.get("ok") is not True:
            raise RuntimeError(f"lark-cli rejected the send: {result.stdout[:400]}")


class LarkWebhookSender:
    """群自定义机器人 webhook。备用通路。"""

    def __init__(self, webhook_url: str, secret: str = "", timeout: float = 5.0) -> None:
        if not webhook_url:
            raise ValueError("webhook_url is required")
        self._url = webhook_url
        self._secret = secret
        self._timeout = timeout

    def send(self, change: StatusChange) -> None:
        payload: dict[str, Any] = {"msg_type": "interactive", "card": render_card(change)}
        if self._secret:
            timestamp = int(time.time())
            payload["timestamp"] = str(timestamp)
            payload["sign"] = sign_webhook(self._secret, timestamp)

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")

        # 飞书对业务失败仍返回 HTTP 200，错误藏在 body 的 code 字段里。
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"lark returned non-JSON body: {raw[:200]}") from exc
        if result.get("code") not in (0, None):
            raise RuntimeError(f"lark rejected the message: {raw[:200]}")
