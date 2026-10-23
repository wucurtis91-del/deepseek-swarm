"""Small, dependency-free helpers: hashing, atomic writes, JSON parsing, token estimates."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
ACCEPT_ID = re.compile(r"[A-Za-z0-9_.-]{1,48}")


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(when: dt.datetime | None = None) -> str:
    return (when or now()).isoformat(timespec="seconds")


def new_run_id() -> str:
    return now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, cap: int | None = None) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except (OSError, ValueError):
        return None


def hash_obj(obj) -> str:
    return sha256_text(canonical(obj))


def _fsync_dir(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(str(path), os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(Path(path), text.encode("utf-8"))


def atomic_write_json(path: Path, obj) -> None:
    atomic_write_text(Path(path), json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n")


def atomic_append_line(path: Path, line: str) -> None:
    """Append one JSONL record and fsync it before returning."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (line.rstrip("\n") + "\n").encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from exc


def json_objects(text: str) -> list:
    """All JSON objects in `text`: fenced blocks first, then the whole text.

    Ordered from the end of the document to the start, because models put their
    final answer last. Never raises.
    """
    found = []
    for m in re.finditer(r"```(?:json)?\s*\n(.*?)```", text, re.S):
        try:
            found.append(json.loads(m.group(1)))
        except ValueError:
            pass
    try:
        found.append(json.loads(text))
    except ValueError:
        pass
    # Also accept a bare object embedded in prose.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            found.append(json.loads(text[start:end + 1]))
        except ValueError:
            pass
    return list(reversed(found))


def estimate_tokens(text: str) -> int:
    """A labelled estimate, never presented as provider-measured usage.

    CJK characters are roughly one token each; other text is roughly 4 bytes per
    token. The estimate deliberately rounds up so budget reservations are not
    optimistic. Provider usage, when present, always wins.
    """
    if not text:
        return 0
    cjk = len(re.findall(r"[㐀-鿿豈-﫿぀-ヿ가-힯]", text))
    rest = len(text) - cjk
    return cjk + (rest + 3) // 4


def truncate(text: str, limit: int, note: str = "…（已截断）") -> str:
    if limit is None or len(text) <= limit:
        return text
    marker = f"\n{note}"
    if limit <= len(marker):
        return text[:max(limit, 0)]
    return text[:limit - len(marker)] + marker


def safe_filename(value: str, fallback: str = "x") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", value).strip("._")
    return (cleaned or fallback)[:64]


def sentence(text: str, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def deep_copy(obj):
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
