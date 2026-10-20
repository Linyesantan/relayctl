import os
import json
from .base import BaseAdapter, NotSupported
from ..core import HttpClient, STATE_DIR, mask_key, AuthError

class WusRouterAdapter(BaseAdapter):
    """
    wusrouter 适配器（script 级）
    端点起点：/api/v1/user, /api/v1/keys (401: Unauthorized)
    状态保存在 ~/.config/relayctl/state/wusrouter.json
    """
    NAME = "wusrouter"

    def __init__(self, name: str, site_cfg: dict, core_cfg: dict):
        super().__init__(name, site_cfg, core_cfg)
        self.state_file = os.path.join(STATE_DIR, f"{name}.json")
        self.client = HttpClient(self.base_url, proxy=self.proxy)
        self._load_session()

    def _load_session(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    token = data.get("token")
                    if token:
                        self.client.auth["access_token"] = f"Bearer {token}"
            except Exception:
                pass

    def probe(self) -> dict:
        try:
            r = self.client.get("/api/v1/user", timeout=8)
            if r.status_code == 200:
                try:
                    d = r.json().get("data", {})
                    return {
                        "site": self.name,
                        "type": "wusrouter 自研面板",
                        "auth_ok": True,
                        "username": d.get("username", "user"),
                        "status": "ready"
                    }
                except Exception:
                    pass
            elif r.status_code == 401:
                return {
                    "site": self.name,
                    "type": "wusrouter 自研面板",
                    "auth_ok": False,
                    "status": "session_expired",
                    "reason": "未登录或会话已过期 (401 Unauthorized)"
                }
            return {
                "site": self.name,
                "type": "wusrouter 自研面板",
                "auth_ok": False,
                "status": "error",
                "http_code": r.status_code
            }
        except Exception as e:
            return {
                "site": self.name,
                "type": "wusrouter 自研面板",
                "auth_ok": False,
                "status": "error",
                "error": str(e)
            }

    def balance(self) -> dict:
        r = self.client.get("/api/v1/user")
        if r.status_code == 401:
            raise AuthError("wusrouter 登录会话已过期或未登录 (401)")
        if r.status_code != 200:
            raise Exception(f"HTTP {r.status_code}: {r.text[:100]}")
        try:
            data = r.json().get("data", {})
            return {
                "balance": f"{data.get('balance', 0)} USD (推测字段)",
                "used": str(data.get("used", "0")),
                "currency": "USD",
                "user_group": data.get("group", "default")
            }
        except Exception:
            raise NotSupported("wusrouter 返回非预期 JSON 格式，字段结构待在 Chrome 抓包后确认")

    def groups(self) -> list:
        return [{"name": "default", "desc": "wusrouter 默认分组 (自研面板未公开分组接口)", "ratio": 1.0}]

    def list_keys(self) -> list:
        r = self.client.get("/api/v1/keys")
        if r.status_code == 401:
            raise AuthError("wusrouter 登录会话已过期或未登录 (401)")
        if r.status_code != 200:
            raise Exception(f"HTTP {r.status_code}: {r.text[:100]}")
        try:
            items = r.json().get("data", []) or []
            keys = []
            for it in items:
                key_val = it.get("key", "")
                keys.append({
                    "id": it.get("id"),
                    "name": it.get("name", "key"),
                    "group": "default",
                    "status": "启用" if it.get("status") == 1 else "禁用",
                    "remain": str(it.get("remain", "无限")),
                    "used": str(it.get("used", "0")),
                    "key_masked": mask_key(key_val),
                    "_raw_key": key_val
                })
            return keys
        except Exception:
            raise NotSupported("wusrouter 返回非预期 JSON 格式，待抓包后适配")

    def create_key(self, name: str, group: str, quota: float, models: list = None) -> dict:
        raise NotSupported("wusrouter 尚未抓取新建 Key 接口")

    def update_key(self, key_id: int, group: str = None, status: int = None, quota: float = None, name: str = None) -> dict:
        raise NotSupported("wusrouter 尚未抓取修改 Key 接口")

    def delete_key(self, key_id: int) -> bool:
        raise NotSupported("wusrouter 尚未抓取删除 Key 接口")

    def reveal_key(self, key_id: int) -> str:
        keys = self.list_keys()
        for k in keys:
            if k["id"] == key_id:
                raw = k.get("_raw_key")
                if raw and "*" not in raw:
                    return raw
        raise NotSupported(f"站点 {self.name} 无法直接获取明文 Key")
