"""Restarting the service stops earlier instances (real processes, free ports)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import psutil
import pytest

from git_bridge.errors import ConfigError
from git_bridge.instance import find_existing_instances, stop_existing_instances, write_pid_file

LISTENER = (
    "import socket, sys, time\n"
    "s = socket.socket(); s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen()\n"
    "time.sleep(120)\n"
)
SLEEPER = "import time; time.sleep(120)"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port: int) -> bool:
    return any(
        c.status == psutil.CONN_LISTEN and c.laddr.port == port for c in psutil.net_connections(kind="tcp")
    )


def _spawn(code: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", code, *args])


def _wait_listening(port: int) -> None:
    deadline = time.monotonic() + 20
    while not _listening(port):
        assert time.monotonic() < deadline, "listener did not start"
        time.sleep(0.1)


@pytest.fixture
def procs():
    started: list[subprocess.Popen] = []
    yield started
    for p in started:
        if p.poll() is None:
            p.kill()
            p.wait()


def test_previous_bridge_instance_on_port_is_stopped(procs, tmp_path):
    port = _free_port()
    # argv mimics "python -m git_bridge ... serve"
    old = _spawn(LISTENER, str(port), "git_bridge", "serve")
    procs.append(old)
    _wait_listening(port)

    stopped = stop_existing_instances(port, tmp_path / "serve.pid", timeout=10)
    assert stopped
    assert old.wait(timeout=10) is not None
    assert not _listening(port)


def test_foreign_program_on_port_is_not_killed(procs, tmp_path):
    port = _free_port()
    foreign = _spawn(LISTENER, str(port), "some-other-app")
    procs.append(foreign)
    _wait_listening(port)

    with pytest.raises(ConfigError, match="in use by another program"):
        stop_existing_instances(port, tmp_path / "serve.pid", timeout=5)
    assert foreign.poll() is None and _listening(port)


def test_instance_recorded_in_pid_file_is_stopped(procs, tmp_path):
    old = _spawn(SLEEPER, "git-bridge", "serve")
    procs.append(old)
    # On Windows the venv launcher spawns the real interpreter; record the deepest one.
    target = psutil.Process(old.pid)
    time.sleep(1)
    children = target.children(recursive=True)
    pid_file = tmp_path / "serve.pid"
    pid_file.write_text(str((children[-1] if children else target).pid))

    stopped = stop_existing_instances(_free_port(), pid_file, timeout=10)
    assert stopped
    assert old.wait(timeout=10) is not None


def test_unrelated_process_in_pid_file_is_left_alone(procs, tmp_path):
    other = _spawn(SLEEPER, "unrelated")
    procs.append(other)
    pid_file = tmp_path / "serve.pid"
    pid_file.write_text(str(other.pid))
    assert stop_existing_instances(_free_port(), pid_file) == []
    assert other.poll() is None


def test_stale_or_garbage_pid_file_is_ignored(tmp_path):
    pid_file = tmp_path / "serve.pid"
    for content in ("999999999", "not a pid", ""):
        pid_file.write_text(content)
        assert stop_existing_instances(_free_port(), pid_file) == []


def test_never_stops_itself_or_its_parents(tmp_path):
    pid_file = tmp_path / "serve.pid"
    for pid in (os.getpid(), os.getppid()):
        pid_file.write_text(str(pid))
        assert find_existing_instances(_free_port(), pid_file) == []


def test_pid_file_written_and_removed_only_if_ours(tmp_path):
    pid_file = tmp_path / "state" / "serve.pid"
    write_pid_file(pid_file)
    assert pid_file.read_text() == str(os.getpid())
