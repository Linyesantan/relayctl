"""站点脚本的进程调用层（阶段 4）。

协议（脚本侧必须严格遵守）:

    <script> <action> [--json-args '<json>']

    action ∈ probe | balance | groups | list_keys | create_key | update_key | delete_key
    stdout  恰好一个 JSON 文档（多余输出 = 协议违规）
    退出码  0 成功 / 2 鉴权失败 / 3 不支持该动作 / 其它非 0 一般失败

宿主侧的安全约束:

    - 脚本路径解析为绝对路径，且必须位于 $RELAYCTL_HOME/scripts/ 下并可执行；
      不存在 / 不可执行 / 越界一律抛 ConfigError（退出码 4）。
    - 环境变量白名单: RELAYCTL_SITE / RELAYCTL_BASE / RELAYCTL_TOKEN / RELAYCTL_COOKIE /
      RELAYCTL_USER_ID / RELAYCTL_PROXY，另外只继承 PATH / HOME / LANG；
      绝不注入其它站点配置（含其它站点的 base / 凭据）。
    - 令牌只通过环境变量传递，绝不会出现在命令行参数里。
    - stderr 一律先脱敏再截断，才允许进入错误信息 / 日志。
"""

import json
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from . import paths
from .cache import redact_text
from .errors import (
    EXIT_AUTH, EXIT_FAIL, EXIT_NOTSUPPORTED,
    AuthError, ConfigError, NotSupported, RelayctlError,
)

# ---------------------------------------------------------------------------
# 协议常量
# ---------------------------------------------------------------------------
ACTIONS: Tuple[str, ...] = (
    "probe", "balance", "groups", "list_keys",
    "create_key", "update_key", "delete_key",
)
# 返回 JSON 数组的动作
LIST_ACTIONS = ("groups", "list_keys")
# 需要传 json-args 的动作
ARG_ACTIONS = ("create_key", "update_key", "delete_key")

DEFAULT_TIMEOUT = 15.0
SCRIPT_FILE_MODE = 0o750

ENV_SITE = "RELAYCTL_SITE"
ENV_BASE = "RELAYCTL_BASE"
ENV_TOKEN = "RELAYCTL_TOKEN"
ENV_COOKIE = "RELAYCTL_COOKIE"
ENV_USER_ID = "RELAYCTL_USER_ID"
ENV_PROXY = "RELAYCTL_PROXY"

SITE_ENV_VARS: Tuple[str, ...] = (
    ENV_SITE, ENV_BASE, ENV_TOKEN, ENV_COOKIE, ENV_USER_ID, ENV_PROXY,
)
# 只继承这三个与解释器运行相关的变量（RELAYCTL_HOME 等一律不继承）
INHERITED_ENV_VARS: Tuple[str, ...] = ("PATH", "HOME", "LANG")
# 数组动作允许的对象包装键
_LIST_WRAPPER_KEYS = ("items", "data", "keys", "groups")

_MAX_STDERR = 2000
_MAX_STDOUT_SNIPPET = 200

STATUS_OK = "ok"
STATUS_UNSUPPORTED = "unsupported"
STATUS_AUTH = "auth"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"


class ScriptError(RelayctlError):
    """脚本执行失败或输出不符合协议（一般失败，退出码 1）。"""
    exit_code = EXIT_FAIL


class ScriptProtocolError(ScriptError):
    """stdout 不是恰好一个 JSON 文档（或类型不符）。"""


class ScriptTimeoutError(ScriptError):
    """脚本执行超时。"""


class ScriptOutcome:
    """一次脚本调用的结果（成功与失败都返回，供 relayctl script test 逐动作展示）。"""

    __slots__ = ("action", "status", "exit_code", "data", "error", "stderr",
                 "elapsed", "exc")

    def __init__(self, action: str, status: str, exit_code: int, data: Any = None,
                 error: str = "", stderr: str = "", elapsed: float = 0.0,
                 exc: Optional[BaseException] = None):
        self.action = action
        self.status = status
        self.exit_code = exit_code
        self.data = data
        self.error = error or ""
        self.stderr = stderr or ""
        self.elapsed = elapsed
        self.exc = exc

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def as_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "status": self.status,
            "exit_code": self.exit_code,
            "result": self.data,
            "error": self.error or None,
            "stderr": self.stderr or None,
            "elapsed_ms": round(self.elapsed * 1000, 2),
        }


