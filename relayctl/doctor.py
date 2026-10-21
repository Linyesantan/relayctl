"""本地运行环境体检；默认不发起网络请求。"""

import json
import os
import stat
from typing import Any, Dict, List, Optional, Tuple

import yaml

from . import cache as rcache
from . import config, paths
from .adapters import get_adapter
from .errors import EXIT_FAIL, EXIT_OK, EXIT_USAGE

_LEVEL_INFO = "info"
_LEVEL_WARN = "warn"
_LEVEL_ERROR = "error"


def is_loopback(host: Any) -> bool:
    """只接受明确的回环监听名/地址，通配地址与普通主机名均视为非回环。"""
    import ipaddress

    value = str(host or "").strip().strip("[]")
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _mode(path: str) -> Optional[int]:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def _detail(value: Any) -> str:
    return rcache.redact_text(str(value or "")).replace("\n", " ")[:400]


def _add(checks: List[Dict[str, Any]], name: str, ok: bool, level: str, detail: str,
         exit_code: int = EXIT_FAIL):
    checks.append({
        "name": name,
        "ok": bool(ok),
        "level": _LEVEL_INFO if ok else level,
        "detail": _detail(detail),
        "_exit_code": EXIT_OK if ok else exit_code,
    })


def _permission_checks(checks: List[Dict[str, Any]]):
    for name, directory in paths.layout_dirs().items():
        mode = _mode(directory)
        if mode is None:
            _add(checks, f"permission.dir.{name}", False, _LEVEL_ERROR,
                 f"目录不存在: {directory}")
        else:
            _add(checks, f"permission.dir.{name}", mode == paths.DIR_MODE, _LEVEL_ERROR,
                 f"{directory} mode={mode:04o}，要求 0700")

    files = [("config", paths.config_path())]
    site_dir = paths.sites_dir()
    if os.path.isdir(site_dir):
        files.extend((f"site.{fn}", os.path.join(site_dir, fn))
                     for fn in sorted(os.listdir(site_dir)) if fn.endswith((".yaml", ".yml")))
    if os.path.exists(paths.keys_dist_path()):
        files.append(("dist.keys", paths.keys_dist_path()))
    cache_dir = paths.cache_dir()
    if os.path.isdir(cache_dir):
        files.extend((f"cache.{fn}", os.path.join(cache_dir, fn))
                     for fn in sorted(os.listdir(cache_dir)) if fn.endswith(".json"))

    for name, filename in files:
        mode = _mode(filename)
        if mode is None:
            _add(checks, f"permission.file.{name}", False, _LEVEL_ERROR,
                 f"文件不存在: {filename}")
        else:
            _add(checks, f"permission.file.{name}", mode == paths.FILE_MODE, _LEVEL_ERROR,
                 f"{filename} mode={mode:04o}，要求 0600")


