import os
import stat
import tempfile
import threading
import unittest
from unittest import mock

from relayctl import cache as rcache
from relayctl import config
from relayctl import paths
from relayctl import scheduler as rsched
from relayctl.errors import AuthError, ConfigError, NotSupported


class Resp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.text = ""
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return self._data


class FakeClient:
    """记录所有请求路径的最小客户端（用于断言绝不请求 pricing）。"""

    def __init__(self):
        self.paths = []

    def get(self, path, **kwargs):
        self.paths.append(path)
        if path == "/api/status":
            return Resp({"data": {"system_name": "New API", "version": "v1.0.0",
                                  "quota_per_unit": 500000, "quota_display_type": "USD"}})
        if path in ("/api/user/self", "/api/v1/user/profile"):
            return Resp({"success": True, "data": {"quota": 1000000, "used_quota": 500000,
                                                   "group": "default", "username": "u"}})
        if path == "/api/user/self/groups":
            return Resp({"success": True, "data": {"default": {"ratio": 1, "desc": ""}}})
        if path.startswith("/api/token/"):
            return Resp({"success": True, "data": {"items": [], "total": 0}})
        return Resp({}, status=404)


class FakeAdapter:
    def __init__(self, name="demo", client=None, errors=None):
        self.name = name
        self.client = client or FakeClient()
        self.calls = []
        self.errors = errors or {}

    def _maybe(self, section):
        self.calls.append(section)
        exc = self.errors.get(section)
        if exc:
            raise exc
        return {"section": section}

    def probe(self):
        self._maybe("probe")
        return {"site": self.name, "system_name": "New API", "version": "v1",
                "auth_ok": True, "quota_per_unit": 500000, "quota_display_type": "USD",
                "status_ok": True}

    def balance(self):
        self._maybe("balance")
        return {"balance": "2.0000", "used": "1.0000", "raw_quota": 1000000,
                "raw_used": 500000, "quota_per_unit": 500000,
                "display_type": "USD", "user_group": "default"}

    def groups(self):
        self._maybe("groups")
        return [{"name": "default", "desc": "", "ratio": 1}]

    def list_keys(self):
        self._maybe("keys")
        return [{"id": 1, "name": "k", "key_masked": "sk-1...abcd"}]


class Virtual:
    """虚拟 clock / random，保证调度计算完全可预测。"""

    def __init__(self, start=0.0, rand=0.5):
        self.now = start
        self.rand_value = rand
        self.rand_calls = 0

    def clock(self):
        return self.now

    def wall(self):
        return 1_700_000_000.0 + self.now

    def rand(self):
        self.rand_calls += 1
        return self.rand_value


def site(name="demo", status=config.STATUS_ACTIVE, refresh_interval=""):
    return {
        "name": name,
        "base": f"https://{name}.example.com",
        "adapter": "newapi",
        "panel": "newapi",
        "auth": dict(config.DEFAULT_AUTH),
        "docs": dict(config.DEFAULT_DOCS),
        "proxy": "",
        "refresh_interval": refresh_interval,
        "tags": [],
        "status": status,
        "parked_reason": "",
        "tier": config.TIER_BY_STATUS[status],
    }


def core_cfg(sites=None, **override):
    cfg = dict(config.DEFAULT_CONFIG)
    cfg["sites"] = sites or {}
    cfg.update(override)
    return cfg


class SchedulerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"RELAYCTL_HOME": self.tmp})
        self._env.start()
        # 默认拦截适配器：测试里绝不允许真的打网络
        self.adapters = {}

        def factory(name, s_cfg, cfg):
            return self.adapters.get(name) or FakeAdapter(name)

        self._ga = mock.patch.object(rsched, "get_adapter", side_effect=factory)
        self._ga.start()

    def tearDown(self):
        self._ga.stop()
        self._env.stop()

    def scheduler(self, sites, **kwargs):
        virtual = kwargs.pop("virtual", None) or Virtual()
        kwargs.setdefault("clock", virtual.clock)
        kwargs.setdefault("wall_clock", virtual.wall)
        kwargs.setdefault("rand", virtual.rand)
        kwargs.setdefault("log", False)
        global_cfg = kwargs.pop("global_cfg", {}) or {}
        sched = rsched.Scheduler(core_cfg(sites, **global_cfg), **kwargs)
        sched._virtual = virtual
        return sched