# ---------------------------------------------------------------------------
# 错误信息处理：脱敏 + 截断
# ---------------------------------------------------------------------------
def clean_stderr(text: Any, limit: int = _MAX_STDERR) -> str:
    """stderr 脱敏并截断（绝不把原始凭据带进报错 / 日志）。"""
    out = redact_text(text).strip()
    if len(out) > limit:
        out = out[:limit] + "…（已截断）"
    return out


# ---------------------------------------------------------------------------
# 脚本路径解析
# ---------------------------------------------------------------------------
def _under_scripts_dir(path: str, scripts_dir: str) -> bool:
    real = os.path.realpath(path)
    root = os.path.realpath(scripts_dir)
    return real == root or real.startswith(root + os.sep)


def resolve_script_path(script: Any, site: Optional[str] = None) -> str:
    """把站点配置里的 script 解析为绝对路径。

    规则:
      - 绝对路径直接使用；
      - 相对路径先按 $RELAYCTL_HOME 解析（配置里写 scripts/<name>.py），
        再按 $RELAYCTL_HOME/scripts 解析（配置里只写 <name>.py）；
      - 结果必须落在 $RELAYCTL_HOME/scripts/ 下，否则视为越界。

    不存在 / 越界 / 不可执行 -> 明确的 ConfigError。
    """
    where = f"站点 {site}" if site else "脚本"
    if not script or not isinstance(script, str):
        raise ConfigError(f"{where} 未配置 script（adapter=script 时必须提供脚本路径）")

    script = script.strip()
    if not script:
        raise ConfigError(f"{where} 的 script 配置为空")

    home = paths.home()
    scripts = paths.scripts_dir()
    if os.path.isabs(script):
        candidate = os.path.abspath(script)
    else:
        in_home = os.path.abspath(os.path.join(home, script))
        in_scripts = os.path.abspath(os.path.join(scripts, script))
        candidate = in_home if os.path.exists(in_home) else in_scripts

    if not _under_scripts_dir(candidate, scripts):
        raise ConfigError(
            f"{where} 的脚本路径越界（必须位于 {scripts} 下）: {script} -> {candidate}")

    if not os.path.exists(candidate):
        raise ConfigError(f"{where} 的脚本不存在: {candidate}")
    if not os.path.isfile(candidate):
        raise ConfigError(f"{where} 的脚本不是普通文件: {candidate}")
    if not os.access(candidate, os.X_OK):
        raise ConfigError(f"{where} 的脚本不可执行（chmod 0750 <脚本>）: {candidate}")
    return candidate


def resolve_timeout(core_cfg: Optional[dict]) -> float:
    """读取全局 script.timeout（秒），缺省 15s。"""
    value = ((core_cfg or {}).get("script") or {}).get("timeout")
    if isinstance(value, bool):
        return DEFAULT_TIMEOUT
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return DEFAULT_TIMEOUT
        return parsed if parsed > 0 else DEFAULT_TIMEOUT
    return DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# 环境变量白名单
