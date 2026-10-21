"""批量导入：外部清单（站点 / Key）的解析、识别与落盘。

职责边界：
- 本模块负责「读文本 -> 候选 -> 探测 -> 规范化 -> 写盘 -> 结果对象」的全部逻辑；
- CLI 只负责接参数、调用本模块、渲染结果；
- 任何输出（含错误信息）都不得包含完整 token / key 明文。

站点导入：逐条 probe-url（公开端点）后用候选鉴权调用适配器 probe，
两者都成功 -> active，否则 parked 并写 parked_reason；单条失败不影响其它站。

Key 导入：写入 `$RELAYCTL_HOME/dist/keys.yaml`（0600，父目录 0700），
按 key 的 sha256 指纹前 16 位去重，base 规范化后匹配已登记站点。
"""

import csv as _csv
import hashlib
import io
import json
import os
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import yaml

from . import config
from . import paths
from . import probe as rprobe
from .core import normalize_base
from .errors import ConfigError
from .models import mask_key

# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
SITE_FORMATS = ("auto", "txt", "csv", "json", "yaml")
KEY_FORMATS = ("auto", "txt", "csv", "json", "yaml", "ccswitch", "newapi")

KEYS_SCHEMA_VERSION = 1
KEY_DIST_FIELDS = ("id", "key", "source_site", "base", "tags", "models",
                   "groups", "assigned_to", "notes", "status")
KEY_STATUS_ACTIVE = "active"

# 输出用的稳定字段（绝不含 key 明文）
KEY_OUT_FIELDS = ("id", "key_masked", "source_site", "base", "tags", "models",
                  "groups", "assigned_to", "notes", "status")

_SITE_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def strip_bom(text: str) -> str:
    """去掉 UTF-8 BOM（导入文件常见于 Windows 导出）。"""
    if not text:
        return ""
    return text.lstrip("\ufeff")


def read_input(source: str) -> str:
    """读取导入来源：`-` 表示 stdin，其余按文件路径读取（自动处理 BOM）。"""
    if not source:
        raise ConfigError("缺少导入来源（文件路径或 - 表示标准输入）")
    if source == "-":
        data = sys.stdin.read()
    else:
        path = os.path.abspath(os.path.expanduser(source))
        if not os.path.isfile(path):
            raise ConfigError(f"导入文件不存在: {source}")
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = f.read()
        except OSError as exc:
            raise ConfigError(f"导入文件无法读取: {source} ({exc})")
    return strip_bom(data)


def _norm_field(name: Any) -> str:
    """字段名归一化（去空格/下划线/连字符 + 小写），便于中英文字段名匹配。"""
    return re.sub(r"[\s_\-./]", "", str(name or "")).strip().lower()


def _pick(mapping: Dict[str, Any], aliases: Iterable[str]) -> Any:
    """按别名字典取值：先按归一化键精确匹配，再退化为原样匹配。"""
    normalized = {_norm_field(k): v for k, v in mapping.items()}
    for alias in aliases:
        key = _norm_field(alias)
        value = normalized.get(key)
        if value not in (None, ""):
            return value
        value = mapping.get(alias)
        if value not in (None, ""):
            return value
    return None


