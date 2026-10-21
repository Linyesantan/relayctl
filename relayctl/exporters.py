"""阶段 5 导出：CC Switch 深链 / New API 渠道 / env / json / csv。

职责边界：
- 只做「单向分发」：把 dist/keys.yaml 里的 Key 变成别处能直接用的东西；
- 不做负载均衡、不做计费、不做用户系统、不做与上游的双向自动同步；
- 默认绝不把完整 Key / 令牌打印到 stdout（只有 --out 写 0600 文件或显式 --reveal 才含明文）；
- 所有错误信息都经过脱敏（见 :func:`scrub_text`）。

=============================================================================
外部协议依据（2026-10-21 核实，写死在代码里的常量都来自下面这些来源）
=============================================================================

CC Switch（当前最新发布 v4.0.17，2026-10）
  - 发布页：https://github.com/farion1231/cc-switch/releases/latest
  - 深链协议（用户手册 5.3，main 分支）：
    https://raw.githubusercontent.com/farion1231/cc-switch/main/docs/user-manual/en/5-faq/5.3-deeplink.md
  - 站长接入指南（**适用 v4.0.10 及以上**，本文实现以它为准）：
    https://ccswitch.io/zh/tutorials/deeplink-site-integration-guide
  - 配置文件说明（Claude Code / Codex 落盘结构）：
    https://ccswitch.io/en/docs?item=config-files&section=faq

  深链格式：ccswitch://v1/import?resource=provider&app=<app>&name=<名称>&...
  编码规则（两层，顺序不能反）：
    1) config / usageScript / content 这类「整块内容」参数：先把 UTF-8 原文做
       Base64（标准或 URL-safe 都接受，末尾 '=' 可省）；
    2) 拼链接时**每个参数值**再做一次 application/x-www-form-urlencoded 编码。
       标准 Base64 里的 '+' 必须变成 %2B，否则会被解成空格；'=' 变成 %3D。
  供应商参数：endpoint / apiKey / apiFormat / model / haikuModel / sonnetModel /
    opusModel / homepage / icon / iconUrl / notes / enabled / config / configFormat。
  注意（文档与旧版源码不一致处，这里按官方现行指南 + 兼容旧版处理）：
    - 用户手册 5.3 仍把 `configUrl`（远程配置）列为可选参数，但站长接入指南与
      当前代码明确「还没实现，带 configUrl 的链接会被直接拒绝」；
      -> 本模块**从不生成 configUrl**，且 :func:`parse_ccswitch_link` 遇到它会标记。
    - app 取值：现行支持 claude/codex/gemini/grokbuild/opencode/openclaw/hermes/pi；
      v3.x 文档里没有 pi。本模块只生成 claude / codex 两种（分发范围如此），
      解析时对其余取值保持宽容（原样返回，不报错）。
    - URL 参数与 config 里同名的值冲突时，**URL 参数优先**（指南原文），
      因此本模块生成的链接里 endpoint/apiKey 与 config 内保持一致，不制造冲突。
  apiFormat 取值：anthropic / openai_chat / openai_responses / gemini_native；
    Claude Code 默认 anthropic，Codex 默认 openai_responses。
  Claude Code 落盘：~/.claude/settings.json 的 env 段
    （ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY /
      ANTHROPIC_MODEL / ANTHROPIC_SMALL_FAST_MODEL）；深链 config 形如 {"env": {...}}，
    env 里的变量原样保留。
  Codex 落盘：~/.codex/config.toml，第三方供应商统一写在 [model_providers.custom]，
    Key 放在该表的 experimental_bearer_token；wire_api 当前**只**用 "responses"
    （Codex CLI 早已移除 chat wire_api，本模块不生成 chat）。
    深链 config 两种写法：configFormat=json 时是
    {"auth": {"OPENAI_API_KEY": "..."}, "config": "<config.toml 原文>"}；
    configFormat=toml 时直接是 config.toml 原文。导入后表名统一改成 custom。
    被拒绝的写法：表里带 auth / aws / gateway_oauth / env_http_headers；env_key 被丢掉。

New API（QuantumNous/new-api，当前 v1.0.0-rc.44，2026-10-10）
  - 渠道管理模块：https://github.com/QuantumNous/new-api-docs/blob/main/docs/api/fei-channel-management.md
  - 官方指南：https://docs.newapi.ai/en/docs/guide/feature-guide/admin/channel
  - 鉴权与响应结构（HTTP 200 + success=false 也算失败）：
    https://mixroute.ai/docs/zh-hans/api-reference/system-api/authentication-and-quota
  - 渠道类型枚举（constant/channel.go）：OpenAI=1 / Anthropic=14 / Gemini=24 / Codex=57

  鉴权头：Authorization: Bearer <管理员系统访问令牌> + New-Api-User: <用户 ID>
  新增渠道：POST /api/channel/，body = {"mode": "single", "channel": {...}}
  更新渠道：PUT  /api/channel/，body = {..., "id": <渠道 ID>}
  回查：GET /api/channel/search?keyword=<名称>（或 GET /api/channel/）
  **字段差异（文档与源码不一致处，按源码/文档实测处理）**：
    - 写入（POST/PUT）用 `groups`（数组），列表/搜索返回的是 `group`（逗号串或单值字符串）；
      -> 本模块写入一律发 `groups` 数组，读取时只认 `name` 精确相等来定位 id。
    - `models` 是逗号分隔字符串（不是数组）。
    - HTTP 200 但 body {"success": false, "message": ...} 必须视为失败。
"""

