class NotSupported(Exception):
    """当前适配器或站点不支持该操作"""
    pass

class BaseAdapter:
    """中转站点统一适配器接口"""
    def __init__(self, name: str, site_cfg: dict, core_cfg: dict):
        self.name = name
        self.cfg = site_cfg
        self.core_cfg = core_cfg
        self.base_url = site_cfg.get("base", "").rstrip("/")
        self.proxy = site_cfg.get("proxy")
        self.auth = site_cfg.get("auth") or {}

    def probe(self) -> dict:
        """探测系统类型、版本、鉴权有效性"""
        raise NotImplementedError

    def balance(self) -> dict:
        """获取余额信息 -> {balance, used, currency, user_group}"""
        raise NotImplementedError

    def groups(self) -> list:
        """获取可用分组列表 -> [{name, desc, ratio}]"""
        raise NotImplementedError

    def list_keys(self) -> list:
        """获取 Key 列表 -> [{id, name, group, status, remain, used, key_masked}]"""
        raise NotImplementedError

    def create_key(self, name: str, group: str, quota: float, models: list = None) -> dict:
        """新建 Key -> {id, key_masked}"""
        raise NotImplementedError

    def update_key(self, key_id: int, group: str = None, status: int = None, quota: float = None, name: str = None, unlimited: bool = False) -> dict:
        """修改 Key"""
        raise NotImplementedError

    def delete_key(self, key_id: int) -> bool:
        """删除 Key"""
        raise NotImplementedError

    def reveal_key(self, key_id: int) -> str:
        """获取 Key 明文（只用于同步至本地，禁止直接命令行打印）"""
        raise NotSupported(f"站点 {self.name} 暂不支持明文密钥提取")

class ParkedAdapter(BaseAdapter):
    """搁置（Parked）站点适配器：记录原因与升级 TODO"""
    def __init__(self, name: str, site_cfg: dict, core_cfg: dict):
        super().__init__(name, site_cfg, core_cfg)
        self.reason = site_cfg.get("parked_reason") or "凭据缺失或接口未探明"
        self.todo = site_cfg.get("todo") or "在 Chrome 登录后抓取凭据或配置系统令牌"

    def _parked_resp(self):
        return {
            "status": "parked",
            "site": self.name,
            "reason": self.reason,
            "todo": self.todo
        }

    def probe(self) -> dict:
        return self._parked_resp()

    def balance(self) -> dict:
        return self._parked_resp()

    def groups(self) -> list:
        return [self._parked_resp()]

    def list_keys(self) -> list:
        return [self._parked_resp()]

    def create_key(self, name: str, group: str, quota: float, models: list = None) -> dict:
        return self._parked_resp()

    def update_key(self, key_id: int, group: str = None, status: int = None, quota: float = None, name: str = None, unlimited: bool = False) -> dict:
        return self._parked_resp()

    def delete_key(self, key_id: int) -> bool:
        return False

    def reveal_key(self, key_id: int) -> str:
        raise NotSupported(f"站点 {self.name} 处于 parked 状态: {self.reason}")
