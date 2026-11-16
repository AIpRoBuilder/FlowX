# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
"""Media workspace, functional transforms, provider adapters, and run journaling."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_EXTENSION = re.compile(r"^\.[A-Za-z0-9]{1,8}$")

WORKFLOW_ROOT_STATE_KEY = "__flowx_workflow_root__"
WORKFLOW_VERSION_STATE_KEY = "__flowx_workflow_version__"
REQUEST_ID_STATE_KEY = "__flowx_request_id__"

_DRY_RUN_ENV = "FLOWX_MEDIA_DRY_RUN"
_BLENDER_ENABLE_ENV = "FLOWX_ENABLE_BLENDER"
_DEFAULT_DOWNLOAD_LIMIT = 200 * 1024 * 1024  # 200 MiB


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _truthy_env(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _safe_segment(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._")
    if not cleaned:
        cleaned = fallback
    cleaned = cleaned[:96]
    if not _SAFE_SEGMENT.match(cleaned):
        return fallback
    return cleaned


def _safe_extension(ext: str, default: str = ".bin") -> str:
    candidate = str(ext or "").strip().lower()
    if candidate and not candidate.startswith("."):
        candidate = f".{candidate}"
    if _SAFE_EXTENSION.match(candidate):
        return candidate
    return default


def _guess_mime(path: Path, fallback: str = "application/octet-stream") -> str:
    suffix = path.suffix.lower()
    known = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
    }
    if suffix in known:
        return known[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or fallback


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _minimal_png_bytes(width: int = 64, height: int = 64, rgb: tuple[int, int, int] = (72, 98, 218)) -> bytes:
    """Build a valid, dependency-free RGB PNG image."""
    w = max(1, min(int(width), 4096))
    h = max(1, min(int(height), 4096))
    r, g, b = (max(0, min(int(c), 255)) for c in rgb)
    try:
        from PIL import Image, ImageDraw  # type: ignore[import-not-found]

        img = Image.new("RGB", (w, h), (r, g, b))
        draw = ImageDraw.Draw(img)
        draw.rectangle([2, 2, max(2, w - 3), max(2, h - 3)], outline=(235, 240, 255), width=max(1, min(w, h) // 32))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        row = b"\x00" + bytes((r, g, b)) * w
        raw = row * h

        def chunk(tag: bytes, payload: bytes) -> bytes:
            crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
            return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

        ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, level=6))
            + chunk(b"IEND", b"")
        )


def _minimal_mp4_bytes(
    *,
    width: int = 320,
    height: int = 240,
    duration: float = 1.0,
    fps: int = 12,
    rgb: tuple[int, int, int] = (54, 83, 198),
) -> bytes:
    """Create a tiny valid H.264/MPEG-4 MP4 via ffmpeg when available, else an ISO BMFF ftyp+moov stub."""
    w = max(16, (int(width) // 2) * 2)
    h = max(16, (int(height) // 2) * 2)
    dur = max(0.2, min(float(duration), 30.0))
    rate = max(1, min(int(fps), 60))
    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin:
        hex_color = "{:02x}{:02x}{:02x}".format(*(max(0, min(int(c), 255)) for c in rgb))
        with tempfile.TemporaryDirectory(prefix="flowx-mp4-") as tmp:
            target = Path(tmp) / "sample.mp4"
            for codec_args in (
                ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart"],
                ["-c:v", "mpeg4", "-pix_fmt", "yuv420p"],
            ):
                proc = subprocess.run(
                    [
                        ffmpeg_bin,
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        f"color=c=0x{hex_color}:s={w}x{h}:r={rate}:d={dur:.2f}",
                        *codec_args,
                        str(target),
                    ],
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
                if proc.returncode == 0 and target.is_file() and target.stat().st_size > 0:
                    return target.read_bytes()
    # Deterministic ISO BMFF ftyp + free box fallback when ffmpeg is absent.
    ftyp = b"isom\x00\x00\x02\x00isomiso2mp41"
    box_ftyp = struct.pack(">I4s", 8 + len(ftyp), b"ftyp") + ftyp
    meta_payload = f"flowx-dry-run:{w}x{h}:{dur:.2f}s".encode("ascii")
    box_free = struct.pack(">I4s", 8 + len(meta_payload), b"free") + meta_payload
    return box_ftyp + box_free


def _probe_image_dimensions(path: Path) -> tuple[int | None, int | None]:
    try:
        from PIL import Image  # type: ignore[import-not-found]

        with Image.open(path) as img:
            return int(img.width), int(img.height)
    except Exception:
        try:
            header = path.read_bytes()[:32]
            if header.startswith(b"\x89PNG\r\n\x1a\n") and header[12:16] == b"IHDR":
                w, h = struct.unpack(">II", header[16:24])
                return int(w), int(h)
        except Exception:
            pass
    return None, None


def _probe_video_metadata(path: Path) -> tuple[int | None, int | None, float | None]:
    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin:
        return None, None, None
    try:
        proc = subprocess.run(
            [
                ffprobe_bin,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,duration:format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            return None, None, None
        info = json.loads(proc.stdout or "{}")
        streams = info.get("streams") or []
        stream = streams[0] if streams else {}
        width = int(stream["width"]) if stream.get("width") else None
        height = int(stream["height"]) if stream.get("height") else None
        raw_dur = stream.get("duration") or (info.get("format") or {}).get("duration")
        duration = round(float(raw_dur), 3) if raw_dur not in (None, "N/A", "") else None
        return width, height, duration
    except Exception:
        return None, None, None


def _extract_video_poster(video_path: Path, poster_path: Path) -> bool:
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin or not video_path.is_file():
        return False
    poster_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-ss",
                "0.0",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                str(poster_path),
            ],
            capture_output=True,
            timeout=15,
            check=False,
        )
        return proc.returncode == 0 and poster_path.is_file() and poster_path.stat().st_size > 0
    except Exception:
        return False


def _validate_http_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Unsupported URL scheme or host: {url!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs containing embedded credentials are not allowed")
    return url.strip()


def _download_bytes(url: str, *, headers: Mapping[str, str] | None = None, timeout: float = 120.0) -> bytes:
    checked = _validate_http_url(url)
    req = urllib.request.Request(checked, headers=dict(headers or {}), method="GET")
    chunks: list[bytes] = []
    total = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        while True:
            block = resp.read(65536)
            if not block:
                break
            total += len(block)
            if total > _DEFAULT_DOWNLOAD_LIMIT:
                raise ValueError("Downloaded media exceeds maximum byte limit")
            chunks.append(block)
    return b"".join(chunks)


def _json_request(
    url: str,
    *,
    method: str = "POST",
    payload: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    checked = _validate_http_url(url)
    req_headers = {"Accept": "application/json", **dict(headers or {})}
    body = None
    if payload is not None:
        body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(checked, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Media API HTTP {exc.code}: {detail}") from exc
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Media API response must be a JSON object")
    return data


@dataclass(frozen=True, slots=True)
class MediaAsset:
    """Immutable record for a saved image, video, or binary artifact inside a workflow run."""

    kind: str  # "image" | "video" | "file"
    abspath: Path
    relpath: str  # POSIX path relative to workflow root (e.g. "runs/<req>/media/<step>/out.png")
    mime: str
    bytes_size: int
    sha256: str
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    poster_abspath: Path | None = None
    poster_relpath: str | None = None

    def to_card(self, *, label: str = "", prompt: str = "", extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Build a standardized JSON-serializable StepRunOutput.card dictionary."""
        card: dict[str, Any] = {
            "kind": self.kind,
            "media": self.relpath,
            "mime": self.mime,
            "bytes": self.bytes_size,
        }
        if label:
            card["label"] = label
        if prompt:
            card["prompt"] = prompt
        if self.width is not None:
            card["width"] = self.width
        if self.height is not None:
            card["height"] = self.height
        if self.duration is not None:
            card["duration"] = self.duration
        if self.poster_relpath:
            card["poster"] = self.poster_relpath
        if extra:
            card.update(dict(extra))
        return card

    def to_derived(self, *, prefix: str = "") -> dict[str, Any]:
        """Build structured derived metadata for downstream Python nodes."""
        key = f"{prefix}_path" if prefix else (f"{self.kind}_path" if self.kind in {"image", "video"} else "file_path")
        result: dict[str, Any] = {
            key: str(self.abspath),
            "relpath": self.relpath,
            "mime": self.mime,
            "bytes": self.bytes_size,
            "sha256": self.sha256,
        }
        if self.width is not None:
            result["width"] = self.width
        if self.height is not None:
            result["height"] = self.height
        if self.duration is not None:
            result["duration"] = self.duration
        if self.poster_abspath is not None:
            result["poster_path"] = str(self.poster_abspath)
        return result


