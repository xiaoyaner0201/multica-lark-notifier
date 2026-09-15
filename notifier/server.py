"""回调服务：接收 Multica Plugin hook 投递，过滤后转发飞书。

只用标准库，不引第三方依赖——这东西要能丢到任何一台机器直接跑。

安全姿态：
  * 签名不过 → 401，且响应体不说明具体原因（避免给爆破者反馈）
  * 未配置签名密钥 → 拒绝启动，不提供「跳过校验」的开关
  * 发送失败 → 500，让平台按其重试策略（3 次 + 熔断）重投
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import ConfigError
from .config import load as load_settings
from .events import extract
from .lark import DryRunSender, LarkBotSender, LarkWebhookSender, Sender
from .lark_api import LarkAPISender
from .signature import SignatureError, parse_secret, verify

LOG = logging.getLogger("notifier")

# 上游 hookMaxResponseBytes 是 1 MiB；入站同样设上限，避免一个超大 body 撑爆内存。
MAX_BODY_BYTES = 1 << 20


class Config:
    def __init__(
        self,
        signing_secret: str,
        app_url: str,
        workspace_slug: str,
        sender: Sender,
        hook_path: str = "/hooks/multica",
        recipients: object | None = None,
        identities: object | None = None,
        debug_target: str = "",
    ) -> None:
        # fail-closed：没有密钥就不给启动，而不是留一个「开发模式跳过校验」的后门。
        if not signing_secret:
            raise ValueError("signing secret is required; refusing to run unauthenticated")
        self.key = parse_secret(signing_secret)
        self.app_url = app_url
        self.workspace_slug = workspace_slug
        self.sender = sender
        self.hook_path = hook_path
        # 两者都配齐才按人分发；缺任一就退回「全部发给 bot_target」的汇总模式。
        self.recipients = recipients
        self.identities = identities
        self.debug_target = debug_target

    @property
    def per_recipient(self) -> bool:
        return bool(self.recipients and self.identities)


def _handler_factory(config: Config) -> type[BaseHTTPRequestHandler]:
    class HookHandler(BaseHTTPRequestHandler):
        server_version = "MulticaLarkNotifier/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            LOG.info("%s - %s", self.address_string(), fmt % args)

        def _respond(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 要求这个名字
            if self.path == "/healthz":
                self._respond(200, {"status": "ok"})
                return
            self._respond(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != config.hook_path:
                self._respond(404, {"error": "not found"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._respond(400, {"error": "bad content-length"})
                return
            if length <= 0 or length > MAX_BODY_BYTES:
                self._respond(400, {"error": "bad content-length"})
                return

            body = self.rfile.read(length)
            timestamp = self.headers.get("X-Multica-Timestamp", "")
            presented = self.headers.get("X-Multica-Signature", "")

            try:
                verify(config.key, timestamp, body, presented)
            except SignatureError as exc:
                # 记详细原因到日志，但只回一个笼统的 401 给对端。
                LOG.warning("rejected delivery: %s", exc)
                self._respond(401, {"error": "invalid signature"})
                return

            try:
                envelope = json.loads(body)
            except json.JSONDecodeError:
                self._respond(400, {"error": "body is not valid json"})
                return

            # hook 投递外层是信封，业务 payload 在 input 字段里。
            payload = envelope.get("input") if isinstance(envelope, dict) else None
            change = extract(payload, config.app_url, config.workspace_slug)
            if change is None:
                # 不该推送的事件也要回 2xx：回错误码会让平台重试，
                # 并把一个正常的「无需推送」计入熔断器的失败计数。
                self._respond(200, {"status": "ignored"})
                return

            # 两种模式：配齐 recipients+identities 就按人发，否则退回汇总。
            # 汇总模式下所有变更都发给同一个 bot_target——只适合单人/小团队，
            # 团队规模上来后每个人都会收到与自己无关的全部 issue。
            if config.per_recipient:
                ok, detail = _deliver_personal(config, change, payload)
            else:
                ok, detail = _deliver_digest(config, change)

            LOG.info("delivered %s (%s) %s", change.identifier, change.status_category, detail)
            if not ok:
                self._respond(500, {"error": "delivery failed"})
                return
            self._respond(200, {"status": "sent", "issue": change.identifier})

    return HookHandler


def _deliver_digest(config: Config, change) -> tuple[bool, str]:
    """汇总模式：整个工作区的变更都发给同一个目标。"""
    try:
        config.sender.send(change)
        return True, "digest=1"
    except Exception as exc:  # noqa: BLE001 - 发送失败要让平台重投
        LOG.error("failed to deliver %s: %s", change.identifier, exc)
        return False, "digest=error"


def _deliver_personal(config: Config, change, payload) -> tuple[bool, str]:
    """按人分发：creator / assignee / mentioned 各收自己那份。

    这是唯一投递链路，没有汇总卡兜底——任何没送出去的卡片都是丢失的通知，
    所以失败必须如实上报，让平台重投。
    """
    try:
        issue = (payload or {}).get("issue") or {}
        workspace_id = issue.get("workspace_id") or ""
        people = config.recipients.resolve(issue, workspace_id)
        if not people:
            LOG.warning("%s: no recipient matched", change.identifier)
            return True, "personal=0"

        # user_id -> email 的映射由 recipients 提供；identities 再把 email 换成 open_id。
        resolutions = config.identities.resolve([p.email for p in people if p.email])
        open_ids = {r.email: r.open_id for r in resolutions if r.resolved}
        unresolved = [r.email for r in resolutions if not r.resolved]

        sent = 0
        targets = [p for p in people if p.email in open_ids]
        for p in targets:
            target = config.debug_target or open_ids[p.email]
            note = ""
            if config.debug_target:
                # 调试期全部改投一个人，但必须写明本该给谁，
                # 否则验证时分不清哪张卡对应哪个收件人。
                note = f"*[调试重定向] 本应发给 {p.reason}：`{p.email}`*"
            try:
                config.sender.send_to(target, change, note)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                LOG.error("personal delivery to %s failed: %s", p.email, exc)

        if unresolved:
            # 解析不出 open_id = 这些人收不到，且重投救不回来（通讯录不会
            # 因为重试就长出账号）。回 200 避免无谓重试，但必须留痕。
            LOG.warning("%s: unresolved recipients %s", change.identifier, unresolved)
        return sent == len(targets), f"personal={sent}/{len(targets)} unresolved={len(unresolved)}"
    except Exception as exc:  # noqa: BLE001
        LOG.error("personal delivery failed for %s: %s", change.identifier, exc)
        return False, "personal=error"


def build_server(config: Config, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _handler_factory(config))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        settings = load_settings()
    except ConfigError as exc:
        # 配置缺失直接退出，并把缺的变量名说清楚——不要带着半套配置跑起来。
        LOG.error("configuration error: %s", exc)
        raise SystemExit(2) from exc

    # 三种投递方式，按优先级选择：
    #   LARK_BOT_TARGET  —— lark-cli bot 身份
    #   LARK_WEBHOOK_URL —— 群自定义机器人
    #   都不设            —— dry-run，只渲染不发送
    sender: Sender
    if settings.use_api:
        # 容器里没有 lark-cli，直连开放平台。
        sender = LarkAPISender(settings.app_id, settings.app_secret, settings.bot_target)
        LOG.info("lark delivery ENABLED via open API")
    elif settings.bot_target:
        sender = LarkBotSender(settings.bot_target)
        LOG.info("lark delivery ENABLED via lark-cli bot identity")
    elif settings.webhook_url:
        sender = LarkWebhookSender(settings.webhook_url, settings.webhook_secret)
        LOG.info("lark delivery ENABLED via group webhook")
    else:
        sender = DryRunSender()
        LOG.info("lark delivery disabled (dry-run); set LARK_BOT_TARGET to enable")

    # 按人分发需要两样东西：查订阅者的 API token，和把邮箱换成 open_id 的解析器。
    # 缺任一就退回汇总模式——宁可退化成「都发给一个人」，也不要静默不发。
    recipients = identities = None
    if settings.api_token:
        try:
            from .identity import IdentityResolver
            from .recipients import RecipientResolver

            recipients = RecipientResolver(settings.app_url, settings.api_token)
            identities = IdentityResolver(
                app_id=settings.app_id,
                app_secret=settings.app_secret,
                override_path=(
                    Path(settings.identity_overrides) if settings.identity_overrides else None
                ),
            )
            mode = f"debug-redirect to {settings.debug_target}" if settings.debug_target else "live"
            LOG.info("per-recipient delivery ENABLED (%s)", mode)
        except Exception as exc:  # noqa: BLE001
            LOG.error("per-recipient delivery disabled: %s", exc)
            recipients = identities = None
    else:
        LOG.info("per-recipient delivery disabled (set MULTICA_API_TOKEN to enable)")

    config = Config(
        signing_secret=settings.hook_secret,
        app_url=settings.app_url,
        workspace_slug=settings.workspace_slug,
        sender=sender,
        hook_path=settings.hook_path,
        recipients=recipients,
        identities=identities,
        debug_target=settings.debug_target,
    )
    server = build_server(config, settings.bind_host, settings.bind_port)
    LOG.info(
        "listening on http://%s:%d%s",
        settings.bind_host,
        settings.bind_port,
        settings.hook_path,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
