import os
import sys
import time
import json
import shutil
import sqlite3
import subprocess
import tempfile
import requests
import uuid
import fcntl
from urllib.parse import urlparse
from typing import Optional
from .core import BACKUP_DIR, HttpClient, secure_write_json

# 本地 New API 的 SQLite 库位置。API 的 GET /api/channel/{id} 不返回 key，
# POST /api/channel/{id}/key 又需要二次安全验证（403 SECURITY_PROOF_INVALID），
# 因此备份/回滚只能以只读方式从库里读明文 key。
# 卷目录一般是 root:700，宿主机用户直读不了，此时退回 docker exec 取一致性快照。
DEFAULT_DB_CANDIDATES = [
    "/var/lib/docker/volumes/new-api_new-api-data/_data/one-api.db",
]
DEFAULT_DB_CONTAINER = "new-api"
DEFAULT_DB_CONTAINER_PATH = "/data/one-api.db"


class ChannelKeyError(RuntimeError):
    """无法以只读方式取得渠道明文 Key —— 备份与回滚的前提条件缺失"""
    pass


class ChannelMismatch(ValueError):
    """渠道不属于目标站点 —— 会把 A 站的 Key 写进 B 站的渠道"""
    pass


class TestModelMissing(ValueError):
    """既没有渠道自身 test_model，也没有命令行指定 —— 拒绝在没有可靠测试模型时动手"""
    pass


