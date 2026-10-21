"""中转站信息本地缓存（只存脱敏数据，600 权限）。

- 每个站点一份: $RELAYCTL_HOME/cache/<site>.json（v2 布局，路径动态解析）
- 分段: probe / balance / groups / keys，各自记录拉取时间
- 任何一次成功的实时拉取都会自动写回缓存；失败不覆盖 data，只标记 stale
- 读操作优先返回缓存（并展示最新更新时间），--refresh 才是强制实时拉取
- 实时拉取失败时：有旧缓存就返回旧 data（保留 fetched_at，标记 stale/last_error），
  没有旧缓存才抛错（cache.serve_stale=false 时严格抛错）
- Key 相关操作（keys / key add/set/rm）会由 CLI 强制实时拉取或失效

meta 契约（fetch / read 返回的字典，五个键恒定存在）:
    fetched_at: 最近一次成功拉取的时间字符串（"%Y-%m-%d %H:%M:%S"）
    ts:         fetched_at 对应的 epoch 秒
    from_cache: 本次数据是否来自缓存
    stale:      是否为「拉取失败后保留的旧数据」
    last_error: 最近一次拉取失败的脱敏错误信息（成功时 None）
"""

import datetime
import json
import os
import re
import time
from typing import Any, Callable, Dict, Optional, Tuple

from . import paths
from .core import secure_write_json, ensure_secure_dir
from .models import sanitize

# 绝不落盘的字段（明文 key / 凭据）。key_masked 保留。
_SECRET_FIELDS = {"key", "access_token", "token", "cookie", "password", "secret",
                  "api_key", "apikey", "authorization", "raw_key"}

MODE_AUTO = "auto"        # 有缓存就直接返回（是否到期由 scheduler 负责）
MODE_REFRESH = "refresh"  # 强制实时拉取并更新缓存
MODE_CACHED = "cached"    # 只读缓存，不发任何网络请求

META_KEYS = ("fetched_at", "ts", "from_cache", "stale", "last_error")

_MAX_ERROR_LEN = 300

# ---------------------------------------------------------------------------
# 错误信息脱敏：任何落盘 / 落日志的错误都要先过一遍
# ---------------------------------------------------------------------------
_REDACT_PATTERNS = (
    re.compile(r"(?i)\b(sk-[A-Za-z0-9._\-]{4,})"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{4,}"),
    re.compile(r"(?i)\b(token|access[_-]?token|api[_-]?key|apikey|password|passwd|secret|"
               r"authorization|cookie|session)\b(\s*[:=]\s*)([\"']?)([^\s,;'\"]+)(\3)"),
    # 既含字母又含数字、且长度 >= 24 的不透明串（用于兜底兜住 TOML/JSON 里回显的令牌）
    re.compile(r"\b(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{24,}\b"),
)

_REDACTED = "***"


def redact_text(text: Any) -> str:
    """把文本里疑似凭据的片段替换为 ***（用于 last_error 与日志）。"""
    out = str(text or "")
    if not out:
        return ""
    for pattern in _REDACT_PATTERNS:
        if pattern.groups >= 4:
            out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{_REDACTED}{m.group(5)}", out)
        else:
            out = pattern.sub(_REDACTED, out)
    return out


def sanitize_error(exc: BaseException) -> str:
    """异常 -> 脱敏后的一行字符串（绝不携带明文凭据）。"""
    text = f"{type(exc).__name__}: {exc}" if exc is not None else ""
    message = redact_text(text).replace("\n", " ").strip()
    if len(message) > _MAX_ERROR_LEN:
        message = message[:_MAX_ERROR_LEN] + "…"
    return message


def __getattr__(name):
    """CACHE_DIR 动态解析（测试可直接 mock.patch.object 覆盖）。"""
    if name == "CACHE_DIR":
        return _cache_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _cache_dir() -> str:
    override = globals().get("CACHE_DIR")
    return override if isinstance(override, str) else paths.cache_dir()


def _safe_name(site: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(site))


def cache_path(site: str) -> str:
    return os.path.join(_cache_dir(), f"{_safe_name(site)}.json")


def _load(site: str) -> dict:
    p = cache_path(site)
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("sections"), dict):
            return d
    except (FileNotFoundError, ValueError, OSError):
        pass
    return {"site": site, "sections": {}}


def _entry_meta(ent: dict, from_cache: bool) -> Dict[str, Any]:
    """把缓存条目转成对外 meta（五个键恒定存在）。"""
    return {
        "fetched_at": ent.get("fetched_at"),
        "ts": ent.get("ts"),
        "from_cache": bool(from_cache),
        "stale": bool(ent.get("stale", False)),
        "last_error": ent.get("last_error"),
    }


def read(site: str, section: str) -> Optional[dict]:
    """返回缓存条目 {data, ts, fetched_at, stale, last_error} 或 None。"""
    ent = _load(site)["sections"].get(section)
    if isinstance(ent, dict) and "ts" in ent:
        return ent
    return None


