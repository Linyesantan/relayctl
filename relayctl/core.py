"""HTTP 客户端、安全会话与通用工具。

v2: 路径与安全写盘统一由 paths 模块提供；本模块保留旧名称（secure_write_json /
ensure_secure_dir / mask_key / AuthError / load_config / save_config）以兼容旧调用方。
"""

import re
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from . import paths
from .errors import AuthError, ConfigError, NotSupported  # noqa: F401 (兼容旧导入)
from .models import mask_key  # noqa: F401 (兼容旧导入)

USER_AGENT = "relayctl/2.0"

# 跨 origin 重定向时绝不允许带走的凭据头
SENSITIVE_HEADERS = ("authorization", "token", "x-api-key", "new-api-user", "cookie")

_DEFAULT_PORTS = {"http": 80, "https": 443}


def ensure_secure_dir(path: str):
    """创建目录并强制 0700。"""
    return paths.ensure_dir(path)


def secure_write_json(file_path: str, data):
    """原子写 JSON 并确保 600 权限（用于兼容旧调用方）。"""
    return paths.atomic_write_json(file_path, data)


def ensure_secure_file(path: str):
    return paths.ensure_file_mode(path)


def load_config(resolve: bool = True, init: bool = True) -> dict:
    """兼容包装：返回 {sites: {...}, ...}（v2 布局）。"""
    from .config import load_config as _load_config
    return _load_config(resolve=resolve, init=init)


def save_config(cfg: dict) -> dict:
    """兼容包装：全局键写 config.yaml，站点写 sites/<name>.yaml。"""
    from .config import save_config as _save_config
    return _save_config(cfg)


def _origin(parsed) -> tuple:
    """规范化 origin（默认端口归一化，避免 :443 与隐式 443 被判为不同源）。"""
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
    return (scheme, host, port)


def _has_sensitive_header(headers) -> bool:
    if not headers:
        return False
    try:
        keys = {str(k).lower() for k in headers.keys()}
    except Exception:  # pragma: no cover - 异常头部容器
        return True
    return any(name in keys for name in SENSITIVE_HEADERS)


class _SafeSession(requests.Session):
    """不要把管理认证头跟随跨 origin 重定向发送。

    任何携带 Authorization / Token / x-api-key / New-Api-User / Cookie 的请求，
    一旦被重定向到不同 origin（含 scheme、host、端口差异），直接拒绝，
    而不是把凭据发给第三方主机。
    """

    def rebuild_auth(self, prepared_request, response):
        old = urlparse(response.request.url)
        new = urlparse(prepared_request.url)
        if _origin(old) != _origin(new):
            # 原始请求或即将发出的请求只要带过凭据头，就拒绝跨 origin 重定向
            if (_has_sensitive_header(getattr(response.request, "headers", None))
                    or _has_sensitive_header(prepared_request.headers)):
                raise requests.exceptions.TooManyRedirects(
                    f"拒绝带认证请求的跨 origin 重定向: {response.request.url} -> {prepared_request.url}")
        super().rebuild_auth(prepared_request, response)


class HttpClient:
    """带有代理、超时与通用安全头的统一 HTTP 客户端"""

    def __init__(self, base_url: str, proxy: Optional[str] = None, auth: Optional[dict] = None):
        self.base_url = (base_url or "").rstrip("/")
        self.auth = auth or {}
        self.session = _SafeSession()
        self.proxy = self._apply_proxy(proxy)

    def _apply_proxy(self, proxy) -> Optional[str]:
        """proxy 必须是字符串或 None；非法类型报配置错误，绝不静默降级。"""
        if proxy is None:
            mode = "none"
        elif isinstance(proxy, str):
            mode = proxy.strip() or "none"
        else:
            raise ConfigError(
                f"proxy 必须是字符串（none / auto / http://host:port）或 None，"
                f"收到 {type(proxy).__name__}"
            )
        lowered = mode.lower()
        if lowered == "auto":
            # auto: 跟随系统环境变量代理（HTTP(S)_PROXY / ALL_PROXY），未设置则直连
            self.session.trust_env = True
            self.session.proxies = {}
        elif lowered in ("none", "direct"):
            # 强制直连，避免被系统代理干扰
            self.session.trust_env = False
            self.session.proxies = {}
        else:
            self.session.trust_env = False
            self.session.proxies = {"http": mode, "https": mode}
        return mode

    def get_headers(self, custom_headers: Optional[dict] = None) -> dict:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        }
        token = self.auth.get("access_token")
        uid = self.auth.get("user_id")
        cookie = self.auth.get("cookie")

        if token:
            if str(token).startswith("user-"):
                headers["x-api-key"] = token
            else:
                headers["Authorization"] = token if str(token).startswith("Bearer ") else f"Bearer {token}"
            # 同时保留两者兼容某些需要特定头的端点
            headers["Token"] = token
        if uid:
            headers["New-Api-User"] = str(uid)
        if cookie:
            headers["Cookie"] = cookie

        if custom_headers:
            headers.update(custom_headers)
        return headers

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        headers = self.get_headers(kwargs.pop("headers", None))
        timeout = kwargs.pop("timeout", 12)
        return self.session.request(method, url, headers=headers, timeout=timeout, **kwargs)

    def get(self, path: str, **kwargs) -> requests.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> requests.Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> requests.Response:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs) -> requests.Response:
        return self.request("DELETE", path, **kwargs)


_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def normalize_base(url: Any) -> str:
    """规范化站点地址：补 https://、去掉末尾 / 与 /v1（可叠加）。

    用于 import 的 base 归一，也是「按 base 匹配已登记站点」的比较基准。
    """
    if url is None:
        return ""
    s = str(url).strip().strip('"').strip("'").strip()
    if not s:
        return ""
    if not _SCHEME_RE.match(s):
        s = "https://" + s
    while True:
        stripped = s.rstrip("/")
        if stripped.lower().endswith("/v1"):
            stripped = stripped[: -len("/v1")]
        if stripped == s:
            return s
        s = stripped


def print_table(headers: list, rows: list):
    """简易整齐对齐表格打印"""
    if not rows:
        print("（暂无数据）")
        return
    col_widths = [len(str(h)) for h in headers]
    for r in rows:
        for i, val in enumerate(r):
            # 处理全角或中文的大致宽度
            w = 0
            for ch in str(val):
                w += 2 if ord(ch) > 127 else 1
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], w)

    header_line = " | ".join(str(h).ljust(col_widths[i]) for i, h in enumerate(headers))
    sep_line = "-+-".join("-" * col_widths[i] for i in range(len(headers)))
    print(header_line)
    print(sep_line)
    for r in rows:
        line_parts = []
        for i, val in enumerate(r):
            s = str(val)
            w = sum(2 if ord(ch) > 127 else 1 for ch in s)
            pad = col_widths[i] - w
            line_parts.append(s + " " * max(0, pad))
        print(" | ".join(line_parts))


def home_dir() -> str:
    """当前 RELAYCTL_HOME（动态）。"""
    return paths.home()


def state_dir() -> str:  # 兼容旧调用方
    return paths.cache_dir()
