"""零依赖 stdio MCP 服务（每行一个 JSON-RPC 2.0 帧）。"""

import json
import sys
from typing import Any, Callable, Dict, List, Optional

from . import cache as rcache
from . import config, distribution, doctor, importers, models
from .adapters import get_adapter
from .errors import (EXIT_AUTH, EXIT_FAIL, EXIT_NOTSUPPORTED, EXIT_USAGE,
                     NotSupported, UsageError, exit_code_for)

JSONRPC = "2.0"


def _schema(properties: Optional[Dict[str, Any]] = None,
            required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


BASE_TOOLS = {
    "status": ("列出站点状态（不显示凭据）", _schema()),
    "balance": ("读取站点余额", _schema({
        "site": {"type": "string"}, "refresh": {"type": "boolean"}}, ["site"])),
    "groups": ("读取站点分组", _schema({
        "site": {"type": "string"}, "refresh": {"type": "boolean"}}, ["site"])),
    "keys": ("读取站点 Key 列表（仅脱敏值）", _schema({
        "site": {"type": "string"}, "refresh": {"type": "boolean"}}, ["site"])),
    "key_add": ("创建上游 Key（结果仅含脱敏值）", _schema({
        "site": {"type": "string"}, "name": {"type": "string"},
        "group": {"type": "string"}, "quota": {"type": ["number", "null"]},
        "unlimited": {"type": "boolean"}, "models": {"type": "array", "items": {"type": "string"}},
        "dry_run": {"type": "boolean"}}, ["site", "name", "group"])),
    "import_sites": ("导入站点配置", _schema({
        "content": {"type": "string"}, "file": {"type": "string"},
        "format": {"type": "string"}, "dry_run": {"type": "boolean"}})),
    "import_keys": ("导入分发 Key（响应仅含脱敏值）", _schema({
        "content": {"type": "string"}, "file": {"type": "string"},
        "format": {"type": "string"}, "dry_run": {"type": "boolean"}})),
    "dist_export": ("导出分发数据；默认禁止明文 stdout/文件导出", _schema({
        "target": {"type": "string"}, "tag": {"type": "string"},
        "site": {"type": "string"}, "app": {"type": "string"},
        "channel_type": {"type": ["string", "integer"]},
        "dry_run": {"type": "boolean"}, "yes": {"type": "boolean"},
        "out": {"type": "string"}, "reveal": {"type": "boolean"}}, ["target"])),
    "site_add": ("非交互新增站点", _schema({
        "site": {"type": "object"}, "name": {"type": "string"},
        "base": {"type": "string"}, "panel": {"type": "string"},
        "adapter": {"type": "string"}, "auth": {"type": "object"},
        "docs": {"type": "object"}, "tags": {"type": "array", "items": {"type": "string"}},
        "refresh_interval": {"type": "string"}, "status": {"type": "string"}})),
    "doctor": ("检查本地配置与权限", _schema({
        "check_connectivity": {"type": "boolean"}})),
}

DESTRUCTIVE_TOOLS = {
    "key_rm": ("删除上游 Key", _schema({
        "site": {"type": "string"}, "id": {"type": "integer"},
        "confirm": {"const": True}}, ["site", "id", "confirm"])),
    "site_rm": ("删除本地站点配置", _schema({
        "site": {"type": "string"}, "confirm": {"const": True}}, ["site", "confirm"])),
}

REVEAL_TOOLS = {
    "dist_export_reveal": ("显式执行可能包含明文 Key 的分发导出", BASE_TOOLS["dist_export"][1]),
}


def _public(value: Any) -> Any:
    """MCP 永远只返回脱敏结构，错误文本也走统一凭据遮盖。"""
    value = models.sanitize(value)
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_public(item) for item in value]
    if isinstance(value, str):
        return rcache.redact_text(value)
    return value


def _site(cfg: Dict[str, Any], name: Any) -> Dict[str, Any]:
    site_name = str(name or "").strip()
    site = (cfg.get("sites") or {}).get(site_name)
    if not site:
        raise UsageError(f"站点 {site_name or '<空>'} 未登记")
    return site


def _mode(args: Dict[str, Any]) -> str:
    return rcache.MODE_REFRESH if args.get("refresh") else rcache.MODE_AUTO


