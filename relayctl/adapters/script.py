"""脚本适配器：把站点能力外包给 $RELAYCTL_HOME/scripts/ 下的可执行脚本。

协议与安全保障全部在 relayctl.script_adapter 里实现，本模块只负责:
- 按动作组装 json-args（create_key 带 name/group/quota/unlimited/models）；
- 把脚本输出规范成与内置适配器一致的字段；
- list_keys 走 models.normalize_keys（稳定字段、永不携带明文）；
- 所有结果先 sanitize 再返回，缓存因此绝不会写入明文。
"""
from typing import Any, Dict, List, Optional

from .base import BaseAdapter
from .. import models
from ..script_adapter import ScriptRunner, ACTIONS, ARG_ACTIONS

# 与内置 newapi 适配器一致的字段契约（脚本缺失时按保守默认值补齐）
PROBE_FIELDS: Dict[str, Any] = {
    "site": "",
    "type": "script",
    "system_name": "",
    "version": "",
    "auth_ok": False,
    "quota_per_unit": 500000,
    "quota_display_type": "USD",
}

BALANCE_FIELDS: Dict[str, Any] = {
    "balance": "0.0000",
    "used": "0.0000",
    "raw_quota": 0,
    "raw_used": 0,
    "quota_per_unit": 500000,
    "display_type": "USD",
    "user_group": "default",
}


class ScriptAdapter(BaseAdapter):
    """adapter=script 时由 get_adapter 选中（NAME 即配置里的 adapter 值）。"""

    NAME = "script"

    def __init__(self, name: str, site_cfg: dict, core_cfg: dict):
        super().__init__(name, site_cfg, core_cfg)
        self.runner = ScriptRunner(name, site_cfg, core_cfg)

    # -- 输出规范 ---------------------------------------------------------
    @staticmethod
    def _clean(data: Any) -> Any:
        """剔除明文凭据与下划线开头的内部字段（缓存只落脱敏结果）。"""
        return models.sanitize(data) if isinstance(data, dict) else data

    def _fields(self, data: Any, defaults: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(defaults)
        if isinstance(data, dict):
            out.update(data)
        out.setdefault("site", self.name)
        return self._clean(out)

    # -- 只读动作 ---------------------------------------------------------
    def probe(self) -> dict:
        data = self.runner.run("probe")
        return self._fields(data, PROBE_FIELDS)

    def balance(self) -> dict:
        data = self.runner.run("balance")
        return self._fields(data, BALANCE_FIELDS)

    def groups(self) -> list:
        data = self.runner.run("groups")
        return self._groups(data)

    def list_keys(self) -> list:
        data = self.runner.run("list_keys")
        return models.normalize_keys(data)

    # -- 写动作 -----------------------------------------------------------
    def create_key(self, name: str, group: str, quota: float = None,
                   unlimited: bool = False, models: list = None) -> dict:
        args = {
            "name": name,
            "group": group,
            "quota": quota,
            "unlimited": bool(unlimited),
            "models": list(models or []),
        }
        return self._clean(self.runner.run("create_key", args))

    def update_key(self, key_id: int, group: str = None, status: int = None,
                   quota: float = None, name: str = None, unlimited: bool = False) -> dict:
        args: Dict[str, Any] = {"key_id": key_id}
        if group is not None:
            args["group"] = group
        if status is not None:
            args["status"] = status
        if quota is not None:
            args["quota"] = quota
        if name is not None:
            args["name"] = name
        if unlimited:
            args["unlimited"] = True
        return self._clean(self.runner.run("update_key", args))

    def delete_key(self, key_id: int) -> bool:
        data = self.runner.run("delete_key", {"key_id": key_id})
        if isinstance(data, dict):
            for key in ("ok", "deleted", "success"):
                if key in data:
                    return bool(data[key])
        return True

    # -- 辅助 -------------------------------------------------------------
    def _groups(self, data: Any) -> List[Dict[str, Any]]:
        if not isinstance(data, (list, tuple)):
            return []
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            out.append({
                "name": item.get("name", ""),
                "desc": item.get("desc", ""),
                "ratio": item.get("ratio", 1),
            })
        return out

    @property
    def script_path(self) -> str:
        return self.runner.path

    def test_actions(self, actions: Optional[List[str]] = None) -> List[Any]:
        """逐动作体检（供 relayctl script test 使用）。"""
        return [self.runner.invoke(action, args=self._test_args(action))
                for action in (actions or ACTIONS)]

    @staticmethod
    def _test_args(action: str) -> Optional[dict]:
        """体检用的参数：写动作一律带 dry_run，避免真的动生产 Key。"""
        if action == "create_key":
            return {"name": "relayctl-script-test", "group": "default",
                    "quota": None, "unlimited": False, "models": [], "dry_run": True}
        if action in ARG_ACTIONS:
            return {"key_id": 0, "dry_run": True}
        return None
