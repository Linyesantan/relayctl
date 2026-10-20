"""数据规范化与脱敏。

- normalize(): 稳定 key 的字典规范化（递归、key 转字符串、按 key 排序）
- sanitize():  递归剔除明文凭据与下划线开头的内部字段
- normalize_key(): Key 列表的稳定输出字段（绝不含 _raw_key）
"""

from typing import Any, Dict, List, Optional

# 绝不落盘 / 绝不出网的字段（明文 key / 凭据）。key_masked 保留。
SECRET_FIELDS = frozenset({
    "key", "access_token", "token", "cookie", "password", "secret",
    "api_key", "apikey", "authorization", "raw_key",
})

# list_keys 的稳定输出字段（顺序固定）
KEY_FIELDS = (
    "id", "name", "group", "enabled", "status",
    "unlimited", "remain", "used", "key_masked", "models",
)


def mask_key(k: Any) -> str:
    """脱敏 Key：仅显示前 4 后 4，其余用 * 遮盖。"""
    if not k:
        return ""
    s = str(k)
    if len(s) <= 10:
        return "***"
    return f"{s[:4]}...{s[-4:]}"


def normalize(obj: Any) -> Any:
    """递归规范化：key 统一为字符串，字典按 key 排序，保证输出稳定可比较。"""
    if isinstance(obj, dict):
        return {str(k): normalize(obj[k]) for k in sorted(obj, key=lambda x: str(x))}
    if isinstance(obj, (list, tuple)):
        return [normalize(x) for x in obj]
    return obj


def sanitize(obj: Any) -> Any:
    """递归去掉明文凭据字段与下划线开头的内部字段。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            ks = str(k)
            if ks.startswith("_") or ks.lower() in SECRET_FIELDS:
                continue
            out[k] = sanitize(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [sanitize(x) for x in obj]
    return obj


def strip_internal(obj: Any) -> Any:
    """只剔除下划线开头的内部字段（保留 auth 等合法字段，用于落盘前的清理）。"""
    if isinstance(obj, dict):
        return {k: strip_internal(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, (list, tuple)):
        return [strip_internal(x) for x in obj]
    return obj


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on", "启用", "enabled", "正常"):
            return True
        if lowered in ("0", "2", "false", "no", "off", "禁用", "disabled", ""):
            return False
    return default


def _models_value(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [m.strip() for m in value.split(",") if m.strip()]
    if isinstance(value, (list, tuple)):
        return [str(m) for m in value if str(m)]
    return [str(value)]


def normalize_key(entry: Any) -> Optional[Dict[str, Any]]:
    """把上游原始 Key 条目规范化成稳定字段（永不携带 _raw_key 等内部/明文字段）。"""
    if not isinstance(entry, dict):
        return None

    raw_status = entry.get("status")
    if "enabled" in entry:
        enabled = _as_bool(entry.get("enabled"))
    else:
        enabled = _as_bool(raw_status, default=True) if raw_status not in (2, "2") else False

    unlimited = _as_bool(entry.get("unlimited", entry.get("unlimited_quota")))

    remain = entry.get("remain")
    if remain is None:
        remain = entry.get("remain_quota")
    if remain is None:
        remain = "无限" if unlimited else 0

    used = entry.get("used")
    if used is None:
        used = entry.get("used_quota", 0)

    return {
        "id": entry.get("id"),
        "name": entry.get("name", ""),
        "group": entry.get("group") or "default",
        "enabled": enabled,
        "status": 1 if enabled else 2,
        "unlimited": unlimited,
        "remain": remain,
        "used": used,
        "key_masked": entry.get("key_masked") or mask_key(entry.get("key", "")),
        "models": _models_value(entry.get("models") or entry.get("model_limits")),
    }


def normalize_keys(items: Any) -> List[Dict[str, Any]]:
    """批量规范化 Key 列表，丢掉无法识别的条目。"""
    if not isinstance(items, (list, tuple)):
        return []
    out = []
    for it in items:
        norm = normalize_key(it)
        if norm is not None:
            out.append(norm)
    return out
