import tempfile
import os
import subprocess
import threading
import unittest
from unittest import mock
from importlib.machinery import SourceFileLoader

import relayctl.sync as sync

CLI = SourceFileLoader(
    "relayctl_cli_test", os.path.join(os.path.dirname(os.path.dirname(__file__)), "bin", "relayctl")
).load_module()


class SyncSafetyTests(unittest.TestCase):
    def test_cmd_sync_failed_but_restored_returns_rc5(self):
        args = type("Args", (), {"site": "x", "channel": None, "key_id": 1,
                                  "dry_run": False, "replace_all": False, "test_model": "m"})()
        cfg = {"sites": {"x": {"tier": "api", "local_channel": 1, "base": "https://x.test"}}}
        adapter = mock.Mock()
        adapter.reveal_key.return_value = "NEW"
        syncer = mock.Mock()
        syncer.sync_site_key.return_value = {"ok": False, "rolled_back": True, "verified": True}
        with mock.patch.object(CLI, "get_adapter", return_value=adapter), \
             mock.patch.object(CLI, "LocalChannelSyncer", return_value=syncer):
            self.assertEqual(CLI.cmd_sync(args, cfg), CLI.EXIT_SYNC_FAILED)

    def test_cmd_sync_failed_and_not_restored_returns_rc6(self):
        args = type("Args", (), {"site": "x", "channel": None, "key_id": 1,
                                  "dry_run": False, "replace_all": False, "test_model": "m"})()
        cfg = {"sites": {"x": {"tier": "api", "local_channel": 1, "base": "https://x.test"}}}
        adapter = mock.Mock()
        adapter.reveal_key.return_value = "NEW"
        syncer = mock.Mock()
        syncer.sync_site_key.return_value = {"ok": False, "rolled_back": False, "verified": False}
        with mock.patch.object(CLI, "get_adapter", return_value=adapter), \
             mock.patch.object(CLI, "LocalChannelSyncer", return_value=syncer):
            self.assertEqual(CLI.cmd_sync(args, cfg), CLI.EXIT_ROLLBACK_FAILED)

    def test_update_timeout_attempts_old_key_and_verifies(self):
        x = object.__new__(sync.LocalChannelSyncer)
        x.local_cfg = {}
        x.get_channel = lambda _: {"id": 1, "base_url": "https://x.test", "name": "x", "test_model": "m"}
        x.read_channel_key = lambda _: "OLD"
        calls = []

        def update(payload):
            calls.append(payload["key"])
            if len(calls) == 1:
                raise TimeoutError("timeout")

        x.update_channel = update
        old_backup = sync.BACKUP_DIR
        sync.BACKUP_DIR = tempfile.mkdtemp()
        try:
            result = x.sync_site_key("x", 1, "NEW", site_base="https://x.test")
        finally:
            sync.BACKUP_DIR = old_backup
        self.assertEqual(calls, ["NEW", "OLD"])
        self.assertTrue(result["rolled_back"])
        self.assertTrue(result["verified"])

    def test_new_key_readback_error_rolls_back_and_verifies(self):
        x = object.__new__(sync.LocalChannelSyncer)
        x.local_cfg = {}
        x.get_channel = lambda _: {"id": 1, "base_url": "https://x.test", "name": "x", "test_model": "m"}
        reads = iter(["OLD", sync.ChannelKeyError("snapshot failed"), "OLD"])
        x.read_channel_key = lambda _: next(reads)
        calls = []
        x.update_channel = lambda payload: calls.append(payload["key"]) or True
        old_backup = sync.BACKUP_DIR
        sync.BACKUP_DIR = tempfile.mkdtemp()
        try:
            result = x.sync_site_key("x", 1, "NEW", site_base="https://x.test")
        finally:
            sync.BACKUP_DIR = old_backup
        self.assertEqual(calls, ["NEW", "OLD"])
        self.assertTrue(result["rolled_back"])
        self.assertTrue(result["verified"])

    def test_rollback_failure_marks_unknown_state(self):
        x = object.__new__(sync.LocalChannelSyncer)
        x.local_cfg = {}
        x.get_channel = lambda _: {"id": 1, "base_url": "https://x.test", "name": "x", "test_model": "m"}
        x.read_channel_key = lambda _: "OLD"
        calls = []
        def update(payload):
            calls.append(payload["key"])
            if len(calls) == 1:
                raise TimeoutError("timeout")
            raise RuntimeError("rollback unavailable")
        x.update_channel = update
        old_backup = sync.BACKUP_DIR
        sync.BACKUP_DIR = tempfile.mkdtemp()
        try:
            result = x.sync_site_key("x", 1, "NEW", site_base="https://x.test")
        finally:
            sync.BACKUP_DIR = old_backup
        self.assertFalse(result["rolled_back"])
        self.assertFalse(result["verified"])

    def test_backup_names_do_not_overwrite_same_timestamp(self):
        x = object.__new__(sync.LocalChannelSyncer)
        x.local_cfg = {}
        x.get_channel = lambda _: {"id": 1, "base_url": "https://x.test", "name": "x", "test_model": "m"}
        current = ["OLD1"]
        x.read_channel_key = lambda _: current[0]
        def update(payload):
            current[0] = payload["key"]
        x.update_channel = update
        x.test_channel = lambda *_: {"ok": True, "time": 0}
        backup = tempfile.mkdtemp()
        old_backup = sync.BACKUP_DIR
        sync.BACKUP_DIR = backup
        try:
            with mock.patch.object(sync.time, "time_ns", return_value=123):
                x.sync_site_key("x", 1, "NEW1", site_base="https://x.test")
                x.sync_site_key("x", 1, "NEW2", site_base="https://x.test")
            files = [p for p in os.listdir(backup) if p.endswith(".json")]
        finally:
            sync.BACKUP_DIR = old_backup
        self.assertEqual(len(files), 2)

    def test_same_channel_syncs_are_mutually_exclusive(self):
        x = object.__new__(sync.LocalChannelSyncer)
        x.local_cfg = {}
        first_entered_update = threading.Event()
        release_first = threading.Event()
        second_lock_attempt = threading.Event()
        second_entered_channel = threading.Event()
        current = ["OLD"]
        thread_ids = {}
        def get_channel(_):
            if threading.get_ident() == thread_ids.get("second"):
                second_entered_channel.set()
            return {"id": 1, "base_url": "https://x.test", "name": "x", "test_model": "m"}
        x.get_channel = get_channel
        x.read_channel_key = lambda _: current[0]
        def update(payload):
            if payload["key"] == "NEW1":
                first_entered_update.set()
                if not release_first.wait(3):
                    raise TimeoutError("test synchronization timed out")
            current[0] = payload["key"]
        x.update_channel = update
        x.test_channel = lambda *_: {"ok": True, "time": 0}
        backup = tempfile.mkdtemp()
        old_backup = sync.BACKUP_DIR
        sync.BACKUP_DIR = backup
        original_flock = sync.fcntl.flock
        def observed_flock(fd, operation):
            if threading.get_ident() == thread_ids.get("second") and operation & sync.fcntl.LOCK_EX:
                second_lock_attempt.set()
            return original_flock(fd, operation)
        results = []
        def first():
            results.append(x.sync_site_key("x", 9, "NEW1", site_base="https://x.test"))
        def second():
            thread_ids["second"] = threading.get_ident()
            results.append(x.sync_site_key("x", 9, "NEW2", site_base="https://x.test"))
        try:
            with mock.patch.object(sync.fcntl, "flock", side_effect=observed_flock):
                t1 = threading.Thread(target=first)
                t1.start()
                self.assertTrue(first_entered_update.wait(2))
                t2 = threading.Thread(target=second)
                t2.start()
                self.assertTrue(second_lock_attempt.wait(2))
                self.assertFalse(second_entered_channel.is_set())
                release_first.set()
                t1.join(3)
                t2.join(3)
            self.assertFalse(t1.is_alive())
            self.assertFalse(t2.is_alive())
            self.assertTrue(second_entered_channel.is_set())
            self.assertEqual(current[0], "NEW2")
            self.assertEqual(len(results), 2)
        finally:
            release_first.set()
            sync.BACKUP_DIR = old_backup

    def test_sqlite_helper_is_pinned_isolated_and_writes_private_file(self):
        x = object.__new__(sync.LocalChannelSyncer)
        x.local_cfg = {"db_helper_image": "python@sha256:test"}
        calls = []
        local_modes = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[1] == "run":
                local_modes.append(os.stat(kwargs["stdout"].name).st_mode & 0o777)
                kwargs["stdout"].write(b"sqlite snapshot")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            if argv[1:3] == ["container", "ls"]:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with mock.patch.object(sync.subprocess, "run", side_effect=run):
            tmpdir, local_db = x._copy_out_db("db-container", "/data/one-api.db")
        try:
            run_argv = calls[0][0]
            self.assertIn("--pull=never", run_argv)
            self.assertIn("--network", run_argv)
            self.assertEqual(run_argv[run_argv.index("--network") + 1], "none")
            self.assertEqual(run_argv[run_argv.index("--read-only")], "--read-only")
            self.assertEqual(run_argv[run_argv.index("--volumes-from") + 1], "db-container:ro")
            self.assertIn("d=sqlite3.connect(':memory:')", run_argv[run_argv.index("-c") + 1])
            self.assertIn("s.backup(d)", run_argv[run_argv.index("-c") + 1])
            self.assertIn("d.serialize()", run_argv[run_argv.index("-c") + 1])
            self.assertEqual(local_modes, [0o600])
            self.assertEqual(os.stat(local_db).st_mode & 0o777, 0o600)
            run_name = run_argv[run_argv.index("--name") + 1]
            cleanup_name = calls[1][0][3]
            self.assertEqual(cleanup_name, run_name)
            self.assertTrue(run_name.startswith("relayctl-snapshot-"))
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_sqlite_helper_failure_still_cleans_unique_container(self):
        x = object.__new__(sync.LocalChannelSyncer)
        x.local_cfg = {"db_helper_image": "python@sha256:test"}
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[1] == "run":
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="mock failure")
            if argv[1:3] == ["container", "ls"]:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with mock.patch.object(sync.subprocess, "run", side_effect=run):
            with self.assertRaises(RuntimeError):
                x._copy_out_db("db-container", "/data/one-api.db")
        run_name = calls[0][calls[0].index("--name") + 1]
        cleanup_name = calls[1][3]
        self.assertEqual(run_name, cleanup_name)
        self.assertTrue(any(c[1:3] == ["container", "ls"] for c in calls))


if __name__ == "__main__":
    unittest.main()
