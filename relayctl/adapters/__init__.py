import os
import importlib
import inspect
from .base import BaseAdapter, ParkedAdapter

ADAPTER_REGISTRY = {}

def register_adapter(name: str, cls):
    ADAPTER_REGISTRY[name] = cls

def get_adapter(name: str, site_cfg: dict, core_cfg: dict) -> BaseAdapter:
    # 兼容 v1 的 tier 与 v2 的 status
    tier = site_cfg.get("tier")
    if not tier:
        tier = "api" if str(site_cfg.get("status", "parked")).lower() == "active" else "parked"
    if str(tier).lower() == "parked":
        return ParkedAdapter(name, site_cfg, core_cfg)
        
    adapter_name = site_cfg.get("adapter", "newapi")
    cls = ADAPTER_REGISTRY.get(adapter_name)
    if not cls:
        return ParkedAdapter(name, site_cfg, core_cfg)
    return cls(name, site_cfg, core_cfg)

def _auto_discover():
    current_dir = os.path.dirname(__file__)
    for filename in os.listdir(current_dir):
        if filename.endswith(".py") and not filename.startswith("_") and filename not in ["base.py"]:
            module_name = filename[:-3]
            try:
                mod = importlib.import_module(f".{module_name}", package=__name__)
                for attr_name in dir(mod):
                    attr = getattr(mod, attr_name)
                    if inspect.isclass(attr) and issubclass(attr, BaseAdapter) and attr not in [BaseAdapter, ParkedAdapter]:
                        # 默认以模块名或者声明的 NAME 作为标识
                        reg_name = getattr(attr, "NAME", module_name)
                        register_adapter(reg_name, attr)
            except Exception as e:
                pass

_auto_discover()
