"""站点脚本便捷基类（阶段 4）——零依赖，可直接复制到脚本里使用。

用法（脚本放在 $RELAYCTL_HOME/scripts/<站点>.py，0750 可执行）:

    #!/usr/bin/env python3
    from relayctl.script_base import ScriptAdapterBase, run_main

    class MySite(ScriptAdapterBase):
        def probe(self):
            return {"site": self.site, "type": "custom", "auth_ok": bool(self.token)}
            # 不实现的方法由基类自动以退出码 3 应答

    if __name__ == "__main__":
        run_main(MySite())

协议: <script> <action> [--json-args '<json>']
    action: probe | balance | groups | list_keys | create_key | update_key | delete_key
    stdout: 恰好一个 JSON 文档（多余输出视为协议违规）
    退出码: 0 成功 / 2 鉴权失败 / 3 不支持该动作 / 其它非 0 一般失败

本模块刻意只依赖标准库：脚本可以只 import 它，不会与宿主的 relayctl 包
产生循环导入，也可以整段复制进脚本独立运行。
"""

import inspect
import json
import os
import re
import sys
from typing import Any

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_AUTH = 2
EXIT_NOTSUPPORTED = 3

ACTIONS = (
    "probe", "balance", "groups", "list_keys",
    "create_key", "update_key", "delete_key",
)

_MAX_ERROR = 300

# 凭据样式的片段一律打码后再写 stderr（stdout 只允许一个 JSON 文档）
_REDACT_PATTERNS = (
    re.compile(r"(?i)\b(sk-[A-Za-z0-9._\-]{4,})"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{4,}"),
    re.compile(r"(?i)\b(token|access[_-]?token|api[_-]?key|apikey|password|passwd|secret|"
               r"authorization|cookie|session)\b(\s*[:=]\s*)([\"']?)([^\s,;'\"]+)(\3)"),
    re.compile(r"\b(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{24,}\b"),
)
_REDACTED = "***"


def redact(text: Any) -> str:
    """把文本里疑似凭据的片段替换为 ***。"""
    out = str(text or "")
    for pattern in _REDACT_PATTERNS:
        if pattern.groups >= 4:
            out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}"
                                        f"{_REDACTED}{m.group(5)}", out)
        else:
            out = pattern.sub(_REDACTED, out)
    return out


def sanitize_error(exc: BaseException) -> str:
    """异常 -> 脱敏后的一行字符串（绝不携带明文凭据）。"""
    text = f"{type(exc).__name__}: {exc}" if exc is not None else ""
    message = redact(text).replace("\n", " ").strip()
    if len(message) > _MAX_ERROR:
        message = message[:_MAX_ERROR] + "…"
    return message


class ScriptError(Exception):
    """一般失败（退出码 1）。"""
    exit_code = EXIT_FAIL


class AuthError(ScriptError):
    """鉴权失败（退出码 2）。"""
    exit_code = EXIT_AUTH


class NotSupportedError(ScriptError):
    """不支持该动作（退出码 3）。"""
    exit_code = EXIT_NOTSUPPORTED


class ActionNotSupported(NotSupportedError):
    def __init__(self, action: str):
        super().__init__(f"脚本未实现动作 {action}")
        self.action = action


class ScriptAdapterBase:
    """站点脚本基类：按动作分发，未实现的方法自动退出 3。"""

    ACTIONS = ACTIONS

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.env = env
        # 宿主只注入这几个变量（拿不到其它站点配置）
        self.site = env.get("RELAYCTL_SITE", "")
        self.base = env.get("RELAYCTL_BASE", "")
        self.token = env.get("RELAYCTL_TOKEN", "")
        self.cookie = env.get("RELAYCTL_COOKIE", "")
        self.user_id = env.get("RELAYCTL_USER_ID", "")
        self.proxy = env.get("RELAYCTL_PROXY", "")

    # -- 默认实现：一律 NotSupported（退出码 3） --------------------------
    def probe(self, **kwargs):
        raise ActionNotSupported("probe")

    def balance(self, **kwargs):
        raise ActionNotSupported("balance")

    def groups(self, **kwargs):
        raise ActionNotSupported("groups")

    def list_keys(self, **kwargs):
        raise ActionNotSupported("list_keys")

    def create_key(self, **kwargs):
        raise ActionNotSupported("create_key")

    def update_key(self, **kwargs):
        raise ActionNotSupported("update_key")

    def delete_key(self, **kwargs):
        raise ActionNotSupported("delete_key")

    # -- 分发 -------------------------------------------------------------
    def handle(self, action: str, args: dict = None) -> object:
        """执行一个动作，返回可 JSON 序列化的结果。"""
        if action not in self.ACTIONS:
            raise ActionNotSupported(action or "<空>")
        method = getattr(self, action, None)
        if not callable(method):
            raise ActionNotSupported(action)
        return self._call(method, args or {})

    @staticmethod
    def _call(method, args: dict):
        """只把方法签名接受的键传进去（宿主额外字段如 dry_run 不会造成 TypeError）。"""
        try:
            params = inspect.signature(method).parameters
        except (TypeError, ValueError):  # pragma: no cover - 内置/不可反射
            return method(**args)
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return method(**args)
        return method(**{k: v for k, v in args.items() if k in params})

    def main(self, argv=None) -> int:
        """进程入口：解析 argv -> 执行 -> 输出恰好一个 JSON 文档。"""
        argv = list(sys.argv[1:] if argv is None else argv)
        action = argv[0] if argv else ""
        try:
            args = parse_args(argv[1:])
            result = self.handle(action, args)
            payload = json.dumps(result, ensure_ascii=False, default=str)
        except NotSupportedError as exc:
            _warn(sanitize_error(exc))
            return exc.exit_code
        except AuthError as exc:
            _warn(sanitize_error(exc))
            return exc.exit_code
        except Exception as exc:  # noqa: BLE001 - 脚本异常一律收敛为退出码 1
            _warn(sanitize_error(exc))
            return EXIT_FAIL
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()
        return EXIT_OK


def parse_args(argv) -> dict:
    """解析 --json-args '<json>'（也接受 --json-args=<json>）。"""
    args: dict = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--json-args":
            if i + 1 >= len(argv):
                raise ScriptError("--json-args 缺少取值")
            args = _load_json(argv[i + 1])
            i += 2
            continue
        if token.startswith("--json-args="):
            args = _load_json(token.split("=", 1)[1])
            i += 1
            continue
        raise ScriptError(f"无法识别的参数: {token}")
    return args


def _load_json(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ScriptError(f"--json-args 不是合法 JSON: {exc}")
    if not isinstance(data, dict):
        raise ScriptError("--json-args 必须是 JSON 对象")
    return data


def _warn(message: str):
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def run_main(adapter: ScriptAdapterBase, argv=None):
    """脚本的 __main__ 入口。"""
    sys.exit(adapter.main(argv))
