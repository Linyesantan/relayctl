"""阶段 5 分发库管理：$RELAYCTL_HOME/dist/keys.yaml。

职责边界：
- 复用 importers 的 load_keys_dist / save_keys_dist / key_item / key_out 与 paths.file_lock，
  不另起一套存储；
- 只做「登记 + 打标 + 归属 + 单向导出」，不做负载均衡 / 计费 / 用户系统，
  也不与上游做自动双向同步；
- 默认输出只有 id 与 key_masked；写出的导出文件一律 0600；
- 删除类操作必须显式确认（--yes），否则拒绝执行。

导出目标见 :mod:`relayctl.exporters`（CC Switch / New API / env / json / csv）。
"""

import json
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import exporters
from . import importers
from . import paths
from .core import normalize_base
from .errors import ConfigError, UsageError

EXPORT_TARGETS = exporters.EXPORT_TARGETS
APP_CHOICES = exporters.CCSWITCH_APP_CHOICES


# ---------------------------------------------------------------------------
# 读写（复用 importers，避免两套 schema）
# ---------------------------------------------------------------------------
def load_keys() -> Dict[str, Any]:
    """读取分发库（不存在返回空 schema）。"""
    return importers.load_keys_dist()


def save_keys(data: Dict[str, Any]) -> str:
    """原子写分发库（0600）。"""
    return importers.save_keys_dist(data)


def list_keys(tag: str = "", site: str = "", status: str = "") -> List[Dict[str, Any]]:
    """列出分发记录（只输出脱敏字段）。可按 tag / 来源站点 / 状态过滤。"""
    items = [importers.key_out(k) for k in load_keys()["keys"]]
    if tag:
        items = [it for it in items if tag in (it.get("tags") or [])]
    if site:
        items = [it for it in items if (it.get("source_site") or "") == site]
    if status:
        items = [it for it in items if (it.get("status") or "") == status]
    return items


def _raw_keys() -> List[Dict[str, Any]]:
    return load_keys()["keys"]


def _match(keys: List[Dict[str, Any]], ref: str) -> List[Dict[str, Any]]:
    """按 id 精确匹配；退化为唯一前缀匹配（歧义时返回多条，由调用方报错）。"""
    target = str(ref or "").strip()
    if not target:
        raise UsageError("缺少 Key id")
    exact = [k for k in keys if str(k.get("id")) == target]
    if exact:
        return exact
    return [k for k in keys if str(k.get("id")).startswith(target)]


def resolve_key(keys: List[Dict[str, Any]], ref: str) -> Dict[str, Any]:
    """定位一条记录：id 精确优先，前缀唯一也可；歧义绝不猜测。"""
    matches = _match(keys, ref)
    if not matches:
        raise ConfigError(f"分发库中没有 id 为 {ref!r} 的记录")
    if len(matches) > 1:
        ids = ", ".join(str(k.get("id")) for k in matches)
        raise ConfigError(f"id {ref!r} 匹配到多条记录（{ids}），请使用完整 id")
    return matches[0]


def _persist_update(ref: str, mutate, dry_run: bool = False) -> Dict[str, Any]:
    """在文件锁内 read-modify-write：保证并发下不丢更新。

    dry_run=True 时算出「会变成什么样」但绝不写盘。
    """
    with paths.file_lock(paths.keys_dist_path()):
        keys = _raw_keys()
        item = resolve_key(keys, ref)
        before = importers.key_out(item)
        changed = bool(mutate(item))
        item["id"] = str(item.get("id") or importers.fingerprint(item.get("key")))
        if changed and not dry_run:
            data = load_keys()
            data["keys"] = keys
            save_keys(data)
            paths.ensure_file_mode(paths.keys_dist_path())
    return {"changed": changed, "dry_run": bool(dry_run),
            "before": before, "after": importers.key_out(item)}


# ---------------------------------------------------------------------------
# 标签 / 归属 / 删除
# ---------------------------------------------------------------------------
def tag_key(ref: str, add: Any = (), remove: Any = (), dry_run: bool = False) -> Dict[str, Any]:
    """增删标签（去重保序；dry-run 绝不写盘）。"""
    to_add = exporters._list(add)
    to_rm = exporters._list(remove)
    if not to_add and not to_rm:
        raise UsageError("dist tag 至少需要 --add 或 --rm 之一")

    def mutate(item: Dict[str, Any]) -> bool:
        tags = exporters._list(item.get("tags"))
        original = list(tags)
        for t in to_add:
            if t not in tags:
                tags.append(t)
        tags = [t for t in tags if t not in to_rm]
        if tags == original:
            return False
        item["tags"] = tags
        return True

    return _persist_update(ref, mutate, dry_run=dry_run)


