"""Running other programs.

One rule for the whole project: commands are argument lists, never shell strings.
Nothing a user or a device can influence is ever interpolated into a shell line.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from collections.abc import Mapping, Sequence


class CommandError(RuntimeError):
    def __init__(self, argv: Sequence[str], code: int | None, stdout: str, stderr: str, timed_out: bool = False):
        self.argv, self.code, self.stdout, self.stderr, self.timed_out = list(argv), code, stdout, stderr, timed_out
        why = "timed out" if timed_out else f"exit {code}"
        tail = (stderr or stdout).strip().splitlines()[-1:] or [""]
        super().__init__(f"{argv[0]} {why}: {tail[0][:300]}")


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def out(self) -> str:
        return self.stdout.strip()


def which(name: str) -> str | None:
    return shutil.which(name)


def _check_argv(argv: Sequence[str]) -> list[str]:
    if isinstance(argv, (str, bytes)) or not argv:
        raise TypeError("command must be a non-empty list of arguments, not a string")
    return [str(a) for a in argv]


def run(argv: Sequence[str], *, timeout: float = 60, check: bool = True, input: str | bytes | None = None,
        env: Mapping[str, str] | None = None, cwd: str | os.PathLike | None = None,
        binary: bool = False) -> Result:
    """Run a command to completion. Raises CommandError on failure when check is true."""
    args = _check_argv(argv)
    try:
        p = subprocess.run(args, input=input, capture_output=True, timeout=timeout, cwd=cwd,
                           env={**os.environ, **env} if env else None, text=not binary,
                           errors=None if binary else "replace")
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode(errors="replace")
        err = e.stderr if isinstance(e.stderr, str) else (e.stderr or b"").decode(errors="replace")
        raise CommandError(args, None, out, err, timed_out=True) from None
    except FileNotFoundError:
        raise CommandError(args, 127, "", f"{args[0]}: command not found") from None
    result = Result(p.returncode, p.stdout, p.stderr if not binary else p.stderr.decode(errors="replace"))
    if check and p.returncode != 0:
        raise CommandError(args, p.returncode, "" if binary else p.stdout, result.stderr)
    return result


async def arun(argv: Sequence[str], *, timeout: float = 60, check: bool = True, input: bytes | None = None,
               env: Mapping[str, str] | None = None, binary: bool = False) -> Result:
    """Async twin of run(). A timed-out child is killed with its whole process group."""
    args = _check_argv(argv)
    try:
        p = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **env} if env else None, start_new_session=True)
    except FileNotFoundError:
        raise CommandError(args, 127, "", f"{args[0]}: command not found") from None
    try:
        out, err = await asyncio.wait_for(p.communicate(input), timeout)
    except asyncio.TimeoutError:
        kill_tree(p.pid)
        await p.wait()
        raise CommandError(args, None, "", "", timed_out=True) from None
    stderr = err.decode(errors="replace")
    result = Result(p.returncode or 0, out if binary else out.decode(errors="replace"), stderr)  # type: ignore[arg-type]
    if check and p.returncode != 0:
        raise CommandError(args, p.returncode, "" if binary else result.stdout, stderr)
    return result


def spawn(argv: Sequence[str], *, env: Mapping[str, str] | None = None, stdout=None, stderr=None,
          stdin=subprocess.DEVNULL) -> subprocess.Popen:
    """Start a long-running child in its own session so it can be stopped as one group."""
    return subprocess.Popen(_check_argv(argv), stdin=stdin, stdout=stdout or subprocess.DEVNULL,
                            stderr=stderr or subprocess.DEVNULL, start_new_session=True,
                            env={**os.environ, **env} if env else None)


def kill_tree(pid: int, sig: int = signal.SIGTERM) -> None:
    """Signal a process started with start_new_session and everything it started."""
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