import base64
import binascii
import csv as _csv
import io
import json
import os
import shlex
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, quote, urlencode, urlparse

from . import importers
from . import paths
from .core import normalize_base
from .errors import EXIT_FAIL, AuthError, ConfigError, RelayctlError
from .models import mask_key

# ---------------------------------------------------------------------------
# CC Switch 常量
# ---------------------------------------------------------------------------
CCSWITCH_SCHEME = "ccswitch"
CCSWITCH_IMPORT_PATH = "/v1/import"
CCSWITCH_BASE = f"{CCSWITCH_SCHEME}://v1/import"
CCSWITCH_RESOURCE = "provider"

# 本模块生成链接时支持的 app（分发范围：Claude Code / Codex）
CCSWITCH_APPS = ("claude", "codex")
# CC Switch v4 支持的全量 app（解析时保持宽容，不做校验拒绝）
CCSWITCH_KNOWN_APPS = ("claude", "codex", "gemini", "grokbuild", "opencode",
                       "openclaw", "hermes", "pi")
CCSWITCH_APP_CHOICES = ("claude", "codex", "all")

# apiFormat 取值与每个 app 的默认格式（来源：站长接入指南「支持哪些应用」表）
CCSWITCH_API_FORMATS = ("anthropic", "openai_chat", "openai_responses", "gemini_native")
CCSWITCH_DEFAULT_API_FORMAT = {"claude": "anthropic", "codex": "openai_responses"}

# configFormat 取值
CONFIG_FORMAT_JSON = "json"
CONFIG_FORMAT_TOML = "toml"

# Codex 当前唯一支持的 wire_api（chat 已被 Codex CLI 移除，绝不生成）
CODEX_WIRE_API = "responses"
CODEX_PROVIDER_TABLE = "custom"

# Claude Code settings.json env 键（来源：配置文件文档 + 站长接入指南）
CLAUDE_ENV_BASE_URL = "ANTHROPIC_BASE_URL"
CLAUDE_ENV_AUTH_TOKEN = "ANTHROPIC_AUTH_TOKEN"
CLAUDE_ENV_API_KEY = "ANTHROPIC_API_KEY"
CLAUDE_ENV_MODEL = "ANTHROPIC_MODEL"
CLAUDE_ENV_SMALL_MODEL = "ANTHROPIC_SMALL_FAST_MODEL"

# Codex config.toml 顶层 / 供应商表键（来源：站长接入指南 Codex 小节白名单）
CODEX_TOP_MODEL = "model"
CODEX_TOP_PROVIDER = "model_provider"

# ---------------------------------------------------------------------------
# New API 常量
# ---------------------------------------------------------------------------
NEWAPI_CHANNEL_PATH = "/api/channel/"
NEWAPI_CHANNEL_SEARCH_PATH = "/api/channel/search"
# constant/channel.go（QuantumNous/new-api）
NEWAPI_CHANNEL_TYPES = {
    "openai": 1,
    "azure": 3,
    "anthropic": 14,
    "claude": 14,
    "gemini": 24,
    "aws": 33,
    "codex": 57,
}
DEFAULT_CHANNEL_TYPE = 1  # OpenAI 兼容（中转站通用）
NEWAPI_DEFAULT_GROUP = "default"
NEWAPI_SEARCH_PAGE_SIZE = 100

EXPORT_TARGETS = ("ccswitch", "newapi", "env", "json", "csv")


class ApiError(RelayctlError):
    """上游 API 返回业务失败（含 HTTP 200 但 success=false）。"""
    exit_code = EXIT_FAIL


