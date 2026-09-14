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
    ) -> None:
        # fail-closed：没有密钥就不给启动，而不是留一个「开发模式跳过校验」的后门。
        if not signing_secret:
            raise ValueError("signing secret is required; refusing to run unauthenticated")
        self.key = parse_secret(signing_secret)
        self.app_url = app_url
        self.workspace_slug = workspace_slug
        self.sender = sender
        self.hook_path = hook_path


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

            try:
                config.sender.send(change)
            except Exception as exc:  # noqa: BLE001 - 发送失败要让平台重投
                LOG.error("failed to deliver %s: %s", change.identifier, exc)
                self._respond(500, {"error": "delivery failed"})
                return

            LOG.info("delivered %s (%s)", change.identifier, change.status_category)
            self._respond(200, {"status": "sent", "issue": change.identifier})

    return HookHandler


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

    config = Config(
        signing_secret=settings.hook_secret,
        app_url=settings.app_url,
        workspace_slug=settings.workspace_slug,
        sender=sender,
        hook_path=settings.hook_path,
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
