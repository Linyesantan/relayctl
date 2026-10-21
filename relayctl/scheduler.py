"""后台刷新调度器（`relayctl daemon` 与 Web 内置调度共用）。

设计要点：
- 刷新间隔支持 s/m/h/d：全局 config.yaml 的 refresh_interval（默认 30m），
  站点级 refresh_interval 覆盖全局；`0` 表示该站只走手动刷新（调度跳过）；
- 每站被调度的分段固定为 probe / balance / groups / keys，
  能力不支持（NotSupported，退出码 3）只记录不重试，按正常间隔来下一轮；
- 每站同一时刻最多 1 个刷新任务；全局并发 worker 数来自 scheduler.max_workers（默认 4）；
- 成功后 下一次 = interval ± jitter（默认 ±10%）；
  失败后 指数退避（默认 60s ×2，上限 30m）再加抖动；
  鉴权失败退避至少为 interval（不暴力重试）；429 尊重 Retry-After；
- 调度状态仅内存；日志落 $RELAYCTL_HOME/logs/daemon.log（0600，脱敏，不含凭据）；
- 绝不请求 /api/pricing（Groups 能力由适配器的 groups 接口决定）。
"""

import datetime
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import cache as rcache
from . import paths
from .adapters import get_adapter
from .errors import AuthError, ConfigError, NotSupported

# 每站被调度的分段 -> 适配器方法
SECTIONS: Tuple[str, ...] = ("probe", "balance", "groups", "keys")
_SECTION_METHODS = {"probe": "probe", "balance": "balance",
                    "groups": "groups", "keys": "list_keys"}

# 失败分类
KIND_AUTH = "auth"
KIND_UNSUPPORTED = "unsupported"
KIND_RATE_LIMIT = "rate_limited"
KIND_ERROR = "error"

DEFAULT_INTERVAL = "30m"
_INTERVAL_RE = re.compile(r"^(\d+)\s*([smhd]?)$")
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}

# 退避默认值（可被 scheduler.* 配置覆盖）
_DEFAULTS = {
    "max_workers": 4,
    "jitter": 0.1,
    "backoff_initial": 60,
    "backoff_factor": 2,
    "backoff_max": 1800,
}

LOG_NAME = "daemon.log"


