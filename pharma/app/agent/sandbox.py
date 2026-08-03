"""Subprocess sandbox for agent-written analysis code.

Process-level containment, honestly stated: `python -I` (isolated, no user
site-packages, no cwd on sys.path), rlimits (CPU, RSS, no core dumps), a wall
timeout, a scrubbed environment (no API keys; proxy vars removed so generated
code has no network channel through the app's egress), and cwd pinned to the
tenant's workspace. This is NOT a VM — container/gVisor isolation is the
pilot-phase upgrade tracked in PHARMA.md.
"""

from __future__ import annotations

import os
import resource
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from app import config

# Only what pandas/matplotlib need to run headless. Everything else is dropped —
# notably ANTHROPIC_*, PHARMA_*, and *_PROXY variables.
_KEEP_ENV = ("PATH", "LANG", "LC_ALL", "PYTHONHASHSEED")


@dataclass
class SandboxResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (config.SANDBOX_CPU_S, config.SANDBOX_CPU_S))
    resource.setrlimit(resource.RLIMIT_AS, (config.SANDBOX_RSS_BYTES, config.SANDBOX_RSS_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.setsid()  # own process group so a timeout kill takes children with it


def run_python(code: str, workdir: Path) -> SandboxResult:
    """Run agent-written Python with `workdir` as cwd. The script file itself
    is written inside the workdir; MPLCONFIGDIR is pinned there too so
    matplotlib runs headless without touching $HOME."""
    workdir = workdir.resolve()
    if not workdir.is_dir():
        return SandboxResult(False, "", "workdir does not exist", -1)

    script = workdir / f"_analysis_{uuid.uuid4().hex[:8]}.py"
    script.write_text(code)

    env = {k: v for k, v in os.environ.items() if k in _KEEP_ENV}
    env["MPLBACKEND"] = "Agg"
    env["MPLCONFIGDIR"] = str(workdir / ".mpl")
    env["HOME"] = str(workdir)

    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(script)],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=config.SANDBOX_WALL_TIMEOUT_S,
            preexec_fn=_limits,
        )
        return SandboxResult(proc.returncode == 0, proc.stdout[-20000:], proc.stderr[-20000:], proc.returncode)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return SandboxResult(False, out[-20000:], f"killed: exceeded {config.SANDBOX_WALL_TIMEOUT_S}s wall timeout", -9)
    finally:
        script.unlink(missing_ok=True)
