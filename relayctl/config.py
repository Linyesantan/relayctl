"""v2 配置层：全局 config.yaml + 每站 sites/<name>.yaml。

设计要点：
- RELAYCTL_HOME 动态解析（paths.home()），不在 import 期写死任何路径；
- 每站一个文件，隔离加载：单文件损坏只影响该站，其余站点照常工作；
- 站点配置「读可兼容、写必完整」：允许为旧站点补全 docs 等默认值并产生 warnings，
  但任何新写出的配置都必须字段完整；
- auth.access_token_env 在运行时解析为 token，明文绝不回写配置文件。
"""

import copy
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import yaml

from . import paths
from .errors import ConfigError
from .models import strip_internal

# ---------------------------------------------------------------------------
# 全局默认配置
# ---------------------------------------------------------------------------
DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 2,
    # 全局默认代理策略: none(直连) / auto(跟随系统环境变量) / http://host:port
    "proxy": "none",
    # 默认刷新间隔（s/m/h/d，如 30m / 1h / 1d）；站点级 refresh_interval 可覆盖
    "refresh_interval": "30m",
    "cache": {
        "enabled": True,
        "mode": "auto",      # auto: 有缓存就用；refresh: 强制实时；cached: 只读缓存
        "ttl": "30m",
        # 实时拉取失败且存在旧缓存时，返回旧数据并标记 stale（无旧缓存才报错）
        "serve_stale": True,
    },
    "scheduler": {
        # 全局并发 worker 数（每站同一时刻仍最多 1 个刷新任务）
        "max_workers": 4,
        # 成功后的下一次间隔抖动比例（±jitter）
        "jitter": 0.1,
        # 失败退避初始值 / 倍数 / 上限（秒）
        "backoff_initial": 60,
        "backoff_factor": 2,
        "backoff_max": 1800,
    },
    "web": {
        "host": "127.0.0.1",
        "port": 8088,
        "access_password": "",   # 空 = 不启用鉴权（完整 Web 鉴权在阶段 6）
        # 是否在 run_web 内一并拉起后台刷新调度线程
        "scheduler_enabled": True,
    },
}

GLOBAL_KEYS = ("version", "proxy", "refresh_interval", "cache", "scheduler", "web")

# ---------------------------------------------------------------------------
# 站点默认配置
# ---------------------------------------------------------------------------
STATUS_ACTIVE = "active"
STATUS_PARKED = "parked"
VALID_STATUS = (STATUS_ACTIVE, STATUS_PARKED)

TIER_BY_STATUS = {STATUS_ACTIVE: "api", STATUS_PARKED: "parked"}

DEFAULT_DOCS: Dict[str, str] = {
    "login_method": "",   # 登录 / 进站方式
    "token_howto": "",    # 长期令牌获取路径
    "script_howto": "",   # 脚本使用说明
    "notes": "",          # 其它排障备注
}
DOCS_FIELDS = tuple(DEFAULT_DOCS.keys())

DEFAULT_AUTH: Dict[str, Any] = {
    "type": "access_token",  # access_token / cookie / password / none
    "access_token": "",      # 明文令牌（不建议，优先用 access_token_env）
    "access_token_env": "",  # 从环境变量读取令牌，明文不落盘
    "user_id": None,
    "username": "",
    "password": "",
    "password_env": "",
    "cookie": "",
}

VALID_AUTH_TYPES = ("access_token", "cookie", "password", "none")
VALID_PANELS = ("newapi", "oneapi", "custom")
VALID_ADAPTERS = ("newapi", "oneapi", "script")

SITE_DEFAULTS: Dict[str, Any] = {
    "name": None,
    "base": "",
    "console_url": "",
    "panel": "newapi",       # 面板类型（New API / One API / 自研面板 ...）
    "adapter": "newapi",     # 适配器标识
    "script": None,          # 站点脚本名（阶段 3）
    "auth": None,            # 见 DEFAULT_AUTH
    "docs": None,            # 见 DEFAULT_DOCS
    "proxy": "",             # 空 = 继承全局
    "refresh_interval": "",  # 空 = 继承全局
    "tags": [],
    "status": STATUS_ACTIVE,
    "parked_reason": "",
}

SITE_FIELDS = (
    "name", "base", "console_url", "panel", "adapter", "script", "auth",
    "docs", "proxy", "refresh_interval", "tags", "status", "parked_reason",
)

_INTERVAL_RE = re.compile(r"^\d+[smhd]?$")
_PROXY_MODES = ("none", "auto", "direct")


