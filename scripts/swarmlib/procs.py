"""Process execution with process-group cleanup and safe placeholder substitution."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

SAFE_SHELL_VALUE = re.compile(r"^[A-Za-z0-9_./:@%+=,-]+$")


class PlaceholderError(ValueError):
    pass


def _placeholder_pattern(name: str) -> re.Pattern:
    # Do not touch shell's own ${name}: only our literal {name}.
    return re.compile(r"(?<!\$)" + re.escape(name))


def substitute_placeholders(template: str, values: dict) -> tuple[str, dict]:
    """Replace {path}-style placeholders without letting a filename run as shell.

    Safe values are inserted literally. Anything containing spaces, quotes,
    $, backticks, glob characters or newlines is passed through an environment
    variable and referenced as "$SWARM_ARG_X", which is safe both bare and inside
    double quotes. A placeholder inside single quotes cannot be made safe; that
    is rejected and the caller should use the argv form.
    """
    env = {}
    out = template
    for name, value in values.items():
        pattern = _placeholder_pattern(name)
        if not pattern.search(out):
            continue
        value = str(value)
        if SAFE_SHELL_VALUE.match(value):
            out = pattern.sub(lambda _m, v=value: v, out)
            continue
        var = "SWARM_ARG_" + re.sub(r"\W", "_", name).strip("_").upper()
        env[var] = value

        def replace(match):
            quote = None
            escaped = False
            for char in out[:match.start()]:
                if escaped:
                    escaped = False
                elif char == "\\" and quote != "'":
                    escaped = True
                elif char == quote:
                    quote = None
                elif quote is None and char in ("'", '"'):
                    quote = char
            if quote == "'" or escaped:
                raise PlaceholderError(
                    f"占位符 {name} 出现在单引号内或被转义；请改用 check_argv 数组形式")
            expansion = "${%s}" % var
            return expansion if quote == '"' else '"' + expansion + '"'

        out = pattern.sub(replace, out)
    return out, env


def _kill_group(pid: int, sig) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def terminate_group(proc: subprocess.Popen, grace: float = 2.0) -> None:
    """Stop the whole process group: SIGTERM, wait, then SIGKILL."""
    if proc.poll() is None:
        _kill_group(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
    _kill_group(proc.pid, signal.SIGKILL)


def _reap_descendants(proc: subprocess.Popen) -> None:
    """After the parent exited normally, stop anything it left in its group."""
    if not hasattr(os, "killpg"):
        return
    _kill_group(proc.pid, signal.SIGTERM)
    time.sleep(0.05)
    _kill_group(proc.pid, signal.SIGKILL)


def run(argv, *, shell: bool = False, cwd=None, env=None, input_text: str | None = None,
        timeout: float | None = None, cancel_event: threading.Event | None = None,
        merge_output: bool = False) -> dict:
    """Run a command, returning a record. Never raises for ordinary failures."""
    start = time.time()
    cwd_path = Path(cwd).expanduser().resolve() if cwd else None
    if cwd_path is not None and not cwd_path.is_dir():
        return {"ok": False, "code": None, "stdout": "", "stderr": "",
                "output": f"工作目录不存在：{cwd_path}", "seconds": 0.0,
                "timed_out": False, "cancelled": False, "error": "cwd_missing"}
    try:
        proc = subprocess.Popen(
            argv, shell=shell, cwd=str(cwd_path) if cwd_path else None, env=env,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_output else subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return {"ok": False, "code": None, "stdout": "", "stderr": "",
                "output": f"启动命令失败：{exc}", "seconds": round(time.time() - start, 2),
                "timed_out": False, "cancelled": False, "error": "spawn_failed"}

    box: dict = {}

    def collect() -> None:
        try:
            out, err = proc.communicate(input_text)
            box["stdout"] = out or ""
            box["stderr"] = err or ""
        except (ValueError, OSError) as exc:      # pipes closed under us
            box.setdefault("stdout", "")
            box.setdefault("stderr", "")
            box["read_error"] = str(exc)

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    deadline = start + timeout if timeout else None
    timed_out = cancelled = False
    while reader.is_alive():
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        if deadline is not None and time.time() > deadline:
            timed_out = True
            break
        reader.join(0.05)
    if timed_out or cancelled:
        terminate_group(proc)
        reader.join(5.0)
    else:
        _reap_descendants(proc)
    stdout = box.get("stdout", "")
    stderr = box.get("stderr", "")
    if merge_output:
        output = stdout
    else:
        output = stdout + (("\n" + stderr) if stderr else "")
    seconds = round(time.time() - start, 2)
    code = proc.poll()
    ok = (code == 0) and not timed_out and not cancelled
    result = {"ok": ok, "code": code, "stdout": stdout, "stderr": stderr,
              "output": output.strip(), "seconds": seconds,
              "timed_out": timed_out, "cancelled": cancelled}
    if timed_out:
        result["error"] = "timeout"
    elif cancelled:
        result["error"] = "cancelled"
    elif code != 0:
        result["error"] = f"exit_{code}"
    if "read_error" in box:
        result["read_error"] = box["read_error"]
    return result