def _as_list(value: Any) -> List[str]:
    """把标量 / 逗号字符串 / 列表统一成字符串列表（去重、保序）。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = [p.strip() for p in re.split(r"[,，;；|]", value)]
    elif isinstance(value, (list, tuple, set)):
        items = []
        for item in value:
            if isinstance(item, str):
                items.extend(p.strip() for p in re.split(r"[,，;；|]", item))
            elif item is not None:
                items.append(str(item).strip())
    else:
        items = [str(value).strip()]
    out = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def fingerprint(key: str) -> str:
    """Key 指纹：sha256 前 16 位（用于去重与稳定 ID）。"""
    return hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()[:16]


def scrub(text: Any, secrets: Iterable[str] = ()) -> str:
    """把文本里出现的明文凭据替换为脱敏形式（错误信息绝不泄露明文）。"""
    out = _as_text(text)
    for secret in secrets:
        s = _as_text(secret)
        if s and len(s) >= 4 and s in out:
            out = out.replace(s, mask_key(s))
    return out


def clean_site_name(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    """站点名规范化：无法收敛为合法名则返回错误原因。"""
    s = _as_text(raw)
    if not s:
        return None, "缺少站点名"
    for ext in (".yaml", ".yml"):
        if s.lower().endswith(ext):
            s = s[: -len(ext)]
    candidate = re.sub(r"[^A-Za-z0-9._-]", "_", s)
    candidate = re.sub(r"^[^A-Za-z0-9]+", "", candidate)
    candidate = re.sub(r"[^A-Za-z0-9]+$", "", candidate)
    if not candidate or not _SITE_NAME_OK.match(candidate):
        return None, f"非法站点名: {raw!r}（只允许字母、数字、点、下划线、连字符）"
    return candidate, None


# ---------------------------------------------------------------------------
# 格式检测
# ---------------------------------------------------------------------------
def _try_json(text: str) -> Optional[Any]:
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def _try_yaml(text: str) -> Optional[Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if isinstance(data, (dict, list)):
        return data
    return None


def _looks_like_csv(text: str, aliases: Iterable[str]) -> bool:
    known = {_norm_field(a) for a in aliases}
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    try:
        rows = list(_csv.reader(io.StringIO(text)))
    except _csv.Error:
        return False
    rows = [r for r in rows if any(_as_text(c) for c in r)]
    if not rows:
        return False
    if any(_norm_field(c) in known for c in rows[0]):
        return True
    # 无表头：多数行含逗号（或中文逗号）才当 CSV
    comma_rows = sum(1 for r in rows if len(r) >= 2)
    return comma_rows >= max(1, len(rows)) and any("," in line or "，" in line for line in lines)


_SITE_ALIAS_VALUES = (
    "name", "site", "site_name", "名称", "站点", "站点名", "base", "base_url", "url",
    "地址", "token", "access_token", "令牌", "密钥", "user_id", "uid", "panel", "adapter",
    "tags", "标签", "分类", "备注", "说明",
)
_KEY_ALIAS_VALUES = (
    "key", "apikey", "api_key", "token", "密钥", "base", "base_url", "endpoint", "地址",
    "site", "site_name", "provider", "站点", "分类", "标签", "备注", "说明",
)


def _secrets_from_text(text: str) -> List[str]:
    """从原文里找出可能的明文凭据（用于错误信息脱敏）。"""
    return list({m.group(0) for m in _KEY_RE.finditer(text or "")})


def detect_format(text: str, kind: str = "sites") -> str:
    """自动识别导入格式（kind: sites / keys）。"""
    body = strip_bom(text or "")
    if not body.strip():
        return "txt"
    if _try_json(body) is not None:
        return _classify_structured(_try_json(body), kind, default="json")
    yaml_data = _try_yaml(body)
    if yaml_data is not None:
        return _classify_structured(yaml_data, kind, default="yaml")
    if kind == "keys":
        if _looks_like_csv(body, _KEY_ALIAS_VALUES):
            return "csv"
    elif _looks_like_csv(body, _SITE_ALIAS_VALUES):
        return "csv"
    return "txt"


def _classify_structured(data: Any, kind: str, default: str) -> str:
    """结构化数据再细分：Key 导入要区分 CC Switch / New API 渠道导出。"""
    if kind != "keys":
        return default
    records = extract_records(data)
    joined = " ".join(
        _norm_field(k) for r in records if isinstance(r, dict) for k in r.keys())
    if any(marker in joined for marker in ("apikey", "endpoint", "app", "providers")):
        return "ccswitch"
    if any(marker in joined for marker in ("baseurl", "channel", "models", "group")):
        return "newapi"
    return default


# ---------------------------------------------------------------------------
# 结构化容器展开
# ---------------------------------------------------------------------------
_CONTAINER_KEYS = ("items", "data", "keys", "channels", "providers", "sites",
                   "results", "records", "list")


def extract_records(data: Any, depth: int = 0) -> List[Any]:
    """从 {sites:{...}} / {items:[...]} / {data:{items:[...]}} / 列表 中取出记录列表。"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, (dict, str))]
    if not isinstance(data, dict) or depth > 3:
        return []
    for key in _CONTAINER_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, (dict, str))]
        if isinstance(value, dict):
            nested = extract_records(value, depth + 1)
            if nested:
                return nested
    return [data]


def _records_with_names(data: Any) -> List[Tuple[Optional[str], Any]]:
    """站点导入：支持 {sites: {name: {...}}} 映射形态。"""
    if isinstance(data, dict):
        for key in ("sites", "站点"):
            value = data.get(key)
            if isinstance(value, dict):
                return [(name, item) for name, item in value.items()
                        if isinstance(item, dict)]
    return [(None, r) for r in extract_records(data)]


