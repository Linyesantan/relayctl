"""relayctl 配置与协议的 JSON Schema（draft 2020-12）。"""

from copy import deepcopy
from typing import Any, Dict

DRAFT = "https://json-schema.org/draft/2020-12/schema"


def _string(default: str = "") -> Dict[str, Any]:
    return {"type": "string", "default": default}


def site_schema() -> Dict[str, Any]:
    auth = {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "access_token", "access_token_env", "user_id", "username",
                     "password", "password_env", "cookie"],
        "properties": {
            "type": {"type": "string", "enum": ["access_token", "cookie", "password", "none"]},
            "access_token": _string(),
            "access_token_env": _string(),
            "user_id": {"type": ["integer", "null"]},
            "username": _string(),
            "password": _string(),
            "password_env": _string(),
            "cookie": _string(),
        },
    }
    docs = {
        "type": "object",
        "additionalProperties": False,
        "required": ["login_method", "token_howto", "script_howto", "notes"],
        "properties": {name: _string() for name in
                       ("login_method", "token_howto", "script_howto", "notes")},
    }
    return {
        "$schema": DRAFT,
        "title": "relayctl v2 site configuration",
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "base", "panel", "adapter", "auth", "docs", "tags", "status"],
        "properties": {
            "name": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
            "base": {"type": "string", "pattern": "^https?://"},
            "console_url": _string(),
            "panel": {"type": "string", "enum": ["newapi", "oneapi", "custom"]},
            "adapter": {"type": "string", "enum": ["newapi", "oneapi", "script"]},
            "script": {"type": ["string", "null"]},
            "auth": auth,
            "docs": docs,
            "proxy": _string(),
            "refresh_interval": {"type": "string", "pattern": "^$|^\\d+[smhd]?$"},
            "tags": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
            "status": {"type": "string", "enum": ["active", "parked"]},
            "parked_reason": _string(),
        },
        "allOf": [{
            "if": {"properties": {"adapter": {"const": "script"}}, "required": ["adapter"]},
            "then": {"properties": {"script": {"type": "string", "minLength": 1}},
                     "required": ["script"]},
        }],
    }


def script_schema() -> Dict[str, Any]:
    action = {"type": "string", "enum": ["probe", "balance", "groups", "list_keys", "create_key",
                                               "update_key", "delete_key"]}
    return {
        "$schema": DRAFT,
        "title": "relayctl custom script protocol",
        "description": "脚本按 <script> <action> [--json-args '<json>'] 调用；stdout 仅输出响应 JSON。",
        "type": "object",
        "additionalProperties": False,
        "required": ["request", "response", "exit_codes"],
        "properties": {
            "request": {
                "type": "object", "additionalProperties": False,
                "required": ["action", "args"],
                "properties": {"action": action, "args": {"type": "object"}},
            },
            "response": {
                "oneOf": [
                    {"type": "object", "required": ["ok", "data"],
                     "properties": {"ok": {"const": True}, "data": {}},
                     "additionalProperties": False},
                    {"type": "object", "required": ["ok", "error"],
                     "properties": {"ok": {"const": False}, "error": {"type": "string"}},
                     "additionalProperties": False},
                ]
            },
            "exit_codes": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "success": {"const": 0}, "failure": {"const": 1},
                    "auth": {"const": 2}, "unsupported": {"const": 3},
                    "usage": {"const": 4},
                },
                "required": ["success", "failure", "auth", "unsupported", "usage"],
            },
        },
    }


def dist_schema() -> Dict[str, Any]:
    item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "key", "source_site", "base", "tags", "models", "groups",
                     "assigned_to", "notes", "status"],
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "key": {"type": "string", "minLength": 1},
            "source_site": {"type": "string"},
            "base": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
            "models": {"type": "array", "items": {"type": "string"}},
            "groups": {"type": "array", "items": {"type": "string"}},
            "assigned_to": {"type": "string"},
            "notes": {"type": "string"},
            "status": {"type": "string"},
        },
    }
    return {
        "$schema": DRAFT,
        "title": "relayctl dist/keys.yaml",
        "type": "object",
        "additionalProperties": False,
        "required": ["version", "keys"],
        "properties": {
            "version": {"const": 1},
            "keys": {"type": "array", "items": item},
        },
    }


def schemas(kind: str = "all") -> Dict[str, Any]:
    available = {"site": site_schema(), "script": script_schema(), "dist": dist_schema()}
    if kind == "all":
        return deepcopy(available)
    if kind not in available:
        raise ValueError(f"未知 schema kind: {kind}")
    return deepcopy(available[kind])
