"""Compression presets.

Each preset knows which kind of file it handles (video/audio/image), what the
output extension should be, and which ffmpeg arguments to use. In videos,
everything except the video stream (audio tracks, subtitles, chapters,
metadata) is stream-copied, so it stays bit-for-bit identical — even with the
lossy presets.

Presets come in three categories:
  lossless  - decoded output is bit-for-bit / pixel-for-pixel identical
  visual    - technically lossy, but tuned so you can't tell the difference
  lossy     - noticeably smaller files, quality loss may be visible/audible
"""

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".3gp"}
LOSSLESS_AUDIO_EXTS = {".wav", ".aif", ".aiff", ".flac", ".ape", ".wv"}
AUDIO_EXTS = LOSSLESS_AUDIO_EXTS | {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma"}
LOSSLESS_IMAGE_EXTS = {".png", ".bmp", ".tif", ".tiff"}
IMAGE_EXTS = LOSSLESS_IMAGE_EXTS | {".jpg", ".jpeg", ".webp"}

# allowed values for the "max resolution" option (short side, so 1080 = 1080p
# for landscape AND portrait videos)
MAX_HEIGHTS = [2160, 1440, 1080, 720, 480]


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
class Quality:
    """One adjustable number per preset: CRF for video, bitrate for audio, 1–100 for images."""
    label: str
    min: int
    max: int
    default: int
    unit: str = ""
    step: int = 1
    higher_is_better: bool = True
    hint: str = ""


@dataclass
class Preset:
    id: str
    kind: str
    category: str  # lossless | visual | lossy
    label: str
    description: str
    # ffmpeg args after the input; "{q}" is replaced by the quality value
    codec_args: list[str]
    # containers the codec can live in; anything else is remuxed to the fallback
    containers: set[str] = field(default_factory=set)
    fallback_ext: str = ".mkv"
    # input extensions this preset makes sense for (None = all of its kind).
    # e.g. lossless FLAC from an MP3 would only make the file bigger.
    inputs: set[str] | None = None
    quality: Quality | None = None
    # turns the UI value into what ffmpeg expects (e.g. 1–100 → JPEG's 31–2 scale)
    quality_to_ffmpeg: Callable[[int], int] | None = None
    scalable: bool = False  # offer the "max resolution" option

    def output_ext(self, src: Path) -> str:
        ext = src.suffix.lower()
        return ext if ext in self.containers else self.fallback_ext

    def accepts(self, src: Path) -> bool:
        return self.inputs is None or src.suffix.lower() in self.inputs

    def public(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "category": self.category,
            "label": self.label, "description": self.description,
            "quality": asdict(self.quality) if self.quality else None,
            "scalable": self.scalable,
        }

    def ffmpeg_args(self, dst: Path, quality: int | None, max_height: int | None) -> list[str]:
        q = self.quality.default if quality is None and self.quality else quality
        if q is not None and self.quality_to_ffmpeg:
            q = self.quality_to_ffmpeg(q)
        codec = [a.format(q=q) for a in self.codec_args]

        if self.kind != "video":
            return ["-map_metadata", "0", *codec]

        args = [
            # 0:V = real video streams (capital V skips cover-art images),
            # '?' = don't fail if the stream type doesn't exist
            "-map", "0:V?", "-map", "0:a?", "-map", "0:s?",
            "-map_metadata", "0", "-map_chapters", "0",
            "-c", "copy",  # copy everything by default...
            *codec,        # ...then re-encode only the video
        ]
        if self.scalable and max_height:
            # limit the SHORT side to max_height, keep aspect ratio, never upscale.
            # -2 = "whatever keeps the aspect ratio, rounded to an even number" (encoders need even sizes)
            h = int(max_height)
            args += ["-vf", f"scale='if(gte(iw,ih),-2,min(iw,{h}))':'if(gte(iw,ih),min(ih,{h}),-2)'"]
        if dst.suffix in (".mp4", ".m4v", ".mov"):
            args += ["-movflags", "+faststart"]
            if "libx265" in self.codec_args:
                args += ["-tag:v", "hvc1"]  # makes HEVC playable on Apple devices
        return args


CRF_HINT = "Lower = higher quality & bigger file."
MP4ISH = {".mp4", ".mkv", ".mov", ".m4v"}

PRESETS: dict[str, Preset] = {
    p.id: p
    for p in [
        # ---------------- video ----------------
        Preset(
            id="hevc", kind="video", category="visual",
            label="H.265 / HEVC — visually lossless",
            description="libx265, slow preset. CRF 18–22 is indistinguishable from the source for most content. Great size savings, plays almost everywhere.",
            codec_args=["-c:v", "libx265", "-crf", "{q}", "-preset", "slow", "-x265-params", "log-level=error"],
            containers=MP4ISH,
            quality=Quality("CRF", 14, 34, 20, higher_is_better=False, hint=CRF_HINT + " The default is visually lossless for most content."),
        ),
        Preset(
            id="av1", kind="video", category="visual",
            label="AV1 (SVT-AV1) — visually lossless",
            description="Best compression, slower and needs newer players. CRF 20–28 is visually transparent for most content.",
            codec_args=["-c:v", "libsvtav1", "-crf", "{q}", "-preset", "5", "-svtav1-params", "tune=0"],
            containers={".mp4", ".mkv", ".webm"},
            quality=Quality("CRF", 14, 40, 24, higher_is_better=False, hint=CRF_HINT + " The default is visually lossless for most content."),
        ),
        Preset(
            id="x264_lossless", kind="video", category="lossless",
            label="H.264 — mathematically lossless",
            description="Bit-exact video. Only shrinks files from uncompressed / intermediate sources (screen recordings, ProRes, raw captures). Usually makes normal videos BIGGER.",
            codec_args=["-c:v", "libx264", "-qp", "0", "-preset", "veryslow"],
            containers=MP4ISH,
        ),
        Preset(
            id="hevc_small", kind="video", category="lossy",
            label="H.265 / HEVC — small",
            description="Much smaller files for archiving or sharing. Fine detail and grain get smoothed; artifacts may show in dark or fast scenes at high CRF.",
            codec_args=["-c:v", "libx265", "-crf", "{q}", "-preset", "medium", "-x265-params", "log-level=error"],
            containers=MP4ISH,
            quality=Quality("CRF", 18, 40, 28, higher_is_better=False, hint=CRF_HINT + " 24–28 is a good balance, 30+ gets visibly soft."),
            scalable=True,
        ),
        Preset(
            id="av1_small", kind="video", category="lossy",
            label="AV1 (SVT-AV1) — small",
            description="Smallest files at a given quality, but slow to encode and needs a newer player (2020+ devices, current browsers).",
            codec_args=["-c:v", "libsvtav1", "-crf", "{q}", "-preset", "6"],
            containers={".mp4", ".mkv", ".webm"},
            quality=Quality("CRF", 20, 55, 35, higher_is_better=False, hint=CRF_HINT + " 30–38 is a good balance, 45+ gets visibly soft."),
            scalable=True,
        ),
        Preset(
            id="h264", kind="video", category="lossy",
            label="H.264 — max compatibility",
            description="Plays on literally everything (old TVs, browsers, phones). Bigger than HEVC/AV1 at the same quality. Converts to 8-bit 4:2:0.",
            codec_args=["-c:v", "libx264", "-crf", "{q}", "-preset", "medium", "-pix_fmt", "yuv420p"],
            containers=MP4ISH,
            quality=Quality("CRF", 16, 35, 23, higher_is_better=False, hint=CRF_HINT + " 20–24 looks good, 28+ gets visibly soft."),
            scalable=True,
        ),
        # ---------------- audio ----------------
        Preset(
            id="flac", kind="audio", category="lossless",
            label="FLAC — lossless",
            description="WAV/AIFF/APE/WavPack → FLAC at max compression. Bit-exact audio, typically 40–60% smaller than WAV. Already-lossy files (MP3, AAC…) are skipped.",
            codec_args=["-map", "0:a", "-c:a", "flac", "-compression_level", "12"],
            fallback_ext=".flac",
            inputs=LOSSLESS_AUDIO_EXTS,
        ),
        Preset(
            id="opus", kind="audio", category="lossy",
            label="Opus — best quality per MB",
            description="Most efficient lossy codec. 128 kbps is transparent for most music, 64–96 kbps is great for speech/podcasts. Plays in browsers, Android, VLC; iOS 17+.",
            codec_args=["-map", "0:a:0", "-c:a", "libopus", "-b:a", "{q}k", "-vbr", "on"],
            fallback_ext=".opus",
            quality=Quality("Bitrate", 32, 256, 128, unit="kbps", step=8, hint="Average target — Opus is VBR, so the real bitrate varies with the content."),
        ),
        Preset(
            id="aac", kind="audio", category="lossy",
            label="AAC (.m4a) — Apple-friendly",
            description="Plays everywhere, native on Apple devices. 192 kbps is transparent for most music.",
            codec_args=["-map", "0:a:0", "-c:a", "aac", "-b:a", "{q}k", "-movflags", "+faststart"],
            fallback_ext=".m4a",
            quality=Quality("Bitrate", 64, 320, 192, unit="kbps", step=8),
        ),
        Preset(
            id="mp3", kind="audio", category="lossy",
            label="MP3 — max compatibility",
            description="Plays on anything, including old car stereos. Least efficient: needs a higher bitrate than Opus/AAC for the same quality.",
            codec_args=["-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", "{q}k"],
            fallback_ext=".mp3",
            quality=Quality("Bitrate", 64, 320, 192, unit="kbps", step=8),
        ),
        # ---------------- images ----------------
        Preset(
            id="webp_lossless", kind="image", category="lossless",
            label="WebP — lossless",
            description="PNG/BMP/TIFF → lossless WebP. Pixel-exact for 8-bit images, usually 25–50% smaller than PNG. JPEG/WebP inputs are skipped.",
            codec_args=["-frames:v", "1", "-c:v", "libwebp", "-lossless", "1", "-compression_level", "6", "-quality", "100"],
            fallback_ext=".webp",
            inputs=LOSSLESS_IMAGE_EXTS,
        ),
        Preset(
            id="png_optimize", kind="image", category="lossless",
            label="PNG — lossless re-compress",
            description="Re-encodes as PNG with maximum compression. Pixel-exact, keeps the PNG format and bit depth. Modest savings. JPEG/WebP inputs are skipped.",
            codec_args=["-frames:v", "1", "-c:v", "png", "-pred", "mixed", "-compression_level", "9"],
            fallback_ext=".png",
            inputs=LOSSLESS_IMAGE_EXTS,
        ),
        Preset(
            id="webp", kind="image", category="lossy",
            label="WebP — lossy",
            description="Typically 25–35% smaller than JPEG at the same quality, keeps transparency. Supported by all current browsers and OSes. Drops EXIF data (camera, date, GPS).",
            codec_args=["-frames:v", "1", "-c:v", "libwebp", "-quality", "{q}", "-compression_level", "6", "-preset", "photo"],
            fallback_ext=".webp",
            quality=Quality("Quality", 40, 100, 80, hint="80 looks great for photos; 90+ for graphics/text."),
        ),
        Preset(
            id="jpeg", kind="image", category="lossy",
            label="JPEG — max compatibility",
            description="Opens anywhere. Transparency is lost (flattened). Drops EXIF data (camera, date, GPS).",
            codec_args=["-frames:v", "1", "-c:v", "mjpeg", "-q:v", "{q}"],
            fallback_ext=".jpg",
            quality=Quality("Quality", 40, 100, 85, hint="85 is a typical high-quality photo setting."),
            # ffmpeg's JPEG scale is 2 (best) … 31 (worst), so flip and stretch 1–100 onto it
            quality_to_ffmpeg=lambda q: round(2 + (100 - q) * 29 / 99),
        ),
    ]
}


def presets_for(kind: str) -> list[Preset]:
    return [p for p in PRESETS.values() if p.kind == kind]
