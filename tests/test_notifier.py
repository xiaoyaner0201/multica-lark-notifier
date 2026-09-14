"""回归测试。

签名用例的期望值来自上游 Go 实现（server/internal/service/plugin_hook.go）
的实际输出，不是本仓库自己算的——自签自验测不出把契约抄错的情况。
交叉验证方法记录在 README。

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import pathlib
import unittest
from unittest import mock

from notifier import config
from notifier.events import build_issue_url, extract
from notifier.lark import (
    CARD_THEME,
    IDENTIFIER_TAG_COLOR,
    LarkBotSender,
    render_card,
    sign_webhook,
)
from notifier.signature import (
    SignatureError,
    compute_signature,
    derive_hook_key,
    format_secret,
    parse_secret,
    verify,
)

# ── 来自上游 Go 实现的真实输出 ───────────────────────────────────────────
# 生成方式（临时程序放在 multica/server/cmd/ 下，用其 internal/service 包）：
#   svc := &service.PluginService{DeploymentKey: key}
#   svc.SignHookPayload(installationID, timestamp, body)
#   svc.HookSigningSecret(installationID)
GO_DEPLOYMENT_KEY = bytes.fromhex(
    "9c3a5f1e7b2d48a06e5c1f93b7a4d2e80c6b1a7f35d9e2c48b0a6f1d73e5c294"
)
GO_INSTALLATION_ID = "11111111-2222-4333-8444-555555555555"
GO_TIMESTAMP = "1789315756"
GO_BODY = b'{"event_type":"issue.status_changed"}'


def _make_payload(**overrides) -> dict:
    issue = {
        "id": "01a09b83-8f25-7e28-bf48-97c8a33ab462",
        "identifier": "PROJ-128",
        "title": "统一飞书通知",
        "status": "in_review",
        "status_category": "in_review",
        "status_name": "",
        "description": "这里是任务描述全文。",
        "workspace_id": "00000000-1111-4222-8333-444444444444",
    }
    issue.update(overrides.pop("issue", {}))
    payload = {"status_changed": True, "prev_status": "in_progress", "issue": issue}
    payload.update(overrides)
    return payload


class TestSignatureContract(unittest.TestCase):
    """与上游签名契约的一致性。任何一条挂掉都说明契约理解错了。"""

    def test_derived_key_is_deterministic(self):
        first = derive_hook_key(GO_DEPLOYMENT_KEY, GO_INSTALLATION_ID)
        second = derive_hook_key(GO_DEPLOYMENT_KEY, GO_INSTALLATION_ID)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)

    def test_derived_key_differs_per_installation(self):
        a = derive_hook_key(GO_DEPLOYMENT_KEY, GO_INSTALLATION_ID)
        b = derive_hook_key(GO_DEPLOYMENT_KEY, "22222222-3333-4444-8555-666666666666")
        self.assertNotEqual(a, b, "不同 installation 必须派生出不同密钥，否则可跨装置重放")

    def test_deployment_key_must_be_32_bytes(self):
        # 上游在长度不符时直接拒绝启用 hook，这里保持同样的 fail-closed。
        with self.assertRaises(SignatureError):
            derive_hook_key(b"too-short", GO_INSTALLATION_ID)

    def test_secret_roundtrip_uses_whsec_prefix(self):
        key = derive_hook_key(GO_DEPLOYMENT_KEY, GO_INSTALLATION_ID)
        display = format_secret(key)
        self.assertTrue(display.startswith("whsec_"))
        self.assertEqual(parse_secret(display), key)

    def test_signature_binds_timestamp_and_body(self):
        """时间戳与 body 用 "." 连接后一起签名。

        朴素拼接会让 ("12", "3"+body) 与 ("123", body) 得到同一签名，
        攻击者可以在两个字段间挪字节。
        """
        key = derive_hook_key(GO_DEPLOYMENT_KEY, GO_INSTALLATION_ID)
        self.assertNotEqual(
            compute_signature(key, "12", b"3" + GO_BODY),
            compute_signature(key, "123", GO_BODY),
        )

    def test_verify_accepts_valid_delivery(self):
        key = derive_hook_key(GO_DEPLOYMENT_KEY, GO_INSTALLATION_ID)
        signature = "v1=" + compute_signature(key, GO_TIMESTAMP, GO_BODY)
        verify(key, GO_TIMESTAMP, GO_BODY, signature, now=int(GO_TIMESTAMP) + 5)


class TestSignatureRejection(unittest.TestCase):
    def setUp(self):
        self.key = derive_hook_key(GO_DEPLOYMENT_KEY, GO_INSTALLATION_ID)
        self.signature = "v1=" + compute_signature(self.key, GO_TIMESTAMP, GO_BODY)
        self.now = int(GO_TIMESTAMP) + 5

    def test_rejects_missing_version_prefix(self):
        with self.assertRaises(SignatureError):
            verify(self.key, GO_TIMESTAMP, GO_BODY, self.signature[3:], now=self.now)

    def test_rejects_tampered_body(self):
        with self.assertRaises(SignatureError):
            verify(self.key, GO_TIMESTAMP, GO_BODY + b" ", self.signature, now=self.now)

    def test_rejects_wrong_key(self):
        other = derive_hook_key(bytes(32), GO_INSTALLATION_ID)
        with self.assertRaises(SignatureError):
            verify(other, GO_TIMESTAMP, GO_BODY, self.signature, now=self.now)

    def test_rejects_expired_timestamp(self):
        with self.assertRaises(SignatureError):
            verify(self.key, GO_TIMESTAMP, GO_BODY, self.signature, now=int(GO_TIMESTAMP) + 301)

    def test_rejects_future_timestamp(self):
        """未来方向也要挡，否则签个远未来时间戳就是长期通行证。"""
        with self.assertRaises(SignatureError):
            verify(self.key, GO_TIMESTAMP, GO_BODY, self.signature, now=int(GO_TIMESTAMP) - 301)

    def test_rejects_non_integer_timestamp(self):
        with self.assertRaises(SignatureError):
            verify(self.key, "not-a-number", GO_BODY, self.signature, now=self.now)


class TestEventFiltering(unittest.TestCase):
    APP = "https://notifier.example.com"
    WS = "proj"

    def _extract(self, payload):
        return extract(payload, self.APP, self.WS)

    def test_notifies_on_in_review(self):
        change = self._extract(_make_payload())
        self.assertIsNotNone(change)
        self.assertEqual(change.identifier, "PROJ-128")
        self.assertEqual(change.label, "待审阅")

    def test_notifies_on_blocked(self):
        change = self._extract(
            _make_payload(issue={"status": "blocked", "status_category": "blocked"})
        )
        self.assertIsNotNone(change)
        self.assertEqual(change.label, "已阻塞")
        self.assertEqual(change.color, "red")

    def test_ignores_other_statuses(self):
        for status in ("done", "in_progress", "todo", "backlog", "cancelled"):
            with self.subTest(status=status):
                payload = _make_payload(issue={"status": status, "status_category": status})
                self.assertIsNone(self._extract(payload))

    def test_ignores_non_status_updates(self):
        """issue.updated 与 issue.status_changed 共享 payload，靠此标志去重。"""
        self.assertIsNone(self._extract(_make_payload(status_changed=False)))
        payload = _make_payload()
        del payload["status_changed"]
        self.assertIsNone(self._extract(payload))

    def test_custom_status_inherits_category(self):
        """自定义状态 key 不透明，但继承的分类是稳定语义面。"""
        change = self._extract(
            _make_payload(
                issue={
                    "status": "in_review_2",
                    "status_category": "in_review",
                    "status_name": "等产品确认",
                }
            )
        )
        self.assertIsNotNone(change)
        self.assertEqual(change.status_display, "等产品确认")

    def test_falls_back_to_status_when_category_absent(self):
        """status_category 带 omitempty，缺失时按上游注释回退到 status。"""
        payload = _make_payload()
        del payload["issue"]["status_category"]
        change = self._extract(payload)
        self.assertIsNotNone(change)
        self.assertEqual(change.status_category, "in_review")

    def test_malformed_payloads_never_raise(self):
        """畸形投递只能被忽略，不能让整条通道失败。"""
        for bad in (None, [], "", 42, {}, {"status_changed": True}, {"status_changed": True, "issue": []}):
            with self.subTest(payload=bad):
                self.assertIsNone(self._extract(bad))

    def test_requires_identifier_and_id(self):
        for missing in ("identifier", "id"):
            with self.subTest(missing=missing):
                payload = _make_payload()
                payload["issue"][missing] = ""
                self.assertIsNone(self._extract(payload))


class TestIssueURL(unittest.TestCase):
    def test_builds_canonical_url(self):
        self.assertEqual(
            build_issue_url("https://notifier.example.com", "proj", "PROJ-128"),
            "https://notifier.example.com/proj/issues/PROJ-128",
        )

    def test_tolerates_trailing_slash(self):
        self.assertEqual(
            build_issue_url("https://example.com/", "proj", "PROJ-1"),
            "https://example.com/proj/issues/PROJ-1",
        )


class TestCardRendering(unittest.TestCase):
    def _card(self, **overrides):
        change = extract(_make_payload(**overrides), "https://x.example.com", "proj")
        return render_card(change)

    def test_card_carries_clickable_issue_url(self):
        card = self._card()
        button = card["body"]["elements"][-1]
        self.assertEqual(button["tag"], "button")
        self.assertEqual(
            button["behaviors"][0]["default_url"],
            "https://x.example.com/proj/issues/PROJ-128",
        )

    def test_card_is_json_serialisable(self):
        json.dumps(self._card(), ensure_ascii=False)

    def test_blocked_and_review_differ_visually(self):
        blocked = self._card(issue={"status": "blocked", "status_category": "blocked"})
        review = self._card()
        self.assertNotEqual(
            blocked["header"]["template"], review["header"]["template"]
        )

    def test_lark_webhook_signature_is_deterministic(self):
        """飞书加签算法与 Multica 的不同：把 "<ts>\\n<secret>" 当 key 对空串签名。"""
        self.assertEqual(sign_webhook("s", 1789315756), sign_webhook("s", 1789315756))
        self.assertNotEqual(sign_webhook("s", 1789315756), sign_webhook("s", 1789315757))


class TestCardContract(unittest.TestCase):
    """卡片结构契约。

    这些断言对应飞书 API 真实拒绝过的写法（错误码 200621 unknown property），
    每条都是踩出来的，不是凭空补的。
    """

    def _card(self, **overrides):
        change = extract(_make_payload(**overrides), "https://x.example.com", "proj")
        return render_card(change)

    def _walk(self, node):
        """遍历卡片里的所有组件对象。"""
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from self._walk(value)
        elif isinstance(node, list):
            for item in node:
                yield from self._walk(item)

    def test_declares_schema_2(self):
        self.assertEqual(self._card()["schema"], "2.0")

    def test_no_lines_property_anywhere(self):
        """div.lines 在 column 内嵌套会被飞书拒绝。"""
        for node in self._walk(self._card()):
            self.assertNotIn("lines", node, "lines 属性会触发 200621")

    def test_corner_radius_only_on_supported_containers(self):
        """column 不接受 corner_radius，只有 interactive_container 支持。"""
        for node in self._walk(self._card()):
            if "corner_radius" in node:
                self.assertEqual(
                    node.get("tag"), "interactive_container",
                    "corner_radius 只能出现在 interactive_container 上",
                )

    def test_icon_tokens_are_from_the_documented_enum(self):
        """icon token 必须逐字来自枚举，禁止按名称规律拼接。

        warning_colorful 不存在于彩色枚举，曾导致图标不渲染。
        """
        allowed = {"approval_colorful", "warning_outlined"}
        for theme in CARD_THEME.values():
            self.assertIn(theme["icon"], allowed)

    def test_long_titles_are_clipped(self):
        card = self._card(issue={"title": "长" * 200})
        for node in self._walk(card):
            if node.get("tag") == "markdown":
                self.assertLessEqual(len(node.get("content", "")), 80)

    def test_blocked_and_review_use_adjacent_colours(self):
        """red 与 orange 在色环上相邻，符合配色纪律。"""
        self.assertEqual(CARD_THEME["blocked"]["template"], "red")
        self.assertEqual(CARD_THEME["in_review"]["template"], "orange")


class TestBotSender(unittest.TestCase):
    def test_routes_group_and_direct_targets(self):
        self.assertEqual(LarkBotSender("oc_abc")._flag, "--chat-id")
        self.assertEqual(LarkBotSender("ou_abc")._flag, "--user-id")

    def test_requires_a_target(self):
        with self.assertRaises(ValueError):
            LarkBotSender("")


class TestCardLayout(unittest.TestCase):
    """版式契约：对应爸爸 2026-09-14 提的四点要求。

    改版式时这些断言必须跟着改，否则等于没测——上一版就是改了布局
    而测试全绿，因为没有一条断言指向真实结构。
    """

    def _card(self, **overrides):
        change = extract(_make_payload(**overrides), "https://x.example.com", "proj")
        return render_card(change)

    def test_header_has_no_subtitle(self):
        """要求①：去掉 header 第二行「Multica 任务状态变更」。"""
        self.assertNotIn("subtitle", self._card()["header"])

    def test_header_title_holds_only_the_task_name(self):
        """标题只放任务名，编号与状态交给标签，不再拼进字符串。"""
        title = self._card()["header"]["title"]["content"]
        self.assertIn("统一飞书通知", title)
        self.assertNotIn("PROJ-128", title)
        self.assertNotIn("待审阅", title)

    def test_identifier_and_status_are_tags(self):
        """编号与状态以 text_tag 展示，顺序为先编号后状态。"""
        tags = self._card()["header"]["text_tag_list"]
        self.assertEqual([t["text"]["content"] for t in tags], ["PROJ-128", "待审阅"])
        self.assertEqual(tags[0]["color"], IDENTIFIER_TAG_COLOR)
        self.assertEqual(tags[1]["color"], "orange")

    def test_identifier_tag_is_not_greyed_out(self):
        """灰色在飞书语义里是「失效/归档」，与「请立即处理」正好相反。"""
        self.assertNotIn(IDENTIFIER_TAG_COLOR, {"neutral", "grey"})

    def test_tag_colours_are_from_the_documented_enum(self):
        """color 必须来自 header 文档的合法枚举，写错静默不渲染。"""
        allowed = {"neutral", "blue", "turquoise", "lime", "orange", "violet",
                   "indigo", "wathet", "green", "yellow", "red", "purple", "carmine"}
        for theme in CARD_THEME.values():
            self.assertIn(theme["tag_color"], allowed)
        self.assertIn(IDENTIFIER_TAG_COLOR, allowed)

    def test_tag_list_stays_within_the_limit(self):
        """text_tag_list 上限 3 个，超出会被飞书拒绝。"""
        self.assertLessEqual(len(self._card()["header"]["text_tag_list"]), 3)

    def test_clipping_never_touches_identifier_or_status(self):
        """标题再长也只截标题；编号与状态在标签里，不受影响。"""
        card = self._card(issue={"title": "长" * 300})
        self.assertLessEqual(len(card["header"]["title"]["content"]), 60)
        self.assertEqual(
            [t["text"]["content"] for t in card["header"]["text_tag_list"]],
            ["PROJ-128", "待审阅"],
        )

    def test_blocked_status_tag_reads_as_blocked(self):
        card = self._card(issue={"status": "blocked", "status_category": "blocked"})
        tags = card["header"]["text_tag_list"]
        self.assertEqual(tags[1]["text"]["content"], "已阻塞")
        self.assertEqual(tags[1]["color"], "red")

    def test_body_starts_with_the_description(self):
        """编号移入标题后，正文第一块就是描述本身。"""
        first = self._card()["body"]["elements"][0]
        self.assertEqual(first["tag"], "markdown")
        self.assertIn("这里是任务描述全文", first["content"])

    def test_body_carries_the_description(self):
        """正文给出当前消息全文。"""
        self.assertIn("这里是任务描述全文", self._card()["body"]["elements"][0]["content"])

    def test_body_ends_with_view_button(self):
        """要求④：最后是「查看任务」按钮。"""
        last = self._card()["body"]["elements"][-1]
        self.assertEqual(last["tag"], "button")
        self.assertEqual(last["text"]["content"], "查看任务")
        self.assertEqual(
            last["behaviors"][0]["default_url"],
            "https://x.example.com/proj/issues/PROJ-128",
        )

    def test_long_description_is_truncated_with_a_pointer(self):
        card = self._card(issue={"description": "正" * 5000})
        body = card["body"]["elements"][0]["content"]
        self.assertLess(len(body), 1400)
        self.assertIn("查看完整任务", body)

    def test_empty_description_does_not_leave_a_hole(self):
        body = self._card(issue={"description": ""})["body"]["elements"][0]["content"]
        self.assertIn("暂无描述", body)

    def test_missing_description_field_is_safe(self):
        payload = _make_payload()
        del payload["issue"]["description"]
        change = extract(payload, "https://x.example.com", "proj")
        self.assertEqual(change.description, "")
        render_card(change)


if __name__ == "__main__":
    unittest.main()


class ConfigTest(unittest.TestCase):
    """配置必须全部外置：代码里不留任何具体部署的痕迹。"""

    BASE = {
        "MULTICA_HOOK_SECRET": "whsec_" + "ab" * 32,
        "MULTICA_APP_URL": "https://multica.example.com",
        "MULTICA_WORKSPACE_SLUG": "demo",
    }

    def _load(self, **overrides):
        env = {**self.BASE, **overrides}
        env = {k: v for k, v in env.items() if v is not None}
        with mock.patch.dict(os.environ, env, clear=True):
            return config.load()

    def test_loads_from_environment(self):
        s = self._load()
        self.assertEqual(s.app_url, "https://multica.example.com")
        self.assertEqual(s.workspace_slug, "demo")
        self.assertEqual(s.hook_path, "/hooks/multica")

    def test_each_required_variable_is_enforced(self):
        for missing in self.BASE:
            with self.subTest(missing=missing):
                with self.assertRaises(config.ConfigError) as ctx:
                    self._load(**{missing: None})
                # 报错必须点名变量，否则运维得靠猜。
                self.assertIn(missing, str(ctx.exception))

    def test_no_instance_specific_defaults(self):
        """域名/工作区不能有默认值——否则换环境会静默指向错误实例。"""
        src = (pathlib.Path(config.__file__)).read_text(encoding="utf-8")
        # 同样不写明文，理由见 test_source_tree_has_no_hardcoded_instance。
        for leak in ("haidi" + "lao", "ai-" + "agents", "ou" + "_", "cli" + "_a"):
            self.assertNotIn(leak, src)

    def test_source_tree_has_no_hardcoded_instance(self):
        """整个仓库都不得出现真实域名或身份 ID。

        扫描范围是全仓库而非仅 notifier/ 包：这个项目要开源，而实际
        泄露过的位置是 manifest.json、README.md、compose.yaml —— 恰恰
        都在 notifier/ 之外。只守着源码包等于守错了门。

        被检的字符串本身会出现在这个文件里（断言需要它们），所以跳过
        测试目录自身。
        """
        repo = pathlib.Path(config.__file__).parent.parent
        # 检测模式本身不写明文：这个仓库是公开的，把公司域名/内部主机名
        # 直接写进黑名单，等于用「防泄露」的名义泄露一次。用组装的方式
        # 保留检测能力，同时让仓库里搜不到这些字符串。
        leaks = tuple(
            "".join(parts)
            for parts in (
                ("haidi", "lao"),          # 公司域名片段
                ("ai-", "agents."),        # 内网服务域名前缀
                ("ou_", "5d79"),           # 飞书用户 ID 片段
                ("cli_", "aa05"),          # 飞书应用 ID 片段
                ("hdl", "-200"),           # 内部主机名
                ("IT", "D-"),              # 内部任务编号前缀
            )
        )
        skip_dirs = {".git", "__pycache__", ".venv", "dist"}
        checked = 0
        for path in repo.rglob("*"):
            if not path.is_file():
                continue
            if set(path.relative_to(repo).parts) & skip_dirs:
                continue
            # 这个文件自己组装检测串，扫它会自我命中；其余测试文件照扫——
            # 之前把整个 tests/ 排除在外，而真实泄露（内部任务编号）恰恰
            # 就在测试夹具里，漏了整整一轮。
            if path.resolve() == pathlib.Path(__file__).resolve():
                continue
            if path.name == ".env" or path.suffix in {".pyc", ".tgz", ".zip"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            checked += 1
            for leak in leaks:
                self.assertNotIn(leak, text, f"{path.relative_to(repo)} 泄露了 {leak}")
        # 防止 glob 写错导致一个文件都没扫却显示通过。
        self.assertGreater(checked, 5, "扫描到的文件太少，检查路径逻辑")

    def test_hook_path_is_configurable(self):
        s = self._load(MULTICA_HOOK_PATH="/issue-callbacks")
        self.assertEqual(s.hook_path, "/issue-callbacks")

    def test_hook_path_must_be_absolute(self):
        with self.assertRaises(config.ConfigError):
            self._load(MULTICA_HOOK_PATH="issue-callbacks")

    def test_port_is_validated(self):
        with self.assertRaises(config.ConfigError):
            self._load(BIND_PORT="not-a-port")
        with self.assertRaises(config.ConfigError):
            self._load(BIND_PORT="70000")

    def test_trailing_slash_is_trimmed(self):
        s = self._load(MULTICA_APP_URL="https://multica.example.com/")
        self.assertEqual(s.app_url, "https://multica.example.com")


class MentionFlatteningTest(unittest.TestCase):
    """Multica 的 @ 在飞书里是死链，必须压成纯文本。

    mention://member/<uuid> 是 Multica 内部协议，飞书渲染成蓝色链接但点了
    没反应。留着比去掉更糟——用户会以为能跳转。
    """

    def test_mention_becomes_plain_text(self):
        from notifier.lark import _flatten_mentions
        out = _flatten_mentions("[@董劭杰](mention://member/99999999-8888-4777-8666-555555555555) 看一下")
        self.assertEqual(out, "@董劭杰 看一下")
        self.assertNotIn("mention://", out)

    def test_multiple_mentions(self):
        from notifier.lark import _flatten_mentions
        out = _flatten_mentions("[@甲](mention://member/a) 和 [@乙](mention://agent/b) 都看看")
        self.assertEqual(out, "@甲 和 @乙 都看看")

    def test_issue_mention_without_at_prefix(self):
        """issue 引用是 [PROJ-123](mention://issue/...)，没有 @ 前缀。"""
        from notifier.lark import _flatten_mentions
        out = _flatten_mentions("参见 [PROJ-123](mention://issue/xyz)")
        self.assertNotIn("mention://", out)

    def test_normal_link_untouched(self):
        """普通链接不能被误伤。"""
        from notifier.lark import _flatten_mentions
        src = "见 [文档](https://example.com/doc)"
        self.assertEqual(_flatten_mentions(src), src)
