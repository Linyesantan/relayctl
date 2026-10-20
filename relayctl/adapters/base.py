from ..errors import NotSupported  # noqa: F401 (兼容旧导入路径)


class BaseAdapter:
    """中转站点统一适配器接口"""

    STATUS_ACTIVE = "active"
    STATUS_PARKED = "parked"

    def __init__(self, name: str, site_cfg: dict, core_cfg: dict):
        self.name = name
        self.cfg = site_cfg
        self.core_cfg = core_cfg or {}
        self.base_url = (site_cfg.get("base", "") or "").rstrip("/")
        self.proxy = site_cfg.get("proxy")
        self.auth = site_cfg.get("auth") or {}
        # v2 站点用 status；旧配置用 tier，这里统一推导
        self.status = site_cfg.get("status") or (
            "active" if str(site_cfg.get("tier", "")).lower() == "api" else "parked")

    @property
    def parked_reason(self) -> str:
        return self.cfg.get("parked_reason") or ""

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
        """获取 Key 列表 -> 稳定字段 [{id,name,group,enabled,status,unlimited,
        remain,used,key_masked,models}]，绝不包含 _raw_key 等明文字段"""
        raise NotImplementedError

    def create_key(self, name: str, group: str, quota: float = None,
                   unlimited: bool = False, models: list = None) -> dict:
        """新建 Key -> {id, name, group, key_masked, unlimited}

        quota=None 且 unlimited=False 时按「未指定额度」处理；是否无限由各适配器判定。
        """
        raise NotImplementedError

    def update_key(self, key_id: int, group: str = None, status: int = None, quota: float = None,
                   name: str = None, unlimited: bool = False) -> dict:
        """修改 Key"""
        raise NotImplementedError

    def delete_key(self, key_id: int) -> bool:
        """删除 Key"""
        raise NotImplementedError


class ParkedAdapter(BaseAdapter):
    """搁置（Parked）站点适配器：记录原因与升级 TODO"""

    def __init__(self, name: str, site_cfg: dict, core_cfg: dict):
        super().__init__(name, site_cfg, core_cfg)
        self.reason = site_cfg.get("parked_reason") or "凭据缺失或接口未探明"
        self.todo = site_cfg.get("todo") or "补齐 auth（推荐 access_token_env）后执行 relayctl site auth"

    def _parked_resp(self):
        return {
            "status": "parked",
            "site": self.name,
            "reason": self.reason,
            "todo": self.todo,
        }

    def probe(self) -> dict:
        return self._parked_resp()

    def balance(self) -> dict:
        return self._parked_resp()

    def groups(self) -> list:
        raise NotSupported(f"站点 {self.name} 已搁置，无法读取分组：{self.reason}")

    def list_keys(self) -> list:
        raise NotSupported(f"站点 {self.name} 已搁置，无法读取 Key：{self.reason}")

    def create_key(self, name: str, group: str, quota: float = None,
                   unlimited: bool = False, models: list = None) -> dict:
        raise NotSupported(f"站点 {self.name} 已搁置，无法新建 Key：{self.reason}")

    def update_key(self, key_id: int, group: str = None, status: int = None, quota: float = None,
                   name: str = None, unlimited: bool = False) -> dict:
        raise NotSupported(f"站点 {self.name} 已搁置，无法修改 Key：{self.reason}")

    def delete_key(self, key_id: int) -> bool:
        raise NotSupported(f"站点 {self.name} 已搁置，无法删除 Key：{self.reason}")