# ---------------------------------------------------------------------------
def _package_root() -> Optional[str]:
    """relayctl 包所在目录的上一级（用于子进程 import relayctl.script_base）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_env(site: str, site_cfg: dict, core_cfg: Optional[dict] = None) -> Dict[str, str]:
    """构造子进程环境：只给本站这几个变量，外加 PATH/HOME/LANG。

    PYTHONPATH 只作为解释器导入路径指向 relayctl 包根目录（不含任何站点配置），
    站点凭据一律只经 RELAYCTL_* 传递，绝不出现在 argv 里。
    """
    cfg = site_cfg or {}
    auth = cfg.get("auth") or {}
    env: Dict[str, str] = {}

    env[ENV_SITE] = str(site or cfg.get("name") or "")
    env[ENV_BASE] = str(cfg.get("base") or "")
    env[ENV_TOKEN] = str(auth.get("access_token") or "")
    env[ENV_COOKIE] = str(auth.get("cookie") or "")
    user_id = auth.get("user_id")
    env[ENV_USER_ID] = "" if user_id is None else str(user_id)
    env[ENV_PROXY] = str(cfg.get("proxy") or (core_cfg or {}).get("proxy") or "")

    for key in INHERITED_ENV_VARS:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value

    root = _package_root()
    if root:
        existing = os.environ.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{root}{os.pathsep}{existing}" if existing else root
    return env


# ---------------------------------------------------------------------------
# stdout 严格解析
# ---------------------------------------------------------------------------
def parse_stdout(text: Any, action: str) -> Any:
    """把 stdout 解析为恰好一个 JSON 文档，并校验动作要求的类型。"""
    raw = (text or "").strip()
    if not raw:
        raise ScriptProtocolError(f"动作 {action} 的 stdout 为空（协议要求恰好一个 JSON 文档）")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        snippet = redact_text(raw)[:_MAX_STDOUT_SNIPPET]
        raise ScriptProtocolError(
            f"动作 {action} 的 stdout 不是恰好一个 JSON 文档（{exc}）: {snippet}")
    if action in LIST_ACTIONS:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in _LIST_WRAPPER_KEYS:
                if isinstance(data.get(key), list):
                    return data[key]
        raise ScriptProtocolError(
            f"动作 {action} 要求 stdout 为 JSON 数组，得到 {type(data).__name__}")
    if not isinstance(data, dict):
        raise ScriptProtocolError(
            f"动作 {action} 要求 stdout 为 JSON 对象，得到 {type(data).__name__}")
    return data


# ---------------------------------------------------------------------------
# 调用器
# ---------------------------------------------------------------------------
class ScriptRunner:
    """按协议调用站点脚本。"""

    def __init__(self, site: str, site_cfg: dict, core_cfg: Optional[dict] = None):
        self.site = site
        self.cfg = site_cfg or {}
        self.core_cfg = core_cfg or {}
        self.path = resolve_script_path(self.cfg.get("script"), site=site)
        self.timeout = resolve_timeout(self.core_cfg)
        self.env = build_env(site, self.cfg, self.core_cfg)

    def build_argv(self, action: str, args: Optional[dict] = None) -> List[str]:
        """argv 里绝不会出现凭据（令牌只走环境变量）。"""
        argv = [self.path, action]
        if args:
            argv += ["--json-args", json.dumps(args, ensure_ascii=False, sort_keys=True)]
        return argv

    def invoke(self, action: str, args: Optional[dict] = None,
               timeout: Optional[float] = None) -> ScriptOutcome:
        """执行一次脚本调用，永远返回 ScriptOutcome（失败不抛，便于逐动作体检）。"""
        if action not in ACTIONS:
            return ScriptOutcome(action, STATUS_UNSUPPORTED, EXIT_NOTSUPPORTED,
                                 error=f"宿主不支持的动作: {action}")
        limit = self.timeout if timeout is None else timeout
        argv = self.build_argv(action, args)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv, shell=False, capture_output=True, text=True,
                errors="replace", timeout=limit, env=self.env,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = clean_stderr(exc.stderr)
            return ScriptOutcome(
                action, STATUS_TIMEOUT, EXIT_FAIL,
                error=f"脚本执行超时（>{limit}s）: {os.path.basename(self.path)}",
                stderr=stderr, elapsed=time.monotonic() - started)
        except OSError as exc:
            return ScriptOutcome(
                action, STATUS_ERROR, EXIT_FAIL,
                error=f"脚本无法执行: {clean_stderr(exc)}",
                elapsed=time.monotonic() - started)

        elapsed = time.monotonic() - started
        stderr = clean_stderr(proc.stderr)
        code = proc.returncode

        if code == 0:
            try:
                data = parse_stdout(proc.stdout, action)
            except ScriptProtocolError as exc:
                return ScriptOutcome(action, STATUS_ERROR, code, error=str(exc),
                                     stderr=stderr, elapsed=elapsed, exc=exc)
            return ScriptOutcome(action, STATUS_OK, code, data=data,
                                 stderr=stderr, elapsed=elapsed)

        detail = f"动作 {action} 失败（退出码 {code}）"
        if stderr:
            detail += f": {stderr}"
        if code == EXIT_AUTH:
            return ScriptOutcome(action, STATUS_AUTH, code, error=detail,
                                 stderr=stderr, elapsed=elapsed)
        if code == EXIT_NOTSUPPORTED:
            return ScriptOutcome(action, STATUS_UNSUPPORTED, code, error=detail,
                                 stderr=stderr, elapsed=elapsed)
        return ScriptOutcome(action, STATUS_ERROR, code, error=detail,
                             stderr=stderr, elapsed=elapsed)

    def run(self, action: str, args: Optional[dict] = None,
            timeout: Optional[float] = None) -> Any:
        """执行并解析结果；失败按退出码语义抛对应异常（3 -> NotSupported）。"""
        outcome = self.invoke(action, args=args, timeout=timeout)
        if outcome.ok:
            return outcome.data
        if outcome.status == STATUS_AUTH:
            raise AuthError(outcome.error)
        if outcome.status == STATUS_UNSUPPORTED:
            raise NotSupported(outcome.error)
        if outcome.status == STATUS_TIMEOUT:
            raise ScriptTimeoutError(outcome.error)
        if outcome.exc is not None:
            raise outcome.exc
        raise ScriptError(outcome.error)


# ---------------------------------------------------------------------------
# 脚本模板（relayctl script new）
# ---------------------------------------------------------------------------
DEFAULT_SCRIPT_HOWTO = (
    "站点脚本由 relayctl 以 <script> <action> [--json-args '<json>'] 调用；"
    "补全脚本里的 TODO 后执行 relayctl script test <站点> 验证。"
    "凭据只从环境变量 RELAYCTL_TOKEN / RELAYCTL_COOKIE 读取，不要写死在脚本里。"
)

SCRIPT_TEMPLATE = '''#!/usr/bin/env python3
"""relayctl 站点脚本：{site}