class IntervalTests(SchedulerTestBase):
    def test_parse_supports_smhd(self):
        self.assertEqual(rsched.parse_interval("45s"), 45.0)
        self.assertEqual(rsched.parse_interval("30m"), 1800.0)
        self.assertEqual(rsched.parse_interval("2h"), 7200.0)
        self.assertEqual(rsched.parse_interval("1d"), 86400.0)
        self.assertEqual(rsched.parse_interval("20"), 20.0)  # 裸数字按秒
        self.assertEqual(rsched.parse_interval("0"), 0.0)
        self.assertEqual(rsched.parse_interval("0s"), 0.0)

    def test_parse_empty_falls_back_to_default(self):
        self.assertEqual(rsched.parse_interval("", default=600.0), 600.0)
        self.assertEqual(rsched.parse_interval(None, default=600.0), 600.0)

    def test_parse_rejects_garbage(self):
        for bad in ("半小时", "10x", "abc", "-5m", "1.5h"):
            with self.assertRaises(ConfigError, msg=bad):
                rsched.parse_interval(bad)

    def test_global_default_is_30m(self):
        self.assertEqual(rsched.site_interval(site(), {"refresh_interval": "30m"}), 1800.0)

    def test_site_interval_overrides_global(self):
        self.assertEqual(
            rsched.site_interval(site(refresh_interval="15m"), {"refresh_interval": "30m"}),
            900.0)
        self.assertEqual(
            rsched.site_interval(site(refresh_interval="0"), {"refresh_interval": "30m"}),
            0.0)


class JitterAndBackoffTests(SchedulerTestBase):
    def test_jitter_stays_within_bounds(self):
        sched = self.scheduler({}, global_cfg={"scheduler": {"jitter": 0.1, "max_workers": 2}})
        base = 1800.0
        sched.rand = lambda: 0.0
        self.assertAlmostEqual(sched.jittered(base), base * 0.9)
        sched.rand = lambda: 0.999999
        self.assertLessEqual(sched.jittered(base), base * 1.1)
        self.assertGreater(sched.jittered(base), base * 1.099)
        sched.rand = lambda: 0.5
        self.assertAlmostEqual(sched.jittered(base), base)

    def test_success_uses_interval_plus_jitter(self):
        virtual = Virtual(rand=0.5)
        sched = self.scheduler({"demo": site()}, virtual=virtual)
        report = sched.run_once()
        entry = report["sites"]["demo"]
        self.assertTrue(entry["refreshed"])
        self.assertAlmostEqual(entry["next_refresh_in"], 1800.0, delta=1.0)

    def test_failure_backoff_is_exponential(self):
        virtual = Virtual(rand=0.5)
        sched = self.scheduler({"demo": site(refresh_interval="5m")}, virtual=virtual)
        adapter = FakeAdapter(errors={"balance": RuntimeError("boom token=sk-abcdef123456")})
        with mock.patch.object(rsched, "get_adapter", return_value=adapter):
            first = sched.run_once()["sites"]["demo"]
            self.assertAlmostEqual(first["next_refresh_in"], 60.0, delta=1.0)
            virtual.now += 60.0
            second = sched.run_once()["sites"]["demo"]
            self.assertAlmostEqual(second["next_refresh_in"], 120.0, delta=1.0)
            virtual.now += 120.0
            third = sched.run_once()["sites"]["demo"]
            self.assertAlmostEqual(third["next_refresh_in"], 240.0, delta=1.0)
        self.assertEqual(third["failures"], 3)

    def test_backoff_is_capped(self):
        virtual = Virtual(rand=0.5)
        cfg = core_cfg({"demo": site(refresh_interval="5m")})
        cfg["scheduler"] = {**config.DEFAULT_CONFIG["scheduler"],
                            "backoff_initial": 1000, "backoff_max": 1500}
        sched = rsched.Scheduler(cfg, clock=virtual.clock, rand=virtual.rand,
                                 wall_clock=virtual.wall, log=False)
        adapter = FakeAdapter(errors={"balance": RuntimeError("boom")})
        with mock.patch.object(rsched, "get_adapter", return_value=adapter):
            first = sched.run_once()["sites"]["demo"]
            self.assertAlmostEqual(first["next_refresh_in"], 1000.0, delta=1.0)
            virtual.now += 1000.0
            second = sched.run_once()["sites"]["demo"]
            self.assertAlmostEqual(second["next_refresh_in"], 1500.0, delta=1.0)

    def test_auth_failure_never_retries_faster_than_interval(self):
        virtual = Virtual(rand=0.5)
        sched = self.scheduler({"demo": site(refresh_interval="30m")}, virtual=virtual)
        adapter = FakeAdapter(errors={"balance": AuthError("HTTP 401 token=abc")})
        with mock.patch.object(rsched, "get_adapter", return_value=adapter):
            entry = sched.run_once()["sites"]["demo"]
        self.assertGreaterEqual(entry["next_refresh_in"], 1800.0 * 0.9)
        self.assertEqual(entry["errors"][0].split(":")[0].strip(), "balance")

    def test_unsupported_does_not_retry_more_often_than_interval(self):
        virtual = Virtual(rand=0.5)
        sched = self.scheduler({"demo": site(refresh_interval="30m")}, virtual=virtual)
        adapter = FakeAdapter(errors={"groups": NotSupported("站点 demo 无法读取分组")})
        with mock.patch.object(rsched, "get_adapter", return_value=adapter):
            entry = sched.run_once()["sites"]["demo"]
        # 能力不支持只记录，不做 60s 级的退避重试
        self.assertFalse(entry["success"])
        self.assertGreaterEqual(entry["next_refresh_in"], 1800.0 * 0.9)
        self.assertFalse(entry["sections"]["groups"]["ok"])
        self.assertTrue(entry["sections"]["balance"]["ok"])

    def test_retry_after_is_respected(self):
        virtual = Virtual(rand=0.5)
        sched = self.scheduler({"demo": site(refresh_interval="1h")}, virtual=virtual)

        class Limited(Exception):
            response = type("R", (), {"headers": {"Retry-After": "900"}})()

        exc = Limited("429 too many requests")
        self.assertEqual(rsched.classify_error(exc), rsched.KIND_RATE_LIMIT)
        self.assertEqual(rsched.retry_after_seconds(exc), 900.0)

        adapter = FakeAdapter(errors={"keys": exc})
        with mock.patch.object(rsched, "get_adapter", return_value=adapter):
            entry = sched.run_once()["sites"]["demo"]
        self.assertGreaterEqual(entry["next_refresh_in"], 900.0 * 0.9)


