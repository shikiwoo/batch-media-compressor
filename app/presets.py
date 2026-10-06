"""Compression presets.

Each preset knows which kind of file it handles (video/audio/image), what the
output extension should be, and which ffmpeg arguments to use. Everything not
re-encoded (audio tracks, subtitles, chapters, metadata) is stream-copied, so
it stays bit-for-bit identical.
"""

from dataclasses import dataclass, field
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".3gp"}
AUDIO_EXTS = {".wav", ".aif", ".aiff", ".flac", ".ape", ".wv"}
IMAGE_EXTS = {".png", ".bmp", ".tif", ".tiff"}


def kind_of(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in IMAGE_EXTS:
        return "image"
    return None


@dataclass
class Preset:
    id: str
    kind: str
    label: str
    description: str
    lossless: bool
    # containers the codec can live in; anything else is remuxed to the fallback
    containers: set[str] = field(default_factory=set)
    fallback_ext: str = ".mkv"
    uses_crf: bool = False
    default_crf: int = 0

    def output_ext(self, src: Path) -> str:
        ext = src.suffix.lower()
        return ext if ext in self.containers else self.fallback_ext

    def ffmpeg_args(self, src: Path, dst: Path, crf: int | None) -> list[str]:
        raise NotImplementedError


class VideoPreset(Preset):
    def __init__(self, *, codec_args, **kw):
        super().__init__(kind="video", **kw)
        self.codec_args = codec_args

    def ffmpeg_args(self, src, dst, crf):
        crf = self.default_crf if crf is None else crf
        args = [
            # 0:V = real video streams (capital V skips cover-art images),
            # '?' = don't fail if the stream type doesn't exist
            "-map", "0:V?", "-map", "0:a?", "-map", "0:s?",
            "-map_metadata", "0", "-map_chapters", "0",
            "-c", "copy",  # copy everything by default...
            *[a.format(crf=crf) for a in self.codec_args],  # ...then re-encode only video
        ]
        if dst.suffix in (".mp4", ".m4v", ".mov"):
            args += ["-movflags", "+faststart"]
            if "libx265" in self.codec_args:
                args += ["-tag:v", "hvc1"]  # makes HEVC playable on Apple devices
        return args


class SimplePreset(Preset):
    def __init__(self, *, kind, codec_args, **kw):
        super().__init__(kind=kind, **kw)
        self.codec_args = codec_args

    def ffmpeg_args(self, src, dst, crf):
        return ["-map_metadata", "0", *self.codec_args]


PRESETS: dict[str, Preset] = {
    p.id: p
    for p in [
        VideoPreset(
            id="hevc",
            label="H.265 / HEVC — visually lossless",
            description="libx265, slow preset. CRF 18–22 is indistinguishable from the source for most content. Great size savings, plays almost everywhere.",
            lossless=False,
            containers={".mp4", ".mkv", ".mov", ".m4v"},
            uses_crf=True,
            default_crf=20,
            codec_args=["-c:v", "libx265", "-crf", "{crf}", "-preset", "slow", "-x265-params", "log-level=error"],
        ),
        VideoPreset(
            id="av1",
            label="AV1 (SVT-AV1) — visually lossless",
            description="Best compression, slower and needs newer players. CRF 20–28 is visually transparent for most content.",
            lossless=False,
            containers={".mp4", ".mkv", ".webm"},
            uses_crf=True,
            default_crf=24,
            codec_args=["-c:v", "libsvtav1", "-crf", "{crf}", "-preset", "5", "-svtav1-params", "tune=0"],
        ),
        VideoPreset(
            id="x264_lossless",
            label="H.264 — mathematically lossless",
            description="Bit-exact video. Only shrinks files from uncompressed / intermediate sources (screen recordings, ProRes, raw captures). Usually makes normal videos BIGGER.",
            lossless=True,
            containers={".mp4", ".mkv", ".mov", ".m4v"},
            codec_args=["-c:v", "libx264", "-qp", "0", "-preset", "veryslow"],
        ),
        SimplePreset(
            id="flac",
            kind="audio",
            label="FLAC — lossless",
            description="WAV/AIFF/APE/WavPack → FLAC at max compression. Bit-exact audio, typically 40–60% smaller than WAV.",
            lossless=True,
            containers={".flac"},
            fallback_ext=".flac",
            codec_args=["-map", "0:a", "-c:a", "flac", "-compression_level", "12"],
        ),
        SimplePreset(
            id="webp_lossless",
            kind="image",
            label="WebP — lossless",
            description="PNG/BMP/TIFF → lossless WebP. Pixel-exact, usually 25–50% smaller than PNG.",
            lossless=True,
            containers={".webp"},
            fallback_ext=".webp",
            codec_args=["-frames:v", "1", "-c:v", "libwebp", "-lossless", "1", "-compression_level", "6", "-quality", "100"],
        ),
        SimplePreset(
            id="png_optimize",
            kind="image",
            label="PNG — lossless re-compress",
            description="Re-encodes as PNG with maximum compression. Pixel-exact, keeps the PNG format. Modest savings.",
            lossless=True,
            containers={".png"},
            fallback_ext=".png",
            codec_args=["-frames:v", "1", "-c:v", "png", "-pred", "mixed", "-compression_level", "9"],
        ),
    ]
}


def presets_for(kind: str) -> list[Preset]:
    return [p for p in PRESETS.values() if p.kind == kind]
