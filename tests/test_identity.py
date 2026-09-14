import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from notifier.identity import IdentityError, IdentityResolver


def _resolver(tmp: Path, overrides: dict | None = None) -> IdentityResolver:
    path = None
    if overrides is not None:
        path = tmp / "overrides.json"
        path.write_text(json.dumps(overrides), encoding="utf-8")
    return IdentityResolver(app_id="a", app_secret="b", override_path=path)


class OverridePrecedenceTest(unittest.TestCase):
    """覆盖表必须赢过通讯录。

    这是整个双信道设计的核心：飞书绑定邮箱可能不准，通讯录会把一个
    绑错邮箱的人解析成别人。顺序反过来就是把通知发给错的同事。
    """

    def test_override_wins_over_directory(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            r = _resolver(tmp, {"a@example.com": "ou_correct"})
            with mock.patch.object(
                IdentityResolver, "_query_directory", return_value={"a@example.com": "ou_WRONG"}
            ) as q:
                out = r.resolve(["a@example.com"])
            self.assertEqual(out[0].open_id, "ou_correct")
            self.assertEqual(out[0].source, "override")
            # 命中覆盖表就不该再去问通讯录
            q.assert_not_called()

    def test_directory_used_when_no_override(self):
        with tempfile.TemporaryDirectory() as d:
            r = _resolver(Path(d), {})
            with mock.patch.object(
                IdentityResolver, "_query_directory", return_value={"b@example.com": "ou_b"}
            ):
                out = r.resolve(["b@example.com"])
            self.assertEqual(out[0].open_id, "ou_b")
            self.assertEqual(out[0].source, "directory")


class DegradationTest(unittest.TestCase):
    def test_directory_failure_does_not_lose_override_hits(self):
        """通讯录挂了，覆盖表里的人照样能收到。

        一个外部依赖故障不该让全部通知停摆。
        """
        with tempfile.TemporaryDirectory() as d:
            r = _resolver(Path(d), {"safe@x.com": "ou_safe"})
            with mock.patch.object(
                IdentityResolver, "_query_directory", side_effect=IdentityError("通讯录 500")
            ):
                out = r.resolve(["safe@x.com", "other@example.com"])
            by_email = {i.email: i for i in out}
            self.assertEqual(by_email["safe@x.com"].open_id, "ou_safe")
            self.assertIsNone(by_email["other@example.com"].open_id)
            self.assertEqual(by_email["other@example.com"].source, "unresolved")

    def test_missing_scope_message_is_actionable(self):
        """缺权限要说清楚缺哪个、怎么办，不能只丢一个错误码。"""
        with tempfile.TemporaryDirectory() as d:
            r = _resolver(Path(d))
            payload = json.dumps({"code": 99991672, "msg": "denied"}).encode()
            fake = mock.MagicMock()
            fake.__enter__.return_value.read.return_value = payload
            with mock.patch.object(IdentityResolver, "_access_token", return_value="t"), \
                 mock.patch("urllib.request.urlopen", return_value=fake):
                with self.assertRaises(IdentityError) as ctx:
                    r._query_directory(["x@example.com"])
            self.assertIn("contact:user.id:readonly", str(ctx.exception))

    def test_broken_override_file_raises(self):
        """覆盖表坏了要响。

        静默退化成“只用通讯录”正是这张表要防的情况 —— 那 4 个外部邮箱
        用户会无声无息地收不到通知。
        """
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "overrides.json"
            p.write_text("{ not json", encoding="utf-8")
            r = IdentityResolver(app_id="a", app_secret="b", override_path=p)
            with self.assertRaises(IdentityError):
                r.resolve(["a@example.com"])


class AuditTest(unittest.TestCase):
    def test_audit_lists_people_needing_manual_mapping(self):
        """审计要能直接给出「该补进覆盖表」的名单。"""
        with tempfile.TemporaryDirectory() as d:
            r = _resolver(Path(d), {"m@x.com": "ou_m"})
            with mock.patch.object(
                IdentityResolver, "_query_directory", return_value={"d@example.com": "ou_d"}
            ):
                buckets = r.audit(["m@x.com", "d@example.com", "gone@example.com"])
            self.assertEqual(buckets["override"], ["m@x.com"])
            self.assertEqual(buckets["directory"], ["d@example.com"])
            self.assertEqual(buckets["unresolved"], ["gone@example.com"])


class CacheTest(unittest.TestCase):
    def test_override_change_takes_effect_without_restart(self):
        """覆盖表改完即时生效，不进缓存。

        它是人工纠错通道，改了要立刻起作用。
        """
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "overrides.json"
            p.write_text(json.dumps({"a@x.com": "ou_v1"}), encoding="utf-8")
            r = IdentityResolver(app_id="a", app_secret="b", override_path=p)
            self.assertEqual(r.resolve(["a@x.com"])[0].open_id, "ou_v1")
            p.write_text(json.dumps({"a@x.com": "ou_v2"}), encoding="utf-8")
            self.assertEqual(r.resolve(["a@x.com"])[0].open_id, "ou_v2")

    def test_directory_result_is_cached(self):
        with tempfile.TemporaryDirectory() as d:
            r = _resolver(Path(d))
            with mock.patch.object(
                IdentityResolver, "_query_directory", return_value={"c@example.com": "ou_c"}
            ) as q:
                r.resolve(["c@example.com"])
                r.resolve(["c@example.com"])
            self.assertEqual(q.call_count, 1)


if __name__ == "__main__":
    unittest.main()
