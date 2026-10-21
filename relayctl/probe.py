"""站点公开探测（不携带任何凭据、只请求轻量公开端点）。

用途：`relayctl probe-url <地址>` 与 `relayctl import sites` 的前置识别。

约束：
- 只允许 PUBLIC_ENDPOINTS 白名单内的路径（优先 /api/status）；
- 任何包含 pricing 的端点一律禁止（全量定价是重量级/敏感端点）；
- 请求不携带 Authorization / Cookie 等凭据头（auth 恒为空）；
- 识别不出面板就返回 panel=custom，绝不凭猜测伪造数据。

本模块的函数可独立导入复用（probe_url / identify_panel / normalize_base）。
"""

from typing import Any, Dict, List, Optional

from .core import HttpClient, normalize_base
from .errors import ConfigError

# 公开轻量端点白名单（按优先级排序）
PUBLIC_ENDPOINTS = ("/api/status", "/api/v1/status")

# 任何情况下都禁止请求的端点片段（重量级 / 敏感）
FORBIDDEN_MARKERS = ("pricing",)

# New API 独有的 /api/status 字段，用于 system_name 缺失时的辅助识别
_NEWAPI_HINT_KEYS = ("system_name", "systemname", "quota_per_unit", "quota_display_type")

PANEL_NEWAPI = "newapi"
PANEL_ONEAPI = "oneapi"
PANEL_CUSTOM = "custom"

# panel -> adapter 的默认映射（custom 按约定落到 newapi）
ADAPTER_BY_PANEL = {PANEL_NEWAPI: "newapi", PANEL_ONEAPI: "oneapi", PANEL_CUSTOM: "newapi"}


def ensure_public_endpoint(path: str) -> str:
    """校验端点路径：必须在白名单内，且不含 pricing 等禁止片段。"""
    text = str(path or "").strip()
    lowered = text.lower()
    for marker in FORBIDDEN_MARKERS:
        if marker in lowered:
            raise ConfigError(f"禁止请求敏感/重量级端点: {text}")
    if lowered not in PUBLIC_ENDPOINTS:
        raise ConfigError(
            f"只允许请求公开轻量端点 {', '.join(PUBLIC_ENDPOINTS)}，得到: {text}")
    return text


def _squash(text: Any) -> str:
    """把名称压缩成只含小写字母数字的形式，便于 new api / new-api 统一识别。"""
    out = []
    for ch in str(text or "").lower():
        if ch.isalnum():
            out.append(ch)
    return "".join(out)


def _payload_data(payload: Any) -> Any:
    """取 {success, data} 外壳里的 data；没有外壳就原样返回。"""
    if isinstance(payload, dict) and isinstance(payload.get("data"), (dict, list)):
        return payload["data"]
    return payload


def identify_panel(data: Any) -> Optional[str]:
    """从公开端点返回的 data 里识别面板类型，识别不出返回 None。"""
    if not isinstance(data, dict):
        return None

    system_name = ""
    for key in ("system_name", "systemName", "systemname", "name", "title"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            system_name = value.strip()
            break
    version = data.get("version") or data.get("v") or ""

    squashed = _squash(f"{system_name} {version}")
    if "newapi" in squashed:
        return PANEL_NEWAPI
    if "oneapi" in squashed:
        return PANEL_ONEAPI

    keys = {str(k).lower() for k in data.keys()}
    if keys & set(_NEWAPI_HINT_KEYS):
        return PANEL_NEWAPI
    return None


def probe_url(url: str, timeout: float = 8.0, proxy: Optional[str] = None,
              client: Optional[Any] = None) -> Dict[str, Any]:
    """探测一个地址的面板类型（只走公开端点，不带凭据）。

    返回稳定结构（可直接 JSON 序列化）:
        {base, url, ok, panel, adapter, version, system_name, endpoints, error}
    """
    base = normalize_base(url)
    result: Dict[str, Any] = {
        "base": base,
        "url": base,
        "ok": False,
        "panel": PANEL_CUSTOM,
        "adapter": ADAPTER_BY_PANEL[PANEL_CUSTOM],
        "version": "",
        "system_name": "",
        "endpoints": [],
        "error": None,
    }
    if not base:
        result["error"] = "地址为空"
        return result

    http = client
    if http is None:
        if not base.startswith(("http://", "https://")):
            result["error"] = f"地址必须是 http(s) URL: {base}"
            return result
        http = HttpClient(base, proxy=proxy, auth={})

    errors: List[str] = []
    for path in PUBLIC_ENDPOINTS:
        ensure_public_endpoint(path)
        result["endpoints"].append(path)
        try:
            resp = http.get(path, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 网络异常逐条记录（无凭据，不含敏感值）
            detail = str(exc)[:80]
            if len(str(exc)) > 80:
                detail += "…"
            errors.append(f"{path}: {type(exc).__name__}: {detail}")
            continue
        status = getattr(resp, "status_code", None)
        if status != 200:
            errors.append(f"{path}: HTTP {status}")
            continue
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001 - 非 JSON 响应（如登录页）
            errors.append(f"{path}: 响应不是 JSON")
            continue

        data = _payload_data(payload)
        panel = identify_panel(data)
        if panel is None:
            errors.append(f"{path}: 无法识别面板")
            continue

        result["ok"] = True
        result["panel"] = panel
        result["adapter"] = ADAPTER_BY_PANEL[panel]
        if isinstance(data, dict):
            result["system_name"] = str(data.get("system_name")
                                        or data.get("systemName")
                                        or data.get("name")
                                        or panel).strip()
            result["version"] = str(data.get("version") or data.get("v") or "").strip()
        result["error"] = None
        return result

    result["error"] = "公开探测失败（" + "; ".join(errors) + "）" if errors else "公开探测失败"
    return result


def probe_report_lines(result: Dict[str, Any]) -> List[str]:
    """人类可读的探测结果（不含任何凭据）。"""
    lines = [
        f"地址: {result.get('base', '')}",
        f"面板: {result.get('panel', '')}",
        f"适配器: {result.get('adapter', '')}",
        f"系统名: {result.get('system_name') or '未知'}",
        f"版本: {result.get('version') or '未知'}",
        f"公开探测: {'成功' if result.get('ok') else '未识别'}",
    ]
    if result.get("error"):
        lines.append(f"诊断: {result['error']}")
    return lines
