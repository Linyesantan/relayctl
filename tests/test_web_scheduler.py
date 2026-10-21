"""Web 内置后台调度器的生命周期测试（全部使用 mock，绝不启动真实长循环 / 端口）。"""

import os
import tempfile
import unittest
from unittest import mock

from relayctl import config
from relayctl import web


class WebSchedulerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"RELAYCTL_HOME": self.tmp})
        self._env.start()
        self.addCleanup(self._env.stop)

    def cfg(self, **override):
        cfg = dict(config.DEFAULT_CONFIG)
        cfg["sites"] = {}
        web_cfg = dict(cfg["web"])
        web_cfg.update(override.pop("web", {}))
        cfg["web"] = web_cfg
        cfg.update(override)
        return cfg


class SchedulerEnabledTests(WebSchedulerTestBase):
    def test_enabled_by_default(self):
        self.assertTrue(web.scheduler_enabled(self.cfg()))
        self.assertTrue(web.scheduler_enabled(config.DEFAULT_CONFIG))

    def test_config_flag_can_disable(self):
        cfg = self.cfg(web={"scheduler_enabled": False})
        self.assertFalse(web.scheduler_enabled(cfg))
        self.assertIsNone(web.start_background_scheduler(cfg))

    def test_run_argument_overrides_config(self):
        enabled = web.scheduler_enabled(self.cfg(web={"scheduler_enabled": False}), True)
        self.assertTrue(enabled)
        disabled = web.scheduler_enabled(self.cfg(), False)
        self.assertFalse(disabled)


class StartStopTests(WebSchedulerTestBase):
    def test_start_uses_daemon_thread_and_stops_on_close(self):
        calls = {"started": 0, "stopped": 0, "daemon": None}

        class FakeScheduler:
            def start(self):
                calls["started"] += 1
                thread = mock.Mock()
                calls["daemon"] = thread.daemon
                return thread

            def stop(self, timeout=None):
                calls["stopped"] += 1

        scheduler = FakeScheduler()
        started = web.start_background_scheduler(self.cfg(), factory=lambda cfg: scheduler)
        self.assertIs(started, scheduler)
        self.assertEqual(calls["started"], 1)

        web.stop_background_scheduler(started)
        self.assertEqual(calls["stopped"], 1)

    def _run_web(self, **kwargs):
        """驱动一次 run_web：HTTPServer / 调度器均为 mock，绝不真实监听端口。"""
        server_calls = []

        class FakeHTTPServer:
            def __init__(self, address, handler_cls):
                server_calls.append(address)

            def serve_forever(self):
                server_calls.append("serve")

            def server_close(self):
                server_calls.append("close")

        with mock.patch.object(web, "HTTPServer", FakeHTTPServer):
            web.run_web(host="127.0.0.1", port=8088, **kwargs)
        return server_calls

    def test_run_web_starts_and_stops_scheduler(self):
        calls = []

        class FakeScheduler:
            def start(self):
                calls.append("start")

            def stop(self, timeout=None):
                calls.append("stop")

        scheduler = FakeScheduler()
        with mock.patch.object(web, "load_config", return_value=self.cfg()), \
                mock.patch.object(web, "start_background_scheduler",
                                  return_value=scheduler) as start_fn, \
                mock.patch.object(web, "stop_background_scheduler") as stop_fn:
            server_calls = self._run_web()

        start_fn.assert_called_once()
        stop_fn.assert_called_once_with(scheduler)
        self.assertIn("serve", server_calls)
        self.assertIn("close", server_calls)
        self.assertIsNone(web._WEB_SCHEDULER)

    def test_run_web_respects_disabled_scheduler(self):
        with mock.patch.object(web, "load_config", return_value=self.cfg()), \
                mock.patch.object(web, "start_background_scheduler",
                                  return_value=None) as start_fn, \
                mock.patch.object(web, "stop_background_scheduler") as stop_fn:
            self._run_web(scheduler_enabled=False)
        kwargs = start_fn.call_args.kwargs
        self.assertFalse(kwargs["enabled"])
        stop_fn.assert_called_once_with(None)

    def test_run_web_stops_scheduler_even_on_keyboard_interrupt(self):
        stopped = []

        class FakeScheduler:
            def start(self):
                pass

            def stop(self, timeout=None):
                stopped.append(True)

        calls = []

        class FakeHTTPServer:
            def __init__(self, address, handler_cls):
                pass

            def serve_forever(self):
                calls.append("serve")
                raise KeyboardInterrupt

            def server_close(self):
                calls.append("close")

        with mock.patch.object(web, "load_config", return_value=self.cfg()), \
                mock.patch.object(web, "start_background_scheduler",
                                  return_value=FakeScheduler()), \
                mock.patch.object(web, "HTTPServer", FakeHTTPServer):
            web.run_web(host="127.0.0.1", port=8088)
        # Ctrl+C 也必须走到 stop + join
        self.assertEqual(calls, ["serve", "close"])
        self.assertEqual(stopped, [True])

    def test_scheduler_thread_is_daemonized(self):
        """真实 Scheduler 的后台线程必须是 daemon，且不启动真实循环。"""
        from relayctl import scheduler as rsched

        sched = rsched.Scheduler(self.cfg(), log=False)
        thread_holder = {}
        with mock.patch.object(rsched.Scheduler, "_loop", lambda self: None):
            thread_holder["thread"] = sched.start()
        self.assertTrue(thread_holder["thread"].daemon)
        self.assertTrue(sched.stop(3.0))

    def test_web_requests_never_create_scheduler(self):
        """Web 请求只读取数据，调度器只在 run_web 生命周期内存在一份。"""
        handler = web.RelayctlWebHandler.__new__(web.RelayctlWebHandler)
        self.assertFalse(hasattr(handler, "scheduler"))
        self.assertIsNone(web._WEB_SCHEDULER)


if __name__ == "__main__":
    unittest.main()
