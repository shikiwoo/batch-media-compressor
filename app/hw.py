"""Hardware (Intel VAAPI) encoder detection.

Having /dev/dri in the container doesn't prove encoding works: the VA driver
may be missing, the ffmpeg build may lack the encoder, or the GPU may not
support that codec. So at startup we run a tiny REAL test encode for each
encoder and remember which ones work, and which rate-control mode they accept:

  ICQ  = "intelligent constant quality" — the closest thing to x264/x265's CRF
  CQP  = constant QP — fallback for drivers without ICQ

Both take a number where lower = better quality and a bigger file, so the UI
slider behaves the same either way.
"""

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path

DEVICE = os.environ.get("VAAPI_DEVICE", "/dev/dri/renderD128")
ENCODERS = ("h264_vaapi", "hevc_vaapi")


@dataclass
class Hardware:
    device: str = DEVICE
    modes: dict[str, str] = field(default_factory=dict)  # encoder -> "ICQ" | "CQP"
    reason: str = "Not checked yet"

    @property
    def available(self) -> bool:
        return bool(self.modes)

    def supports(self, encoder: str) -> bool:
        return encoder in self.modes

    def init_args(self) -> list[str]:
        """Global options that open the GPU. They must come BEFORE -i."""
        return ["-init_hw_device", f"vaapi=hw:{self.device}", "-filter_hw_device", "hw"]

    def quality_args(self, encoder: str, q: int) -> list[str]:
        if self.modes[encoder] == "ICQ":
            return ["-rc_mode", "ICQ", "-global_quality", str(q)]
        return ["-rc_mode", "CQP", "-qp", str(q)]

    def public(self) -> dict:
        return {
            "available": self.available,
            "device": self.device,
            "encoders": sorted(self.modes),
            "reason": "" if self.available else self.reason,
        }


# one shared instance; detect() fills it in at startup
HW = Hardware()


async def _test_encode(encoder: str, rc_args: list[str]) -> tuple[bool, str]:
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
        *HW.init_args(),
        "-f", "lavfi", "-i", "color=c=black:s=640x360:r=25:d=0.4",
        # same filter chain real jobs use: convert to nv12 in software, upload to the GPU
        "-vf", "format=nv12,hwupload",
        "-c:v", encoder, *rc_args,
        "-f", "null", "-",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        return False, "test encode timed out"
    except FileNotFoundError:
        return False, "ffmpeg not found"
    last = [l for l in err.decode(errors="replace").splitlines() if l.strip()][-2:]
    return proc.returncode == 0, " / ".join(last)


async def detect() -> Hardware:
    HW.modes = {}
    HW.device = DEVICE
    if not Path(DEVICE).exists():
        HW.reason = (
            f"{DEVICE} not found. Pass the GPU into the container "
            "(compose: devices: - /dev/dri:/dev/dri)."
        )
        return HW

    problems = []
    for enc in ENCODERS:
        for mode, rc_args in (("ICQ", ["-rc_mode", "ICQ", "-global_quality", "25"]),
                              ("CQP", ["-rc_mode", "CQP", "-qp", "25"])):
            ok, err = await _test_encode(enc, rc_args)
            if ok:
                HW.modes[enc] = mode
                break
        else:
            problems.append(f"{enc}: {err}")

    if not HW.modes:
        HW.reason = "GPU test encode failed. " + " | ".join(problems)
    else:
        HW.reason = ""
    return HW