class MediaSpace:
    """Scoped per-step media workspace writing into `<workflow_root>/runs/<request_id>/media/<step_id>/`."""

    def __init__(
        self,
        *,
        step_id: str,
        workflow_root: Path,
        request_id: str,
        dry_run: bool | None = None,
    ) -> None:
        self.step_id = _safe_segment(step_id, "step")
        self.workflow_root = workflow_root.expanduser().resolve()
        self.request_id = _safe_segment(request_id, "local")
        self.dry_run = _truthy_env(_DRY_RUN_ENV) if dry_run is None else bool(dry_run)
        self.directory = self.workflow_root / "runs" / self.request_id / "media" / self.step_id
        self.directory.mkdir(parents=True, exist_ok=True)

    def output_path(self, stem: str = "output", ext: str = ".png") -> Path:
        """Allocate a fresh collision-free path inside this step's media folder."""
        safe_stem = _safe_segment(stem, "output")
        safe_ext = _safe_extension(ext, ".bin")
        candidate = self.directory / f"{safe_stem}{safe_ext}"
        if not candidate.exists():
            return candidate
        return self.directory / f"{safe_stem}_{uuid.uuid4().hex[:8]}{safe_ext}"

    def _register_file(
        self,
        path: Path,
        *,
        kind: str | None = None,
        mime: str | None = None,
        poster_path: Path | None = None,
        width: int | None = None,
        height: int | None = None,
        duration: float | None = None,
    ) -> MediaAsset:
        resolved = path.expanduser().resolve()
        if not resolved.is_relative_to(self.workflow_root):
            dest = self.output_path(resolved.stem or "asset", resolved.suffix or ".bin")
            shutil.copy2(resolved, dest)
            resolved = dest.resolve()
        data = resolved.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        actual_mime = mime or _guess_mime(resolved)
        actual_kind = kind or (
            "image" if actual_mime.startswith("image/") else "video" if actual_mime.startswith("video/") else "file"
        )
        w, h, dur = width, height, duration
        if actual_kind == "image" and (w is None or h is None):
            pw, ph = _probe_image_dimensions(resolved)
            w = w if w is not None else pw
            h = h if h is not None else ph
        elif actual_kind == "video":
            pw, ph, pdur = _probe_video_metadata(resolved)
            w = w if w is not None else pw
            h = h if h is not None else ph
            dur = dur if dur is not None else pdur
            if poster_path is None:
                auto_poster = self.output_path(f"{resolved.stem}_poster", ".png")
                if _extract_video_poster(resolved, auto_poster):
                    poster_path = auto_poster
                elif self.dry_run:
                    auto_poster.write_bytes(_minimal_png_bytes(w or 320, h or 240))
                    poster_path = auto_poster

        poster_abs: Path | None = None
        poster_rel: str | None = None
        if poster_path is not None and poster_path.is_file():
            resolved_poster = poster_path.expanduser().resolve()
            if resolved_poster.is_relative_to(self.workflow_root):
                poster_abs = resolved_poster
                poster_rel = resolved_poster.relative_to(self.workflow_root).as_posix()

        rel = resolved.relative_to(self.workflow_root).as_posix()
        return MediaAsset(
            kind=actual_kind,
            abspath=resolved,
            relpath=rel,
            mime=actual_mime,
            bytes_size=len(data),
            sha256=digest,
            width=w,
            height=h,
            duration=dur,
            poster_abspath=poster_abs,
            poster_relpath=poster_rel,
        )

    def save_bytes(
        self,
        data: bytes | bytearray,
        *,
        stem: str = "output",
        ext: str = ".bin",
        kind: str | None = None,
        mime: str | None = None,
    ) -> MediaAsset:
        """Persist raw bytes into this step's media folder and return a MediaAsset."""
        target = self.output_path(stem, ext)
        target.write_bytes(bytes(data))
        return self._register_file(target, kind=kind, mime=mime)

    def save_image(
        self,
        image: Any,
        *,
        stem: str = "image",
        format: str = "PNG",
    ) -> MediaAsset:
        """Persist bytes, a file path, or a PIL.Image.Image into this step's media folder."""
        fmt = str(format or "PNG").strip().upper()
        ext = ".jpg" if fmt in {"JPG", "JPEG"} else f".{fmt.lower()}"
        target = self.output_path(stem, ext)
        if isinstance(image, (bytes, bytearray)):
            target.write_bytes(bytes(image))
        elif isinstance(image, (str, os.PathLike)):
            shutil.copy2(Path(image).expanduser(), target)
        elif hasattr(image, "save"):
            image.save(target, format="JPEG" if fmt == "JPG" else fmt)
        else:
            raise TypeError(f"Unsupported image value of type {type(image).__name__}")
        return self._register_file(target, kind="image")

    def resize_image(
        self,
        source: str | os.PathLike[str] | bytes | bytearray,
        *,
        width: int,
        height: int,
        fit: str = "cover",
        stem: str = "resized",
        format: str = "PNG",
    ) -> MediaAsset:
        """Resize an image using Pillow (`contain`, `cover`, or `stretch`)."""
        if width < 1 or height < 1:
            raise ValueError("width and height must be positive integers")
        from PIL import Image, ImageOps  # type: ignore[import-not-found]

        if isinstance(source, (bytes, bytearray)):
            opened = Image.open(io.BytesIO(bytes(source)))
        else:
            opened = Image.open(Path(source).expanduser())
        with opened as img:
            rgb = img.convert("RGBA" if format.upper() in {"PNG", "WEBP"} and "A" in img.getbands() else "RGB")
            mode = fit.strip().lower()
            if mode == "contain":
                out = ImageOps.contain(rgb, (int(width), int(height)), Image.Resampling.LANCZOS)
            elif mode == "cover":
                out = ImageOps.fit(rgb, (int(width), int(height)), Image.Resampling.LANCZOS)
            elif mode == "stretch":
                out = rgb.resize((int(width), int(height)), Image.Resampling.LANCZOS)
            else:
                raise ValueError(f"Unsupported fit mode: {fit!r}")
            return self.save_image(out, stem=stem, format=format)

    def crop_image(
        self,
        source: str | os.PathLike[str] | bytes | bytearray,
        *,
        left: int,
        top: int,
        right: int,
        bottom: int,
        stem: str = "cropped",
        format: str = "PNG",
    ) -> MediaAsset:
        """Crop an image box `(left, top, right, bottom)` using Pillow."""
        if right <= left or bottom <= top:
            raise ValueError("Invalid crop box: require right > left and bottom > top")
        from PIL import Image  # type: ignore[import-not-found]

        if isinstance(source, (bytes, bytearray)):
            opened = Image.open(io.BytesIO(bytes(source)))
        else:
            opened = Image.open(Path(source).expanduser())
        with opened as img:
            cropped = img.crop((int(left), int(top), int(right), int(bottom)))
            return self.save_image(cropped, stem=stem, format=format)

    def generate_image(
        self,
        prompt: str,
        *,
        size: str = "1024x1024",
        model: str = "",
        api_url: str = "",
        api_key: str = "",
        extra_body: Mapping[str, Any] | None = None,
        stem: str = "generated",
        timeout: float = 180.0,
    ) -> MediaAsset:
        """Call an OpenAI-compatible or custom image generation API (or synthesize in dry-run)."""
        text = str(prompt or "").strip()
        if not text:
            raise ValueError("Image generation prompt must be non-empty")
        match = re.match(r"^(\d+)\s*[xX]\s*(\d+)$", str(size).strip())
        w, h = (int(match.group(1)), int(match.group(2))) if match else (1024, 1024)
        if self.dry_run:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            png = _minimal_png_bytes(min(w, 256), min(h, 256), (digest[0], digest[1], digest[2]))
            target = self.output_path(stem, ".png")
            target.write_bytes(png)
            return self._register_file(target, kind="image", mime="image/png", width=w, height=h)

        base_url = (
            api_url
            or os.environ.get("FLOWX_IMAGE_API_URL")
            or os.environ.get("FLOWX_MEDIA_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        endpoint = base_url if base_url.endswith("/images/generations") else f"{base_url}/images/generations"
        key = (
            api_key
            or os.environ.get("FLOWX_IMAGE_API_KEY")
            or os.environ.get("FLOWX_MEDIA_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("FLOWX_LLM_API_KEY")
            or ""
        )
        if not key:
            raise RuntimeError("Missing image generation API key (set FLOWX_IMAGE_API_KEY or OPENAI_API_KEY)")
        chosen_model = model or os.environ.get("FLOWX_IMAGE_MODEL", "gpt-image-1")
        body: dict[str, Any] = {"model": chosen_model, "prompt": text, "size": f"{w}x{h}"}
        if extra_body:
            body.update(dict(extra_body))
        response = _json_request(
            endpoint,
            payload=body,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
        items = response.get("data") or response.get("images") or []
        first = items[0] if isinstance(items, list) and items and isinstance(items[0], Mapping) else response
        b64 = first.get("b64_json") or first.get("base64")
        if isinstance(b64, str) and b64.strip():
            raw = base64.b64decode(b64)
            return self.save_bytes(raw, stem=stem, ext=".png", kind="image", mime="image/png")
        image_url = first.get("url") or response.get("output_url")
        if isinstance(image_url, str) and image_url.strip():
            raw = _download_bytes(image_url, timeout=timeout)
            ext = Path(urllib.parse.urlsplit(image_url).path).suffix or ".png"
            return self.save_bytes(raw, stem=stem, ext=ext, kind="image")
        raise RuntimeError("Image generation API response contained neither b64_json nor url")

    def generate_video(
        self,
        prompt: str,
        *,
        image: str | os.PathLike[str] | None = None,
        duration: float = 4.0,
        size: str = "720x720",
        model: str = "",
        api_url: str = "",
        status_url_template: str = "",
        api_key: str = "",
        extra_body: Mapping[str, Any] | None = None,
        stem: str = "generated_video",
        poll_interval: float = 2.0,
        timeout: float = 600.0,
    ) -> MediaAsset:
        """Submit and poll a video generation job (or synthesize a valid MP4 in dry-run)."""
        text = str(prompt or "").strip()
        if not text and image is None:
            raise ValueError("Video generation requires a prompt or source image")
        match = re.match(r"^(\d+)\s*[xX]\s*(\d+)$", str(size).strip())
        w, h = (int(match.group(1)), int(match.group(2))) if match else (720, 720)
        if self.dry_run:
            seed_bytes = hashlib.sha256((text or str(image)).encode("utf-8")).digest()
            mp4 = _minimal_mp4_bytes(
                width=min(w, 320),
                height=min(h, 240),
                duration=min(max(float(duration), 0.5), 2.0),
                rgb=(seed_bytes[0], seed_bytes[1], seed_bytes[2]),
            )
            target = self.output_path(stem, ".mp4")
            target.write_bytes(mp4)
            return self._register_file(
                target, kind="video", mime="video/mp4", width=w, height=h, duration=float(duration),
            )

        endpoint = (
            api_url
            or os.environ.get("FLOWX_VIDEO_API_URL")
            or (
                f"{os.environ['FLOWX_MEDIA_BASE_URL'].rstrip('/')}/videos/generations"
                if os.environ.get("FLOWX_MEDIA_BASE_URL")
                else ""
            )
        )
        if not endpoint:
            raise RuntimeError("Missing video generation endpoint (set FLOWX_VIDEO_API_URL)")
        key = (
            api_key
            or os.environ.get("FLOWX_VIDEO_API_KEY")
            or os.environ.get("FLOWX_MEDIA_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("FLOWX_LLM_API_KEY")
            or ""
        )
        if not key:
            raise RuntimeError("Missing video generation API key (set FLOWX_VIDEO_API_KEY)")

        body: dict[str, Any] = {
            "model": model or os.environ.get("FLOWX_VIDEO_MODEL", "video-1"),
            "prompt": text,
            "size": f"{w}x{h}",
            "duration": float(duration),
        }
        if image is not None:
            img_bytes = Path(image).expanduser().read_bytes()
            img_mime = _guess_mime(Path(image), "image/png")
            body["image"] = f"data:{img_mime};base64,{base64.b64encode(img_bytes).decode('ascii')}"
        if extra_body:
            body.update(dict(extra_body))

        auth_headers = {"Authorization": f"Bearer {key}"}
        submitted = _json_request(endpoint, payload=body, headers=auth_headers, timeout=min(timeout, 120.0))

        def extract_video_url(data: Mapping[str, Any]) -> str | None:
            for key_name in ("video_url", "output_url", "url"):
                val = data.get(key_name)
                if isinstance(val, str) and val.startswith(("http://", "https://")):
                    return val
            nested = data.get("output") or data.get("result") or data.get("data")
            if isinstance(nested, Mapping):
                return extract_video_url(nested)
            if isinstance(nested, list) and nested and isinstance(nested[0], Mapping):
                return extract_video_url(nested[0])
            return None

        direct_url = extract_video_url(submitted)
        if direct_url:
            raw = _download_bytes(direct_url, timeout=timeout)
            return self.save_bytes(raw, stem=stem, ext=".mp4", kind="video", mime="video/mp4")

        job_id = str(submitted.get("id") or submitted.get("task_id") or submitted.get("job_id") or "").strip()
        status_url = str(submitted.get("status_url") or "").strip()
        if not status_url:
            if not job_id:
                raise RuntimeError("Video API returned neither a direct URL nor a job id")
            template = status_url_template or os.environ.get("FLOWX_VIDEO_STATUS_URL", "")
            status_url = template.format(id=job_id) if template else f"{endpoint.rstrip('/')}/{job_id}"

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(max(0.2, min(poll_interval, max(0.2, deadline - time.monotonic()))))
            state = _json_request(status_url, method="GET", headers=auth_headers, timeout=60.0)
            status_text = str(state.get("status") or state.get("state") or "").strip().lower()
            if status_text in {"failed", "error", "cancelled", "canceled"}:
                raise RuntimeError(f"Video generation job {job_id or status_url} failed: {state.get('error') or status_text}")
            ready_url = extract_video_url(state)
            if ready_url and status_text in {"", "succeeded", "completed", "done", "success", "finished"}:
                raw = _download_bytes(ready_url, timeout=timeout)
                return self.save_bytes(raw, stem=stem, ext=".mp4", kind="video", mime="video/mp4")
        raise TimeoutError(f"Video generation job {job_id or status_url} timed out after {timeout}s")

    def ffmpeg(
        self,
        args: Sequence[str],
        *,
        stem: str = "clip",
        ext: str = ".mp4",
        timeout: float = 300.0,
    ) -> MediaAsset:
        """Run `ffmpeg` with explicit argv (`shell=False`) producing an output asset."""
        target = self.output_path(stem, ext)
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            if self.dry_run:
                payload = _minimal_mp4_bytes() if ext.lower() == ".mp4" else _minimal_png_bytes()
                target.write_bytes(payload)
                return self._register_file(target)
            raise RuntimeError("ffmpeg executable is not installed on PATH")
        cmd = [ffmpeg_bin, "-y", *[str(a) for a in args], str(target)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode != 0 or not target.is_file():
            stderr_tail = (proc.stderr or "")[-600:]
            raise RuntimeError(f"ffmpeg failed (exit {proc.returncode}): {stderr_tail}")
        return self._register_file(target)

    def ffmpeg_trim_resize(
        self,
        source: str | os.PathLike[str],
        *,
        start: float = 0.0,
        duration: float | None = None,
        width: int | None = None,
        height: int | None = None,
        stem: str = "trimmed",
        timeout: float = 300.0,
    ) -> MediaAsset:
        """Trim and/or scale a video clip with ffmpeg (falls back cleanly in dry-run without ffmpeg)."""
        src_path = Path(source).expanduser().resolve()
        if not src_path.is_file():
            raise FileNotFoundError(f"Input video not found: {src_path}")
        if self.dry_run and not shutil.which("ffmpeg"):
            target = self.output_path(stem, ".mp4")
            target.write_bytes(_minimal_mp4_bytes(width=width or 320, height=height or 240, duration=duration or 1.0))
            return self._register_file(
                target, kind="video", mime="video/mp4", width=width, height=height, duration=duration,
            )
        argv: list[str] = []
        if start > 0:
            argv.extend(["-ss", f"{float(start):.3f}"])
        argv.extend(["-i", str(src_path)])
        if duration is not None:
            if duration <= 0:
                raise ValueError("duration must be positive")
            argv.extend(["-t", f"{float(duration):.3f}"])
        if width is not None or height is not None:
            w_expr = str((int(width) // 2) * 2) if width is not None else "-2"
            h_expr = str((int(height) // 2) * 2) if height is not None else "-2"
            argv.extend(["-vf", f"scale={w_expr}:{h_expr}"])
        argv.extend(["-pix_fmt", "yuv420p"])
        return self.ffmpeg(argv, stem=stem, ext=".mp4", timeout=timeout)

    def blender_render(
        self,
        script_or_blend: str | os.PathLike[str],
        *,
        is_python_script: bool = True,
        extra_args: Sequence[str] = (),
        stem: str = "render",
        ext: str = ".png",
        timeout: float = 600.0,
    ) -> MediaAsset:
        """Run headless Blender (`--background --factory-startup`) when explicitly enabled."""
        if self.dry_run:
            target = self.output_path(stem, ext)
            data = _minimal_mp4_bytes() if ext.lower() == ".mp4" else _minimal_png_bytes(256, 256, (90, 140, 210))
            target.write_bytes(data)
            return self._register_file(target)
        if not _truthy_env(_BLENDER_ENABLE_ENV):
            raise RuntimeError(
                "Headless Blender execution requires explicit opt-in via FLOWX_ENABLE_BLENDER=1"
            )
        blender_bin = os.environ.get("FLOWX_BLENDER_BIN") or shutil.which("blender")
        if not blender_bin:
            raise RuntimeError("Blender executable not found (set FLOWX_BLENDER_BIN or add blender to PATH)")
        source_path = Path(script_or_blend).expanduser().resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"Blender input not found: {source_path}")
        target = self.output_path(stem, ext)
        cmd = [blender_bin, "--background", "--factory-startup"]
        if is_python_script:
            cmd.extend(["--python-exit-code", "1", "--python", str(source_path)])
        else:
            cmd.extend([str(source_path), "-o", str(target), "-F", ext.lstrip(".").upper(), "-f", "1"])
        if extra_args:
            cmd.extend(["--", *[str(a) for a in extra_args], str(target)])
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode != 0 or not target.is_file():
            stderr_tail = (proc.stderr or "")[-600:]
            raise RuntimeError(f"Blender render failed (exit {proc.returncode}): {stderr_tail}")
        return self._register_file(target)


def media_space(
    step_id: str,
    session_state: Mapping[str, Any] | None = None,
    *,
    workflow_root: str | os.PathLike[str] | None = None,
    request_id: str | None = None,
    dry_run: bool | None = None,
) -> MediaSpace:
    """Resolve the run-scoped MediaSpace for `step_id` without reading literal `session_state[...]` keys in nodes."""
    state = session_state if isinstance(session_state, Mapping) else {}
    active_req = ""
    try:
        from ag_ui_workflow.params import ACTIVE_WORKFLOW_PARAM

        active_param = ACTIVE_WORKFLOW_PARAM.get()
        if active_param is not None and getattr(active_param, "request_id", ""):
            active_req = str(active_param.request_id)
    except Exception:
        pass
    raw_root = (
        workflow_root
        or state.get(WORKFLOW_ROOT_STATE_KEY)
        or os.environ.get("FLOWX_WORKFLOW_ROOT")
        or Path(tempfile.gettempdir()) / "flowx_media_runs"
    )
    raw_req = (
        request_id
        or state.get(REQUEST_ID_STATE_KEY)
        or active_req
        or os.environ.get("FLOWX_RUN_REQUEST_ID")
        or "local"
    )
    return MediaSpace(
        step_id=step_id,
        workflow_root=Path(raw_root),
        request_id=str(raw_req),
        dry_run=dry_run,
    )


def prompt_card(prompt: str, *, label: str = "Prompt", extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a standardized `StepRunOutput.card` for prompt/text nodes."""
    card: dict[str, Any] = {"kind": "prompt", "prompt": str(prompt), "summary": str(prompt)[:240]}
    if label:
        card["label"] = label
    if extra:
        card.update(dict(extra))
    return card


@dataclass
class _RunSnapshot:
    run_id: str
    session_id: str
    workflow_version: int
    status: str = "running"
    created_at: str = field(default_factory=_utc_iso)
    updated_at: str = field(default_factory=_utc_iso)
    awaiting: dict[str, Any] | None = None
    error: str | None = None
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "workflow_version": self.workflow_version,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "awaiting": self.awaiting,
            "error": self.error,
            "nodes": self.nodes,
        }


class RunJournal:
    """Thread-safe filesystem run journal writing `<workflow_root>/runs/<run_id>/run.json`."""

    def __init__(self, workflow_root: str | os.PathLike[str], *, workflow_version: int | None = None) -> None:
        self.workflow_root = Path(workflow_root).expanduser().resolve()
        self.runs_dir = self.workflow_root / "runs"
        self._lock = RLock()
        self._explicit_version = workflow_version
        self._snapshots: dict[str, _RunSnapshot] = {}

    def _detect_workflow_version(self) -> int:
        if self._explicit_version is not None:
            return int(self._explicit_version)
        manifest_path = self.workflow_root / "manifest.json"
        try:
            if manifest_path.is_file():
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                ver = data.get("workflow_version")
                if isinstance(ver, int) and not isinstance(ver, bool) and ver >= 0:
                    return ver
        except Exception:
            pass
        return 0

    def _load_or_create(self, run_id: str, session_id: str) -> _RunSnapshot:
        safe_id = _safe_segment(run_id, "run")
        snap = self._snapshots.get(safe_id)
        if snap is not None:
            if session_id:
                snap.session_id = session_id
            return snap
        existing_file = self.runs_dir / safe_id / "run.json"
        if existing_file.is_file():
            try:
                raw = json.loads(existing_file.read_text(encoding="utf-8"))
                snap = _RunSnapshot(
                    run_id=safe_id,
                    session_id=str(raw.get("session_id") or session_id),
                    workflow_version=int(raw.get("workflow_version") or self._detect_workflow_version()),
                    status=str(raw.get("status") or "running"),
                    created_at=str(raw.get("created_at") or _utc_iso()),
                    updated_at=_utc_iso(),
                    awaiting=raw.get("awaiting") if isinstance(raw.get("awaiting"), dict) else None,
                    error=raw.get("error") if isinstance(raw.get("error"), str) else None,
                    nodes=dict(raw.get("nodes") or {}),
                )
                self._snapshots[safe_id] = snap
                return snap
            except Exception:
                pass
        snap = _RunSnapshot(
            run_id=safe_id,
            session_id=session_id,
            workflow_version=self._detect_workflow_version(),
        )
        self._snapshots[safe_id] = snap
        return snap

    def _flush(self, snap: _RunSnapshot) -> None:
        snap.updated_at = _utc_iso()
        payload = snap.to_dict()
        run_file = self.runs_dir / snap.run_id / "run.json"
        _write_json_atomic(run_file, payload)
        _write_json_atomic(
            self.runs_dir / "latest.json",
            {
                "schema_version": 1,
                "run_id": snap.run_id,
                "session_id": snap.session_id,
                "workflow_version": snap.workflow_version,
                "status": snap.status,
                "updated_at": snap.updated_at,
            },
        )

    def mark_cancelled(self, run_id: str) -> None:
        safe_id = _safe_segment(run_id, "run")
        with self._lock:
            snap = self._snapshots.get(safe_id)
            if snap is None and not (self.runs_dir / safe_id / "run.json").is_file():
                return
            snap = self._load_or_create(safe_id, "")
            if snap.status in {"running", "input_required"}:
                snap.status = "cancelled"
                snap.awaiting = None
                self._flush(snap)

    def tee(self, run_id: str, session_id: str, events: Iterator[str]) -> Iterator[str]:
        """Yield SSE lines unchanged while atomically journaling per-node status and cards."""
        safe_id = _safe_segment(run_id, "run")
        with self._lock:
            snap = self._load_or_create(safe_id, session_id)
            snap.status = "running"
            snap.awaiting = None
            snap.error = None
            self._flush(snap)
        for chunk in events:
            self._ingest_chunk(safe_id, session_id, chunk)
            yield chunk

    def _ingest_chunk(self, safe_id: str, session_id: str, chunk: str) -> None:
        for line in chunk.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            with self._lock:
                snap = self._load_or_create(safe_id, session_id)
                etype = str(event.get("type") or "")
                ename = str(event.get("name") or "")
                if etype == "STEP_STARTED":
                    step_name = str(event.get("stepName") or "").strip()
                    if step_name:
                        prev = snap.nodes.get(step_name) or {}
                        snap.nodes[step_name] = {
                            **prev,
                            "status": "running",
                            "updated_at": _utc_iso(),
                        }
                        self._flush(snap)
                elif ename == "step_card" and isinstance(event.get("value"), Mapping):
                    val = event["value"]
                    step_name = str(val.get("stepId") or "").strip()
                    if step_name:
                        card = val.get("card")
                        snap.nodes[step_name] = {
                            "status": str(val.get("state") or "completed"),
                            "title": str(val.get("title") or step_name),
                            "card": dict(card) if isinstance(card, Mapping) else {},
                            "is_final": bool(val.get("isFinal")),
                            "updated_at": _utc_iso(),
                        }
                        self._flush(snap)
                elif ename == "input_required" and isinstance(event.get("value"), Mapping):
                    val = event["value"]
                    step_name = str(val.get("stepId") or "").strip()
                    snap.status = "input_required"
                    snap.awaiting = {
                        "stepId": step_name,
                        "title": str(val.get("title") or ""),
                        "prompt": str(val.get("prompt") or ""),
                    }
                    if step_name:
                        prev = snap.nodes.get(step_name) or {}
                        snap.nodes[step_name] = {
                            **prev,
                            "status": "awaiting_input",
                            "updated_at": _utc_iso(),
                        }
                    self._flush(snap)
                elif etype == "RUN_ERROR":
                    snap.status = "failed"
                    snap.error = str(event.get("message") or "Workflow step failed")
                    self._flush(snap)
                elif etype == "RUN_FINISHED":
                    res = event.get("result") if isinstance(event.get("result"), Mapping) else {}
                    if res.get("ok") is False:
                        snap.status = "failed"
                        failed_step = str(res.get("stepId") or "").strip()
                        if failed_step:
                            prev = snap.nodes.get(failed_step) or {}
                            snap.nodes[failed_step] = {
                                **prev,
                                "status": "failed",
                                "updated_at": _utc_iso(),
                            }
                    elif res.get("awaitingInput") or snap.awaiting:
                        snap.status = "input_required"
                    else:
                        snap.status = "completed"
                        snap.awaiting = None
                    self._flush(snap)


__all__ = [
    "MediaAsset",
    "MediaSpace",
    "REQUEST_ID_STATE_KEY",
    "RunJournal",
    "WORKFLOW_ROOT_STATE_KEY",
    "WORKFLOW_VERSION_STATE_KEY",
    "media_space",
    "prompt_card",
]