# ---------------------------------------------------------------------------
# 间隔解析
# ---------------------------------------------------------------------------
def parse_interval(value: Any, default: float = 1800.0) -> float:
    """解析 s/m/h/d 间隔为秒。

    "30m" -> 1800.0；"90s" -> 90.0；"2h" -> 7200.0；"1d" -> 86400.0；
    "0" / "0s" -> 0.0（表示只手动刷新）；空值 -> default。非法值抛 ConfigError。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        raise ConfigError(f"refresh_interval 非法: {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
        return max(0.0, seconds)
    text = str(value).strip()
    if not text:
        return default
    match = _INTERVAL_RE.match(text)
    if not match:
        raise ConfigError(f"refresh_interval 非法: {value!r}（如 30s / 30m / 2h / 1d / 0）")
    amount = int(match.group(1))
    return float(amount * _UNIT_SECONDS[match.group(2)])


def site_interval(site_cfg: Dict[str, Any], core_cfg: Optional[Dict[str, Any]] = None) -> float:
    """站点生效间隔（秒）：站点 refresh_interval 覆盖全局 refresh_interval。"""
    cfg = dict(site_cfg or {})
    core = dict(core_cfg or {})
    raw = cfg.get("refresh_interval")
    if raw is None or str(raw).strip() == "":
        raw = core.get("refresh_interval")
    return parse_interval(raw, default=parse_interval(DEFAULT_INTERVAL))


def _status_code(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def retry_after_seconds(exc: BaseException) -> Optional[float]:
    """从异常携带的响应里读取 Retry-After（秒 / HTTP 日期）。"""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:  # noqa: BLE001 - 非映射型 headers
        return None
    if raw is None:
        return None
    raw = str(raw).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except Exception:  # noqa: BLE001 - 解析失败即视为不可得
        return None
    if when is None:
        return None
    delta = when.timestamp() - time.time()
    return max(0.0, delta)


_RATE_LIMIT_TEXT = ("429", "too many requests", "rate limit", "rate-limited")


def probe_failure(data: Any) -> Optional[str]:
    """probe 自身不抛异常，这里把「两头都探不到」的结果判定为失败。

    status_ok（/api/status）与 auth_ok（/api/user/self）只要有一个成立就视为可用。
    """
    if not isinstance(data, dict):
        return "探测返回了非预期数据"
    if data.get("status_ok") or data.get("auth_ok"):
        return None
    detail = str(data.get("status_error") or data.get("auth_msg")
                 or data.get("auth_error") or "").strip()
    if data.get("auth_status"):
        detail = f"HTTP {data['auth_status']} {detail}".strip()
    return detail or "探测未完成：既拿不到面板状态也拿不到鉴权结论"


def classify_error(exc: BaseException) -> str:
    """判断失败类型，决定退避策略。"""
    if isinstance(exc, AuthError):
        return KIND_AUTH
    if isinstance(exc, NotSupported):
        return KIND_UNSUPPORTED
    if _status_code(exc) == 429:
        return KIND_RATE_LIMIT
    text = str(exc or "").lower()
    if any(marker in text for marker in _RATE_LIMIT_TEXT):
        return KIND_RATE_LIMIT
    return KIND_ERROR


# ---------------------------------------------------------------------------
# 日志：$RELAYCTL_HOME/logs/daemon.log（0600，脱敏）
# ---------------------------------------------------------------------------
class DaemonLogger:
    """极简线程安全日志：只写本地文件，绝不含凭据。"""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(paths.logs_dir(), LOG_NAME)
        self._lock = threading.Lock()
        self._ensured = False

    def _ensure(self):
        if self._ensured:
            return
        paths.ensure_dir(os.path.dirname(self.path), paths.DIR_MODE)
        if os.path.exists(self.path):
            paths.ensure_file_mode(self.path, paths.FILE_MODE)
        self._ensured = True

    def emit(self, message: str, level: str = "INFO", clock: Optional[Callable[[], float]] = None):
        now = (clock or time.time)()
        stamp = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} [{level}] {rcache.redact_text(message)}\n"
        with self._lock:
            try:
                self._ensure()
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, paths.FILE_MODE)
                try:
                    os.write(fd, line.encode("utf-8"))
                finally:
                    os.close(fd)
                paths.ensure_file_mode(self.path, paths.FILE_MODE)
            except OSError:  # pragma: no cover - 日志失败绝不影响主流程
                pass
        return line


# ---------------------------------------------------------------------------
# 每站调度状态（仅内存）
# ---------------------------------------------------------------------------
class SiteState:
    def __init__(self, name: str, interval: float):
        self.name = name
        self.interval = interval
        self.next_refresh_at: Optional[float] = None   # 时钟域同 Scheduler.clock
        self.failures = 0
        self.runs = 0
        self.last_success_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.last_error_kind: Optional[str] = None
        self.last_retry_after: Optional[float] = None

    def as_dict(self, now: Optional[float] = None,
                to_wall: Optional[Callable[[float], float]] = None) -> Dict[str, Any]:
        remaining = None
        if self.next_refresh_at is not None and now is not None:
            remaining = max(0.0, self.next_refresh_at - now)
        wall = None
        if self.next_refresh_at is not None and to_wall:
            wall = to_wall(self.next_refresh_at)
        return {
            "interval": self.interval,
            "next_refresh_at": wall,
            "next_refresh_at_local": datetime.datetime.fromtimestamp(wall).strftime(
                "%Y-%m-%d %H:%M:%S") if wall is not None else None,
            "next_refresh_in": remaining,
            "failures": self.failures,
            "last_error": self.last_error,
            "last_error_kind": self.last_error_kind,
        }


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------
class Scheduler:
    """按站点刷新缓存的后台调度器（可为守护线程，不会阻止进程退出）。"""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None,
                 sections: Tuple[str, ...] = SECTIONS,
                 clock: Optional[Callable[[], float]] = None,
                 rand: Optional[Callable[[], float]] = None,
                 logger: Optional[DaemonLogger] = None,
                 log: bool = True,
                 wall_clock: Optional[Callable[[], float]] = None,
                 sleep: Optional[Callable[[float], None]] = None):
        self.cfg = dict(cfg or {})
        self.sections = tuple(sections) or SECTIONS
        self.clock = clock or time.monotonic
        self.wall_clock = wall_clock or time.time
        self.rand = rand or random.random
        self._sleep = sleep or time.sleep
        self.logger = logger or (DaemonLogger() if log else None)
        self._configure_workers()

        self._lock = threading.RLock()
        self._states: Dict[str, SiteState] = {}
        self._busy: set = set()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # -- 配置 -------------------------------------------------------------
    def _conf(self, name: str) -> Any:
        raw = (self.cfg.get("scheduler") or {}).get(name, _DEFAULTS[name])
        return _DEFAULTS[name] if raw is None else raw

    def _configure_workers(self):
        self.max_workers = max(1, int(self._conf("max_workers") or 1))
        try:
            self.jitter = abs(float(self._conf("jitter")))
        except (TypeError, ValueError):
            self.jitter = _DEFAULTS["jitter"]
        self.jitter = min(self.jitter, 0.5)
        self.backoff_initial = self._positive_float("backoff_initial")
        self.backoff_factor = max(1.0, self._positive_float("backoff_factor"))
        self.backoff_max = self._positive_float("backoff_max")

    def _positive_float(self, name: str) -> float:
        try:
            value = float(self._conf(name))
        except (TypeError, ValueError):
            value = float(_DEFAULTS[name])
        return value if value > 0 else float(_DEFAULTS[name])

    # -- 站点筛选 ---------------------------------------------------------
    def _sites(self) -> Dict[str, Dict[str, Any]]:
        sites = self.cfg.get("sites") or {}
        return {str(k): v for k, v in sites.items() if isinstance(v, dict)}

    def plan(self) -> List[Dict[str, Any]]:
        """返回每个站点的计划信息（含跳过原因）。"""
        plan = []
        for name, s_cfg in sorted(self._sites().items()):
            status = str(s_cfg.get("status") or s_cfg.get("tier") or "").lower()
            if status == "parked" or str(s_cfg.get("tier", "")).lower() == "parked":
                plan.append({"site": name, "eligible": False, "interval": 0.0,
                             "reason": "站点已搁置（parked）"})
                continue
            try:
                interval = site_interval(s_cfg, self.cfg)
            except ConfigError as exc:
                plan.append({"site": name, "eligible": False, "interval": 0.0,
                             "reason": rcache.sanitize_error(exc)})
                continue
            if interval <= 0:
                plan.append({"site": name, "eligible": False, "interval": 0.0,
                             "reason": "refresh_interval=0（只手动刷新）"})
                continue
            plan.append({"site": name, "eligible": True, "interval": interval, "reason": None})
        return plan

    def _state(self, name: str, interval: float) -> SiteState:
        state = self._states.get(name)
        if state is None:
            state = SiteState(name, interval)
            self._states[name] = state
        state.interval = interval
        return state

    @staticmethod
    def _is_due(state: SiteState, now: float) -> bool:
        return state.next_refresh_at is None or now >= state.next_refresh_at

    # -- 抖动 / 退避 ------------------------------------------------------
    def jittered(self, base: float) -> float:
        if base <= 0:
            return 0.0
        if self.jitter <= 0:
            return base
        factor = 1.0 + (float(self.rand()) * 2.0 - 1.0) * self.jitter
        return max(0.0, base * factor)

    def backoff_delay(self, failures: int) -> float:
        power = max(0, int(failures) - 1)
        base = self.backoff_initial * (self.backoff_factor ** power)
        return min(base, self.backoff_max)

    def next_delay(self, state: SiteState, kinds: Optional[set] = None,
                   retry_after: Optional[float] = None) -> float:
        """成功后用 interval，失败后按分类退避，最后统一加抖动。"""
        kinds = kinds or set()
        interval = state.interval if state.interval > 0 else self.backoff_max
        if state.failures <= 0:
            return self.jittered(interval)

        delay = self.backoff_delay(state.failures)
        if not kinds or kinds == {KIND_UNSUPPORTED}:
            # 能力不支持不做退避式高频重试，沿用正常间隔
            delay = max(delay, interval)
        if KIND_AUTH in kinds:
            # 鉴权失败退避至少为一个完整 interval，绝不暴力重试
            delay = max(delay, interval)
        if KIND_RATE_LIMIT in kinds and retry_after:
            delay = max(delay, retry_after)
        return self.jittered(delay)

    # -- 单站刷新 ---------------------------------------------------------
    def refresh_section(self, name: str, s_cfg: Dict[str, Any], adapter: Any,
                        section: str) -> Dict[str, Any]:
        """强刷一个分段：成功写缓存（清 stale），失败保留旧缓存并标记 stale。"""
        method_name = _SECTION_METHODS.get(section, section)
        fn = getattr(adapter, method_name, None)
        if fn is None or not callable(fn):
            return {"ok": False, "from_cache": False, "fetched_at": None, "stale": False,
                    "error": f"适配器不支持 {section}", "kind": KIND_UNSUPPORTED}
        try:
            data = fn()
        except Exception as exc:  # noqa: BLE001 - 单分段失败不牵连其它分段
            stale_ent = rcache.mark_stale(name, section, exc)
            return {"ok": False, "from_cache": stale_ent is not None,
                    "fetched_at": stale_ent.get("fetched_at") if stale_ent else None,
                    "stale": stale_ent is not None,
                    "error": rcache.sanitize_error(exc), "kind": classify_error(exc),
                    "retry_after": retry_after_seconds(exc)}
        ent = rcache.write(name, section, data)
        if section == "probe":
            failure = probe_failure(data)
            if failure:
                rcache.mark_stale(name, section, RuntimeError(failure))
                text = f"{failure}".lower()
                kind = KIND_AUTH if ("401" in text or "unauthorized" in text) else KIND_ERROR
                return {"ok": False, "from_cache": False, "fetched_at": ent.get("fetched_at"),
                        "stale": True, "error": rcache.redact_text(failure), "kind": kind,
                        "retry_after": None}
        return {"ok": True, "from_cache": False, "fetched_at": ent.get("fetched_at"),
                "stale": False, "error": None, "kind": None, "retry_after": None}

    def refresh_site(self, name: str, s_cfg: Dict[str, Any]) -> Dict[str, Any]:
        """刷新单个站点的全部分段（串行，天然满足「每站同时只有 1 个任务」）。"""
        sections: Dict[str, Any] = {}
        errors: List[str] = []
        kinds: set = set()
        retry_after_values = [0.0]
        adapter = None
        try:
            adapter = get_adapter(name, s_cfg, self.cfg)
        except Exception as exc:  # noqa: BLE001 - 适配器不可用即整站失败
            errors.append(rcache.sanitize_error(exc))
            kinds.add(classify_error(exc))

        if adapter is not None:
            for section in self.sections:
                report = self.refresh_section(name, s_cfg, adapter, section)
                sections[section] = {
                    "ok": bool(report["ok"]),
                    "from_cache": bool(report["from_cache"]),
                    "fetched_at": report["fetched_at"],
                    "stale": bool(report["stale"]),
                    "error": report["error"],
                }
                if not report["ok"]:
                    errors.append(f"{section}: {report['error']}")
                    kinds.add(report["kind"] or KIND_ERROR)
                    if report.get("retry_after"):
                        retry_after_values.append(float(report["retry_after"]))

        retry_after = max(retry_after_values) or None
        return {"sections": sections, "success": not errors, "errors": errors,
                "kinds": kinds, "retry_after": retry_after}

    # -- 一轮调度 ---------------------------------------------------------
    def run_once(self, now: Optional[float] = None) -> Dict[str, Any]:
        """刷新所有「已到期」站点；返回结构化报告。"""
        now = self.clock() if now is None else now
        plan = self.plan()
        eligible = {item["site"]: item for item in plan if item["eligible"]}

        with self._lock:
            # 计划 + busy 标记必须在同一把锁内完成，否则并发调用会把同一站提交两次
            due: List[Tuple[str, Dict[str, Any], SiteState]] = []
            skipped: Dict[str, Dict[str, Any]] = {}
            for name, s_cfg in self._sites().items():
                item = eligible.get(name)
                state = self._state(name, item["interval"] if item else 0.0)
                if item is None:
                    continue
                if name in self._busy:
                    skipped[name] = {"site": name, "eligible": True, "refreshed": False,
                                     "busy": True, "sections": {}, "success": True,
                                     "errors": [], "skipped_reason": "该站正在刷新中"}
                    continue
                if not self._is_due(state, now):
                    continue
                self._busy.add(name)
                due.append((name, s_cfg, state))

        reports: Dict[str, Dict[str, Any]] = {}
        if due:
            workers = max(1, min(self.max_workers, len(due)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {name: pool.submit(self.refresh_site, name, s_cfg)
                           for name, s_cfg, _state in due}
                for name, future in futures.items():
                    try:
                        reports[name] = future.result()
                    except Exception as exc:  # noqa: BLE001 - 兜底，避免整个 daemon 挂掉
                        reports[name] = {"sections": {}, "success": False,
                                         "errors": [rcache.sanitize_error(exc)],
                                         "kinds": {KIND_ERROR}, "retry_after": None}
                with self._lock:
                    for name in futures:
                        self._busy.discard(name)

        sites_report: Dict[str, Dict[str, Any]] = {}
        refreshed = failed = 0
        end = self.clock()
        wall_base = self.wall_clock()
        to_wall = lambda at: wall_base + (at - end)  # noqa: E731 - 时钟域换算
        for name, item in ((i["site"], i) for i in plan):
            if name in skipped:
                state = self._state(name, item["interval"] if item["eligible"] else 0.0)
                entry = dict(skipped[name])
                entry.update(state.as_dict(end, to_wall))
                sites_report[name] = entry
                continue
            if not item["eligible"]:
                sites_report[name] = {
                    "site": name, "eligible": False, "refreshed": False, "busy": False,
                    "sections": {}, "success": True, "errors": [],
                    "skipped_reason": item["reason"],
                    "next_refresh_at": None, "next_refresh_at_local": None,
                }
                continue

            state = self._state(name, item["interval"])
            report = reports.get(name)
            if report is None:
                sites_report[name] = {
                    "site": name, "eligible": True, "refreshed": False, "busy": False,
                    "sections": {}, "success": True, "errors": [],
                    "skipped_reason": "未到刷新时间",
                }
                sites_report[name].update(state.as_dict(end, to_wall))
                continue

            refreshed += 1
            if report["success"]:
                state.failures = 0
                state.last_error = None
                state.last_error_kind = None
                state.last_retry_after = None
                state.last_success_at = end
            else:
                failed += 1
                state.failures += 1
                state.last_error = (report["errors"] or [None])[0]
                state.last_error_kind = sorted(report["kinds"])[0] if report["kinds"] else KIND_ERROR
                state.last_retry_after = report["retry_after"]
            state.runs += 1
            state.next_refresh_at = end + self.next_delay(
                state, report["kinds"], report["retry_after"])

            entry = {
                "site": name, "eligible": True, "refreshed": True, "busy": False,
                "sections": report["sections"], "success": bool(report["success"]),
                "errors": list(report["errors"]), "skipped_reason": None,
            }
            entry.update(state.as_dict(end, to_wall))
            sites_report[name] = entry
            self._log_site(name, entry)

        summary = {
            "total": len(plan),
            "eligible": len(eligible),
            "refreshed": refreshed,
            "failed": failed,
            "skipped": len(plan) - len(eligible) + len(skipped),
        }
        return {"generated_at_ts": self.wall_clock(),
                "generated_at": datetime.datetime.fromtimestamp(
                    self.wall_clock()).strftime("%Y-%m-%d %H:%M:%S"),
                "sites": sites_report, "summary": summary}

    def status(self, now: Optional[float] = None) -> Dict[str, Any]:
        """只报历史状态（不刷新）：每站 sections/success/errors/next_refresh_at。"""
        now = self.clock() if now is None else now
        wall_base = self.wall_clock()
        to_wall = lambda at: wall_base + (at - now)  # noqa: E731 - 时钟域换算
        sites: Dict[str, Dict[str, Any]] = {}
        for item in self.plan():
            name = item["site"]
            interval = item["interval"] if item["eligible"] else 0.0
            state = self._state(name, interval)
            entry = {
                "site": name,
                "eligible": item["eligible"],
                "busy": name in self._busy,
                "success": state.last_error is None,
                "errors": [state.last_error] if state.last_error else [],
                "sections": {},
                "refreshed": False,
                "skipped_reason": item["reason"],
            }
            entry.update(state.as_dict(now, to_wall))
            if name in self._states and self._states[name].next_refresh_at is None:
                entry["due"] = True
            sites[name] = entry
        return {"generated_at_ts": self.wall_clock(), "sites": sites}

    def _log_site(self, name: str, entry: Dict[str, Any]):
        if not self.logger:
            return
        if entry["success"]:
            ok_sections = [s for s, r in entry["sections"].items() if r.get("ok")]
            self.logger.emit(f"站点 {name} 刷新成功（{len(ok_sections)}/{len(self.sections)} 段）",
                             clock=self.wall_clock)
        else:
            self.logger.emit(f"站点 {name} 刷新失败: {'; '.join(entry['errors'])}",
                             level="ERROR", clock=self.wall_clock)

    # -- 常驻运行 ---------------------------------------------------------
    def start(self) -> threading.Thread:
        """启动后台常驻线程（daemon thread，不会阻止进程退出）。"""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self._thread
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, name="relayctl-scheduler",
                                            daemon=True)
            self._thread.start()
            return self._thread

    def stop(self, timeout: float = 5.0) -> bool:
        """通知后台线程退出并 join；返回线程是否已停止。"""
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout)
        self._thread = None
        return not (thread is not None and thread.is_alive())

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def next_delay_seconds(self, now: Optional[float] = None) -> float:
        """距离下一次有站点到期还有多久（秒）。"""
        now = self.clock() if now is None else now
        with self._lock:
            deadlines = [s.next_refresh_at for s in self._states.values()
                         if s.next_refresh_at is not None]
        if not deadlines:
            return 0.0
        return max(0.0, min(deadlines) - now)

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - 常驻线程绝不因一轮异常退出
                if self.logger:
                    self.logger.emit(f"调度轮次异常: {rcache.sanitize_error(exc)}",
                                     level="ERROR", clock=self.wall_clock)
            delay = self.next_delay_seconds()
            # 1s 下限避免忙等；300s 上限保证能及时感知配置/到期变化
            if self._stop_event.wait(min(max(delay, 1.0), 300.0)):
                break

    def serve_forever(self, poll_interval: float = 1.0) -> int:
        """常驻入口：可被 Ctrl+C / SIGTERM（systemd stop）正常中断。"""
        from .errors import EXIT_OK

        previous = None
        installed = False
        try:  # 只有主线程才允许改信号处理器（Web 内置场景下静默跳过）
            import signal
            previous = signal.signal(signal.SIGTERM, lambda *_: self._stop_event.set())
            installed = True
        except Exception:  # noqa: BLE001 - 嵌在非主线程里时不支持信号处理
            pass

        self.start()
        if self.logger:
            self.logger.emit("后台刷新调度已启动", clock=self.wall_clock)
        try:
            while not self._stop_event.is_set():
                self._sleep(poll_interval)
        except KeyboardInterrupt:
            pass
        finally:
            if self.logger:
                self.logger.emit("后台刷新调度已停止", clock=self.wall_clock)
            self.stop()
            if installed:
                try:
                    import signal
                    signal.signal(signal.SIGTERM, previous)
                except Exception:  # noqa: BLE001 - 恢复失败无所谓
                    pass
        return EXIT_OK


# ---------------------------------------------------------------------------
# 人类可读输出
# ---------------------------------------------------------------------------
def daemon_report_lines(report: Dict[str, Any]) -> List[str]:
    summary = report.get("summary") or {}
    ok_num = summary.get("refreshed", 0) - summary.get("failed", 0)
    lines = [
        f"[*] relayctl daemon 刷新完成: 成功 {ok_num} 站，"
        f"失败 {summary.get('failed', 0)} 站，跳过 {summary.get('skipped', 0)} 站"
        f"（纳管 {summary.get('total', 0)} 站）",
    ]
    for name, entry in (report.get("sites") or {}).items():
        if entry.get("busy"):
            lines.append(f"  - {name}: 上一轮仍在刷新中，本次跳过")
            continue
        if not entry.get("refreshed"):
            lines.append(f"  - {name}: 跳过（{entry.get('skipped_reason') or '未到刷新时间'}）")
            continue
        ok_sections = [s for s, r in (entry.get("sections") or {}).items() if r.get("ok")]
        if entry.get("success"):
            lines.append(
                f"  - {name}: 成功（{len(ok_sections)}/{len(entry.get('sections') or {})} 段）"
                f"下次刷新 {entry.get('next_refresh_at_local')}")
        else:
            detail = "; ".join(str(e) for e in (entry.get("errors") or [])[:2])
            lines.append(
                f"  - {name}: 失败（{detail}）下次重试 {entry.get('next_refresh_at_local')}"
                f"（连续失败 {entry.get('failures')} 次）")
    return lines


def build_scheduler(cfg: Optional[Dict[str, Any]] = None, **kwargs) -> Scheduler:
    """工厂函数（便于测试注入 / Web 复用）。"""
    from .config import load_config
    return Scheduler(cfg if cfg is not None else load_config(), **kwargs)