class PlanAndRunTests(SchedulerTestBase):
    def test_parked_and_zero_interval_are_skipped(self):
        sched = self.scheduler({
            "act": site("act"),
            "off": site("off", status=config.STATUS_PARKED),
            "manual": site("manual", refresh_interval="0"),
        })
        report = sched.run_once()
        self.assertTrue(report["sites"]["act"]["refreshed"])
        self.assertFalse(report["sites"]["off"]["refreshed"])
        self.assertIn("parked", report["sites"]["off"]["skipped_reason"])
        self.assertFalse(report["sites"]["manual"]["refreshed"])
        self.assertIn("0", report["sites"]["manual"]["skipped_reason"])
        self.assertEqual(report["summary"]["skipped"], 2)

    def test_first_run_has_no_state_so_all_active_are_due(self):
        sched = self.scheduler({"a": site("a", refresh_interval="1h"),
                                "b": site("b", refresh_interval="2h")})
        report = sched.run_once()
        self.assertEqual(report["summary"]["refreshed"], 2)

    def test_not_due_site_is_not_refreshed(self):
        virtual = Virtual(rand=0.5)
        sched = self.scheduler({"demo": site(refresh_interval="10m")}, virtual=virtual)
        sched.run_once()
        again = sched.run_once()
        self.assertFalse(again["sites"]["demo"]["refreshed"])
        self.assertEqual(again["sites"]["demo"]["skipped_reason"], "未到刷新时间")
        virtual.now += 600.0
        self.assertTrue(sched.run_once()["sites"]["demo"]["refreshed"])

    def test_report_shape(self):
        sched = self.scheduler({"demo": site()})
        adapter = FakeAdapter(errors={"groups": RuntimeError("分组接口挂了")})
        with mock.patch.object(rsched, "get_adapter", return_value=adapter):
            report = sched.run_once()
        entry = report["sites"]["demo"]
        for key in ("sections", "success", "errors", "next_refresh_at",
                    "next_refresh_at_local", "refreshed"):
            self.assertIn(key, entry)
        self.assertEqual(sorted(entry["sections"]), sorted(rsched.SECTIONS))
        self.assertFalse(entry["success"])
        self.assertTrue(entry["errors"])
        self.assertIsNotNone(entry["next_refresh_at_local"])
        self.assertIn("summary", report)

    def test_probe_without_panel_and_auth_counts_as_failure(self):
        """probe 本身不抛异常，但「两头都探不到」的站点必须记为失败。"""
        virtual = Virtual(rand=0.5)
        sched = self.scheduler({"demo": site(refresh_interval="30m")}, virtual=virtual)

        class DeadAdapter(FakeAdapter):
            def probe(self):
                self.calls.append("probe")
                return {"site": self.name, "status_ok": False, "auth_ok": False,
                        "status_error": "还未配置中转站信息", "auth_status": 406}

        with mock.patch.object(rsched, "get_adapter", return_value=DeadAdapter("demo")):
            entry = sched.run_once()["sites"]["demo"]
        self.assertFalse(entry["success"])
        self.assertFalse(entry["sections"]["probe"]["ok"])
        self.assertTrue(entry["sections"]["probe"]["stale"])
        self.assertTrue(rcache.read("demo", "probe")["stale"])

    def test_status_does_not_refresh(self):
        sched = self.scheduler({"demo": site()})
        sched.run_once()
        with mock.patch.object(rsched, "get_adapter", return_value=FakeAdapter()) as ga:
            sched.status()
            ga.assert_not_called()
        status = sched.status()
        self.assertIn("demo", status["sites"])
        self.assertIsNotNone(status["sites"]["demo"]["next_refresh_at"])
        for key in ("sections", "success", "errors", "next_refresh_at"):
            self.assertIn(key, status["sites"]["demo"])

    def test_stale_cache_is_preserved_on_refresh_failure(self):
        sched = self.scheduler({"demo": site()})
        sched.run_once()
        self.assertFalse(rcache.read("demo", "balance")["stale"])

        adapter = FakeAdapter(errors={"balance": RuntimeError("upstream kaput")})
        sched._virtual.now += 2000.0  # 推进虚拟时钟使站点到期
        with mock.patch.object(rsched, "get_adapter", return_value=adapter):
            sched.run_once()
        ent = rcache.read("demo", "balance")
        self.assertTrue(ent["stale"])
        self.assertTrue(ent["last_error"])
        self.assertEqual(ent["data"]["balance"], "2.0000")

    def test_success_clears_stale_marker(self):
        sched = self.scheduler({"demo": site()})
        sched.run_once()  # 先成功写入一次，后面才有「旧缓存」可保
        adapter = FakeAdapter(errors={"balance": RuntimeError("upstream kaput")})
        sched._virtual.now += 2000.0
        with mock.patch.object(rsched, "get_adapter", return_value=adapter):
            sched.run_once()
        self.assertTrue(rcache.read("demo", "balance")["stale"])
        sched._virtual.now += 2000.0
        sched.run_once()
        ent = rcache.read("demo", "balance")
        self.assertFalse(ent["stale"])
        self.assertIsNone(ent["last_error"])

    def test_cache_file_stays_private_after_stale_update(self):
        sched = self.scheduler({"demo": site()})
        adapter = FakeAdapter(errors={"balance": RuntimeError("upstream kaput")})
        sched.run_once()
        sched._virtual.now += 2000.0
        with mock.patch.object(rsched, "get_adapter", return_value=adapter):
            sched.run_once()
        self.assertEqual(stat.S_IMODE(os.stat(rcache.cache_path("demo")).st_mode), 0o600)

    def test_no_plaintext_key_is_cached(self):
        from relayctl.adapters.newapi import NewAPIAdapter  # noqa: E402

        client = FakeClient()
        adapter = NewAPIAdapter("demo", {"base": "https://demo.example.com",
                                         "adapter": "newapi", "auth": {}}, {})
        adapter.client = client
        adapter._probed = True
        adapter._quota_unit_known = True
        sched = self.scheduler({"demo": site()})
        with mock.patch.object(rsched, "get_adapter", return_value=adapter):
            sched.run_once()
        with open(rcache.cache_path("demo"), encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("SUPERSECRET", raw)
        # 绝不请求定价/折算端点
        self.assertTrue(client.paths)
        for path in client.paths:
            self.assertNotIn("pricing", path.lower())

    def test_workers_come_from_config(self):
        cfg = core_cfg({"demo": site()})
        cfg["scheduler"] = {**config.DEFAULT_CONFIG["scheduler"], "max_workers": 7}
        sched = rsched.Scheduler(cfg, clock=Virtual().clock, rand=Virtual().rand, log=False)
        self.assertEqual(sched.max_workers, 7)
        sched2 = rsched.Scheduler(core_cfg({"demo": site()}), clock=Virtual().clock,
                                  rand=Virtual().rand, log=False)
        self.assertEqual(sched2.max_workers, 4)


class ConcurrencyTests(SchedulerTestBase):
    def test_same_site_never_runs_twice_concurrently(self):
        started = threading.Event()
        release = threading.Event()
        counter = {"calls": 0, "concurrent": 0, "live": 0}
        base_refresh = rsched.Scheduler.refresh_site

        class SlowScheduler(rsched.Scheduler):
            def refresh_site(self, name, s_cfg):
                counter["calls"] += 1
                counter["live"] += 1
                counter["concurrent"] = max(counter["concurrent"], counter["live"])
                started.set()
                release.wait(3)
                try:
                    return base_refresh(self, name, s_cfg)
                finally:
                    counter["live"] -= 1

        virtual = Virtual(rand=0.5)
        cfg = core_cfg({"demo": site(refresh_interval="1m")})
        sched = SlowScheduler(cfg, clock=virtual.clock, rand=virtual.rand,
                              wall_clock=virtual.wall, log=False)
        first = {}
        worker = threading.Thread(target=lambda: first.update(sched.run_once()))
        worker.start()
        self.assertTrue(started.wait(3))

        second = sched.run_once()
        release.set()
        worker.join(5)

        self.assertEqual(counter["calls"], 1)
        self.assertEqual(counter["concurrent"], 1)
        self.assertTrue(first["sites"]["demo"]["refreshed"])
        self.assertFalse(second["sites"]["demo"]["refreshed"])
        self.assertTrue(second["sites"]["demo"]["busy"])
        self.assertIn("正在刷新", second["sites"]["demo"]["skipped_reason"])


class DaemonLoggerTests(SchedulerTestBase):
    def test_log_file_is_private_and_redacted(self):
        logger = rsched.DaemonLogger()
        logger.emit("站点 demo 失败: token=sk-SUPERSECRET123456 catty Secret name")
        path = os.path.join(paths.logs_dir(), rsched.LOG_NAME)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(paths.logs_dir()).st_mode), 0o700)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("SUPERSECRET", content)
        self.assertIn("relayctl", content.lower()) if "relayctl" in content else None

    def test_scheduler_logs_refresh_result(self):
        sched = self.scheduler({"demo": site()}, log=True)
        with mock.patch.object(rsched, "get_adapter", return_value=FakeAdapter()):
            sched.run_once()
        path = os.path.join(paths.logs_dir(), rsched.LOG_NAME)
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("demo", content)


class ReportLinesTests(unittest.TestCase):
    def test_report_lines_are_human_readable(self):
        report = {
            "summary": {"total": 2, "refreshed": 1, "failed": 1, "skipped": 0},
            "sites": {
                "ok-site": {"refreshed": True, "success": True, "busy": False,
                            "sections": {"balance": {"ok": True}, "keys": {"ok": True}},
                            "errors": [], "failures": 0,
                            "next_refresh_at_local": "2026-01-01 00:00:00"},
                "bad-site": {"refreshed": True, "success": False, "busy": False,
                             "sections": {"balance": {"ok": False}},
                             "errors": ["balance: HTTP 401"], "failures": 2,
                             "next_refresh_at_local": "2026-01-01 01:00:00"},
            },
        }
        lines = rsched.daemon_report_lines(report)
        joined = "\n".join(lines)
        self.assertIn("ok-site", joined)
        self.assertIn("成功", joined)
        self.assertIn("HTTP 401", joined)
        self.assertIn("失败", joined)


if __name__ == "__main__":
    unittest.main()