class AmbiguousChannel(ApiError):
    """按名字回查渠道时出现多个同名渠道：绝不猜测，直接报错。"""
    exit_code = EXIT_FAIL


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _list(value: Any) -> List[str]:
    """标量 / 分隔符字符串 / 列表 -> 去重保序的字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        raw = [p.strip() for p in _split_many(value)]
    elif isinstance(value, (list, tuple, set)):
        raw = []
        for item in value:
            if isinstance(item, str):
                raw.extend(p.strip() for p in _split_many(item))
            elif item is not None:
                raw.append(str(item).strip())
    else:
        raw = [str(value).strip()]
    out: List[str] = []
    for item in raw:
        if item and item not in out:
            out.append(item)
    return out


def _split_many(text: str) -> List[str]:
    import re as _re
    return _re.split(r"[,，;；|]", str(text))


def scrub_text(text: Any, secrets: Iterable[str] = ()) -> str:
    """错误信息脱敏：把所有明文 Key / 令牌替换为脱敏形式。"""
    return importers.scrub(text, secrets)


def write_export_file(path: str, text: str) -> str:
    """写导出文件：原子写 + 0600（导出物含凭据，权限绝不放松）。"""
    target = os.path.abspath(os.path.expanduser(str(path)))
    if os.path.isdir(target):
        raise ConfigError(f"--out 指向了目录: {target}")
    paths.atomic_write_text(target, text, mode=paths.FILE_MODE)
    paths.ensure_file_mode(target)
    return target


# ---------------------------------------------------------------------------
# Base64 + URL 编码（CC Switch 两层编码）
# ---------------------------------------------------------------------------
def b64_encode(text: Any) -> str:
    """标准 Base64（UTF-8 原文 -> ASCII，保留 '=' padding）。

    注意：config 是整块原文，**不能** strip（TOML 末尾换行等是有意义的）。
    """
    raw = "" if text is None else str(text)
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def b64_decode(data: Any) -> str:
    """宽松 Base64 解码：标准 / URL-safe 都接受，padding 可省，空格还原成 '+'。

    来源：站长接入指南「两种编码都要做」——标准 Base64 的 '+' 在 URL 里必须写成
    %2B，被手抄成空格时 CC Switch 也会尝试还原；这里按同样策略容错。
    """
    s = _text(data).replace(" ", "+")
    if not s:
        return ""
    pad = "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s + pad).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        pass
    try:
        return base64.urlsafe_b64decode(s + pad).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise ConfigError("config 参数 Base64 解码失败")


def ccswitch_link(params: Sequence[Tuple[str, str]]) -> str:
    """按 CC Switch 规则拼深链：每个参数值都做 urlencode（'+'->%2B, '='->%3D）。"""
    items = [(k, v) for k, v in params if v not in (None, "")]
    return f"{CCSWITCH_BASE}?{urlencode(items, safe='', quote_via=quote)}"


def build_ccswitch_link(app: str, name: str, endpoint: str = "", api_key: str = "",
                        model: str = "", api_format: str = "", notes: str = "",
                        homepage: str = "", config: str = "",
                        config_format: str = CONFIG_FORMAT_JSON,
                        enabled: Optional[bool] = None) -> str:
    """生成 ccswitch://v1/import 深链（resource=provider）。

    config 传的是**原文**（JSON 或 TOML 文本），本函数负责 Base64 后再 URL 编码。
    """
    if app not in CCSWITCH_KNOWN_APPS:
        raise ConfigError(f"不支持的 app: {app!r}（可选 {', '.join(CCSWITCH_KNOWN_APPS)}）")
    if not _text(name):
        raise ConfigError("深链缺少必填参数 name")
    params: List[Tuple[str, str]] = [
        ("resource", CCSWITCH_RESOURCE),
        ("app", app),
        ("name", _text(name)),
    ]
    if _text(endpoint):
        params.append(("endpoint", _text(endpoint)))
    if _text(api_key):
        params.append(("apiKey", _text(api_key)))
    if _text(api_format):
        if api_format not in CCSWITCH_API_FORMATS:
            raise ConfigError(
                f"不支持的 apiFormat: {api_format!r}（可选 {', '.join(CCSWITCH_API_FORMATS)}）")
        params.append(("apiFormat", api_format))
    if _text(model):
        params.append(("model", _text(model)))
    if _text(homepage):
        params.append(("homepage", _text(homepage)))
    if _text(notes):
        params.append(("notes", _text(notes)))
    if config:
        fmt = config_format if config_format in (CONFIG_FORMAT_JSON, CONFIG_FORMAT_TOML) else CONFIG_FORMAT_JSON
        # 第一层：原文 -> Base64；第二层：ccswitch_link() 里统一 URL 编码
        params.append(("config", b64_encode(config)))
        params.append(("configFormat", fmt))
    if enabled is not None:
        params.append(("enabled", "true" if enabled else "false"))
    return ccswitch_link(params)


def parse_ccswitch_link(url: str) -> Dict[str, Any]:
    """解析深链（供 round-trip 与自检使用）。

    宽容解析：不校验 app/apiFormat 取值，只还原参数并尝试解 config，
    便于测试与排障；真正决定能否导入的是 CC Switch 自己。
    """
    raw = _text(url)
    parsed = urlparse(raw)
    if parsed.scheme != CCSWITCH_SCHEME:
        raise ConfigError(f"不是 ccswitch:// 深链: {raw[:32]}")
    # urlparse 把 ccswitch://v1/import 的 "v1" 当成 netloc，因此两种写法都接受
    if parsed.path.rstrip("/") not in ("/import", CCSWITCH_IMPORT_PATH.rstrip("/")):
        raise ConfigError(f"深链路径不是 {CCSWITCH_IMPORT_PATH}: {parsed.path}")
    params = {k: v for k, v in parse_qsl(parsed.query, keep_blank_values=True)}
    out: Dict[str, Any] = {
        "resource": params.get("resource", ""),
        "app": params.get("app", ""),
        "name": params.get("name", ""),
        "endpoint": params.get("endpoint", ""),
        "apiKey": params.get("apiKey", ""),
        "apiFormat": params.get("apiFormat", ""),
        "model": params.get("model", ""),
        "homepage": params.get("homepage", ""),
        "notes": params.get("notes", ""),
        "enabled": params.get("enabled", ""),
        "config_raw": params.get("config", ""),
        "configFormat": params.get("configFormat", ""),
        "config_text": "",
        # configUrl 尚未实现（带它的链接会被 CC Switch 拒绝），这里只做标记
        "configUrl": params.get("configUrl", ""),
    }
    if out["config_raw"]:
        out["config_text"] = b64_decode(out["config_raw"])
    return out


# ---------------------------------------------------------------------------
# 端点（按 apiFormat 的惯例：Anthropic 不带 /v1，OpenAI 两种带 /v1）
# ---------------------------------------------------------------------------
def claude_endpoint(base: Any) -> str:
    """Claude Code（anthropic 格式）端点：不带 /v1。"""
    return normalize_base(base)


def codex_endpoint(base: Any) -> str:
    """Codex（openai_responses 格式）端点：带 /v1。"""
    b = normalize_base(base)
    if not b:
        return ""
    return b if b.lower().endswith("/v1") else b + "/v1"


def endpoint_for_app(app: str, base: Any) -> str:
    if app == "claude":
        return claude_endpoint(base)
    if app == "codex":
        return codex_endpoint(base)
    raise ConfigError(f"不支持的 app: {app!r}")


# ---------------------------------------------------------------------------
# 供应商配置：Claude Code / Codex
# ---------------------------------------------------------------------------
def claude_provider_config(base: Any, key: str = "", model: str = "",
                           small_model: str = "",
                           extra_env: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Claude Code 供应商配置（~/.claude/settings.json 的 env 段）。

    深链 config 形如 {"env": {...}}，env 里的变量原样保留。
    Key 同时写 ANTHROPIC_AUTH_TOKEN 与 ANTHROPIC_API_KEY：
    部分版本只读其中一个（来源：配置文件文档把两者都列为可用字段）。
    """
    env: Dict[str, str] = {}
    if _text(base):
        env[CLAUDE_ENV_BASE_URL] = _text(base)
    if _text(key):
        env[CLAUDE_ENV_AUTH_TOKEN] = _text(key)
        env[CLAUDE_ENV_API_KEY] = _text(key)
    if _text(model):
        env[CLAUDE_ENV_MODEL] = _text(model)
    if _text(small_model):
        env[CLAUDE_ENV_SMALL_MODEL] = _text(small_model)
    for name, value in (extra_env or {}).items():
        env[str(name)] = "" if value is None else str(value)
    return {"env": env}