class SiteLoadError:
    """单个站点加载失败的详情（不影响其它站点）。"""

    def __init__(self, name: str, path: str, error: BaseException):
        self.name = name
        self.path = path
        self.error = error

    @property
    def message(self) -> str:
        return f"{self.name}: {self.error}"

    def as_dict(self) -> Dict[str, str]:
        return {"name": self.name, "path": self.path, "error": str(self.error)}

    def __repr__(self):  # pragma: no cover - 调试用
        return f"<SiteLoadError {self.name} {self.error!r}>"


# ---------------------------------------------------------------------------
# 全局配置
# ---------------------------------------------------------------------------
def _merge_defaults(raw: Dict[str, Any]) -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    for key in GLOBAL_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(cfg.get(key), dict) and isinstance(value, dict):
            merged = copy.deepcopy(cfg[key])
            merged.update(value)
            cfg[key] = merged
        elif value is not None:
            cfg[key] = value
    return cfg


def load_global() -> Dict[str, Any]:
    """读取全局 config.yaml（缺失则回落到默认配置，不写盘）。"""
    path = paths.config_path()
    if not os.path.exists(path):
        return copy.deepcopy(DEFAULT_CONFIG)
    raw = paths.read_yaml(path, default=None)
    if raw is None:
        return copy.deepcopy(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        raise ConfigError(f"全局配置格式非法（应为映射）: {path}")
    paths.ensure_file_mode(path)
    return _merge_defaults(raw)


def save_global(cfg: Optional[Dict[str, Any]] = None) -> str:
    """写回全局 config.yaml（仅保留已知全局键，0600 原子写）。"""
    data = _merge_defaults(cfg or {})
    path = paths.config_path()
    paths.atomic_write_yaml(path, data)
    return path


def init_layout() -> Dict[str, str]:
    """创建 v2 运行时目录（0700）并在缺失时生成默认 config.yaml。"""
    dirs = paths.init_dirs()
    if not os.path.exists(paths.config_path()):
        save_global(DEFAULT_CONFIG)
    return dirs


# ---------------------------------------------------------------------------
# 站点配置：规范化 / 校验
# ---------------------------------------------------------------------------
def _warn(warnings: List[str], text: str):
    warnings.append(text)


def _normalize_auth(raw: Any, warnings: List[str], strict: bool) -> Dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("auth 必须是映射")
    auth = copy.deepcopy(DEFAULT_AUTH)
    for key, value in raw.items():
        if key not in DEFAULT_AUTH:
            continue
        auth[key] = value

    auth_type = auth.get("type") or "access_token"
    auth_type = str(auth_type).strip().lower()
    legacy_type = {"bearer": "access_token", "apikey": "access_token"}.get(auth_type)
    if legacy_type:
        _warn(warnings, f"auth.type={auth_type} 是旧值，已迁移为 {legacy_type}")
        auth_type = legacy_type
    if auth_type not in VALID_AUTH_TYPES:
        msg = f"auth.type 非法: {auth_type!r}（可选 {', '.join(VALID_AUTH_TYPES)}）"
        if strict:
            raise ConfigError(msg)
        _warn(warnings, msg + "，已回退为 access_token")
        auth_type = "access_token"
    auth["type"] = auth_type

    for field in ("access_token", "access_token_env", "username", "password", "password_env", "cookie"):
        value = auth.get(field)
        if value is None:
            auth[field] = ""
        elif not isinstance(value, str):
            msg = f"auth.{field} 必须是字符串"
            if strict:
                raise ConfigError(msg)
            _warn(warnings, msg + "，已置空")
            auth[field] = ""

    uid = auth.get("user_id")
    if uid in (None, ""):
        auth["user_id"] = None
    elif isinstance(uid, bool) or not isinstance(uid, int):
        try:
            auth["user_id"] = int(str(uid).strip())
        except (TypeError, ValueError):
            msg = f"auth.user_id 必须是整数，得到 {uid!r}"
            if strict:
                raise ConfigError(msg)
            _warn(warnings, msg + "，已置空")
            auth["user_id"] = None
    return auth


def _normalize_docs(raw: Any, warnings: List[str], strict: bool) -> Dict[str, str]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("docs 必须是映射")
    docs = {}
    for field in DOCS_FIELDS:
        value = raw.get(field)
        if value is None:
            if strict:
                raise ConfigError(f"docs.{field} 缺失（新写配置必须包含 docs 四个字段）")
            _warn(warnings, f"docs.{field} 缺失，已补默认值")
            docs[field] = ""
        elif isinstance(value, str):
            docs[field] = value
        else:
            msg = f"docs.{field} 必须是字符串"
            if strict:
                raise ConfigError(msg)
            _warn(warnings, msg + "，已转为字符串")
            docs[field] = str(value)
    return docs


def _normalize_tags(raw: Any, warnings: List[str], strict: bool) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    if isinstance(raw, (list, tuple)):
        out = []
        for t in raw:
            if isinstance(t, (str, int, float)):
                out.append(str(t))
            elif strict:
                raise ConfigError(f"tags 元素必须是标量，得到 {t!r}")
            else:
                _warn(warnings, f"忽略非法 tag: {t!r}")
        return out
    msg = "tags 必须是列表或逗号分隔字符串"
    if strict:
        raise ConfigError(msg)
    _warn(warnings, msg + "，已置空")
    return []


def _check_base(base: Any, strict: bool, warnings: List[str]) -> str:
    if base is None:
        base = ""
    if not isinstance(base, str):
        raise ConfigError("base 必须是字符串")
    base = base.strip()
    if not base:
        msg = "base 不能为空"
        if strict:
            raise ConfigError(msg)
        _warn(warnings, msg)
        return ""
    if not base.startswith(("http://", "https://")):
        msg = f"base 必须是 http(s) URL，得到 {base!r}"
        if strict:
            raise ConfigError(msg)
        _warn(warnings, msg)
    return base


def _check_proxy(value: Any, strict: bool, warnings: List[str]) -> str:
    """站点级 proxy：空字符串表示继承全局。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError("proxy 必须是字符串（none / auto / http://host:port）")
    value = value.strip()
    if not value or value.lower() in _PROXY_MODES:
        return value
    if value.startswith(("http://", "https://", "socks5://", "socks5h://")):
        return value
    msg = f"proxy 非法: {value!r}（none / auto / http://host:port）"
    if strict:
        raise ConfigError(msg)
    _warn(warnings, msg + "，已置空")
    return ""


def _check_interval(value: Any, strict: bool, warnings: List[str]) -> str:
    """站点级 refresh_interval：空字符串表示继承全局。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError("refresh_interval 必须是字符串（如 30m / 1h）")
    value = value.strip()
    if not value:
        return ""
    if not _INTERVAL_RE.match(value):
        msg = f"refresh_interval 非法: {value!r}（如 30s / 30m / 2h / 1d）"
        if strict:
            raise ConfigError(msg)
        _warn(warnings, msg + "，已置空")
        return ""
    return value


def normalize_site(raw: Any, name: Optional[str] = None, strict: bool = False
                   ) -> Tuple[Dict[str, Any], List[str]]:
    """把原始站点配置规范化为完整结构。

    strict=True 用于「写盘前」校验：任何缺失/非法字段都直接报错。
    strict=False 用于「读取兼容」：尽可能补全默认值并记录 warnings。
    """
    if not isinstance(raw, dict):
        raise ConfigError("站点配置必须是映射")
    warnings: List[str] = []
    cfg = dict(raw)

    raw_name = cfg.get("name") or name
    if not raw_name:
        raise ConfigError("站点缺少 name 字段")
    cfg["name"] = paths.safe_site_name(raw_name)

    cfg["base"] = _check_base(cfg.get("base"), strict, warnings)

    console_url = cfg.get("console_url")
    if console_url is None:
        console_url = ""
    if not isinstance(console_url, str):
        raise ConfigError("console_url 必须是字符串")
    cfg["console_url"] = console_url.strip()

    adapter = cfg.get("adapter")
    if not adapter:
        if strict:
            raise ConfigError("adapter 不能为空")
        _warn(warnings, "adapter 缺失，已默认 newapi")
        adapter = "newapi"
    if not isinstance(adapter, str):
        raise ConfigError("adapter 必须是字符串")
    adapter = adapter.strip().lower()
    if adapter not in VALID_ADAPTERS:
        msg = f"adapter 非法: {adapter!r}（可选 {', '.join(VALID_ADAPTERS)}）"
        if strict:
            raise ConfigError(msg)
        _warn(warnings, msg + "，已回退为 newapi")
        adapter = "newapi"
    cfg["adapter"] = adapter

    panel = cfg.get("panel")
    if not panel:
        if strict:
            raise ConfigError("panel 不能为空")
        _warn(warnings, "panel 缺失，已默认取 adapter 值")
        panel = cfg["adapter"] if cfg["adapter"] in ("newapi", "oneapi") else "custom"
    if not isinstance(panel, str):
        raise ConfigError("panel 必须是字符串")
    panel = panel.strip().lower()
    if panel not in VALID_PANELS:
        msg = f"panel 非法: {panel!r}（可选 {', '.join(VALID_PANELS)}）"
        if strict:
            raise ConfigError(msg)
        _warn(warnings, msg + "，已回退为 custom")
        panel = "custom"
    cfg["panel"] = panel

    script = cfg.get("script")
    if script is not None and not isinstance(script, str):
        raise ConfigError("script 必须是字符串或空")
    cfg["script"] = script.strip() if isinstance(script, str) else None
    if cfg["adapter"] == "script" and not cfg["script"]:
        msg = "adapter=script 时必须提供 script"
        if strict:
            raise ConfigError(msg)
        _warn(warnings, msg)

    cfg["auth"] = _normalize_auth(cfg.get("auth"), warnings, strict)
    cfg["docs"] = _normalize_docs(cfg.get("docs"), warnings, strict)
    cfg["tags"] = _normalize_tags(cfg.get("tags"), warnings, strict)
    cfg["proxy"] = _check_proxy(cfg.get("proxy"), strict, warnings)
    cfg["refresh_interval"] = _check_interval(cfg.get("refresh_interval"), strict, warnings)

    # status：缺失时兼容旧字段 tier（api -> active, parked -> parked）
    status = cfg.get("status")
    if status is None or str(status).strip() == "":
        tier = str(cfg.get("tier") or "").strip().lower()
        status = STATUS_PARKED if tier == "parked" else STATUS_ACTIVE
        _warn(warnings, "status 缺失，已由 tier 推导")
    status = str(status).strip().lower()
    if status not in VALID_STATUS:
        msg = f"status 非法: {status!r}（可选 {STATUS_ACTIVE} / {STATUS_PARKED}）"
        if strict:
            raise ConfigError(msg)
        _warn(warnings, msg + f"，已回退为 {STATUS_PARKED}")
        status = STATUS_PARKED
    cfg["status"] = status

    parked_reason = cfg.get("parked_reason")
    if parked_reason is None:
        parked_reason = ""
    if not isinstance(parked_reason, str):
        raise ConfigError("parked_reason 必须是字符串")
    cfg["parked_reason"] = parked_reason

    # 兼容旧适配器：由 status 映射出旧 tier
    cfg["tier"] = TIER_BY_STATUS.get(cfg["status"], "parked")

    cfg["_warnings"] = warnings
    return cfg, warnings


def validate_site(cfg: Dict[str, Any], name: Optional[str] = None
                  ) -> Tuple[Dict[str, Any], List[str]]:
    """严格校验（写盘前调用）。"""
    return normalize_site(cfg, name=name, strict=True)


# ---------------------------------------------------------------------------
# 运行时凭据解析
# ---------------------------------------------------------------------------
def resolve_auth(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """返回一份运行时副本：access_token_env -> access_token。

    解析结果只存在于内存，save_site 会把 env 来源的明文清除后再落盘。
    """
    out = copy.deepcopy(cfg)
    auth = out.get("auth") or {}
    token_env = (auth.get("access_token_env") or "").strip()
    if token_env:
        token = os.environ.get(token_env)
        if token:
            auth["access_token"] = token.strip()
            auth["_resolved_from_env"] = True
            auth["_token_source"] = f"env:{token_env}"
        else:
            auth["access_token"] = auth.get("access_token") or ""
            auth["_token_source"] = f"env:{token_env}(missing)"
    else:
        auth["_token_source"] = "config" if auth.get("access_token") else "none"

    password_env = (auth.get("password_env") or "").strip()
    if password_env:
        password = os.environ.get(password_env)
        if password:
            auth["password"] = password
            auth["_password_resolved_from_env"] = True
            auth["_password_source"] = f"env:{password_env}"
        else:
            auth["password"] = auth.get("password") or ""
            auth["_password_source"] = f"env:{password_env}(missing)"
    else:
        auth["_password_source"] = "config" if auth.get("password") else "none"
    out["auth"] = auth
    return out


def _strip_resolved_token(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """落盘前清除由环境变量解析出的明文 token。"""
    auth = cfg.get("auth")
    if isinstance(auth, dict):
        auth = dict(auth)
        if auth.get("access_token_env") or auth.get("_resolved_from_env"):
            auth["access_token"] = ""
        if auth.get("password_env") or auth.get("_password_resolved_from_env"):
            auth["password"] = ""
        cfg = dict(cfg)
        cfg["auth"] = auth
    return cfg


# ---------------------------------------------------------------------------
# 站点读取 / 写入
# ---------------------------------------------------------------------------
def load_site(name: str, resolve: bool = False, default: Any = None) -> Any:
    """加载单个站点配置；不存在或解析失败抛 ConfigError（除指定 default）。"""
    site_name = paths.safe_site_name(name)
    path = paths.site_path(site_name)
    try:
        raw = paths.read_yaml(path, default=None)
    except ConfigError:
        if default is not None:
            return default
        raise
    if raw is None:
        if default is not None:
            return default
        raise ConfigError(f"站点 {site_name} 未登记（{path} 不存在）")
    cfg, _ = normalize_site(raw, name=site_name)
    cfg["_path"] = path
    if resolve:
        cfg = resolve_auth(cfg)
    return cfg


def site_exists(name: str) -> bool:
    try:
        return os.path.exists(paths.site_path(name))
    except ConfigError:
        return False


def save_site(name: str, cfg: Dict[str, Any]) -> str:
    """严格校验后原子写出 sites/<name>.yaml（0600）。"""
    site_name = paths.safe_site_name(name)
    if cfg.get("name") and paths.safe_site_name(cfg.get("name")) != site_name:
        raise ConfigError(f"站点名不一致: 文件名 {site_name} != 配置 name {cfg.get('name')}")
    normalized, _ = normalize_site(cfg, name=site_name, strict=True)
    normalized["name"] = site_name
    payload = strip_internal(_strip_resolved_token(normalized))
    ordered = {key: payload[key] for key in SITE_FIELDS if key in payload}
    for key, value in payload.items():
        if key not in ordered:
            ordered[key] = value
    path = paths.site_path(site_name)
    paths.atomic_write_yaml(path, ordered)
    return path


def delete_site(name: str) -> bool:
    """删除单个站点配置文件（不影响其它站点）。"""
    path = paths.site_path(name)
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    return True


def list_site_names() -> List[str]:
    """列出 sites/ 下所有站点名（坏文件也照常列出，由 load_sites 报错）。"""
    directory = paths.sites_dir()
    if not os.path.isdir(directory):
        return []
    names = []
    for fn in sorted(os.listdir(directory)):
        if fn.endswith(".yaml") or fn.endswith(".yml"):
            names.append(fn.rsplit(".", 1)[0])
    return names


def load_sites(resolve: bool = True) -> Tuple[Dict[str, Dict[str, Any]], List[SiteLoadError]]:
    """加载全部站点：正常站点进 dict，失败站点逐个记录，互不牵连。"""
    sites: Dict[str, Dict[str, Any]] = {}
    errors: List[SiteLoadError] = []
    for name in list_site_names():
        try:
            site = load_site(name, resolve=resolve)
        except Exception as exc:  # noqa: BLE001 - 隔离单站故障
            errors.append(SiteLoadError(name, _safe_path(name), exc))
            continue
        sites[site["name"]] = site
    return sites, errors


def _safe_path(name: str) -> str:
    try:
        return paths.site_path(name)
    except ConfigError:
        return os.path.join(paths.sites_dir(), f"{name}.yaml")


# ---------------------------------------------------------------------------
# 聚合入口（兼容旧调用方: cfg["sites"]）
# ---------------------------------------------------------------------------
def load_config(resolve: bool = True, init: bool = True) -> Dict[str, Any]:
    """返回旧调用方可用的 {sites: {...}}，并附带全局配置与逐站错误。"""
    if init:
        init_layout()
    cfg = load_global()
    sites, errors = load_sites(resolve=resolve)
    cfg["sites"] = sites
    cfg["site_errors"] = [e.as_dict() for e in errors]
    return cfg


def save_config(cfg: Dict[str, Any]) -> Dict[str, str]:
    """拆分保存：全局键 -> config.yaml，每个站点 -> sites/<name>.yaml。"""
    written: Dict[str, str] = {}
    global_part = {k: v for k, v in cfg.items() if k in GLOBAL_KEYS}
    if global_part:
        written["config"] = save_global(global_part)
    for name, site in (cfg.get("sites") or {}).items():
        written[f"site:{name}"] = save_site(name, site)
    return written


def site_warnings(cfg: Dict[str, Any]) -> List[str]:
    return list(cfg.get("_warnings") or [])


def dump_site_yaml(cfg: Dict[str, Any]) -> str:
    """导出站点配置 YAML（脱敏：不输出任何明文凭据）。"""
    payload = strip_internal(_strip_resolved_token(copy.deepcopy(cfg)))
    if isinstance(payload.get("auth"), dict):
        token = payload["auth"].get("access_token")
        payload["auth"]["access_token"] = "***已设置***" if token else ""
        if payload["auth"].get("cookie"):
            payload["auth"]["cookie"] = "***已设置***"
        if payload["auth"].get("password"):
            payload["auth"]["password"] = "***已设置***"
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False)