# ---------------------------------------------------------------------------
# 站点：字段别名与解析
# ---------------------------------------------------------------------------
SITE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "name": ("name", "site", "site_name", "sitename", "名称", "站点", "站点名", "站点名称", "站名"),
    "base": ("base", "base_url", "baseurl", "url", "endpoint", "address", "api_base",
             "地址", "网址", "接口地址", "API地址"),
    "token": ("token", "access_token", "accesstoken", "api_key", "apikey", "key",
              "令牌", "密钥", "访问令牌"),
    "user_id": ("user_id", "uid", "userid", "用户id", "用户ID", "UID"),
    "panel": ("panel", "面板", "面板类型", "系统类型"),
    "adapter": ("adapter", "适配器"),
    "console_url": ("console_url", "console", "控制台", "控制台地址", "后台地址"),
    "tags": ("tags", "tag", "标签", "分类"),
    "login_method": ("login_method", "登录方式", "进站方式", "登录"),
    "token_howto": ("token_howto", "令牌获取", "令牌获取方式", "获取方式"),
    "script_howto": ("script_howto", "脚本说明", "脚本使用说明"),
    "notes": ("notes", "note", "remark", "备注", "说明"),
    "proxy": ("proxy", "代理"),
    "refresh_interval": ("refresh_interval", "刷新间隔", "间隔"),
}


def _candidate_from_mapping(mapping: Dict[str, Any], line: Any = None,
                            preset_name: Any = None) -> Dict[str, Any]:
    cand: Dict[str, Any] = {"line": line}
    for field, aliases in SITE_ALIASES.items():
        cand[field] = _pick(mapping, aliases)
    if preset_name and not cand.get("name"):
        cand["name"] = preset_name
    return cand