def _toml_string(value: Any) -> str:
    """TOML 基本字符串转义（仓库运行依赖只有 requests/PyYAML，手写即可）。"""
    s = "" if value is None else str(value)
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return '"' + "".join(ch if (ch >= " " and ch != "\x7f") else "\\u%04x" % ord(ch)
                         for ch in s) + '"'


def codex_config_toml(base: Any, key: str = "", model: str = "",
                      provider: str = CODEX_PROVIDER_TABLE,
                      extra_table: Optional[Dict[str, Any]] = None) -> str:
    """生成 Codex config.toml 片段（~/.codex/config.toml 的供应商部分）。

    只写属于这个供应商的键（其余是用户自己的全局设置）：
      model_provider / model + [model_providers.custom] 的
      name / base_url / wire_api / experimental_bearer_token。
    wire_api 固定 "responses"（Codex 当前唯一支持的 wire；chat 已移除）。
    """
    table = _text(provider) or CODEX_PROVIDER_TABLE
    lines: List[str] = []
    lines.append(f"{CODEX_TOP_PROVIDER} = {_toml_string(table)}")
    if _text(model):
        lines.append(f"{CODEX_TOP_MODEL} = {_toml_string(model)}")
    lines.append("")
    lines.append(f"[model_providers.{table}]")
    lines.append(f"name = {_toml_string(table)}")
    if _text(base):
        lines.append(f"base_url = {_toml_string(base)}")
    lines.append(f"wire_api = {_toml_string(CODEX_WIRE_API)}")
    if _text(key):
        lines.append(f"experimental_bearer_token = {_toml_string(key)}")
    for name, value in (extra_table or {}).items():
        lines.append(f"{name} = {_toml_string(value)}")
    return "\n".join(lines) + "\n"


