"""Single-instance handling for ``git-bridge serve``.

Before a new service instance binds its port, any earlier bridge instance is
stopped: the process listening on the configured port and the process named
in the PID file, provided its command line shows it is a bridge ``serve``
process. A foreign program holding the port is never killed; startup is
refused instead. The current process and its ancestors (e.g. a debugger or
the Windows venv launcher) are never touched.
"""

from __future__ import annotations

import atexit
import os
import time
from pathlib import Path

import psutil

from .errors import ConfigError

TERMINATE_TIMEOUT = 10.0


def is_bridge_serve(proc: psutil.Process) -> bool:
    try:
        args = proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    joined = " ".join(args).lower()
    return ("git_bridge" in joined or "git-bridge" in joined) and "serve" in args


def _listeners(port: int) -> set[int]:
    pids = set()
    try:
        connections = psutil.net_connections(kind="tcp")
    except psutil.AccessDenied:
        return pids
    for conn in connections:
        if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port and conn.pid:
            pids.add(conn.pid)
    return pids


def _read_pid(pid_file: Path | None) -> int | None:
    if pid_file is None:
        return None
    try:
        return int(pid_file.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def find_existing_instances(port: int, pid_file: Path | None) -> list[psutil.Process]:
    me = psutil.Process()
    protected = {me.pid, *(p.pid for p in me.parents())}
    found: dict[int, psutil.Process] = {}

    for pid in _listeners(port):
        if pid in protected:
            continue
        try:
            proc = psutil.Process(pid)
            name = proc.name()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            raise ConfigError(f"port {port} is held by process {pid}, which cannot be inspected") from None
        if not is_bridge_serve(proc):
            raise ConfigError(f"port {port} is in use by another program ({name}, pid {pid}); not stopping it")
        found[pid] = proc

    recorded = _read_pid(pid_file)
    if recorded is not None and recorded not in protected and recorded not in found:
        try:
            proc = psutil.Process(recorded)
            if is_bridge_serve(proc):
                found[recorded] = proc
        except psutil.NoSuchProcess:
            pass  # stale PID file

    # Include wrapper parents with the same command (the Windows venv launcher).
    for proc in list(found.values()):
        try:
            parent = proc.parent()
        except psutil.NoSuchProcess:
            continue
        if parent is not None and parent.pid not in protected and parent.pid not in found and is_bridge_serve(parent):
            found[parent.pid] = parent
    return list(found.values())


def stop_existing_instances(port: int, pid_file: Path | None, timeout: float = TERMINATE_TIMEOUT) -> list[int]:
    """Stop earlier bridge instances; return their PIDs."""
    procs = find_existing_instances(port, pid_file)
    if not procs:
        return []
    for proc in procs:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
    _, still_alive = psutil.wait_procs(alive, timeout=timeout)
    if still_alive:
        raise ConfigError(f"could not stop previous instance(s): {[p.pid for p in still_alive]}")
    deadline = time.monotonic() + timeout
    while _listeners(port) and time.monotonic() < deadline:
        time.sleep(0.2)
    return [p.pid for p in procs]


def write_pid_file(pid_file: Path) -> None:
    """Record this process; removed at exit if it still names this process."""
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    tmp = pid_file.with_suffix(pid_file.suffix + ".tmp")
    tmp.write_text(str(pid), encoding="ascii")
    os.replace(tmp, pid_file)

    def _cleanup() -> None:
        if _read_pid(pid_file) == pid:
            pid_file.unlink(missing_ok=True)

    atexit.register(_cleanup)
