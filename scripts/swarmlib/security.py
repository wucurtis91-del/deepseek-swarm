"""Minimal environments and output redaction.

This is not a sandbox. It removes inherited credentials from child processes and
filters common credential shapes out of captured output before that output is
stored or sent to another model. A check command can still read any file the
user can read.
"""
from __future__ import annotations

import os
import re

# Variables a toolchain normally needs. Everything else is dropped by default.
BASE_ALLOW = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "TEMP", "TMP", "TERM", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
    "PYTHONIOENCODING", "PYTHONUTF8", "VIRTUAL_ENV", "CONDA_PREFIX",
)

SECRET_NAME = re.compile(r"(?i)(api[_-]?key|access[_-]?key|secret|token|passw|credential|auth)")

# Common credential shapes. Keep these conservative: a false positive only
# redacts a little output; a false negative leaks a key into a prompt.
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{12,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{12,}=*"),
    re.compile(r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|secret|password)"
               r"\s*[:=]\s*['\"]?[^\s'\"]{6,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
)

REDACTED = "[REDACTED]"


def minimal_env(extra: dict | None = None, allow: tuple = (), environ: dict | None = None) -> dict:
    """A child environment with no inherited provider credentials."""
    src = os.environ if environ is None else environ
    names = list(BASE_ALLOW) + list(allow)
    env = {k: src[k] for k in names if k in src and src[k] is not None}
    if extra:
        env.update({str(k): str(v) for k, v in extra.items() if v is not None})
    return env


def secret_values(environ: dict | None = None, minimum: int = 6) -> list[str]:
    """Values of env vars whose names look like credentials, longest first."""
    src = os.environ if environ is None else environ
    values = []
    for name, value in src.items():
        if not value or not isinstance(value, str) or len(value) < minimum:
            continue
        if SECRET_NAME.search(name):
            values.append(value)
    return sorted(set(values), key=len, reverse=True)


def redact(text: str, extra_secrets=()) -> str:
    """Remove known secret values and common credential shapes from text."""
    if not text:
        return text
    text = str(text)
    for value in list(extra_secrets) + secret_values():
        if value and len(value) >= 6:
            text = text.replace(value, REDACTED)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def has_sensitive_name(name: str) -> bool:
    return bool(SECRET_NAME.search(name or ""))