def codex_provider_config(base: Any, key: str = "", model: str = "",
                          provider: str = CODEX_PROVIDER_TABLE) -> Dict[str, Any]:
    """Codex 供应商配置（configFormat=json 时的 {"auth": ..., "config": ...} 形式）。

    默认链接用 configFormat=toml 直接带 toml 原文；这里留着给需要 auth.json
    写法的场景（以及测试断言结构）使用。
    """
    toml_text = codex_config_toml(base, key=key, model=model, provider=provider)
    payload: Dict[str, Any] = {"config": toml_text}
    if _text(key):
        payload["auth"] = {"OPENAI_API_KEY": _text(key)}
    return payload


def provider_config_for_app(app: str, base: Any, key: str = "", model: str = "",
                            small_model: str = "") -> Tuple[str, str, Dict[str, Any]]:
    """返回 (config 原文, configFormat, 结构化配置对象)。"""
    if app == "claude":
        payload = claude_provider_config(base, key=key, model=model, small_model=small_model)
        return json.dumps(payload, ensure_ascii=False, indent=2), CONFIG_FORMAT_JSON, payload
    if app == "codex":
        toml_text = codex_config_toml(codex_endpoint(base), key=key, model=model)
        payload = codex_provider_config(codex_endpoint(base), key=key, model=model)
        return toml_text, CONFIG_FORMAT_TOML, payload
    raise ConfigError(f"不支持的 app: {app!r}")


# ---------------------------------------------------------------------------
# 单条 Key -> CC Switch 导出条目
# ---------------------------------------------------------------------------
def ccswitch_entry(item: Dict[str, Any], app: str, *, reveal: bool = False,
                   name: str = "", model: str = "", small_model: str = "",
                   notes: str = "", homepage: str = "",
                   enabled: Optional[bool] = None) -> Dict[str, Any]:
    """把一条 dist 记录变成 CC Switch 导出条目（含深链与供应商配置）。

    reveal=False（默认）：链接与配置里只放脱敏 Key（`sk-xx...xxxx`），
    保证预览/日志/stdout 永不泄露明文。
    """
    if app not in CCSWITCH_APPS:
        raise ConfigError(f"导出只支持 app={'/'.join(CCSWITCH_APPS)}，收到 {app!r}")
    key = _text(item.get("key"))
    shown_key = key if reveal else mask_key(key)
    base = _text(item.get("base"))
    endpoint = endpoint_for_app(app, base)
    if not endpoint:
        raise ConfigError(f"记录 {item.get('id')} 缺少 base，无法生成 {app} 端点")
    model_name = _text(model) or (_list(item.get("models")) or [""])[0]
    display = _text(name) or _text(item.get("source_site")) or _text(item.get("id")) or "relayctl"
    config_text, config_format, payload = provider_config_for_app(
        app, base, key=shown_key, model=model_name, small_model=small_model)

    link = build_ccswitch_link(
        app=app,
        name=display,
        endpoint=endpoint,
        api_key=shown_key,
        model=model_name,
        api_format=CCSWITCH_DEFAULT_API_FORMAT[app],
        notes=_text(notes) or _text(item.get("notes")),
        homepage=homepage,
        config=config_text,
        config_format=config_format,
        enabled=enabled,
    )
    return {
        "id": _text(item.get("id")),
        "key_masked": mask_key(key),
        "app": app,
        "api_format": CCSWITCH_DEFAULT_API_FORMAT[app],
        "name": display,
        "endpoint": endpoint,
        "model": model_name,
        "config_format": config_format,
        "config": config_text,
        "provider_config": payload,
        "link": link,
        "reveal": bool(reveal),
    }