def _parse_sites_txt(text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """txt: 每行 `名称 地址 令牌 [UID]`，空行与 # 注释跳过。"""
    candidates: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for idx, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            errors.append({"line": str(idx), "name": parts[0] if parts else "",
                           "error": "字段不足（至少 名称 地址）"})
            continue
        cand: Dict[str, Any] = {"line": str(idx)}
        for field in SITE_ALIASES:
            cand[field] = None
        cand["name"] = parts[0]
        cand["base"] = parts[1]
        cand["token"] = parts[2] if len(parts) >= 3 else None
        if len(parts) >= 4 and parts[3].isdigit():
            cand["user_id"] = parts[3]
        candidates.append(cand)
    return candidates, errors


def _csv_table(text: str) -> List[List[str]]:
    """读取 CSV 全部非空行（不假定第一行一定是表头）。"""
    return [r for r in _csv.reader(io.StringIO(text)) if any(_as_text(c) for c in r)]


def _csv_split(rows: List[List[str]], aliases: Dict[str, Tuple[str, ...]],
               positional: Tuple[str, ...]) -> Tuple[Dict[str, int], List[List[str]], int]:
    """识别表头：命中已知字段名则跳过首行，否则整表按 positional 顺序当数据。"""
    if not rows:
        return {}, [], 1
    header_map = _csv_field_map(rows[0], aliases, positional)
    if any(_norm_field(cell) in {_norm_field(a) for alias in aliases.values() for a in alias}
           for cell in rows[0]):
        return header_map, rows[1:], 2
    return header_map, rows, 1


def _csv_field_map(header: List[str], aliases: Dict[str, Tuple[str, ...]],
                   positional: Tuple[str, ...]) -> Dict[str, int]:
    """表头识别：命中已知字段名则用表头映射，否则按 positional 顺序。"""
    known = {field: {_norm_field(a) for a in alias} for field, alias in aliases.items()}
    mapping: Dict[str, int] = {}
    for idx, cell in enumerate(header):
        norm = _norm_field(cell)
        for field, alias_set in known.items():
            if norm in alias_set and field not in mapping:
                mapping[field] = idx
                break
    if mapping:
        return mapping
    return {field: idx for idx, field in enumerate(positional) if idx < len(header)}


def _parse_sites_csv(text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    mapping, rows, start = _csv_split(_csv_table(text), SITE_ALIASES,
                                      ("name", "base", "token", "user_id"))
    candidates: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for offset, row in enumerate(rows, start=start):
        values = {field: (row[idx] if idx < len(row) else "")
                  for field, idx in mapping.items()}
        name = _as_text(values.get("name"))
        if not name:
            errors.append({"line": str(offset), "name": "", "error": "缺少站点名"})
            continue
        cand: Dict[str, Any] = {"line": str(offset)}
        for field in SITE_ALIASES:
            cand[field] = values.get(field)
        cand["name"] = name
        candidates.append(cand)
    return candidates, errors


def _parse_sites_structured(data: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    candidates: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for preset_name, item in _records_with_names(data):
        if isinstance(item, dict):
            candidates.append(_candidate_from_mapping(item, line=None, preset_name=preset_name))
        else:
            errors.append({"line": "", "name": _as_text(preset_name), "error": "记录不是对象"})
    return candidates, errors


def parse_sites(text: str, fmt: str = "auto") -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """解析站点清单。

    返回 (candidates, errors)；errors 里的坏行同样会在导入结果里计入 failed。
    """
    body = strip_bom(text or "")
    fmt = (fmt or "auto").strip().lower()
    if fmt == "auto":
        fmt = detect_format(body, kind="sites")
    if fmt not in SITE_FORMATS:
        raise ConfigError(f"不支持的导入格式: {fmt}（可选 {', '.join(SITE_FORMATS)}）")

    if fmt == "txt":
        candidates, errors = _parse_sites_txt(body)
    elif fmt == "csv":
        candidates, errors = _parse_sites_csv(body)
    elif fmt == "json":
        data = _try_json(body)
        if data is None:
            raise ConfigError("导入内容不是合法 JSON")
        candidates, errors = _parse_sites_structured(data)
    else:  # yaml
        try:
            data = yaml.safe_load(body)
        except yaml.YAMLError as exc:
            raise ConfigError(f"导入内容不是合法 YAML: {scrub(exc, _secrets_from_text(body))}")
        if data is None:
            candidates, errors = [], []
        elif isinstance(data, (dict, list)):
            candidates, errors = _parse_sites_structured(data)
        else:
            raise ConfigError("导入内容不是合法 YAML 映射或列表")

    return _finalize_site_candidates(candidates, errors)


def _name_from_base(base: str) -> Optional[str]:
    """名称不可用时从地址主机派生一个合法站点名（如 中文站 -> cn-example-com）。"""
    host = urlparse(base).hostname or ""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", host).strip("-")
    if not slug or not slug[0].isalnum():
        slug = "site-" + slug.strip("-")
    name, error = clean_site_name(slug)
    return None if error else name


def _finalize_site_candidates(candidates: List[Dict[str, Any]],
                              errors: List[Dict[str, str]]
                              ) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """候选规范化：站点名、base、tags、docs 四字段；不合格的转 failed。"""
    good: List[Dict[str, Any]] = []
    for cand in candidates:
        line = str(cand.get("line") or "")
        raw_name = cand.get("name")
        base = normalize_base(cand.get("base"))
        name, err = clean_site_name(raw_name)
        if err:
            # 名称非法或缺失（如纯中文名）：从地址主机名派生，仍不可用才记为失败
            name = _name_from_base(base)
            if not name:
                errors.append({"line": line, "name": _as_text(raw_name),
                               "error": "缺少站点名且无法从地址派生"})
                continue
        if not base:
            errors.append({"line": line, "name": name, "error": "缺少站点地址"})
            continue
        if not base.startswith(("http://", "https://")):
            errors.append({"line": line, "name": name, "error": f"地址必须是 http(s) URL: {base}"})
            continue
        cand["name"] = name
        cand["base"] = base
        cand["tags"] = _as_list(cand.get("tags"))
        cand["docs"] = {
            "login_method": _as_text(cand.get("login_method")),
            "token_howto": _as_text(cand.get("token_howto")),
            "script_howto": _as_text(cand.get("script_howto")),
            "notes": _as_text(cand.get("notes")),
        }
        good.append(cand)
    return good, errors


# ---------------------------------------------------------------------------
# 站点：配置构建与落盘
# ---------------------------------------------------------------------------
def build_site_config(cand: Dict[str, Any], panel: Optional[str] = None,
                      adapter: Optional[str] = None) -> Dict[str, Any]:
    """把候选规范化为完整站点配置（docs 四字段一定存在）。

    panel / adapter 由公开探测结果提供；输入显式指定优先。
    """
    raw_panel = _as_text(cand.get("panel")).lower()
    raw_adapter = _as_text(cand.get("adapter")).lower()

    final_panel = raw_panel if raw_panel in config.VALID_PANELS else (
        panel if panel in config.VALID_PANELS else rprobe.PANEL_CUSTOM)
    final_adapter = raw_adapter if raw_adapter in config.VALID_ADAPTERS else (
        adapter if adapter in config.VALID_ADAPTERS
        else rprobe.ADAPTER_BY_PANEL.get(final_panel, "newapi"))

    token = _as_text(cand.get("token"))
    user_id = cand.get("user_id")
    if user_id in (None, ""):
        user_id = None
    else:
        try:
            user_id = int(str(user_id).strip())
        except (TypeError, ValueError):
            user_id = None

    auth = dict(config.DEFAULT_AUTH)
    auth["type"] = "access_token" if token else "none"
    auth["access_token"] = token
    auth["user_id"] = user_id

    docs = dict(config.DEFAULT_DOCS)
    for field in config.DOCS_FIELDS:
        value = (cand.get("docs") or {}).get(field)
        if value:
            docs[field] = _as_text(value)

    base = normalize_base(cand.get("base"))
    return {
        "name": cand["name"],
        "base": base,
        "console_url": _as_text(cand.get("console_url")) or base,
        "panel": final_panel,
        "adapter": final_adapter,
        "script": None,
        "auth": auth,
        "docs": docs,
        "proxy": _as_text(cand.get("proxy")),
        "refresh_interval": _as_text(cand.get("refresh_interval")),
        "tags": list(cand.get("tags") or []),
        "status": config.STATUS_PARKED,
        "parked_reason": "",
    }


def _auth_probe(site: Dict[str, Any], cfg: Dict[str, Any], name: str) -> Dict[str, Any]:
    """用候选鉴权调用适配器 probe（Parked 站点也强制走真实适配器）。"""
    from .adapters import get_adapter  # 延迟导入：避免与 adapters 形成循环依赖

    runtime = dict(site)
    runtime["status"] = config.STATUS_ACTIVE
    runtime["tier"] = "api"
    adapter = get_adapter(name, runtime, cfg or {})
    return adapter.probe() or {}


def apply_sites_import(candidates: List[Dict[str, Any]], dry_run: bool = False,
                       cfg: Optional[Dict[str, Any]] = None,
                       parse_errors: Optional[List[Dict[str, str]]] = None,
                       probe_url_fn=None, auth_probe_fn=None) -> Dict[str, Any]:
    """逐条导入站点：公开探测 + 鉴权探测 -> active / parked。

    dry_run=True 时允许探测但绝不写盘。返回稳定结果对象（绝不含完整 token）。
    """
    probe_fn = probe_url_fn or rprobe.probe_url
    auth_fn = auth_probe_fn or _auth_probe
    proxy = (cfg or {}).get("proxy")

    result: Dict[str, Any] = {
        "dry_run": bool(dry_run),
        "total": len(candidates or []) + len(parse_errors or []),
        "added": [],
        "skipped": [],
        "failed": [],
    }
    secrets = [_as_text(c.get("token")) for c in (candidates or []) if _as_text(c.get("token"))]

    for err in (parse_errors or []):
        result["failed"].append({
            "name": _as_text(err.get("name")),
            "line": _as_text(err.get("line")),
            "error": scrub(err.get("error"), secrets),
        })

    seen: List[str] = []
    for cand in (candidates or []):
        name = cand["name"]
        if name in seen or config.site_exists(name):
            result["skipped"].append({"name": name, "base": cand["base"],
                                      "reason": "站点已存在（默认不覆盖）"})
            seen.append(name)
            continue
        seen.append(name)

        try:
            probe_result = probe_fn(cand["base"], proxy=proxy) or {}
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响其它站
            probe_result = {"ok": False, "panel": rprobe.PANEL_CUSTOM,
                            "adapter": "newapi", "version": "", "system_name": "",
                            "error": scrub(exc, secrets)}

        site = build_site_config(cand, panel=probe_result.get("panel"),
                                 adapter=probe_result.get("adapter"))

        auth_ok = False
        auth_error = ""
        if probe_result.get("ok") and site["auth"]["type"] != "none":
            try:
                probed = auth_fn(site, cfg, name) or {}
                auth_ok = bool(probed.get("auth_ok"))
                if not auth_ok:
                    auth_error = scrub(
                        probed.get("auth_msg") or probed.get("auth_error")
                        or (f"HTTP {probed.get('auth_status')}" if probed.get("auth_status") else "")
                        or "鉴权未通过", secrets)
            except Exception as exc:  # noqa: BLE001 - 网络 / 适配器异常
                auth_error = scrub(f"{type(exc).__name__}: {exc}", secrets)

        if probe_result.get("ok") and auth_ok:
            site["status"] = config.STATUS_ACTIVE
            site["parked_reason"] = ""
        else:
            site["status"] = config.STATUS_PARKED
            if not probe_result.get("ok"):
                site["parked_reason"] = scrub(
                    f"公开探测未识别面板（{probe_result.get('error') or '无响应'}）；"
                    f"已按 {site['panel']}+{site['adapter']} 搁置", secrets)
            elif auth_error:
                site["parked_reason"] = scrub(f"鉴权失败: {auth_error}", secrets)
            else:
                site["parked_reason"] = "缺少凭据（未导入令牌），已搁置"

        entry = {
            "name": name,
            "base": site["base"],
            "panel": site["panel"],
            "adapter": site["adapter"],
            "status": site["status"],
            "system_name": probe_result.get("system_name") or "",
            "version": probe_result.get("version") or "",
            "parked_reason": site["parked_reason"],
            "tags": list(site["tags"]),
        }

        if dry_run:
            result["added"].append(entry)
            continue
        try:
            config.save_site(name, site)
        except Exception as exc:  # noqa: BLE001 - 单条写失败不影响其它站
            result["failed"].append({"name": name, "line": _as_text(cand.get("line")),
                                     "error": scrub(f"{type(exc).__name__}: {exc}", secrets)})
            continue
        result["added"].append(entry)
    return result


# ---------------------------------------------------------------------------
# Key：字段别名与解析
# ---------------------------------------------------------------------------
KEY_ALIASES: Dict[str, Tuple[str, ...]] = {
    "key": ("key", "api_key", "apikey", "apiKey", "token", "access_token", "secret_key",
            "secretKey", "密钥", "令牌", "Key值"),
    "base": ("base", "base_url", "baseurl", "api_base", "endpoint", "api_endpoint", "url",
             "地址", "站点地址", "接口地址", "渠道地址"),
    "site": ("site", "site_name", "sitename", "source_site", "provider", "channel_site",
             "站点", "站点名", "来源站点", "渠道站点"),
    "tags": ("tags", "tag", "category", "categories", "app", "label", "分类", "标签", "应用"),
    "models": ("models", "model", "model_limits", "模型", "模型列表", "可用模型"),
    "groups": ("groups", "group", "分组", "渠道分组", "分组名"),
    "notes": ("notes", "note", "remark", "description", "desc", "备注", "说明", "描述"),
    "assigned_to": ("assigned_to", "owner", "assignee", "分配给", "负责人", "使用人"),
}

_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]{4,}")


def _parse_keys_txt(text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """文本：一行一个 sk-（后面可跟 站点/分类/备注 等可选字段）。"""
    candidates: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for idx, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _KEY_RE.search(line)
        if not match:
            errors.append({"line": str(idx), "key_masked": "", "error": "未识别到 sk- 开头的 Key"})
            continue
        key = match.group(0)
        rest = [p for p in line.replace(key, " ").split() if p]
        candidates.append({
            "line": str(idx),
            "key": key,
            "base": "",
            "site": rest[0] if rest else "",
            "tags": _as_list(rest[1] if len(rest) > 1 else ""),
            "models": [],
            "groups": [],
            "notes": " ".join(rest[2:]) if len(rest) > 2 else "",
            "assigned_to": "",
        })
    return candidates, errors


def _parse_keys_csv(text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    mapping, rows, start = _csv_split(_csv_table(text), KEY_ALIASES,
                                      ("key", "site", "tags", "notes"))
    candidates: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for offset, row in enumerate(rows, start=start):
        values = {field: (row[idx] if idx < len(row) else "")
                  for field, idx in mapping.items()}
        key = _as_text(values.get("key"))
        if not key:
            errors.append({"line": str(offset), "key_masked": "", "error": "缺少 Key"})
            continue
        candidates.append({
            "line": str(offset),
            "key": key,
            "base": _as_text(values.get("base")),
            "site": _as_text(values.get("site")),
            "tags": _as_list(values.get("tags")),
            "models": _as_list(values.get("models")),
            "groups": _as_list(values.get("groups")),
            "notes": _as_text(values.get("notes")),
            "assigned_to": _as_text(values.get("assigned_to")),
        })
    return candidates, errors


def _parse_keys_structured(data: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    candidates: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for idx, record in enumerate(extract_records(data), start=1):
        if isinstance(record, str):
            match = _KEY_RE.search(record)
            if not match:
                errors.append({"line": str(idx), "key_masked": "", "error": "未识别到 Key"})
                continue
            record = {"key": match.group(0)}
        if not isinstance(record, dict):
            errors.append({"line": str(idx), "key_masked": "", "error": "记录不是对象"})
            continue
        key = _as_text(_pick(record, KEY_ALIASES["key"]))
        if not key:
            errors.append({"line": str(idx), "key_masked": "", "error": "缺少 Key"})
            continue
        site = _as_text(_pick(record, KEY_ALIASES["site"]))
        tags = _as_list(_pick(record, KEY_ALIASES["tags"]))
        provider = _as_text(_pick(record, ("provider", "供应商", "服务商")))
        if provider and provider != site and provider not in tags:
            tags.append(provider)
        candidates.append({
            "line": str(idx),
            "key": key,
            "base": _as_text(_pick(record, KEY_ALIASES["base"])),
            "site": site,
            "tags": tags,
            "models": _as_list(_pick(record, KEY_ALIASES["models"])),
            "groups": _as_list(_pick(record, KEY_ALIASES["groups"])),
            "notes": _as_text(_pick(record, KEY_ALIASES["notes"])),
            "assigned_to": _as_text(_pick(record, KEY_ALIASES["assigned_to"])),
        })
    return candidates, errors


def parse_keys(text: str, fmt: str = "auto") -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """解析 Key 清单（txt / csv / json / yaml / ccswitch / newapi）。"""
    body = strip_bom(text or "")
    fmt = (fmt or "auto").strip().lower()
    if fmt == "auto":
        fmt = detect_format(body, kind="keys")
    if fmt not in KEY_FORMATS:
        raise ConfigError(f"不支持的导入格式: {fmt}（可选 {', '.join(KEY_FORMATS)}）")

    if fmt == "txt":
        candidates, errors = _parse_keys_txt(body)
    elif fmt == "csv":
        candidates, errors = _parse_keys_csv(body)
    else:  # json / yaml / ccswitch / newapi：同一套通用结构化解析
        if fmt == "yaml":
            try:
                data = yaml.safe_load(body)
            except yaml.YAMLError as exc:
                raise ConfigError(f"导入内容不是合法 YAML: {scrub(exc, _secrets_from_text(body))}")
            if data is None:
                data = []
            elif not isinstance(data, (dict, list)):
                raise ConfigError("导入内容不是合法 YAML 映射或列表")
        else:
            data = _try_json(body)
            if data is None:
                try:
                    data = yaml.safe_load(body)
                except yaml.YAMLError:
                    data = None
            if data is None:
                raise ConfigError("导入内容不是合法 JSON / YAML")
        candidates, errors = _parse_keys_structured(data)

    for cand in candidates:
        cand["key"] = _as_text(cand.get("key"))
        cand["base"] = normalize_base(cand.get("base"))
    return candidates, errors


# ---------------------------------------------------------------------------
# Key：dist/keys.yaml 读写
# ---------------------------------------------------------------------------
def load_keys_dist() -> Dict[str, Any]:
    """读取分发 Key 库；不存在或非法时返回空 schema（不抛异常）。"""
    data = paths.read_yaml(paths.keys_dist_path(), default={})
    if not isinstance(data, dict):
        return {"version": KEYS_SCHEMA_VERSION, "keys": []}
    keys = data.get("keys")
    keys = [k for k in keys if isinstance(k, dict)] if isinstance(keys, list) else []
    try:
        version = int(data.get("version") or KEYS_SCHEMA_VERSION)
    except (TypeError, ValueError):
        version = KEYS_SCHEMA_VERSION
    return {"version": version, "keys": keys}


def save_keys_dist(data: Dict[str, Any]) -> str:
    """原子写 dist/keys.yaml（0600，父目录 0700）。"""
    path = paths.keys_dist_path()
    paths.ensure_dir(paths.dist_dir())
    payload = {
        "version": KEYS_SCHEMA_VERSION,
        "keys": [key_item(k) for k in (data.get("keys") or [])],
    }
    paths.atomic_write_yaml(path, payload)
    paths.ensure_file_mode(path)
    return path


def key_item(cand: Dict[str, Any]) -> Dict[str, Any]:
    """把候选（或已有记录）规范成 dist 条目（字段顺序固定）。"""
    key = _as_text(cand.get("key"))
    item = {
        "id": _as_text(cand.get("id")) or fingerprint(key),
        "key": key,
        "source_site": _as_text(cand.get("source_site") or cand.get("site")),
        "base": normalize_base(cand.get("base")),
        "tags": _as_list(cand.get("tags")),
        "models": _as_list(cand.get("models")),
        "groups": _as_list(cand.get("groups")),
        "assigned_to": _as_text(cand.get("assigned_to")),
        "notes": _as_text(cand.get("notes")),
        "status": _as_text(cand.get("status")) or KEY_STATUS_ACTIVE,
    }
    return {field: item[field] for field in KEY_DIST_FIELDS}


def key_out(item: Dict[str, Any]) -> Dict[str, Any]:
    """输出用条目：只给 key_masked / id，绝不含明文。"""
    key = _as_text(item.get("key"))
    masked = {
        "id": _as_text(item.get("id")) or fingerprint(key),
        "key_masked": mask_key(key),
        "source_site": _as_text(item.get("source_site")),
        "base": normalize_base(item.get("base")),
        "tags": _as_list(item.get("tags")),
        "models": _as_list(item.get("models")),
        "groups": _as_list(item.get("groups")),
        "assigned_to": _as_text(item.get("assigned_to")),
        "notes": _as_text(item.get("notes")),
        "status": _as_text(item.get("status")) or KEY_STATUS_ACTIVE,
    }
    return {field: masked[field] for field in KEY_OUT_FIELDS}


def match_site_by_base(base: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    """用规范化后的 base 匹配已登记站点，返回站点名。"""
    target = normalize_base(base)
    if not target:
        return ""
    for name, site in ((cfg or {}).get("sites") or {}).items():
        if normalize_base(site.get("base")) == target:
            return name
    return ""


def apply_keys_import(candidates: List[Dict[str, Any]], dry_run: bool = False,
                      cfg: Optional[Dict[str, Any]] = None,
                      parse_errors: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """导入 Key 到 dist/keys.yaml：指纹去重、base 匹配站点、dry-run 绝不写。"""
    result: Dict[str, Any] = {
        "dry_run": bool(dry_run),
        "total": len(candidates or []) + len(parse_errors or []),
        "added": [],
        "skipped": [],
        "failed": [],
    }
    secrets = [_as_text(c.get("key")) for c in (candidates or []) if _as_text(c.get("key"))]
    for err in (parse_errors or []):
        result["failed"].append({
            "key_masked": _as_text(err.get("key_masked")),
            "line": _as_text(err.get("line")),
            "error": scrub(err.get("error"), secrets),
        })

    def process(cand: Dict[str, Any]):
        key = _as_text(cand.get("key"))
        if not key:
            result["failed"].append({"key_masked": "", "line": _as_text(cand.get("line")),
                                     "error": "缺少 Key"})
            return
        fp = fingerprint(key)
        existing = load_keys_dist()
        if any(_as_text(item.get("id")) == fp for item in existing["keys"]):
            result["skipped"].append({"id": fp, "key_masked": mask_key(key),
                                      "reason": "该 Key 已存在（按指纹去重）"})
            return
        site = _as_text(cand.get("site")) or match_site_by_base(cand.get("base"), cfg)
        item = key_item({**cand, "source_site": site})
        if not dry_run:
            existing["keys"].append(item)
            save_keys_dist(existing)
        result["added"].append(key_out(item))

    if dry_run:
        for cand in (candidates or []):
            try:
                process(cand)
            except Exception as exc:  # noqa: BLE001
                result["failed"].append({
                    "key_masked": mask_key(_as_text(cand.get("key"))),
                    "line": _as_text(cand.get("line")),
                    "error": scrub(f"{type(exc).__name__}: {exc}", secrets)})
        return result

    with paths.file_lock(paths.keys_dist_path()):
        for cand in (candidates or []):
            try:
                process(cand)
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响其它 Key
                result["failed"].append({
                    "key_masked": mask_key(_as_text(cand.get("key"))),
                    "line": _as_text(cand.get("line")),
                    "error": scrub(f"{type(exc).__name__}: {exc}", secrets)})
    return result


# ---------------------------------------------------------------------------
# 渲染（CLI 只做渲染，逻辑都在上面）
# ---------------------------------------------------------------------------
def sites_report_lines(result: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    for item in result["added"]:
        status = item.get("status")
        mark = "[✓]" if status == config.STATUS_ACTIVE else "[~]"
        extra = ""
        if status != config.STATUS_ACTIVE and item.get("parked_reason"):
            extra = f"  ({item['parked_reason']})"
        lines.append(f"{mark} 新增 {item['name']}  {item['base']}  "
                     f"{item['panel']}/{item['adapter']}  {status}{extra}")
    for item in result["skipped"]:
        lines.append(f"[-] 跳过 {item.get('name')}  {item.get('base', '')}  {item.get('reason')}")
    for item in result["failed"]:
        line = f"（第 {item['line']} 行）" if item.get("line") else ""
        lines.append(f"[!] 失败 {item.get('name') or item.get('key_masked') or ''}{line}  {item.get('error')}")
    lines.append("--- 新增 {} / 跳过 {} / 失败 {}{}".format(
        len(result["added"]), len(result["skipped"]), len(result["failed"]),
        "（试运行未写盘）" if result["dry_run"] else ""))
    return lines


def keys_report_lines(result: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    for item in result["added"]:
        lines.append(f"[✓] 新增 {item['id']}  {item['key_masked']}  "
                     f"站点={item['source_site'] or '-'}  分类={','.join(item['tags']) or '-'}  "
                     f"模型={','.join(item['models']) or '-'}  分组={','.join(item['groups']) or '-'}")
    for item in result["skipped"]:
        lines.append(f"[-] 跳过 {item.get('id')}  {item.get('key_masked', '')}  {item.get('reason')}")
    for item in result["failed"]:
        line = f"（第 {item['line']} 行）" if item.get("line") else ""
        lines.append(f"[!] 失败 {item.get('key_masked', '')}{line}  {item.get('error')}")
    lines.append("--- 新增 {} / 跳过 {} / 失败 {}{}".format(
        len(result["added"]), len(result["skipped"]), len(result["failed"]),
        "（试运行未写盘）" if result["dry_run"] else ""))
    return lines