协议: <script> <action> [--json-args '<json>']
  action: probe | balance | groups | list_keys | create_key | update_key | delete_key
  stdout: 恰好一个 JSON 文档（多余输出视为协议违规）
  退出码: 0 成功 / 2 鉴权失败 / 3 不支持该动作 / 其它非 0 一般失败

宿主注入的环境变量（只有这些，拿不到其它站点配置）:
  RELAYCTL_SITE / RELAYCTL_BASE / RELAYCTL_TOKEN / RELAYCTL_COOKIE
  RELAYCTL_USER_ID / RELAYCTL_PROXY

docs.script_howto:
{howto}

未实现的方法由基类自动返回退出码 3（NotSupported），
relayctl script test {site} 会把它标记为 unsupported 而不是 failed。
"""
from relayctl.script_base import ScriptAdapterBase, run_main


class {cls}(ScriptAdapterBase):
    """TODO: 按站点实际接口实现下面的方法。

    没实现的方法保持不动即可：基类会自动以退出码 3 应答，
    宿主会把它映射为 NotSupported（relayctl script test 记为 unsupported）。
    """

    # def probe(self):
    #     return {{
    #         "site": self.site,
    #         "type": "custom",
    #         "system_name": "自定义面板",
    #         "version": "",
    #         "auth_ok": bool(self.token),
    #         "quota_per_unit": 500000,
    #         "quota_display_type": "USD",
    #     }}

    # def balance(self):
    #     return {{"balance": "0.0000", "used": "0.0000", "raw_quota": 0,
    #              "raw_used": 0, "quota_per_unit": 500000,
    #              "display_type": "USD", "user_group": "default"}}

    # def groups(self):
    #     return [{{"name": "default", "desc": "默认分组", "ratio": 1.0}}]

    # def list_keys(self):
    #     return [{{"id": 1, "name": "k1", "group": "default", "status": 1,
    #               "unlimited": False, "remain": 1.0, "used": 0.0,
    #               "key": "sk-...", "models": []}}]

    # def create_key(self, name=None, group=None, quota=None, unlimited=False,
    #                models=None, **kwargs):
    #     return {{"id": 1, "name": name, "group": group, "unlimited": unlimited,
    #              "key_masked": ""}}

    # def update_key(self, key_id=None, **kwargs):
    #     return {{"id": key_id, "updated": True}}

    # def delete_key(self, key_id=None, **kwargs):
    #     return {{"id": key_id, "ok": True}}


if __name__ == "__main__":
    run_main({cls}())
'''


def script_class_name(site: str) -> str:
    """站点名 -> 模板里的类名（demo-site -> DemoSiteScript）。"""
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", str(site)) if p]
    name = "".join(p[:1].upper() + p[1:] for p in parts) or "Site"
    if not name.endswith("Script"):
        name += "Script"
    return name


def script_template(site: str, howto: str = "") -> str:
    """渲染可直接运行的脚本模板。"""
    return SCRIPT_TEMPLATE.format(
        site=site,
        cls=script_class_name(site),
        howto=(howto or DEFAULT_SCRIPT_HOWTO).strip(),
    )
