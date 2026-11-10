"""Download clips and turn them into vertical Shorts."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .sources import Clip

log = logging.getLogger(__name__)


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


def make_short(src: Path, dst: Path, vertical: bool, max_seconds: int) -> Path:
    """Re-encode to a Shorts-friendly MP4. With vertical=True a 16:9 clip is placed
    on a blurred, zoomed copy of itself in a 1080x1920 frame."""
    if vertical:
        vf = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
              "boxblur=20:2[bg];[0:v]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-t", str(max_seconds),
               "-filter_complex", vf, "-map", "[v]", "-map", "0:a?"]
    else:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-t", str(max_seconds)]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst)]
    if not _run(cmd):
        raise RuntimeError(f"ffmpeg could not convert {src.name}")
    return dst
