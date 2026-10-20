"""中转站信息本地缓存（只存脱敏数据，600 权限）。

- 每个站点一份: ~/.config/relayctl/state/cache/<site>.json
- 分段: balance / groups / keys / probe，各自记录拉取时间
- 任何一次成功的实时拉取都会自动写回缓存；失败不写，不覆盖旧缓存
- 缓存永久有效：有缓存就直接用，只在没有缓存或显式 --refresh 时才请求中转站
- Key 相关操作（keys / key add/set/rm）一律强制实时拉取并更新缓存
- 所有输出都标注该数据的最新更新时间
"""
import os
import re
import time
import json
import datetime
from typing import Any, Callable, Optional, Tuple

from .core import STATE_DIR, secure_write_json, ensure_secure_dir

CACHE_DIR = os.path.join(STATE_DIR, "cache")

# 绝不落盘的字段（明文 key / 凭据）。key_masked 保留。
_SECRET_FIELDS = {"key", "access_token", "token", "cookie", "password", "secret",
                  "api_key", "apikey", "authorization", "raw_key"}

MODE_AUTO = "auto"        # 有缓存就用（永久），没有才拉取并写缓存
MODE_REFRESH = "refresh"  # 强制实时拉取并更新缓存
MODE_CACHED = "cached"    # 只读缓存，不发任何网络请求


def _safe_name(site: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", site)


def cache_path(site: str) -> str:
    return os.path.join(CACHE_DIR, f"{_safe_name(site)}.json")


def sanitize(obj: Any) -> Any:
    """递归去掉明文 key / 凭据字段，以及以下划线开头的内部字段。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            ks = str(k)
            if ks.startswith("_") or ks.lower() in _SECRET_FIELDS:
                continue
            out[k] = sanitize(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [sanitize(x) for x in obj]
    return obj


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
    ensure_secure_dir(CACHE_DIR)
    d = _load(site)
    now = time.time()
    ent = {
        "ts": now,
        "fetched_at": datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        "data": sanitize(data),
    }
    d["site"] = site
    d["sections"][section] = ent
    secure_write_json(cache_path(site), d)
    return ent


def invalidate(site: str, sections: Optional[list] = None):
    """sections=None 删除整个站点缓存；否则只删指定分段。"""
    p = cache_path(site)
    if sections is None:
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
        return
    d = _load(site)
    changed = False
    for s in sections:
        if d["sections"].pop(s, None) is not None:
            changed = True
    if changed:
        secure_write_json(p, d)


def clear_all():
    if not os.path.isdir(CACHE_DIR):
        return 0
    n = 0
    for fn in os.listdir(CACHE_DIR):
        if fn.endswith(".json"):
            os.unlink(os.path.join(CACHE_DIR, fn))
            n += 1
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
