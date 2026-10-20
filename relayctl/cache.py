"""中转站信息本地缓存（只存脱敏数据，600 权限）。

- 每个站点一份: $RELAYCTL_HOME/cache/<site>.json（v2 布局，路径动态解析）
- 分段: balance / groups / keys / probe，各自记录拉取时间
- 任何一次成功的实时拉取都会自动写回缓存；失败不写，不覆盖旧缓存
- 缓存永久有效：有缓存就直接用，只在没有缓存或显式 --refresh 时才请求中转站
- Key 相关操作（keys / key add/set/rm）一律强制实时拉取并更新缓存
- 所有输出都标注该数据的最新更新时间
"""

import datetime
import json
import os
import re
import time
from typing import Any, Callable, Optional, Tuple

from . import paths
from .core import secure_write_json, ensure_secure_dir
from .models import sanitize

# 绝不落盘的字段（明文 key / 凭据）。key_masked 保留。
_SECRET_FIELDS = {"key", "access_token", "token", "cookie", "password", "secret",
                  "api_key", "apikey", "authorization", "raw_key"}

MODE_AUTO = "auto"        # 有缓存就用（永久），没有才拉取并写缓存
MODE_REFRESH = "refresh"  # 强制实时拉取并更新缓存
MODE_CACHED = "cached"    # 只读缓存，不发任何网络请求


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


def read(site: str, section: str) -> Optional[dict]:
    """返回 {data, ts, fetched_at} 或 None。"""
    ent = _load(site)["sections"].get(section)
    if isinstance(ent, dict) and "ts" in ent:
        return ent
    return None


def write(site: str, section: str, data: Any) -> dict:
    """写入一个分段。read-modify-write 全程持锁，避免并发分段互相覆盖。"""
    path = cache_path(site)
    ensure_secure_dir(os.path.dirname(path))
    with paths.file_lock(path):
        d = _load(site)
        now = time.time()
        ent = {
            "ts": now,
            "fetched_at": datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
            "data": sanitize(data),
        }
        d["site"] = site
        d["sections"][section] = ent
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


def fetch(site: str, section: str, fn: Callable[[], Any],
          mode: str = MODE_AUTO) -> Tuple[Any, dict]:
    """统一取数入口。缓存永久有效。

    返回 (data, meta)，meta = {fetched_at, ts, from_cache}。
    实时拉取抛出的异常原样上抛，且不写缓存、不覆盖旧缓存。
    """
    if mode != MODE_REFRESH:
        ent = read(site, section)
        if ent is not None:
            return ent.get("data"), {"fetched_at": ent.get("fetched_at"), "ts": ent["ts"],
                                     "from_cache": True}
        if mode == MODE_CACHED:
            raise CacheMiss(f"站点 {site} 没有 {section} 缓存，去掉 --cached 拉取一次即可")
    data = fn()
    ent = write(site, section, data)
    return data, {"fetched_at": ent["fetched_at"], "ts": ent["ts"], "from_cache": False}


def mode_from_args(args) -> str:
    if getattr(args, "refresh", False):
        return MODE_REFRESH
    if getattr(args, "cached", False):
        return MODE_CACHED
    return MODE_AUTO
