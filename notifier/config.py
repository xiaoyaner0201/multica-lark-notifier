"""集中式配置：所有部署相关的值都从环境变量来，代码里不留任何实例特征。

设计原则：

* **没有默认值**。域名、工作区、投递目标这些东西一旦写了默认值，
  就等于把某个具体部署烙进代码——换个环境跑就会静默指向错误的地方。
  宁可启动失败，也不要连到一个你以为不是它的实例。
* **缺失即拒绝启动**（fail-closed）。不提供「开发模式跳过」的开关。
* 代码里不出现真实域名、open_id、app_id、工作区名。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """配置缺失或不合法。错误信息里带上变量名，让人一眼知道该设什么。"""


def _require(name: str, hint: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required — {hint}")
    return value


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    """一次启动所需的全部配置。"""

    # ---- Multica 侧 ----
    hook_secret: str
    """插件安装的签名密钥（whsec_ 开头）。用于校验投递来源。"""

    app_url: str
    """Multica 前端地址，用来拼 Issue 链接。例：https://multica.example.com"""

    workspace_slug: str
    """工作区 slug，Issue 链接的路径段。"""

    hook_path: str
    """接收投递的路径。与在 Multica 侧登记的回调地址必须逐字一致。"""

    # ---- 飞书侧 ----
    bot_target: str
    """投递目标（open_id 或 chat_id）。"""

    app_id: str
    """飞书应用 App ID。配了就走 OpenAPI 直连（容器里用这条）。"""

    app_secret: str
    """飞书应用 App Secret。"""

    webhook_url: str
    """群自定义机器人地址。与 bot_target 二选一。"""

    webhook_secret: str

    # ---- 网络 ----
    bind_host: str
    bind_port: int

    @property
    def use_api(self) -> bool:
        """凭 app_id/app_secret 直连 OpenAPI——容器里唯一可行的方式。"""
        return bool(self.app_id and self.app_secret and self.bot_target)

    @property
    def delivery_configured(self) -> bool:
        return bool(self.bot_target or self.webhook_url)


def load() -> Settings:
    """从环境变量装配配置。任一必填项缺失都会抛 ConfigError。"""

    hook_path = _optional("MULTICA_HOOK_PATH", "/hooks/multica")
    if not hook_path.startswith("/"):
        raise ConfigError("MULTICA_HOOK_PATH must start with '/'")

    try:
        port = int(_optional("BIND_PORT", "8787"))
    except ValueError as exc:
        raise ConfigError("BIND_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ConfigError("BIND_PORT must be within 1-65535")

    return Settings(
        hook_secret=_require(
            "MULTICA_HOOK_SECRET",
            "the plugin installation signing secret (starts with whsec_)",
        ),
        app_url=_require(
            "MULTICA_APP_URL",
            "your Multica web address, used to build issue links",
        ).rstrip("/"),
        workspace_slug=_require(
            "MULTICA_WORKSPACE_SLUG",
            "the workspace slug that appears in issue URLs",
        ),
        hook_path=hook_path,
        bot_target=_optional("LARK_BOT_TARGET"),
        app_id=_optional("LARK_APP_ID"),
        app_secret=_optional("LARK_APP_SECRET"),
        webhook_url=_optional("LARK_WEBHOOK_URL"),
        webhook_secret=_optional("LARK_WEBHOOK_SECRET"),
        bind_host=_optional("BIND_HOST", "127.0.0.1"),
        bind_port=port,
    )
