from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from moshi_data_pipeline.exceptions import ExternalToolError, InputValidationError

LOGGER = logging.getLogger(__name__)
SUPPORTED_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mp3", ".m4a", ".wav", ".flac"}


def require_tools() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise ExternalToolError(f"Required executable(s) not found in PATH: {', '.join(missing)}")


def run_command(arguments: list[str], *, tool: str) -> subprocess.CompletedProcess[str]:
    LOGGER.debug("Running %s with %d arguments", tool, len(arguments) - 1)
    try:
        result = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
    except OSError as exc:
        raise ExternalToolError(f"Could not start {tool}: {exc}") from exc
    if result.returncode:
        raise ExternalToolError(
            f"{tool} failed with exit code {result.returncode}\n"
            f"command: {subprocess.list2cmdline(arguments)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def inspect_media(path: Path) -> dict[str, Any]:
    require_tools()
    if not path.is_file():
        raise InputValidationError(f"Input file does not exist: {path}")
    probe = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        tool="ffprobe",
    )
    try:
        metadata = json.loads(probe.stdout)
    except json.JSONDecodeError as exc:
        raise InputValidationError(f"ffprobe returned invalid JSON for {path}: {exc}") from exc
    audio_streams = [
        stream for stream in metadata.get("streams", []) if stream.get("codec_type") == "audio"
    ]
    video_streams = [
        stream for stream in metadata.get("streams", []) if stream.get("codec_type") == "video"
    ]
    if not audio_streams:
        raise InputValidationError(f"No audio stream found in {path}")
    stream = audio_streams[0]
    duration_value = stream.get("duration") or metadata.get("format", {}).get("duration")
    try:
        duration = float(duration_value)
    except (TypeError, ValueError) as exc:
        raise InputValidationError(f"Invalid or missing audio duration in {path}") from exc
    if duration <= 0:
        raise InputValidationError(f"Audio duration must be positive, got {duration}")

    # This both verifies complete decoding and gives a conservative source peak indicator.
    decode = run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            "volumedetect",
            "-f",
            "null",
            os.devnull,
        ],
        tool="ffmpeg decode validation",
    )
    peak_match = re.search(r"max_volume:\s*(-?(?:inf|\d+(?:\.\d+)?))\s*dB", decode.stderr)
    mean_match = re.search(r"mean_volume:\s*(-?(?:inf|\d+(?:\.\d+)?))\s*dB", decode.stderr)

    def parse_db(match: re.Match[str] | None) -> float | None:
        if match is None or match.group(1) == "-inf":
            return None
        return float(match.group(1))

    sample_rate = int(stream["sample_rate"]) if stream.get("sample_rate") else None
    channels = int(stream["channels"]) if stream.get("channels") else None

    def parse_rate(value: str | None) -> float | None:
        if not value:
            return None
        try:
            numerator, denominator = value.split("/", 1)
            rate = float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError):
            return None
        return rate if rate > 0 else None

    channel_difference_db: float | None = None
    channel_difference_relative_db: float | None = None
    dual_mono = False
    if channels == 2:
        difference = run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-vn",
                "-af",
                "pan=mono|c0=c0-c1,volumedetect",
                "-f",
                "null",
                os.devnull,
            ],
            tool="ffmpeg channel comparison",
        )
        difference_match = re.search(
            r"mean_volume:\s*(-?(?:inf|\d+(?:\.\d+)?))\s*dB", difference.stderr
        )
        channel_difference_db = parse_db(difference_match)
        program_level = parse_db(mean_match)
        if channel_difference_db is None:
            dual_mono = True
        elif program_level is not None:
            channel_difference_relative_db = channel_difference_db - program_level
            dual_mono = channel_difference_relative_db <= -30.0
    result = {
        "path": str(path.resolve()),
        "format_name": metadata.get("format", {}).get("format_name"),
        "audio_codec": stream.get("codec_name"),
        "duration": duration,
        "sample_rate": sample_rate,
        "channels": channels,
        "channel_layout": stream.get("channel_layout"),
        "source_is_stereo": channels == 2,
        "source_channel_difference_db": channel_difference_db,
        "source_channel_difference_relative_db": channel_difference_relative_db,
        "source_is_dual_mono": dual_mono,
        "has_video": bool(video_streams),
        "video_codec": video_streams[0].get("codec_name") if video_streams else None,
        "video_width": int(video_streams[0]["width"])
        if video_streams and video_streams[0].get("width")
        else None,
        "video_height": int(video_streams[0]["height"])
        if video_streams and video_streams[0].get("height")
        else None,
        "video_frame_rate": parse_rate(video_streams[0].get("avg_frame_rate"))
        if video_streams
        else None,
        "mean_volume_db": parse_db(mean_match),
        "max_volume_db": parse_db(peak_match),
        "possible_source_clipping": bool(
            peak_match and parse_db(peak_match) is not None and parse_db(peak_match) >= -0.01
        ),
        "decode_validated": True,
    }
    if result["mean_volume_db"] is None and result["max_volume_db"] is None:
        raise InputValidationError(f"Decoded audio appears empty or fully silent: {path}")
    return result


def extract_working_wav(source: Path, destination: Path, sample_rate: int = 24_000) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.{os.getpid()}.tmp.wav")
    try:
        run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-c:a",
                "pcm_s16le",
                str(temporary),
            ],
            tool="ffmpeg extraction",
        )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def create_video_proxy(source: Path, destination: Path) -> None:
    """Create a browser-compatible review proxy while preserving source timing."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.{os.getpid()}.tmp.mp4")
    try:
        run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-vf",
                "scale=w='min(1280,iw)':h=-2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "26",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(temporary),
            ],
            tool="ffmpeg video proxy",
        )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
