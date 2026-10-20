"""
新 Script 级适配器开发模板（适用于无公开 API、需复用 Chrome 登录态的站点）
"""
import os
import json
import subprocess
from .base import BaseAdapter, NotSupported
from ..core import HttpClient, STATE_DIR, mask_key

class CustomScriptAdapter(BaseAdapter):
    NAME = "custom_script"

    def __init__(self, name: str, site_cfg: dict, core_cfg: dict):
        super().__init__(name, site_cfg, core_cfg)
        self.state_file = os.path.join(STATE_DIR, f"{name}.json")
        self.client = HttpClient(self.base_url, proxy=self.proxy)

    def _fetch_via_cdp(self, js_expr: str, tab_id: str = None) -> dict:
        """借助 browser-cdp 工具在现有已登录标签页中执行 fetch 或读取 DOM"""
        cmd = ["/home/lunaadmin/bin/browser-cdp", "eval"]
        if tab_id:
            cmd.append(tab_id)
        cmd.append(js_expr)
        res = subprocess.run(cmd, capture_output=True, text=True)
        try:
            return json.loads(res.stdout)
        except Exception:
            return {"ok": False, "raw": res.stdout}

    def probe(self) -> dict:
        return {
            "site": self.name,
            "type": "Script Mode",
            "auth_ok": False,
            "reason": "需要人工在 Chrome 中访问并刷新凭据"
        }

    def balance(self) -> dict:
        raise NotSupported("请先抓取并实现余额接口")

    def groups(self) -> list:
        return [{"name": "default", "desc": "默认", "ratio": 1.0}]

    def list_keys(self) -> list:
        return []

    def create_key(self, name: str, group: str, quota: float, models: list = None) -> dict:
        raise NotSupported("页面级新建 Key 暂未实现")

    def update_key(self, key_id: int, group: str = None, status: int = None, quota: float = None, name: str = None) -> dict:
        raise NotSupported("页面级更新 Key 暂未实现")

    def reveal_key(self, key_id: int) -> str:
        raise NotSupported("无法提取明文 Key")