def ccswitch_export(items: Iterable[Dict[str, Any]], app: str = "all",
                    reveal: bool = False, **kwargs: Any) -> Dict[str, Any]:
    """批量生成 CC Switch 导出（app=all 时每个 Key 同时出 claude / codex 两份）。"""
    apps = CCSWITCH_APPS if app in ("all", "", None) else (app,)
    entries: List[Dict[str, Any]] = []
    for item in items:
        for target_app in apps:
            entries.append(ccswitch_entry(item, target_app, reveal=reveal, **kwargs))
    return {
        "target": "ccswitch",
        "apps": list(apps),
        "count": len(entries),
        "reveal": bool(reveal),
        "items": entries,
    }


# ---------------------------------------------------------------------------
# New API 渠道
# ---------------------------------------------------------------------------
def channel_payload(name: str, key: str, base_url: str = "", models: Any = "",
                    groups: Any = "", channel_type: Any = DEFAULT_CHANNEL_TYPE,
                    priority: Optional[int] = None,
                    weight: Optional[int] = None) -> Dict[str, Any]:
    """构造 channel 对象（POST/PUT 共用）。

    字段差异提醒：写入用 `groups`（数组），而列表/搜索接口返回的是 `group`（字符串）；
    `models` 是逗号分隔字符串，不是数组。
    """
    if not _text(name):
        raise ConfigError("渠道名不能为空")
    if not _text(key):
        raise ConfigError("渠道 Key 不能为空（绝不写入空 Key）")
    type_id = NEWAPI_CHANNEL_TYPES.get(str(channel_type).strip().lower(), channel_type)
    try:
        type_id = int(type_id)
    except (TypeError, ValueError):
        raise ConfigError(f"渠道 type 非法: {channel_type!r}")
    group_list = _list(groups) or [NEWAPI_DEFAULT_GROUP]
    payload: Dict[str, Any] = {
        "name": _text(name),
        "type": type_id,
        "key": _text(key),
        "models": ",".join(_list(models)),
        "groups": group_list,
    }
    if _text(base_url):
        payload["base_url"] = _text(base_url)
    if priority is not None:
        payload["priority"] = int(priority)
    if weight is not None:
        payload["weight"] = int(weight)
    return payload