def write(site: str, section: str, data: Any) -> dict:
    """写入一个分段。read-modify-write 全程持锁，避免并发分段互相覆盖。

    一次成功写入即代表「数据新鲜」：清掉 stale / last_error。
    """
    path = cache_path(site)
    ensure_secure_dir(os.path.dirname(path))
    with paths.file_lock(path):
        d = _load(site)
        now = time.time()
        ent = {
            "ts": now,
            "fetched_at": datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
            "data": sanitize(data),
            "stale": False,
            "last_error": None,
        }
        d["site"] = site
        d["sections"][section] = ent
        secure_write_json(path, d)
    return ent


def mark_stale(site: str, section: str, error: Any) -> Optional[dict]:
    """标记某个分段为「拉取失败、保留旧值」。

    保留 data 与 fetched_at/ts，只补 stale=True 与脱敏后的 last_error。
    返回更新后的条目；缓存不存在则返回 None。
    """
    path = cache_path(site)
    ensure_secure_dir(os.path.dirname(path))
    with paths.file_lock(path):
        d = _load(site)
        ent = d["sections"].get(section)
        if not isinstance(ent, dict) or "ts" not in ent:
            return None
        ent["stale"] = True
        ent["last_error"] = sanitize_error(error) if isinstance(error, BaseException) \
            else redact_text(str(error))[:_MAX_ERROR_LEN]
        d["site"] = site
        secure_write_json(path, d)
    return ent


def invalidate(site: str, sections: Optional[list] = None):
    """sections=None 删除整个站点缓存；否则只删指定分段（也接受单个字符串）。"""
    if isinstance(sections, str):
        sections = [sections]
    p = cache_path(site)
    if not sections:
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
        return
    with paths.file_lock(p):
        d = _load(site)
        changed = False
        for s in sections:
            if d["sections"].pop(s, None) is not None:
                changed = True
        if changed:
            secure_write_json(p, d)


def clear_all() -> int:
    directory = _cache_dir()
    if not os.path.isdir(directory):
        return 0
    n = 0
    for fn in os.listdir(directory):
        if fn.endswith(".json"):
            try:
                os.unlink(os.path.join(directory, fn))
                n += 1
            except OSError:
                pass
    return n


def age_text(ts: float) -> str:
    sec = max(0, int(time.time() - ts))
    if sec < 60:
        return f"{sec}秒前"
    if sec < 3600:
        return f"{sec // 60}分钟前"
    if sec < 86400:
        return f"{sec // 3600}小时{(sec % 3600) // 60}分前"
    return f"{sec // 86400}天前"


class CacheMiss(Exception):
    """--cached 模式下没有可用缓存"""
    pass


def serve_stale_enabled() -> bool:
    """读取全局 cache.serve_stale 配置（默认 true，读不到配置也按 true 处理）。"""
    try:
        from .config import load_global
        value = (load_global().get("cache") or {}).get("serve_stale", True)
    except Exception:  # noqa: BLE001 - 配置不可读时按最安全的默认值处理
        return True
    return value if isinstance(value, bool) else str(value).strip().lower() not in ("0", "false", "no", "off")


def fetch(site: str, section: str, fn: Callable[[], Any],
          mode: str = MODE_AUTO, serve_stale: Optional[bool] = None) -> Tuple[Any, dict]:
    """统一取数入口。

    返回 (data, meta)，meta 恒含 fetched_at / ts / from_cache / stale / last_error。

    - MODE_AUTO:  有缓存直接返回；无缓存才拉取（缓存是否到期由 scheduler 负责）
    - MODE_CACHED:只读缓存，没有任何网络请求
    - MODE_REFRESH: 强制实时拉取

    实时拉取失败时：存在旧缓存且允许 serve_stale（默认跟随 cache.serve_stale）
    -> 返回旧 data（保留 fetched_at），落盘并回填 stale=True / last_error；
    否则异常原样上抛（且不写、不覆盖旧 data）。
    """
    allow_stale = serve_stale_enabled() if serve_stale is None else bool(serve_stale)
    ent = None if mode == MODE_REFRESH else read(site, section)

    if ent is not None:
        return ent.get("data"), _entry_meta(ent, True)
    if mode == MODE_CACHED:
        raise CacheMiss(f"站点 {site} 没有 {section} 缓存，去掉 --cached 拉取一次即可")

    try:
        data = fn()
    except Exception as exc:  # noqa: BLE001 - 统一决定「保旧还是抛错」
        stale_ent = mark_stale(site, section, exc) if allow_stale else None
        if stale_ent is None:
            raise
        return stale_ent.get("data"), _entry_meta(stale_ent, True)

    new_ent = write(site, section, data)
    return data, _entry_meta(new_ent, False)


def mode_from_args(args) -> str:
    if getattr(args, "refresh", False):
        return MODE_REFRESH
    if getattr(args, "cached", False):
        return MODE_CACHED
    return MODE_AUTO
