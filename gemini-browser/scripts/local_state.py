"""Local profiles, run records, and downloaded image validation.

This module never reads browser cookies or authentication tokens.
"""

from __future__ import annotations

import hashlib
import json
import math
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


def _video_container_name(container, prefix: bytes) -> str:
    """Identify actual containers, never a filename extension or image decoder."""
    names = set(container.format.name.split(","))
    if names & {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}:
        brand = container.metadata.get("major_brand", "").strip().lower()
        if brand in {"avif", "avis", "heic", "heix", "hevc", "hevx", "mif1", "msf1"}:
            raise ValueError("文件是静态图片容器，不是视频")
        return "MOV" if brand == "qt" else "MP4"
    if names & {"matroska", "webm"}:
        # EBML DocType (0x4282) is a short string in the container header.
        offset = prefix.find(b"\x42\x82")
        if offset >= 0 and offset + 2 < len(prefix):
            first = prefix[offset + 2]
            mask, length = 0x80, 1
            while mask and not first & mask:
                mask >>= 1
                length += 1
            if mask and offset + 2 + length <= len(prefix):
                size = first & (mask - 1)
                for value in prefix[offset + 3:offset + 2 + length]:
                    size = (size << 8) | value
                start = offset + 2 + length
                if prefix[start:start + size].lower() == b"webm":
                    return "WEBM"
        return "MKV"
    if "avi" in names:
        return "AVI"
    raise ValueError(f"不支持的视频容器：{container.format.name}")


def verify_video(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Fully decode local video streams and reject damaged or non-video files.

    PyAV uses the locally installed FFmpeg libraries; this function neither
    downloads a decoder nor opens remote media. Images, audio-only containers,
    attached cover art and declared-but-missing video frames are not accepted.
    """
    target = Path(path).expanduser().resolve()
    try:
        import av
    except ImportError as exc:
        raise RuntimeError("视频校验需要 PyAV；请安装本 Skill requirements.txt 中的 av 依赖。") from exc
    try:
        initial_stat = target.stat()
        if not target.is_file() or initial_stat.st_size == 0:
            raise ValueError("视频文件为空或不是普通文件")
        with target.open("rb") as source:
            prefix = source.read(4096)
            source.seek(0)
            # Passing a local file object prevents the input from being treated
            # as an FFmpeg URL. Only real video containers below are accepted.
            with av.logging.Capture() as logs, av.open(
                source, mode="r", options={"err_detect": "explode"},
                container_options={"protocol_whitelist": "file,pipe"},
            ) as container:
                video_format = _video_container_name(container, prefix)
                excluded = av.stream.Disposition.attached_pic | av.stream.Disposition.still_image
                videos = [stream for stream in container.streams.video if not stream.disposition & excluded]
                if not videos:
                    raise ValueError("容器中没有可播放的视频流")
                stats = {}
                for stream in videos:
                    stream.codec_context.options = {**stream.codec_context.options, "err_detect": "explode"}
                    rate = float(stream.average_rate) if stream.average_rate else 0.0
                    stats[stream.index] = {
                        "frames": 0, "width": 0, "height": 0, "first": None, "last_end": None,
                        "frame_duration": 1.0 / rate if rate > 0 else 0.0,
                    }
                for packet in container.demux(videos):
                    if packet.is_corrupt:
                        raise ValueError("视频包含损坏的数据包")
                    for frame in packet.decode():
                        stat = stats[packet.stream.index]
                        if frame.width <= 0 or frame.height <= 0:
                            raise ValueError("视频帧尺寸无效")
                        stat["frames"] += 1
                        stat["width"], stat["height"] = frame.width, frame.height
                        duration = (float(frame.duration * frame.time_base)
                                    if frame.duration and frame.time_base else stat["frame_duration"])
                        if duration > 0:
                            stat["frame_duration"] = max(stat["frame_duration"], duration)
                        if frame.pts is not None and frame.time_base is not None:
                            timestamp = float(frame.pts * frame.time_base)
                            stat["first"] = timestamp if stat["first"] is None else min(stat["first"], timestamp)
                            end = timestamp + max(0.0, duration)
                            stat["last_end"] = end if stat["last_end"] is None else max(stat["last_end"], end)
                durations = {}
                for stream in videos:
                    stat = stats[stream.index]
                    if not stat["frames"]:
                        raise ValueError("视频流未能解码出完整画面")
                    if stream.frames and stat["frames"] != stream.frames:
                        raise ValueError("视频解码帧数与容器声明不一致，文件可能被截断")
                    decoded_duration = (stat["last_end"] - stat["first"]
                                        if stat["first"] is not None and stat["last_end"] is not None
                                        else stat["frames"] * stat["frame_duration"])
                    declared_duration = (float(stream.duration * stream.time_base)
                                         if stream.duration and stream.time_base else 0.0)
                    if not declared_duration and len(videos) == 1 and not container.streams.audio and container.duration:
                        declared_duration = float(container.duration) / av.time_base
                    tolerance = max(0.05, 2 * stat["frame_duration"])
                    if declared_duration > 0 and decoded_duration > 0 and decoded_duration + tolerance < declared_duration:
                        raise ValueError("视频未解码到声明的结尾，文件可能被截断")
                    duration = declared_duration or decoded_duration
                    if not math.isfinite(duration) or duration <= 0:
                        raise ValueError("不能确认有效的视频时长")
                    durations[stream.index] = duration
                bad_log = re.compile(r"truncat|premature|corrupt|partial file|invalid data|error.{0,20}decod", re.I)
                if any(level <= av.logging.ERROR or bad_log.search(message) for level, _, message in logs):
                    raise ValueError("解码器报告视频损坏或内容不完整")
                primary = videos[0]
                primary_stat = stats[primary.index]
                result = {
                    "path": str(target), "format": video_format, "media_type": "video",
                    "width": primary_stat["width"], "height": primary_stat["height"],
                    "duration_seconds": round(durations[primary.index], 6),
                    "decoded_frames": primary_stat["frames"], "video_streams": len(videos),
                    "codec": primary.codec_context.name,
                }
        final_stat = target.stat()
        if (initial_stat.st_size, initial_stat.st_mtime_ns) != (final_stat.st_size, final_stat.st_mtime_ns):
            raise ValueError("视频在校验期间发生变化")
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        result.update(size_bytes=final_stat.st_size, sha256=digest.hexdigest())
        return result
    except (OSError, EOFError, ValueError, av.error.FFmpegError) as exc:
        raise ValueError(f"视频校验失败：{target.name}：{exc}") from exc


def verify_media(path: str | os.PathLike[str], media_type: str | None = None) -> dict[str, Any]:
    """Validate the requested media type, or detect images/videos from content."""
    if media_type is not None:
        media_type = media_type.strip().lower()
        if media_type not in {"image", "video"}:
            raise ValueError("media_type 只能是 image、video 或 None")
    if media_type == "image":
        return {**verify_image(path), "media_type": "image"}
    if media_type == "video":
        return verify_video(path)
    try:
        with Image.open(path):
            pass
    except (OSError, ValueError):
        return verify_video(path)
    return {**verify_image(path), "media_type": "image"}


def verify_saved_files(files: list[dict[str, Any]], media_type: str) -> list[dict[str, Any]]:
    """Revalidate saved media and match recorded hashes when available."""
    checked_files = []
    for file in files:
        checked = verify_media(file["path"], media_type)
        if file.get("sha256") and file["sha256"] != checked["sha256"]:
            raise ValueError(f"本地文件已改变：{Path(file['path']).name}")
        checked_files.append(checked)
    return checked_files