class NewApiChannelClient:
    """New API 渠道管理客户端（管理员令牌 + New-Api-User）。

    transport 可注入（测试用 mock，不打网络）：
        transport(method, url, headers, body, timeout) -> 响应对象
    响应对象只需有 status_code / text / json()。
    """

    def __init__(self, base: str, token: str, user_id: Any = None,
                 transport: Any = None, timeout: float = 12.0,
                 proxy: Optional[str] = None):
        self.base = normalize_base(base)
        self.token = _text(token)
        self.user_id = "" if user_id is None else str(user_id).strip()
        self.timeout = timeout
        self.proxy = proxy
        self._transport = transport
        self._http = None
        if not self.base:
            raise ConfigError("New API 站点地址为空")
        if not self.token:
            raise ConfigError("缺少站点管理员令牌（auth.access_token / access_token_env）")

    # -- 请求 ---------------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        if self.user_id:
            headers["New-Api-User"] = self.user_id
        return headers

    def _secrets(self) -> List[str]:
        return [self.token] if len(self.token) >= 4 else []

    def _http_request(self, method: str, path: str, body: Optional[dict]):
        from .core import HttpClient
        if self._http is None:
            self._http = HttpClient(self.base, proxy=self.proxy,
                                    auth={"access_token": self.token, "user_id": self.user_id})
        kwargs: Dict[str, Any] = {"timeout": self.timeout}
        if body is not None:
            kwargs["json"] = body
        return self._http.request(method, path, **kwargs)

    def _interpret(self, resp: Any) -> Dict[str, Any]:
        status = getattr(resp, "status_code", None)
        try:
            text = resp.text or ""
        except Exception:  # pragma: no cover - 异常响应对象
            text = ""
        safe_text = scrub_text(text[:400], self._secrets())
        if status == 401:
            raise AuthError(f"HTTP 401 鉴权失败: {safe_text}")
        if status is None:
            raise ApiError("上游无响应（未拿到 HTTP 状态码）")
        try:
            data = resp.json()
        except Exception:
            raise ApiError(f"HTTP {status} 响应不是 JSON: {safe_text}")
        if status != 200:
            raise ApiError(f"HTTP {status}: {safe_text}")
        if not isinstance(data, dict):
            raise ApiError(f"响应不是 JSON 对象: {safe_text}")
        if data.get("success") is not True:
            # HTTP 200 但 success=false：New API 明确把它算失败
            raise ApiError(scrub_text(_text(data.get("message")) or "上游返回 success=false",
                                      self._secrets()))
        return data

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Dict[str, Any]:
        if self._transport is not None:
            resp = self._transport(method, f"{self.base}{path}", self._headers(), body, self.timeout)
        else:  # pragma: no cover - 真实网络路径不在单测里执行
            resp = self._http_request(method, path, body)
        return self._interpret(resp)

    # -- 渠道 ---------------------------------------------------------------
    def create_channel(self, channel: Dict[str, Any], mode: str = "single") -> Dict[str, Any]:
        """POST /api/channel/：body = {"mode": ..., "channel": {...}}。"""
        return self._request("POST", NEWAPI_CHANNEL_PATH, {"mode": mode, "channel": channel})

    def update_channel(self, channel_id: Any, channel: Dict[str, Any]) -> Dict[str, Any]:
        """PUT /api/channel/：body = {...,"id": <渠道 ID>}。"""
        payload = dict(channel)
        payload["id"] = channel_id
        return self._request("PUT", NEWAPI_CHANNEL_PATH, payload)

    def search_channels(self, keyword: str = "",
                        page_size: int = NEWAPI_SEARCH_PAGE_SIZE) -> List[Dict[str, Any]]:
        """GET /api/channel/search?keyword=...：返回 items（不含密钥明文）。"""
        path = (f"{NEWAPI_CHANNEL_SEARCH_PATH}?keyword={quote(_text(keyword))}"
                f"&page_size={int(page_size)}")
        data = self._request("GET", path)
        container = data.get("data") or {}
        items = container.get("items") if isinstance(container, dict) else container
        return [it for it in (items or []) if isinstance(it, dict)]

    def find_channel_id(self, name: str) -> Optional[int]:
        """按**精确名字**回查渠道 id：0 个返回 None，多个直接报错（绝不猜测）。"""
        matches = [it for it in self.search_channels(name) if _text(it.get("name")) == _text(name)]
        if len(matches) > 1:
            ids = ", ".join(str(it.get("id")) for it in matches)
            raise AmbiguousChannel(
                f"按名字 {name!r} 回查到 {len(matches)} 个渠道（id: {ids}），拒绝猜测，请人工确认")
        if not matches:
            return None
        try:
            return int(matches[0].get("id"))
        except (TypeError, ValueError):
            return None

    def push_channel(self, name: str, channel: Dict[str, Any],
                     channel_id: Optional[int] = None,
                     dry_run: bool = True) -> Dict[str, Any]:
        """创建或更新渠道，并在写入后按名字回查 id。

        dry_run=True：**完全不发网络请求**（连回查都不做），因此无法判定
        是新建还是更新，只给出预演结论。
        """
        outcome: Dict[str, Any] = {
            "name": name,
            "dry_run": bool(dry_run),
            "action": "",
            "channel_id": channel_id,
            "ok": False,
            "message": "",
        }
        if dry_run:
            outcome["action"] = "update" if channel_id is not None else "create"
            outcome["ok"] = True
            outcome["message"] = "试运行未写入（不查询上游）"
            return outcome
        resolved_id = channel_id if channel_id is not None else self.find_channel_id(name)
        outcome["action"] = "update" if resolved_id is not None else "create"
        try:
            if resolved_id is not None:
                self.update_channel(resolved_id, channel)
                outcome["channel_id"] = resolved_id
            else:
                self.create_channel(channel)
                # 创建后按名字回查 id（同名多个 -> 报错，绝不猜）
                outcome["channel_id"] = self.find_channel_id(name)
        except RelayctlError as exc:
            outcome["message"] = scrub_text(str(exc), [channel.get("key", "")])
            raise
        outcome["ok"] = True
        return outcome


