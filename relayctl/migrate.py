"""v1 -> v2 配置迁移：RELAYCTL_HOME/sites.yaml -> sites/<name>.yaml。

行为约定：
- --dry-run 只计算不写盘；
- 实际迁移逐站原子写，单站失败不影响其它站；
- 迁移成功后不删除旧 sites.yaml，可重复执行（已存在的站记 skipped，保持幂等）；
- 旧字段 adapter/base/proxy/auth/tier/parked_reason 一律保留，
  缺失的 name/console_url/panel/docs/tags/refresh_interval/status 由迁移补全。
"""

import copy
import os
from typing import Any, Dict, List

from . import config, paths
from .errors import ConfigError, MigrationError

RESULT_KEYS = ("added", "skipped", "failed")


def legacy_sites() -> Dict[str, Any]:
    """读取旧 sites.yaml 的 sites 映射；不存在返回空 dict。"""
    path = paths.legacy_sites_path()
    if not os.path.exists(path):
        return {}
    raw = paths.read_yaml(path, default=None)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise MigrationError(f"旧配置文件格式非法（应为映射）: {path}")
    sites = raw.get("sites")
    if sites is None:
        return {}
    if not isinstance(sites, dict):
        raise MigrationError("旧配置的 sites 字段必须是映射")
    return sites


def build_site(name: str, old: Dict[str, Any]) -> Dict[str, Any]:
    """把旧站点条目映射为 v2 站点配置。"""
    if not isinstance(old, dict):
        raise ConfigError("旧站点条目必须是映射")

    adapter = old.get("adapter") or "newapi"
    new: Dict[str, Any] = {
        "name": name,
        # 旧字段原样保留
        "base": old.get("base") or "",
        "adapter": adapter,
        "proxy": old.get("proxy") or "",
        "parked_reason": old.get("parked_reason") or "",
        # v2 新增字段
        "console_url": old.get("console_url") or "",
        "panel": old.get("panel") or adapter,
        "script": old.get("script"),
        "tags": copy.deepcopy(old.get("tags")) or [],
        "refresh_interval": old.get("refresh_interval") or "",
    }

    # tier -> status（旧 tier: api / parked）
    tier = str(old.get("tier") or "").strip().lower()
    if "status" in old and str(old.get("status")).strip():
        new["status"] = str(old["status"]).strip()
    elif tier == "parked":
        new["status"] = config.STATUS_PARKED
    elif tier:
        new["status"] = config.STATUS_ACTIVE
    else:
        new["status"] = (config.STATUS_ACTIVE if (old.get("auth") or {}).get("access_token")
                         else config.STATUS_PARKED)

    auth = copy.deepcopy(old.get("auth")) if isinstance(old.get("auth"), dict) else {}
    merged_auth = copy.deepcopy(config.DEFAULT_AUTH)
    merged_auth.update(auth)
    new["auth"] = merged_auth

    docs = copy.deepcopy(old.get("docs")) if isinstance(old.get("docs"), dict) else {}
    merged_docs = copy.deepcopy(config.DEFAULT_DOCS)
    merged_docs.update({k: v for k, v in docs.items() if k in config.DEFAULT_DOCS})
    new["docs"] = merged_docs

    return new


def migrate(dry_run: bool = False) -> Dict[str, Any]:
    """执行迁移，返回 {added, skipped, failed, counts, dry_run, source}。"""
    result: Dict[str, Any] = {
        "dry_run": bool(dry_run),
        "source": paths.legacy_sites_path(),
        "added": [],
        "skipped": [],
        "failed": {},
    }
    try:
        old_sites = legacy_sites()
    except MigrationError:
        raise
    except ConfigError as exc:
        raise MigrationError(str(exc))

    for raw_name, entry in old_sites.items():
        try:
            name = paths.safe_site_name(raw_name)
        except ConfigError as exc:
            result["failed"][str(raw_name)] = str(exc)
            continue
        if config.site_exists(name):
            result["skipped"].append(name)
            continue
        try:
            site_cfg = build_site(name, entry)
            config.validate_site(site_cfg, name=name)
            if not dry_run:
                paths.init_dirs()
                config.save_site(name, site_cfg)
            result["added"].append(name)
        except Exception as exc:  # noqa: BLE001 - 单站失败不影响其它站
            result["failed"][name] = str(exc)

    result["counts"] = {key: len(result[key]) for key in RESULT_KEYS}
    return result


def migrate_report(result: Dict[str, Any]) -> List[str]:
    """把迁移结果渲染为可读文本行。"""
    counts = result.get("counts") or {}
    lines = [
        f"源文件: {result.get('source')}",
        f"模式: {'试运行 (--dry-run，未写盘)' if result.get('dry_run') else '实际迁移'}",
        f"新增: {counts.get('added', 0)}  跳过: {counts.get('skipped', 0)}  失败: {counts.get('failed', 0)}",
    ]
    added: List[str] = result.get("added") or []
    if added:
        lines.append("  新增站点: " + ", ".join(added))
    skipped: List[str] = result.get("skipped") or []
    if skipped:
        lines.append("  已存在跳过: " + ", ".join(skipped))
    failed: Dict[str, str] = result.get("failed") or {}
    for name, err in failed.items():
        lines.append(f"  失败 {name}: {err}")
    return lines
