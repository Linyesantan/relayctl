"""
新 API 适配器开发模板
复制本文件为 adapters/<name>.py，实现所需方法后即可自动注册。
"""
from .base import BaseAdapter, NotSupported
from ..core import HttpClient, mask_key

class CustomApiAdapter(BaseAdapter):
    NAME = "custom_api"  # 在 sites.yaml 中对应的 adapter 名称

    def __init__(self, name: str, site_cfg: dict, core_cfg: dict):
        super().__init__(name, site_cfg, core_cfg)
        self.client = HttpClient(self.base_url, proxy=self.proxy, auth=self.auth)

    def probe(self) -> dict:
        # 实现探针逻辑
        return {
            "site": self.name,
            "type": "Custom API",
            "auth_ok": True
        }

    def balance(self) -> dict:
        # 实现余额读取
        return {
            "balance": "0.00 USD",
            "used": "0.00",
            "currency": "USD",
            "user_group": "default"
        }

    def groups(self) -> list:
        # 实现分组读取
        return [{"name": "default", "desc": "默认", "ratio": 1.0}]

    def list_keys(self) -> list:
        # 实现 Key 列表读取
        return []

    def create_key(self, name: str, group: str, quota: float, models: list = None) -> dict:
        raise NotSupported("新建 Key 暂未实现")

    def update_key(self, key_id: int, group: str = None, status: int = None, quota: float = None, name: str = None) -> dict:
        raise NotSupported("更新 Key 暂未实现")

    def reveal_key(self, key_id: int) -> str:
        raise NotSupported("明文提取暂未实现")