def _load_raw_global(checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    filename = paths.config_path()
    try:
        with open(filename, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError("顶层必须是映射")
        for section in ("cache", "script", "scheduler", "web"):
            if section in raw and not isinstance(raw[section], dict):
                raise ValueError(f"{section} 必须是映射")
        if "version" in raw and raw["version"] != 2:
            raise ValueError("version 必须为 2")
        web = raw.get("web") or {}
        if "host" in web and not isinstance(web["host"], str):
            raise ValueError("web.host 必须是字符串")
        if "access_password" in web and not isinstance(web["access_password"], str):
            raise ValueError("web.access_password 必须是字符串")
        if "port" in web and (isinstance(web["port"], bool) or not isinstance(web["port"], int)
                              or not 1 <= web["port"] <= 65535):
            raise ValueError("web.port 必须是 1..65535 的整数")
        cfg = config._merge_defaults(raw)
        _add(checks, "config.global", True, _LEVEL_INFO, "config.yaml 可解析")
        return cfg
    except (OSError, ValueError, yaml.YAMLError) as exc:
        _add(checks, "config.global", False, _LEVEL_ERROR,
             f"config.yaml 无法解析: {exc}", EXIT_USAGE)
        return {}


def _site_checks(checks: List[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], bool]:
    sites: Dict[str, Dict[str, Any]] = {}
    invalid = False
    site_dir = paths.sites_dir()
    if not os.path.isdir(site_dir):
        return sites, invalid
    for filename in sorted(os.listdir(site_dir)):
        if not filename.endswith((".yaml", ".yml")):
            continue
        name = filename.rsplit(".", 1)[0]
        pathname = os.path.join(site_dir, filename)
        try:
            with open(pathname, "r", encoding="utf-8") as handle:
                raw = yaml.safe_load(handle)
            site, warnings = config.normalize_site(raw, name=name, strict=True)
            sites[site["name"]] = site
            _add(checks, f"site.{name}.config", True, _LEVEL_INFO,
                 "站点配置可解析" + (f"；兼容警告: {'；'.join(warnings)}" if warnings else ""))
        except Exception as exc:  # 单站隔离，继续检查其余站点
            invalid = True
            _add(checks, f"site.{name}.config", False, _LEVEL_ERROR,
                 f"站点配置无效: {exc}", EXIT_USAGE)
    return sites, invalid


def _credential_and_script_checks(checks: List[Dict[str, Any]], sites: Dict[str, Dict[str, Any]]):
    for name, site in sites.items():
        auth = site.get("auth") or {}
        for field in ("access_token_env", "password_env"):
            env_name = str(auth.get(field) or "").strip()
            if env_name:
                exists = env_name in os.environ and bool(os.environ.get(env_name))
                _add(checks, f"site.{name}.env.{field}", exists, _LEVEL_ERROR,
                     f"环境变量 {env_name} {'已设置' if exists else '不存在或为空'}", EXIT_USAGE)
        if site.get("adapter") == "script":
            script_name = str(site.get("script") or "")
            script_path = script_name if os.path.isabs(script_name) else os.path.join(paths.home(), script_name)
            exists = os.path.isfile(script_path)
            _add(checks, f"site.{name}.script.exists", exists, _LEVEL_ERROR,
                 f"脚本 {'存在' if exists else '不存在'}: {script_path}", EXIT_USAGE)
            executable = exists and os.access(script_path, os.X_OK)
            _add(checks, f"site.{name}.script.executable", executable, _LEVEL_ERROR,
                 f"脚本 {'可执行' if executable else '不可执行'}: {script_path}", EXIT_USAGE)


def _cache_checks(checks: List[Dict[str, Any]]):
    directory = paths.cache_dir()
    if not os.path.isdir(directory):
        return
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".json"):
            continue
        pathname = os.path.join(directory, filename)
        try:
            with open(pathname, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            valid = isinstance(data, dict) and isinstance(data.get("site"), str) \
                and isinstance(data.get("sections"), dict)
            if valid:
                for section in data["sections"].values():
                    if not isinstance(section, dict) or "data" not in section or "ts" not in section:
                        valid = False
                        break
            _add(checks, f"cache.{filename}", valid, _LEVEL_WARN,
                 "缓存结构有效" if valid else "缓存结构无效")
        except (OSError, ValueError) as exc:
            _add(checks, f"cache.{filename}", False, _LEVEL_WARN, f"缓存无法解析: {exc}")


def _web_check(checks: List[Dict[str, Any]], global_cfg: Dict[str, Any]):
    web = global_cfg.get("web") if isinstance(global_cfg, dict) else {}
    web = web if isinstance(web, dict) else {}
    host = web.get("host") or "127.0.0.1"
    password = str(web.get("access_password") or "")
    ok = is_loopback(host) or bool(password)
    _add(checks, "web.access", ok, _LEVEL_ERROR,
         f"host={host}；" + ("已配置访问口令" if password else "未配置访问口令"), EXIT_USAGE)


def _connectivity_checks(checks: List[Dict[str, Any]], global_cfg: Dict[str, Any],
                         sites: Dict[str, Dict[str, Any]]):
    cfg = dict(global_cfg)
    cfg["sites"] = {}
    for name, site in sites.items():
        resolved = config.resolve_auth(site)
        cfg["sites"][name] = resolved
        try:
            result = get_adapter(name, resolved, cfg).probe()
            ok = bool(result.get("auth_ok", True))
            _add(checks, f"connectivity.{name}", ok, _LEVEL_ERROR,
                 "连通且鉴权有效" if ok else "连通但鉴权失败",
                 EXIT_FAIL)
        except Exception as exc:
            _add(checks, f"connectivity.{name}", False, _LEVEL_ERROR,
                 f"连通性检查失败: {exc}", EXIT_FAIL)


def run(check_connectivity: bool = False) -> Tuple[Dict[str, Any], int]:
    """返回稳定报告与退出码；网络检查仅在显式启用时执行。"""
    checks: List[Dict[str, Any]] = []
    # 首次安装时目录可能还没建：先按 0700 建好，再检查权限
    # （doctor 不改变任何站点配置，只创建运行时目录骨架）。
    # 只补建缺失的目录（不改已有目录的权限，doctor 要能照实报出权限问题）
    for directory in paths.layout_dirs().values():
        if not os.path.isdir(directory):
            try:
                paths.ensure_dir(directory)
            except OSError as exc:
                _add(checks, "permission.dir.init", False, _LEVEL_ERROR,
                     f"无法创建运行时目录 {directory}: {exc}", EXIT_USAGE)
    _permission_checks(checks)
    global_cfg = _load_raw_global(checks)
    sites, _ = _site_checks(checks)
    _credential_and_script_checks(checks, sites)
    _cache_checks(checks)
    _web_check(checks, global_cfg)
    if check_connectivity:
        _connectivity_checks(checks, global_cfg, sites)

    codes = [int(item["_exit_code"]) for item in checks if not item["ok"]]
    exit_code = EXIT_USAGE if EXIT_USAGE in codes else (EXIT_FAIL if codes else EXIT_OK)
    public = [{k: v for k, v in item.items() if not k.startswith("_")} for item in checks]
    summary = {
        "total": len(public),
        "passed": sum(1 for item in public if item["ok"]),
        "failed": sum(1 for item in public if not item["ok"]),
        "connectivity_checked": bool(check_connectivity),
    }
    return {"checks": public, "summary": summary}, exit_code


def report_lines(report: Dict[str, Any]) -> List[str]:
    lines = []
    for item in report.get("checks") or []:
        marker = "OK" if item.get("ok") else str(item.get("level") or "error").upper()
        lines.append(f"[{marker}] {item.get('name')}: {item.get('detail')}")
    summary = report.get("summary") or {}
    lines.append(f"汇总: passed={summary.get('passed', 0)} failed={summary.get('failed', 0)}")
    return lines
