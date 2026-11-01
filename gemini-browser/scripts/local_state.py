"""Local profiles, run records, and downloaded image validation.

This module never reads browser cookies or authentication tokens.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image


def default_data_dir() -> Path:
    """Return the per-user data directory, outside the distributable skill."""
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "GeminiBrowserSkill"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_json(path: str | os.PathLike[str], data: Any) -> None:
    """Atomically replace a JSON file using a temporary file on the same volume."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent,
            prefix=f".{target.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = stream.name
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass


def read_json(path: str | os.PathLike[str]) -> Any:
    with Path(path).open("r", encoding="utf-8") as stream:
        return json.load(stream)


_LOCK_GUARD = threading.Lock()
_HELD_PROFILES: set[str] = set()


class ProfileLock:
    """Hold an OS file lock for one browser profile without deleting its lock file."""

    def __init__(self, profile_path: str | os.PathLike[str]):
        self.profile_path = Path(profile_path).resolve()
        self.lock_path = self.profile_path / ".gemini-browser.lock"
        self._key = os.path.normcase(str(self.profile_path))
        self._stream = None

    def __enter__(self) -> "ProfileLock":
        with _LOCK_GUARD:
            if self._key in _HELD_PROFILES:
                raise RuntimeError(f"浏览器资料目录正在使用：{self.profile_path}")
            _HELD_PROFILES.add(self._key)
        stream = None
        try:
            self.profile_path.mkdir(parents=True, exist_ok=True)
            stream = self.lock_path.open("a+b")
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._stream = stream
            return self
        except BaseException as exc:
            if stream is not None:
                stream.close()
            with _LOCK_GUARD:
                _HELD_PROFILES.discard(self._key)
            if isinstance(exc, OSError) and exc.errno in (11, 13, 35, 36):
                raise RuntimeError(f"浏览器资料目录正在使用：{self.profile_path}") from exc
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self._stream = None
            with _LOCK_GUARD:
                _HELD_PROFILES.discard(self._key)


_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class RunStore:
    """Persist each generation run beneath its own output directory."""

    def __init__(self, output_dir: str | os.PathLike[str]):
        self.output_dir = Path(output_dir)

    def path_for(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError("无效的任务 ID")
        return self.output_dir / run_id / "record.json"

    def create(self, prompt: str, profile: str | os.PathLike[str]) -> dict[str, Any]:
        now = utc_now()
        record = {
            "id": uuid.uuid4().hex,
            "status": "prepared",
            "prompt": prompt,
            "profile": str(profile),
            "created_at": now,
            "updated_at": now,
            "chat_url": None,
            "files": [],
        }
        self.save(record)
        return record

    def save(self, record: dict[str, Any]) -> dict[str, Any]:
        path = self.path_for(record["id"])
        record["updated_at"] = utc_now()
        write_json(path, record)
        return record

    def load(self, run_id: str) -> dict[str, Any]:
        record = read_json(self.path_for(run_id))
        if not isinstance(record, dict) or record.get("id") != run_id:
            raise ValueError("任务记录与 ID 不匹配")
        return record


def verify_image(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Verify an image's container and decode all frames before accepting it."""
    target = Path(path)
    allowed = {"PNG", "JPEG", "WEBP", "GIF"}
    try:
        with Image.open(target) as image:
            image_format = image.format
            if image_format not in allowed:
                raise ValueError(f"不支持的图片格式：{image_format}")
            image.verify()
        with Image.open(target) as image:
            width, height = image.size
            for frame in range(getattr(image, "n_frames", 1)):
                image.seek(frame)
                image.load()
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return {
            "path": str(target.resolve()),
            "format": image_format,
            "width": width,
            "height": height,
            "size_bytes": target.stat().st_size,
            "sha256": digest.hexdigest(),
        }
    except (OSError, SyntaxError, EOFError, ValueError) as exc:
        raise ValueError(f"图片校验失败：{target.name}：{exc}") from exc
