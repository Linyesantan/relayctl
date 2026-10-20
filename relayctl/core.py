import os
import sys
import yaml
import json
import requests
import tempfile
from urllib.parse import urlparse
from typing import Dict, Any, Optional

CONFIG_DIR = os.path.expanduser("~/.config/relayctl")
CONFIG_PATH = os.path.join(CONFIG_DIR, "sites.yaml")
STATE_DIR = os.path.join(CONFIG_DIR, "state")
BACKUP_DIR = os.path.join(CONFIG_DIR, "backup")

class AuthError(Exception):
    """鉴权失败异常（HTTP 401 或业务错误码 AUTH_*）"""
    pass

def mask_key(k: str) -> str:
    """脱敏 Key：仅显示前 4 后 4，其余用 * 遮盖"""
    if not k:
        return ""
    if len(k) <= 10:
        return "***"
    return f"{k[:4]}...{k[-4:]}"

def ensure_secure_dir(path: str):
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except Exception:
        pass

def secure_write_json(file_path: str, data: Any):
    """安全写入 JSON 文件并确保 600 权限"""
    ensure_secure_dir(os.path.dirname(file_path))
    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(file_path)}.",
                                     dir=os.path.dirname(file_path), text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass

def load_config() -> dict:
    ensure_secure_dir(CONFIG_DIR)
    ensure_secure_dir(STATE_DIR)
    ensure_secure_dir(BACKUP_DIR)
    
    if not os.path.exists(CONFIG_PATH):
        # 默认基础骨架
        cfg = {
            "local": {
                "base": "http://127.0.0.1:3001",
                "access_token": "",
                "user_id": 1
            },
            "sites": {}
        }
        save_config(cfg)
        return cfg
        
    # 强制检查权限
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass
        
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def save_config(cfg: dict):
    """原子写入配置：临时文件 -> chmod 600 -> os.replace，避免写一半崩溃损坏 sites.yaml"""
    ensure_secure_dir(CONFIG_DIR)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(CONFIG_PATH)}.",
                                     dir=CONFIG_DIR, text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, CONFIG_PATH)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass

class _SafeSession(requests.Session):
    """不要把管理认证头跟随跨 origin 重定向发送。"""
    def rebuild_auth(self, prepared_request, response):
        old = urlparse(response.request.url)
        new = urlparse(prepared_request.url)
        old_origin = (old.scheme.lower(), old.hostname.lower() if old.hostname else "", old.port)
        new_origin = (new.scheme.lower(), new.hostname.lower() if new.hostname else "", new.port)
        super().rebuild_auth(prepared_request, response)
        auth_headers = ("Authorization", "Token", "x-api-key", "New-Api-User")
        if old_origin != new_origin and any(name in prepared_request.headers for name in auth_headers):
            raise requests.exceptions.TooManyRedirects(
                "拒绝带认证请求的跨 origin 重定向")

class HttpClient:
    """带有代理、超时与通用安全头的统一 HTTP 客户端"""
    def __init__(self, base_url: str, proxy: Optional[str] = None, auth: Optional[dict] = None):
        self.base_url = base_url.rstrip("/")
        self.proxy = proxy
        self.auth = auth or {}
        self.session = _SafeSession()
        
        mode = (self.proxy or "none").strip() if isinstance(self.proxy, str) else "none"
        if mode.lower() == "auto":
            # auto: 跟随系统环境变量代理（HTTP(S)_PROXY / ALL_PROXY），未设置则直连
            self.session.trust_env = True
        elif mode.lower() in ("none", "direct", ""):
            # 强制直连，避免被系统代理干扰
            self.session.trust_env = False
        else:
            self.session.trust_env = False
            self.session.proxies = {"http": mode, "https": mode}

    def get_headers(self, custom_headers: Optional[dict] = None) -> dict:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*"
        }
        token = self.auth.get("access_token")
        uid = self.auth.get("user_id")
        cookie = self.auth.get("cookie")
        
        if token:
            if token.startswith("user-"):
                headers["x-api-key"] = token
            else:
                headers["Authorization"] = token if token.startswith("Bearer ") else f"Bearer {token}"
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