def assign_key(ref: str, to: str, note: str = "", dry_run: bool = False) -> Dict[str, Any]:
    """记录归属（谁 / 哪个客户端）。note 给空则保留原备注。"""
    target = str(to or "").strip()
    if not target:
        raise UsageError("dist assign 需要 --to（谁 / 哪个客户端）")

    def mutate(item: Dict[str, Any]) -> bool:
        changed = (item.get("assigned_to") or "") != target
        item["assigned_to"] = target
        if str(note or "").strip():
            if (item.get("notes") or "") != str(note).strip():
                item["notes"] = str(note).strip()
                changed = True
        return changed

    return _persist_update(ref, mutate, dry_run=dry_run)


def remove_key(ref: str, yes: bool = False, dry_run: bool = False) -> Dict[str, Any]:
    """从分发库删除一条记录（破坏性：必须 --yes 确认）。

    只删本地登记，不影响上游站点上的真实 Key（本工具不做双向同步）。
    """
    if not yes:
        raise UsageError("删除分发记录需显式确认：加 --yes（只删本地登记，不影响上游）")
    with paths.file_lock(paths.keys_dist_path()):
        keys = _raw_keys()
        item = resolve_key(keys, ref)
        removed = importers.key_out(item)
        if dry_run:
            return {"removed": removed, "dry_run": True, "remaining": len(keys)}
        data = load_keys()
        data["keys"] = [k for k in keys if k is not item]
        save_keys(data)
        paths.ensure_file_mode(paths.keys_dist_path())
    return {"removed": removed, "dry_run": False, "remaining": len(data["keys"])}


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def select_keys(tag: str = "", site: str = "", status: str = "",
                ids: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """按条件挑出待导出的原始记录（含 key 明文，只在本模块内流转）。"""
    keys = _raw_keys()
    if ids:
        wanted = [str(i).strip() for i in ids if str(i).strip()]
        keys = [k for k in keys if str(k.get("id")) in wanted]
    if tag:
        keys = [k for k in keys if tag in exporters._list(k.get("tags"))]
    if site:
        keys = [k for k in keys if (k.get("source_site") or "") == site]
    if status:
        keys = [k for k in keys if (k.get("status") or "") == status]
    return keys


def _site_channel_credentials(cfg: Optional[Dict[str, Any]], site: str) -> Dict[str, Any]:
    """取出站点 base / 管理员令牌 / 用户 ID（令牌只存在于内存，不写进结果）。"""
    if not site:
        raise ConfigError("导出到 New API 需要 --site <已登记站点名>")
    site_cfg = ((cfg or {}).get("sites") or {}).get(site)
    if not site_cfg:
        raise ConfigError(f"站点 {site} 未登记")
    auth = site_cfg.get("auth") or {}
    token = str(auth.get("access_token") or "").strip()
    if not token:
        raise ConfigError(
            f"站点 {site} 缺少管理员令牌（auth.access_token / access_token_env）")
    return {
        "base": site_cfg.get("base") or "",
        "token": token,
        "user_id": auth.get("user_id"),
        "proxy": site_cfg.get("proxy") or None,
    }


def export(keys: Iterable[Dict[str, Any]], target: str, **opts: Any) -> Dict[str, Any]:
    """统一导出入口。

    opts:
        app             claude / codex / all（仅 ccswitch）
        out             导出文件路径（写出即 0600；给出时默认允许明文）
        dry_run         True 则绝不写盘、绝不发写请求
        yes             newapi 实际写入的开关（默认 dry-run）
        reveal          显式要求把明文打到 stdout
        site            target=newapi 时的目标站点名
        cfg             全局配置（供取站点凭据）
        channel_type    New API 渠道 type
        transport       New API 的 mock transport（测试用，不打网络）
        timeout / name_prefix / tag / status / ids
    """
    target = str(target or "").strip().lower()
    if target not in EXPORT_TARGETS:
        raise ConfigError(f"不支持的导出目标: {target!r}（可选 {', '.join(EXPORT_TARGETS)}）")
    # 缺 base 时用已登记同站点的地址补齐（只在内存里补，不回写分发库）
    cfg = opts.get("cfg") or {}
    items: List[Dict[str, Any]] = []
    for raw in keys:
        item = dict(raw)
        if not str(item.get("base") or "").strip() and item.get("source_site"):
            site_cfg = (cfg.get("sites") or {}).get(item["source_site"]) or {}
            if site_cfg.get("base"):
                item["base"] = normalize_base(site_cfg["base"])
        items.append(item)
    out = opts.get("out")
    dry_run = bool(opts.get("dry_run"))
    yes = bool(opts.get("yes"))
    app = opts.get("app") or "all"
    # stdout 是否出现明文：只有用户显式 --reveal；写文件时才默认带明文（文件 0600）
    reveal_stdout = bool(opts.get("reveal"))

    def build(reveal: bool) -> str:
        if target == "ccswitch":
            return json.dumps(exporters.ccswitch_export(items, app=app, reveal=reveal),
                              ensure_ascii=False, indent=2) + "\n"
        return exporters.generic_export(target, items, reveal=reveal)

    result: Dict[str, Any] = {
        "target": target,
        "count": len(items),
        "dry_run": dry_run,
        "reveal": reveal_stdout,
        "path": None,
        "text": None,
        "items": None,
    }

    if target == "newapi":
        creds = _site_channel_credentials(opts.get("cfg"), opts.get("site") or "")
        # 实际写入必须显式 --yes；否则一律试运行
        write = bool(yes) and not dry_run
        payload = exporters.newapi_export(
            items, creds["base"], creds["token"], user_id=creds["user_id"],
            dry_run=not write, transport=opts.get("transport"),
            channel_type=opts.get("channel_type") or exporters.DEFAULT_CHANNEL_TYPE,
            name_prefix=opts.get("name_prefix") or "relayctl",
            timeout=opts.get("timeout") or 12.0, proxy=creds.get("proxy"))
        result.update(payload)
        result["dry_run"] = payload["dry_run"]
        result["site"] = opts.get("site")
        return result

    if target == "ccswitch":
        # 列表里的条目按 stdout 的 reveal 口径生成（写盘时另外生成明文版本）
        result.update(exporters.ccswitch_export(items, app=app, reveal=reveal_stdout))
    else:
        result["items"] = [importers.key_out(k) for k in items]

    if out and not dry_run:
        # 导出文件 0600，里面才允许出现明文；stdout 不再回显
        result["path"] = exporters.write_export_file(out, build(True))
        result["reveal"] = True
        result["text"] = None
    else:
        result["text"] = build(reveal_stdout)
    return result


def export_lines(result: Dict[str, Any]) -> List[str]:
    """渲染导出结果（绝不含明文 Key / 令牌）。"""
    lines = [f"[*] 分发导出 target={result['target']} 共 {result['count']} 条"
             + ("（试运行未写盘）" if result.get("dry_run") else "")]
    if result.get("path"):
        lines.append(f"[✓] 已写入 {result['path']}（0600，含凭据，请勿外传）")
    if result.get("site"):
        lines.append(f"    目标站点: {result['site']}")
    for item in (result.get("items") or []):
        if result["target"] == "ccswitch":
            lines.append(f"  {item['id']}  {item['key_masked']}  app={item['app']}  "
                         f"{'明文' if item.get('reveal') else '脱敏'}  {item['link']}")
        elif result["target"] == "newapi":
            mark = "[✓]" if item.get("ok") else "[!]"
            lines.append(f"  {mark} {item['id']}  {item['key_masked']}  "
                         f"渠道={item['channel_name']}  动作={item['action'] or '-'}  "
                         f"id={item.get('channel_id')}  {item.get('message') or ''}".rstrip())
        else:
            lines.append(f"  {item['id']}  {item['key_masked']}  "
                         f"站点={item.get('source_site') or '-'}  "
                         f"归属={item.get('assigned_to') or '-'}")
    if result["target"] in ("env", "json", "csv") and result.get("text"):
        lines.append("")
        lines.extend(result["text"].splitlines())
    if result["target"] == "newapi" and result.get("failed"):
        lines.append(f"[!] 失败 {result['failed']} 条")
    return lines


def list_lines(items: Sequence[Dict[str, Any]]) -> List[str]:
    """默认列表输出：只有 id 与 key_masked（避免截屏泄露）。"""
    if not items:
        return ["（暂无分发 Key）"]
    width = max([len(str(it.get("id") or "")) for it in items] + [2])
    return [f"{str(it.get('id') or ''):<{width}}  {it.get('key_masked')}" for it in items]