class MCPServer:
    def __init__(self, allow_destructive: bool = False, allow_reveal: bool = False):
        self.allow_destructive = bool(allow_destructive)
        self.allow_reveal = bool(allow_reveal)
        self._specs = dict(BASE_TOOLS)
        if self.allow_destructive:
            self._specs.update(DESTRUCTIVE_TOOLS)
        if self.allow_reveal:
            self._specs.update(REVEAL_TOOLS)

    def tools(self) -> List[Dict[str, Any]]:
        return [{"name": name, "description": spec[0], "inputSchema": spec[1]}
                for name, spec in sorted(self._specs.items())]

    def _status(self, args: Dict[str, Any]) -> Any:
        cfg = config.load_config()
        return {
            "sites": [{
                "name": name,
                "status": site.get("status"),
                "panel": site.get("panel"),
                "adapter": site.get("adapter"),
                "tags": list(site.get("tags") or []),
            } for name, site in sorted((cfg.get("sites") or {}).items())],
            "site_errors": cfg.get("site_errors") or [],
        }

    def _read_adapter(self, tool: str, args: Dict[str, Any]) -> Any:
        cfg = config.load_config()
        name = str(args.get("site") or "")
        site = _site(cfg, name)
        adapter = get_adapter(name, site, cfg)
        method: Callable[[], Any] = {
            "balance": adapter.balance,
            "groups": adapter.groups,
            "keys": adapter.list_keys,
        }[tool]
        data, meta = rcache.fetch(name, tool, method, _mode(args))
        return {"site": name, "value": data, "meta": meta}

    def _key_add(self, args: Dict[str, Any]) -> Any:
        cfg = config.load_config()
        name = str(args.get("site") or "")
        site = _site(cfg, name)
        unlimited = bool(args.get("unlimited"))
        quota = args.get("quota")
        if not unlimited and quota is None:
            raise UsageError("必须提供 quota 或 unlimited=true")
        preview = {
            "site": name, "name": str(args.get("name") or ""),
            "group": str(args.get("group") or ""), "quota": quota,
            "unlimited": unlimited, "models": list(args.get("models") or []),
            "dry_run": bool(args.get("dry_run")),
        }
        if preview["dry_run"]:
            return preview
        result = get_adapter(name, site, cfg).create_key(
            name=preview["name"], group=preview["group"], quota=quota,
            unlimited=unlimited, models=preview["models"] or None)
        rcache.invalidate(name, "keys")
        return {"site": name, "id": result.get("id"), "key_masked": result.get("key_masked")}

    @staticmethod
    def _input_text(args: Dict[str, Any]) -> str:
        if "content" in args:
            return str(args.get("content") or "")
        filename = str(args.get("file") or "").strip()
        if not filename:
            raise UsageError("必须提供 content 或 file")
        return importers.read_input(filename)

    def _import(self, kind: str, args: Dict[str, Any]) -> Any:
        text = self._input_text(args)
        fmt = str(args.get("format") or "auto")
        dry_run = bool(args.get("dry_run"))
        cfg = config.load_config()
        if kind == "sites":
            candidates, errors = importers.parse_sites(text, fmt=fmt)
            return importers.apply_sites_import(candidates, dry_run=dry_run,
                                                cfg=cfg, parse_errors=errors)
        candidates, errors = importers.parse_keys(text, fmt=fmt)
        return importers.apply_keys_import(candidates, dry_run=dry_run,
                                           cfg=cfg, parse_errors=errors)

    def _dist_export(self, args: Dict[str, Any], reveal_enabled: bool = False) -> Any:
        target = str(args.get("target") or "")
        reveal = bool(args.get("reveal"))
        out = str(args.get("out") or "").strip() or None
        if not reveal_enabled and (reveal or out):
            raise NotSupported("MCP 默认禁止明文 stdout/文件导出；请用 --allow-reveal 启动")
        cfg = config.load_config()
        site_filter = "" if target == "newapi" else str(args.get("site") or "")
        keys = distribution.select_keys(tag=str(args.get("tag") or ""), site=site_filter)
        result = distribution.export(
            keys, target, app=str(args.get("app") or "all"), out=out,
            dry_run=bool(args.get("dry_run", True)), yes=bool(args.get("yes")),
            reveal=reveal and reveal_enabled, site=args.get("site"), cfg=cfg,
            channel_type=str(args.get("channel_type") or "1"))
        # 即使显式允许 reveal，协议响应仍不回传明文；明文只允许进入用户指定的 0600 文件。
        return _public(result)

    def _site_add(self, args: Dict[str, Any]) -> Any:
        supplied = args.get("site") if isinstance(args.get("site"), dict) else args
        data = dict(config.SITE_DEFAULTS)
        data.update({key: value for key, value in supplied.items()
                     if key in config.SITE_FIELDS and value is not None})
        data["auth"] = {**config.DEFAULT_AUTH, **(supplied.get("auth") or {})}
        data["docs"] = {**config.DEFAULT_DOCS, **(supplied.get("docs") or {})}
        name = str(data.get("name") or "").strip()
        if not name or not data.get("base"):
            raise UsageError("site_add 必须提供 name 与 base")
        filename = config.save_site(name, data)
        return {"name": name, "path": filename, "site": config.load_site(name)}

    def _destructive(self, tool: str, args: Dict[str, Any]) -> Any:
        if args.get("confirm") is not True:
            raise UsageError("破坏性工具必须提供 confirm=true")
        name = str(args.get("site") or "")
        if tool == "site_rm":
            if not config.delete_site(name):
                raise UsageError(f"站点 {name} 不存在")
            rcache.invalidate(name)
            return {"removed": name}
        cfg = config.load_config()
        site = _site(cfg, name)
        get_adapter(name, site, cfg).delete_key(key_id=int(args.get("id")))
        rcache.invalidate(name, "keys")
        return {"site": name, "removed_id": int(args.get("id"))}

    def call_tool(self, name: str, args: Optional[Dict[str, Any]] = None) -> Any:
        if name not in self._specs:
            raise MethodNotFound(f"未知或未启用的工具: {name}")
        values = args or {}
        if not isinstance(values, dict):
            raise UsageError("arguments 必须是对象")
        if name == "status":
            return self._status(values)
        if name in ("balance", "groups", "keys"):
            return self._read_adapter(name, values)
        if name == "key_add":
            return self._key_add(values)
        if name == "import_sites":
            return self._import("sites", values)
        if name == "import_keys":
            return self._import("keys", values)
        if name == "dist_export":
            return self._dist_export(values, reveal_enabled=False)
        if name == "dist_export_reveal":
            return self._dist_export(values, reveal_enabled=True)
        if name == "site_add":
            return self._site_add(values)
        if name == "doctor":
            report, code = doctor.run(bool(values.get("check_connectivity")))
            return {"exit_code": code, **report}
        if name in DESTRUCTIVE_TOOLS:
            return self._destructive(name, values)
        raise MethodNotFound(f"未知方法: {name}")

    def handle(self, request: Any) -> Optional[Dict[str, Any]]:
        request_id = request.get("id") if isinstance(request, dict) else None
        if not isinstance(request, dict) or request.get("jsonrpc") != JSONRPC \
                or not isinstance(request.get("method"), str):
            return _error(request_id, -32600, "Invalid Request", EXIT_USAGE)
        method = request["method"]
        params = request.get("params") or {}
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "relayctl", "version": "2.0.0"},
                }
            elif method == "notifications/initialized":
                return None
            elif method == "tools/list":
                result = {"tools": self.tools()}
            elif method == "tools/call":
                if not isinstance(params, dict):
                    raise UsageError("params 必须是对象")
                name = params.get("name")
                if not isinstance(name, str):
                    raise UsageError("tools/call 缺少 name")
                value = self.call_tool(name, params.get("arguments") or {})
                result = {
                    "content": [{"type": "text", "text": json.dumps(
                        _public(value), ensure_ascii=False, sort_keys=True)}],
                    "structuredContent": _public(value),
                    "isError": False,
                }
            else:
                raise MethodNotFound(f"Method not found: {method}")
        except MethodNotFound as exc:
            return _error(request_id, -32601, str(exc), EXIT_NOTSUPPORTED)
        except Exception as exc:  # 每帧独立失败，不终止 stdio 服务
            exit_code = exit_code_for(exc)
            rpc_code = {-1: -32603, EXIT_FAIL: -32001, EXIT_AUTH: -32002,
                        EXIT_NOTSUPPORTED: -32003, EXIT_USAGE: -32602}.get(exit_code, -32603)
            return _error(request_id, rpc_code, str(exc), exit_code)
        if "id" not in request:
            return None
        return {"jsonrpc": JSONRPC, "id": request_id, "result": result}


class MethodNotFound(NotSupported):
    pass


def _error(request_id: Any, code: int, message: str, exit_code: int) -> Dict[str, Any]:
    return {
        "jsonrpc": JSONRPC,
        "id": request_id,
        "error": {
            "code": int(code),
            "message": rcache.redact_text(message or "请求失败"),
            "data": {"exit_code": int(exit_code)},
        },
    }


def run_stdio(allow_destructive: bool = False, allow_reveal: bool = False,
              stdin=None, stdout=None, stderr=None) -> int:
    """运行逐行 JSON-RPC；stdout 仅写协议帧，诊断日志只写 stderr。"""
    source = stdin or sys.stdin
    target = stdout or sys.stdout
    logs = stderr or sys.stderr
    server = MCPServer(allow_destructive=allow_destructive, allow_reveal=allow_reveal)
    for line in source:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except ValueError:
            response = _error(None, -32700, "Parse error", EXIT_USAGE)
        else:
            response = server.handle(request)
        if response is not None:
            target.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            target.flush()
    # 保留 stderr 参数为协议日志专用；正常 EOF 不产生任何日志。
    del logs
    return 0