def newapi_export(items: Iterable[Dict[str, Any]], site_base: str, token: str,
                  user_id: Any = None, dry_run: bool = True, transport: Any = None,
                  channel_type: Any = DEFAULT_CHANNEL_TYPE,
                  name_prefix: str = "relayctl", timeout: float = 12.0,
                  proxy: Optional[str] = None) -> Dict[str, Any]:
    """把 dist 记录推送为 New API 渠道（默认 dry-run，实际写入需上层显式放行）。

    绝不打印完整 Key / 令牌：结果里只有 id 与 key_masked。
    """
    client = NewApiChannelClient(site_base, token, user_id=user_id, transport=transport,
                                 timeout=timeout, proxy=proxy)
    results: List[Dict[str, Any]] = []
    failed = 0
    for item in items:
        key = _text(item.get("key"))
        name = f"{name_prefix}-{_text(item.get('id'))}" if name_prefix else _text(item.get("id"))
        entry: Dict[str, Any] = {
            "id": _text(item.get("id")),
            "key_masked": mask_key(key),
            "channel_name": name,
            "ok": False,
            "action": "",
            "channel_id": None,
            "message": "",
        }
        try:
            payload = channel_payload(
                name, key, base_url=codex_endpoint(item.get("base")),
                models=item.get("models"), groups=item.get("groups"),
                channel_type=channel_type)
            outcome = client.push_channel(name, payload, dry_run=dry_run)
            entry.update({k: outcome.get(k) for k in ("ok", "action", "channel_id", "message")})
        except RelayctlError as exc:
            failed += 1
            entry["message"] = scrub_text(str(exc), [key])
        results.append(entry)
    return {
        "target": "newapi",
        "site_base": client.base,
        "dry_run": bool(dry_run),
        "count": len(results),
        "failed": failed,
        "items": results,
    }


# ---------------------------------------------------------------------------
# env / json / csv 通用导出
# ---------------------------------------------------------------------------
def _env_var(suffix: str, ident: str) -> str:
    safe = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in _text(ident)).upper()
    return f"RELAYCTL_{safe}_{suffix}" if safe else f"RELAYCTL_{suffix}"


def env_export(items: Iterable[Dict[str, Any]], reveal: bool = False) -> str:
    """生成可直接 source 的 env 文本（明文只出现在 0600 文件或 --reveal 时）。"""
    lines = ["# relayctl dist export (target=env)",
             "# 含凭据，文件权限 0600；明文 Key 仅在 --out 或 --reveal 时出现", ""]
    for item in items:
        key = _text(item.get("key"))
        shown = key if reveal else mask_key(key)
        ident = _text(item.get("id"))
        models = _list(item.get("models"))
        lines.append(f"# id={ident} site={_text(item.get('source_site')) or '-'} "
                     f"tags={','.join(_list(item.get('tags'))) or '-'}")
        lines.append(f"export {_env_var('BASE', ident)}={shlex.quote(_text(item.get('base')))}")
        lines.append(f"export {_env_var('KEY', ident)}={shlex.quote(shown)}")
        lines.append(f"export {_env_var('MODEL', ident)}={shlex.quote(models[0] if models else '')}")
        lines.append("")
    return "\n".join(lines)


def json_export(items: Iterable[Dict[str, Any]], reveal: bool = False) -> str:
    """生成 JSON 导出（结构化、便于二次加工）。"""
    out: List[Dict[str, Any]] = []
    for item in items:
        record = importers.key_out(item)
        if reveal:
            record["key"] = _text(item.get("key"))
        out.append(record)
    return json.dumps({"target": "json", "count": len(out), "reveal": bool(reveal),
                       "items": out}, ensure_ascii=False, indent=2) + "\n"


CSV_FIELDS = ("id", "key_masked", "source_site", "base", "tags", "models",
              "groups", "assigned_to", "status")


def csv_export(items: Iterable[Dict[str, Any]], reveal: bool = False) -> str:
    """生成 CSV 导出（reveal 时额外带一列明文 key）。"""
    buf = io.StringIO(newline="")
    header = list(CSV_FIELDS) + (["key"] if reveal else [])
    writer = _csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    for item in items:
        row = [
            _text(item.get("id")),
            mask_key(item.get("key")),
            _text(item.get("source_site")),
            _text(item.get("base")),
            ",".join(_list(item.get("tags"))),
            ",".join(_list(item.get("models"))),
            ",".join(_list(item.get("groups"))),
            _text(item.get("assigned_to")),
            _text(item.get("status")),
        ]
        if reveal:
            row.append(_text(item.get("key")))
        writer.writerow(row)
    return buf.getvalue()


def generic_export(target: str, items: Iterable[Dict[str, Any]], reveal: bool = False) -> str:
    if target == "env":
        return env_export(items, reveal=reveal)
    if target == "json":
        return json_export(items, reveal=reveal)
    if target == "csv":
        return csv_export(items, reveal=reveal)
    raise ConfigError(f"不支持的导出目标: {target!r}")
