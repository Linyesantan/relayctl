"""统一错误类型与进程退出码。

退出码约定（CLI 与上层调用方共用）：
    0 成功
    1 一般失败
    2 鉴权失败（HTTP 401 / AUTH_*）
    3 站点或适配器不支持该操作
    4 参数或配置问题（usage / config）
"""

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_AUTH = 2
EXIT_NOTSUPPORTED = 3
EXIT_USAGE = 4


class RelayctlError(Exception):
    """所有 relayctl 业务异常的基类。"""
    exit_code = EXIT_FAIL


class ConfigError(RelayctlError):
    """配置缺失 / 损坏 / 字段非法。"""
    exit_code = EXIT_USAGE


class UsageError(RelayctlError):
    """命令行参数问题。"""
    exit_code = EXIT_USAGE


class AuthError(RelayctlError):
    """鉴权失败：HTTP 401 或业务错误码 AUTH_*。"""
    exit_code = EXIT_AUTH


class NotSupported(RelayctlError):
    """当前站点或适配器不支持该操作（不猜测、不编造数据）。"""
    exit_code = EXIT_NOTSUPPORTED


class MigrationError(RelayctlError):
    """旧配置迁移失败。"""
    exit_code = EXIT_FAIL


def exit_code_for(exc: BaseException) -> int:
    """把异常映射为进程退出码。"""
    if isinstance(exc, SystemExit):
        code = exc.code
        if code is None:
            return EXIT_OK
        return code if isinstance(code, int) else EXIT_FAIL
    code = getattr(exc, "exit_code", None)
    if isinstance(code, int):
        return code
    return EXIT_FAIL
