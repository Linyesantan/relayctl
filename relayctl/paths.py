"""v2 运行时布局（全部路径动态解析，支持 RELAYCTL_HOME 覆盖）。

RELAYCTL_HOME 默认为 ~/.config/relayctl（目录 0700），布局：

    config.yaml            全局配置
    sites/<name>.yaml      每站一个文件，隔离加载
    scripts/               站点脚本（阶段 3）
    cache/<name>.json      每站缓存
    dist/keys.yaml         分发产物（阶段 5）
    logs/                  日志

所有文件写入均为「临时文件 -> chmod 0600 -> os.replace」原子写。
"""

import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

import yaml

from .errors import ConfigError

ENV_HOME = "RELAYCTL_HOME"
DEFAULT_HOME = "~/.config/relayctl"

DIR_MODE = 0o700
FILE_MODE = 0o600

_SITE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def home() -> str:
    """动态解析 RELAYCTL_HOME（每次调用都读环境变量，便于测试重定向）。"""
    raw = os.environ.get(ENV_HOME)
    if raw is None or not str(raw).strip():
        raw = DEFAULT_HOME
    return os.path.abspath(os.path.expanduser(str(raw).strip()))


def config_path() -> str:
    return os.path.join(home(), "config.yaml")


def sites_dir() -> str:
    return os.path.join(home(), "sites")


def legacy_sites_path() -> str:
    """v1 遗留的单文件 sites.yaml（仅迁移时读取）。"""
    return os.path.join(home(), "sites.yaml")


def scripts_dir() -> str:
    return os.path.join(home(), "scripts")


def cache_dir() -> str:
    return os.path.join(home(), "cache")


def dist_dir() -> str:
    return os.path.join(home(), "dist")


def keys_dist_path() -> str:
    return os.path.join(dist_dir(), "keys.yaml")


def logs_dir() -> str:
    return os.path.join(home(), "logs")


def layout_dirs() -> Dict[str, str]:
    return {
        "home": home(),
        "sites": sites_dir(),
        "scripts": scripts_dir(),
        "cache": cache_dir(),
        "dist": dist_dir(),
        "logs": logs_dir(),
    }


def cache_path(name: str) -> str:
    """站点缓存路径（站点名先做防穿越清洗）。"""
    return os.path.join(cache_dir(), f"{safe_name(name)}.json")


def site_path(name: str) -> str:
    """站点配置路径 sites/<name>.yaml。"""
    return os.path.join(sites_dir(), f"{safe_site_name(name)}.yaml")


def safe_name(name: Any) -> str:
    """宽松清洗：把非 [A-Za-z0-9._-] 的字符替换为 _，保证不逃出目录。"""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(name))


def safe_site_name(name: Any) -> str:
    """严格校验站点名：拒绝空值、路径分隔符与相对路径穿越。"""
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("站点名不能为空")
    candidate = name.strip()
    if candidate.endswith(".yaml") or candidate.endswith(".yml"):
        candidate = candidate.rsplit(".", 1)[0]
    if candidate in (".", "..") or "/" in candidate or "\\" in candidate or os.sep in candidate:
        raise ConfigError(f"非法站点名（含路径分隔符）: {name!r}")
    if not _SITE_NAME_RE.match(candidate):
        raise ConfigError(
            f"非法站点名: {name!r}（只允许字母、数字、点、下划线、连字符，且以字母或数字开头）"
        )
    return candidate


def ensure_dir(path: str, mode: int = DIR_MODE) -> str:
    """创建目录并强制 0700。"""
    os.makedirs(path, mode=mode, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return path


def init_dirs() -> Dict[str, str]:
    """创建全部运行时目录（0700）。"""
    dirs = layout_dirs()
    for key in ("home", "sites", "scripts", "cache", "dist", "logs"):
        ensure_dir(dirs[key])
    return dirs


def atomic_write_text(path: str, text: str, mode: int = FILE_MODE) -> str:
    """原子写文本：临时文件 -> chmod -> fsync -> os.replace。"""
    directory = os.path.dirname(os.path.abspath(path))
    ensure_dir(directory)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=directory, text=True)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
    return path


def atomic_write_yaml(path: str, data: Any, mode: int = FILE_MODE) -> str:
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return atomic_write_text(path, text, mode=mode)


def atomic_write_json(path: str, data: Any, mode: int = FILE_MODE) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return atomic_write_text(path, text, mode=mode)


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def read_yaml(path: str, default: Any = None) -> Any:
    """读取 YAML；解析失败抛 ConfigError（调用方按站点隔离处理）。"""
    try:
        text = read_text(path)
    except FileNotFoundError:
        if default is None:
            raise ConfigError(f"配置文件不存在: {path}")
        return default
    except OSError as e:
        raise ConfigError(f"配置文件无法读取: {path} ({e})")
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"配置文件解析失败（YAML 语法错误）: {path} ({e})")


def read_json(path: str, default: Any = None) -> Any:
    try:
        return json.loads(read_text(path))
    except FileNotFoundError:
        return default
    except (ValueError, OSError):
        return default


def ensure_file_mode(path: str, mode: int = FILE_MODE):
    """尽力收紧已存在文件的权限。"""
    try:
        os.chmod(path, mode)
    except (FileNotFoundError, OSError):
        pass


# --------------------------------------------------------------------------
# 文件锁：同进程内用 threading 锁避免自锁，跨进程用 flock
# --------------------------------------------------------------------------
_thread_locks: Dict[str, threading.RLock] = {}
_thread_locks_guard = threading.Lock()

try:  # pragma: no cover - Windows 无 fcntl
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore


def _thread_lock(key: str) -> threading.RLock:
    with _thread_locks_guard:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _thread_locks[key] = lock
        return lock


@contextmanager
def file_lock(path: str, timeout: Optional[float] = 10.0) -> Iterator[str]:
    """跨线程 / 跨进程互斥锁（保护 read-modify-write）。

    同进程内先拿 threading.RLock（flock 对同进程不同 fd 也会阻塞，必须先串行化），
    再对 <path>.lock 加 flock 独占锁。
    """
    key = os.path.abspath(path)
    with _thread_lock(key):
        if fcntl is None:  # pragma: no cover
            yield key
            return
        ensure_dir(os.path.dirname(key))
        lock_path = key + ".lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, FILE_MODE)
        try:
            try:
                os.fchmod(fd, FILE_MODE)
            except OSError:
                pass
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if deadline is None:
                        fcntl.flock(fd, fcntl.LOCK_EX)
                        break
                    if time.monotonic() >= deadline:
                        raise ConfigError(f"等待文件锁超时: {lock_path}")
                    time.sleep(0.005)
            try:
                yield key
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            os.close(fd)
