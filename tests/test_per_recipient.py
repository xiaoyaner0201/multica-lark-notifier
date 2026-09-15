"""按人分发的接线测试。

重点覆盖两个「不报错但结果错」的场景：
  1. creator/assignee 的邮箱被订阅表补全（否则他们永远收不到）
  2. 发送失败如实上报（否则平台不会重投，通知永久丢失）
"""

import types
import unittest

from notifier import server
from notifier.recipients import Recipient, RecipientResolver


def _change():
    # 只用到 identifier / status_category，不构造完整 StatusChange，
    # 免得上游字段一变测试就跟着碎。
    return types.SimpleNamespace(identifier="DEMO-1", status_category="blocked")


class RecipientEmailBackfillTest(unittest.TestCase):
    """creator/assignee 先以空邮箱占位，必须被订阅表的完整记录补全。"""

    def _resolver_with(self, subscribers):
        r = RecipientResolver("https://example.com", "tok")
        r._fetch_subscribers = lambda issue_id, workspace_id: subscribers  # noqa: SLF001
        return r

    def test_creator_email_is_backfilled(self):
        issue = {
            "id": "i1", "identifier": "DEMO-1",
            "creator_id": "u1", "creator_type": "member",
        }
        resolver = self._resolver_with(
            [Recipient(user_id="u1", email="a@x.com", reason="commenter")]
        )
        out = resolver.resolve(issue, "ws")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].email, "a@x.com", "邮箱必须被补全，否则解析不出 open_id")
        self.assertEqual(out[0].reason, "creator", "reason 保留更准确的那个")

    def test_distinct_users_both_kept(self):
        issue = {
            "id": "i1", "identifier": "DEMO-1",
            "creator_id": "u1", "creator_type": "member",
        }
        resolver = self._resolver_with(
            [Recipient(user_id="u2", email="b@x.com", reason="mentioned")]
        )
        self.assertEqual(len(resolver.resolve(issue, "ws")), 2)


class DeliveryFailureTest(unittest.TestCase):
    """个人分发是唯一链路时，失败必须回报，让平台重投。"""

    def _config(self, sender, people, resolutions):
        return types.SimpleNamespace(
            sender=sender,
            debug_target="",
            recipients=types.SimpleNamespace(resolve=lambda issue, workspace_id: people),
            identities=types.SimpleNamespace(resolve=lambda emails: resolutions),
        )

    def test_send_failure_reports_not_ok(self):
        class Boom:
            def send_to(self, *a, **k):
                raise RuntimeError("飞书 500")

        cfg = self._config(
            Boom(),
            [Recipient(user_id="u1", email="a@x.com", reason="creator")],
            [types.SimpleNamespace(email="a@x.com", open_id="ou_a", resolved=True)],
        )
        ok, detail = server._deliver_personal(cfg, _change(), {"issue": {}})
        self.assertFalse(ok, "发送失败必须报告失败")
        self.assertIn("0/1", detail)

    def test_unresolved_does_not_fail_the_request(self):
        """解析不出 open_id 时重投也救不回来，回 200 但要留痕。"""
        sent = []
        cfg = self._config(
            types.SimpleNamespace(send_to=lambda t, c, n="": sent.append(t)),
            [Recipient(user_id="u1", email="a@x.com", reason="creator")],
            [types.SimpleNamespace(email="a@x.com", open_id=None, resolved=False)],
        )
        ok, detail = server._deliver_personal(cfg, _change(), {"issue": {}})
        self.assertTrue(ok)
        self.assertIn("unresolved=1", detail)
        self.assertEqual(sent, [], "解析不出的人不该被发送")


if __name__ == "__main__":
    unittest.main()