class LocalChannelSyncer:
    """与本地 New API (http://127.0.0.1:3001) 的安全渠道同步器"""
    def __init__(self, core_cfg: dict):
        self.local_cfg = core_cfg.get("local", {})
        self.base_url = self.local_cfg.get("base", "http://127.0.0.1:3001").rstrip("/")
        self.access_token = self.local_cfg.get("access_token", "")
        self.user_id = self.local_cfg.get("user_id", 1)
        
        self.client = HttpClient(
            self.base_url,
            auth={"access_token": self.access_token, "user_id": self.user_id}
        )

    def list_channels(self) -> list:
        """循环翻页获取本地全部渠道列表，兼容 p 从 0 或 1 开始"""
        all_channels = []
        page_size = 50
        
        for start_p in [0, 1]:
            p = start_p
            cur_channels = []
            while True:
                r = self.client.get(f"/api/channel/?p={p}&size={page_size}")
                if r.status_code != 200:
                    break
                d = r.json()
                if not d.get("success"):
                    break
                data = d.get("data", [])
                if isinstance(data, dict):
                    data = data.get("data") or data.get("items") or []
                if not data:
                    break
                cur_channels.extend(data)
                if len(data) < page_size:
                    break
                p += 1
                if p > 20:
                    break
            if cur_channels:
                all_channels = cur_channels
                break
                
        return all_channels

    def get_channel(self, channel_id: int) -> dict:
        r = self.client.get(f"/api/channel/{channel_id}")
        if r.status_code == 404:
            raise ValueError(
                f"本地渠道 {channel_id} 不存在（HTTP 404）。"
                f"请核对 sites.yaml 里该站点的 local_channel 配置，本地现有渠道可用 relayctl site list 对照。"
            )
        if r.status_code != 200:
            raise Exception(f"读取本地渠道 {channel_id} 失败: HTTP {r.status_code} {r.text[:100]}")
        d = r.json()
        if not d.get("success"):
            msg = str(d.get("message") or "")
            if "not found" in msg.lower() or "不存在" in msg:
                raise ValueError(
                    f"本地渠道 {channel_id} 不存在。请核对 sites.yaml 里该站点的 local_channel 配置。"
                )
            raise Exception(f"读取本地渠道 {channel_id} 错误: {msg}")
        data = d.get("data") or {}
        if not data or not data.get("id"):
            raise ValueError(
                f"本地渠道 {channel_id} 不存在（接口返回空对象）。"
                f"请核对 sites.yaml 里该站点的 local_channel 配置。"
            )
        return data

    @staticmethod
    def _domain_of(url: str) -> str:
        """取 URL 的主机名（去掉 www. 前缀，统一小写）用于比对"""
        u = (url or "").strip().lower()
        if not u:
            return ""
        if "://" not in u:
            u = "https://" + u
        try:
            host = urlparse(u).hostname or ""
        except Exception:
            host = ""
        return host[4:] if host.startswith("www.") else host

    def update_channel(self, channel_data: dict) -> bool:
        r = self.client.put("/api/channel/", json=channel_data)
        if r.status_code != 200:
            raise Exception(f"更新本地渠道失败: HTTP {r.status_code} {r.text[:100]}")
        d = r.json()
        if not d.get("success"):
            raise Exception(f"更新本地渠道错误: {d.get('message')}")
        return True

    def _candidate_host_paths(self) -> list:
        paths = []
        cfg_path = self.local_cfg.get("db_path")
        if cfg_path:
            paths.append(os.path.expanduser(cfg_path))
        for p in DEFAULT_DB_CANDIDATES:
            if p not in paths:
                paths.append(p)
        return paths

    def _copy_out_db(self, container: str, container_path: str):
        """用隔离 helper 容器制作一致 SQLite online backup。"""
        tmpdir = tempfile.mkdtemp(prefix="relayctl-db-")
        base = os.path.basename(container_path)
        helper_name = f"relayctl-snapshot-{uuid.uuid4().hex}"
        local_db = os.path.join(tmpdir, base)
        try:
            fd = os.open(local_db, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            image = self.local_cfg.get("db_helper_image", "python@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4")
            script = ("import sqlite3,sys; s=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); "
                      "d=sqlite3.connect(':memory:'); s.backup(d); sys.stdout.buffer.write(d.serialize()); d.close(); s.close()")
            with open(local_db, "wb") as out:
                r = subprocess.run(["docker", "run", "--pull=never", "--rm", "--name", helper_name,
                                    "--network", "none", "--read-only", "--cap-drop", "ALL",
                                    "--security-opt", "no-new-privileges", "--volumes-from", f"{container}:ro",
                                    "--entrypoint", "python3", image, "-c", script, container_path],
                                   stdout=out, stderr=subprocess.PIPE, timeout=90)
            if r.returncode != 0 or os.path.getsize(local_db) == 0:
                raise RuntimeError("helper SQLite online backup 失败；已拒绝不一致复制")
            return tmpdir, local_db
        except Exception as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise RuntimeError(f"无法制作一致 SQLite 快照: {e}")
        finally:
            pending_error = sys.exc_info()[1]
            try:
                cleanup = subprocess.run(["docker", "rm", "-f", helper_name], capture_output=True,
                                          text=True, timeout=20, check=False)
                check = subprocess.run(["docker", "container", "ls", "-aq", "--filter",
                                        f"name=^/{helper_name}$"], capture_output=True,
                                       text=True, timeout=20, check=False)
                if check.returncode != 0 or (check.stdout or "").strip():
                    detail = ((check.stderr or check.stdout or cleanup.stderr or
                               "helper container still exists").strip()[:200])
                    raise RuntimeError(f"SQLite 快照 helper 清理未验证成功: {detail}")
            except Exception as e:
                shutil.rmtree(tmpdir, ignore_errors=True)
                detail = str(e)
                if pending_error is not None:
                    detail = f"原操作失败: {pending_error}；清理失败: {e}"
                raise RuntimeError(f"{detail}；已删除本地快照")

    def _query_key(self, db_file: str, channel_id: int, allow_rw_fallback: bool = False) -> str:
        """以只读 URI 打开 SQLite 读取渠道 Key"""
        try:
            conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=5)
        except sqlite3.OperationalError:
            if not allow_rw_fallback:
                raise
            # 仅在私有临时副本上退让：WAL 需要可写 -shm，副本本身不影响真实库
            conn = sqlite3.connect(db_file, timeout=5)
        try:
            row = conn.execute("SELECT key FROM channels WHERE id = ?", (channel_id,)).fetchone()
        finally:
            conn.close()
        return (row[0] or "").strip() if row else ""

    def read_channel_key(self, channel_id: int) -> str:
        """
        只读读取渠道明文 Key。
        读不到必须抛错让调用方拒绝同步 —— 否则备份里 old_key 为空，
        回滚时会用空 key 覆盖，新 key 留在渠道里却报「已回滚」。
        """
        errors = []

        for host_path in self._candidate_host_paths():
            if not (os.path.exists(host_path) and os.access(host_path, os.R_OK)):
                errors.append(f"{host_path}: 不存在或不可读")
                continue
            try:
                key = self._query_key(host_path, channel_id)
            except Exception as e:
                errors.append(f"{host_path}: {type(e).__name__} {e}")
                continue
            if key:
                return key
            errors.append(f"{host_path}: 渠道 {channel_id} 的 Key 为空")

        container = self.local_cfg.get("db_container", DEFAULT_DB_CONTAINER)
        container_path = self.local_cfg.get("db_container_path", DEFAULT_DB_CONTAINER_PATH)
        tmpdir = None
        if not container or not container_path:
            raise ChannelKeyError(" | ".join(errors[:4]))
        try:
            tmpdir, local_db = self._copy_out_db(container, container_path)
            key = self._query_key(local_db, channel_id, allow_rw_fallback=True)
            if key:
                return key
            errors.append(f"{container}:{container_path}: 渠道 {channel_id} 的 Key 为空")
        except Exception as e:
            errors.append(f"{container}:{container_path}: {e}")
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

        raise ChannelKeyError(" | ".join(errors[:4]))

    @staticmethod
    def is_multi_key(channel: dict, key: str) -> bool:
        """多 Key 轮询渠道判定：优先用 channel_info，其次看 key 是否含换行"""
        info = channel.get("channel_info")
        if isinstance(info, dict):
            if info.get("is_multi_key"):
                return True
            size = info.get("multi_key_size")
            if isinstance(size, int) and size > 1:
                return True
        return "\n" in (key or "")

    def test_channel(self, channel_id: int, model: str) -> dict:
        try:
            r = self.client.get(f"/api/channel/test/{channel_id}?model={model}", timeout=45)
            if r.status_code != 200:
                return {"ok": False, "msg": f"HTTP {r.status_code}"}
            d = r.json()
            return {"ok": d.get("success", False), "msg": d.get("message", ""), "time": d.get("time", 0)}
        except requests.exceptions.Timeout:
            return {"ok": False, "msg": "测试请求超时 (>45s)"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def sync_site_key(self, site_name: str, channel_id: int, new_key: str,
                      replace_all: bool = False, site_base: str = None,
                      test_model: str = None) -> dict:
        """
        核心安全同步流程：
        1. 严格检查新 Key（绝不接受打码 Key）
        2. 只读从 SQLite 取出旧 Key（取不到直接拒绝同步）
        3. 多 Key 渠道检查（channel_info.is_multi_key 或换行 Key 必须指定 replace_all）
        4. 校验渠道确实属于目标站点（域名比对），配错数字也不写错渠道
        5. 确定测试模型（--test-model > 渠道自身 test_model > 拒绝动手）
        6. 备份旧 Key 到 backup/（权限 600）
        7. PUT 安全保存，并回读确认新 Key 真的落库
        8. 调用测试接口验证
        9. 测试失败自动回滚，并回读比对确认真的恢复了
        """
        if not new_key or "*" in new_key:
            raise ValueError(f"拒绝同步打码 Key 至本地渠道: {new_key}")

        os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
        lock_path = os.path.join(BACKUP_DIR, f".channel-{channel_id}.lock")
        lock_fh = open(lock_path, "a+")
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            return self._sync_site_key_locked(site_name, channel_id, new_key, replace_all,
                                              site_base, test_model)
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            lock_fh.close()

    def _sync_site_key_locked(self, site_name, channel_id, new_key, replace_all=False,
                              site_base=None, test_model=None):

        old_channel = self.get_channel(channel_id)

        # 写入前先确认渠道属于这个站点：只错一个数字就会把 A 站的 Key 写进 B 站渠道
        if site_base:
            ch_domain = self._domain_of(old_channel.get("base_url"))
            site_domain = self._domain_of(site_base)
            if not ch_domain or not site_domain:
                raise ChannelMismatch(
                    f"无法比对域名：渠道 [{channel_id}] base_url={old_channel.get('base_url')!r}，"
                    f"站点 {site_name} base={site_base!r}，已拒绝同步。"
                )
            if ch_domain != site_domain:
                raise ChannelMismatch(
                    f"渠道 [{channel_id}]（{old_channel.get('name')}）的地址是 {ch_domain}，"
                    f"不属于站点 {site_name}（{site_domain}）。已拒绝同步，避免把 Key 写进别的站点渠道。"
                    f"请核对 sites.yaml 里 {site_name} 的 local_channel。"
                )

        # 旧 Key 只能从库里读：接口返回的 key 恒为空，拿它备份/回滚等于没有备份
        try:
            old_key = self.read_channel_key(channel_id)
        except ChannelKeyError as e:
            raise RuntimeError(
                f"拒绝同步：无法只读读取本地渠道 [{channel_id}] 的旧 Key，备份与回滚将失效（{e}）。"
                f"请在 sites.yaml 的 local.db_path 指定 New API 的 SQLite 库路径，"
                f"或用 local.db_container / local.db_container_path 指定容器与库位置。"
            )

        # 渠道本身是禁用状态时，测试通过也不代表流量会走它，提前说清楚
        if old_channel.get("status") == 2:
            print(f"[!] 注意：本地渠道 [{channel_id}] 当前为禁用状态 (status=2)，本次同步不会改变其启用状态。")

        # 多 Key 渠道安全检查（比测试模型更优先：整体覆盖多个 Key 的后果更严重）
        if self.is_multi_key(old_channel, old_key) and not replace_all:
            raise ValueError(
                f"本地渠道 [{channel_id}] 为多 Key 轮询渠道（channel_info.is_multi_key=true 或 Key 含多行），"
                f"默认拒绝整体替换！如确认整体替换，请附带 --replace-all 参数。"
            )

        # 测试模型优先级：命令行 --test-model > 渠道自身的 test_model。
        # 拿不准模型就测试，会把「上游没有这个模型」误判成「Key 不可用」，进而误回滚。
        chosen = (test_model or "").strip() or (old_channel.get("test_model") or "").strip()
        if not chosen:
            models_str = old_channel.get("models", "") or ""
            models = [m.strip() for m in models_str.split(",") if m.strip()]
            first = models[0] if models else "(该渠道未配置模型)"
            raise TestModelMissing(
                f"本地渠道 [{channel_id}] 未设置 test_model，且未通过 --test-model 指定。"
                f"若拿渠道模型列表的第一个（{first}）去测，上游不提供该模型时会误判为 Key 不可用并触发回滚。"
                f"已拒绝同步。请用 --test-model <上游确实支持的模型> 显式指定，"
                f"或先在 New API 渠道配置里填好 test_model。"
            )
        test_model = chosen

        # 1. 备份旧 key
        timestamp = time.time_ns()
        backup_id = f"{timestamp}_{uuid.uuid4().hex[:8]}"
        backup_file = os.path.join(BACKUP_DIR, f"{site_name}_ch{channel_id}_{backup_id}.json")
        backup_payload = {
            "site": site_name,
            "channel_id": channel_id,
            "timestamp": timestamp,
            "old_key": old_key,
            "old_key_source": "sqlite(ro)",
            "test_model": test_model,
            "channel_snapshot": old_channel
        }
        secure_write_json(backup_file, backup_payload)

        # 2. 准备新数据（精简为安全可更新字段）
        allowed_keys = ["id", "name", "type", "base_url", "models", "group", "weight", "priority", "other", "model_mapping", "auto_ban"]
        updated_payload = {k: old_channel.get(k) for k in allowed_keys if k in old_channel}
        updated_payload["key"] = new_key

        print(f"[*] 正在更新本地渠道 [{channel_id}] Key，备份已存至: {backup_file}")
        try:
            self.update_channel(updated_payload)
        except Exception as e:
            return self._rollback_result(old_channel, old_key, channel_id, backup_file,
                                         f"更新请求异常/状态不确定: {e}", allowed_keys)

        # 3. 回读确认新 Key 已写入数据库
        try:
            applied = self.read_channel_key(channel_id)
        except ChannelKeyError:
            applied = None
        if applied != new_key:
            print("[严重错误] 更新请求已发出，但回读到的渠道 Key 与新 Key 不一致，请立即人工核对！")
            return self._rollback_result(old_channel, old_key, channel_id, backup_file,
                                         "回读校验失败：渠道 Key 未更新为预期值", allowed_keys)

        # 4. 验证测试
        print(f"[*] 正在验证渠道连通性 (测试模型: {test_model}，超时限制 45s)...")
        test_res = self.test_channel(channel_id, test_model)
        
        if test_res.get("ok"):
            print(f"[✓] 渠道测试通过！响应耗时: {test_res.get('time', 0):.2f}s")
            return {
                "ok": True,
                "backup_file": backup_file,
                "tested_model": test_model,
                "response_time": test_res.get("time")
            }
        else:
            print(f"[!] 渠道测试未通过: {test_res.get('msg')}，正在自动回滚原 Key...")
            return self._rollback_result(old_channel, old_key, channel_id, backup_file,
                                         test_res.get('msg'), allowed_keys)

    def _rollback_result(self, old_channel, old_key, channel_id, backup_file, reason, allowed_keys):
        rollback_payload = {k: old_channel.get(k) for k in allowed_keys if k in old_channel}
        rollback_payload["key"] = old_key
        try:
            self.update_channel(rollback_payload)
        except Exception as e:
            print(f"[严重错误] 回滚请求失败: {e}")
            return {"ok": False, "rolled_back": False, "verified": False,
                    "error": f"{reason}；回滚请求失败: {e}", "backup_file": backup_file}
        try:
            verified = self.read_channel_key(channel_id) == old_key
        except Exception as e:
            verified = False
            print(f"[严重错误] 回滚后回读失败: {e}")

        if verified:
            print("[✓] 已成功回滚至原 Key！（回读数据库内容一致，二次校验通过）")
        else:
            print("[严重错误] 回滚请求已发送，但回读到的 Key 与旧 Key 不一致！渠道当前处于未知状态，请立即人工检查！")

        return {
            "ok": False,
            "rolled_back": verified,
            "verified": verified,
            "error": reason,
            "backup_file": backup_file
        }
