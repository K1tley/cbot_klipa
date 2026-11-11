"""Download clips and turn them into vertical Shorts."""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from .sources import Clip

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
W, H = 1080, 1920  # Shorts frame


def _run(cmd: list[str]) -> bool:
    log.debug("run: %s", " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        log.warning("command failed (%s): %s", cmd[0], p.stderr[-800:])
    return p.returncode == 0


def download(clip: Clip, out: Path) -> bool:
    """Try the clip page with yt-dlp, then any direct media URLs."""
    out.parent.mkdir(parents=True, exist_ok=True)
    candidates = [clip.url] + clip.media_urls
    for url in candidates:
        for extra in (["--impersonate", "chrome"], []):
            out.unlink(missing_ok=True)
            cmd = ["yt-dlp", "--no-playlist", "--quiet", "--no-warnings",
                   "-f", "best[ext=mp4]/best", "--merge-output-format", "mp4",
                   "-o", str(out), *extra, url]
            if _run(cmd) and out.exists() and out.stat().st_size > 0:
                return True
    # last resort: ffmpeg straight from a direct (m3u8/mp4) URL
    for url in clip.media_urls:
        if _run(["ffmpeg", "-y", "-loglevel", "error", "-headers", "Referer: https://kick.com/\r\n",
                 "-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc", str(out)]) and out.exists():
            return True
    return False


def _video_size(path: Path) -> tuple[int, int]:
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "json", str(path)],
                       capture_output=True, text=True)
    try:
        s = json.loads(p.stdout)["streams"][0]
        return int(s["width"]), int(s["height"])
    except (KeyError, IndexError, ValueError):
        return 1920, 1080


def _overlay_xy(position: str, ow: int, oh: int, clip_h: int, margin: int) -> tuple[int, int]:
    """Where to put an overlay of size ow x oh. 'top-band'/'bottom-band' centre it in the
    blurred area above/below the clip; corners are measured from the frame edge."""
    band = max((H - clip_h) // 2, 0)
    cx = (W - ow) // 2
    positions = {
        "top-band": (cx, max((band - oh) // 2, margin)),
        "bottom-band": (cx, H - band + max((band - oh) // 2, 0)),
        "top-left": (margin, margin), "top-right": (W - ow - margin, margin),
        "bottom-left": (margin, H - oh - margin), "bottom-right": (W - ow - margin, H - oh - margin),
        "center": (cx, (H - oh) // 2),
    }
    return positions.get(position, positions["top-band"])


def make_short(src: Path, dst: Path, vertical: bool, max_seconds: int, overlay: dict | None = None,
               bg_saturation: float = 1.0) -> Path:
    """Re-encode to a Shorts-friendly MP4. With vertical=True a 16:9 clip is placed
    on a blurred, zoomed copy of itself in a 1080x1920 frame, optionally with a logo on top."""
    inputs = ["-i", str(src)]
    logo = None
    if overlay and overlay.get("image"):
        logo = ROOT / overlay["image"]
        if not logo.exists():
            log.warning("Overlay image %s not found — skipping the logo", logo)
            logo = None

    if vertical:
        sw, sh = _video_size(src)
        clip_h = min(H, round(W * sh / sw / 2) * 2)
        # blur a small copy and scale it up: looks the same, ~5x faster than blurring at full size
        graph = ("[0:v]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=6:1,"
                 f"eq=saturation={bg_saturation:.2f},scale=1080:1920[bg];[0:v]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[base]")
    else:
        clip_h = H
        graph = "[0:v]null[base]"

    if logo:
        from PIL import Image  # only to read the logo's aspect ratio
        with Image.open(logo) as im:
            lw0, lh0 = im.size
        ow = int(overlay.get("width", 320))
        oh = round(ow * lh0 / lw0)
        x, y = _overlay_xy(overlay.get("position", "top-band"), ow, oh, clip_h, int(overlay.get("margin", 40)))
        opacity = float(overlay.get("opacity", 1.0))
        inputs += ["-i", str(logo)]
        graph += (f";[1:v]scale={ow}:{oh},format=rgba,colorchannelmixer=aa={opacity}[lg]"
                  f";[base][lg]overlay={x}:{y}:format=auto[ov];[ov]format=yuv420p[v]")
    else:
        graph += ";[base]format=yuv420p[v]"

    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-t", str(max_seconds),
           "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst)]
    if not _run(cmd):
        raise RuntimeError(f"ffmpeg could not convert {src.name}")
    return dst
