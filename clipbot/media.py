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


def _stream_info(path: Path) -> tuple[float, bool]:
    """(frame rate, has audio) of a video file."""
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,avg_frame_rate",
                        "-of", "json", str(path)], capture_output=True, text=True)
    fps, audio = 30.0, False
    try:
        for st in json.loads(p.stdout)["streams"]:
            if st.get("codec_type") == "audio":
                audio = True
            elif st.get("codec_type") == "video" and st.get("avg_frame_rate", "0/0") != "0/0":
                n, d = st["avg_frame_rate"].split("/")
                fps = float(n) / float(d) if float(d) else fps
    except (KeyError, ValueError, ZeroDivisionError):
        pass
    return (60.0 if fps > 45 else 30.0), audio


def duration(path: Path) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True)
    try:
        return float(json.loads(p.stdout)["format"]["duration"])
    except (KeyError, ValueError):
        return 0.0


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
               bg_saturation: float = 1.0, intro: dict | None = None) -> Path:
    """Re-encode to a Shorts-friendly MP4. With vertical=True a 16:9 clip is placed
    on a blurred, zoomed copy of itself in a 1080x1920 frame, optionally with a logo on top.
    intro: {"enabled", "seconds", "logo_width", "volume"} = TV static with the logo before the clip."""
    inputs = ["-t", str(max_seconds), "-i", str(src)]
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

    lg_in = None
    if logo:
        from PIL import Image  # only to read the logo's aspect ratio
        with Image.open(logo) as im:
            lw0, lh0 = im.size
        ow = int(overlay.get("width", 320))
        oh = round(ow * lh0 / lw0)
        x, y = _overlay_xy(overlay.get("position", "top-band"), ow, oh, clip_h, int(overlay.get("margin", 40)))
        opacity = float(overlay.get("opacity", 1.0))
        inputs += ["-i", str(logo)]
        lg_in = 1
        graph += (f";[1:v]scale={ow}:{oh},format=rgba,colorchannelmixer=aa={opacity}[lg]"
                  f";[base][lg]overlay={x}:{y}:format=auto[ov];[ov]format=yuv420p[v]")
    else:
        graph += ";[base]format=yuv420p[v]"

    maps = ["-map", "[v]", "-map", "0:a?"]
    intro = intro or {}
    secs = float(intro.get("seconds", 0.5))
    if intro.get("enabled") and secs > 0:
        fps, has_audio = _stream_info(src)
        fw, fh = (W, H) if vertical else _video_size(src)
        n = 1 + (1 if lg_in else 0)
        inputs += ["-f", "lavfi", "-i", f"color=c=0x808080:s={fw}x{fh}:r={fps:g}:d={secs}",
                   "-f", "lavfi", "-i", f"anoisesrc=d={secs}:c=white:a=1:r=48000"]
        st_v, st_a = n, n + 1
        graph += (f";[{st_v}:v]noise=alls=100:allf=t+u,hue=s=0,eq=contrast=2.3:brightness=-0.02,"
                  f"drawgrid=w={fw}:h=4:t=1:c=black@0.25,setsar=1[st]")
        last = "st"
        if logo:
            inputs += ["-loop", "1", "-t", f"{secs}", "-i", str(logo)]
            lw = int(intro.get("logo_width", 560))
            graph += f";[{n + 2}:v]scale={lw}:-1,format=rgba[il];[st][il]overlay=(W-w)/2:(H-h)/2:shortest=1[sl]"
            last = "sl"
        graph += f";[{last}]format=yuv420p,fps={fps:g}[iv];[v]setsar=1,fps={fps:g}[cv]"
        vol = float(intro.get("volume", 0.12))
        if has_audio:
            graph += (f";[{st_a}:a]highpass=f=300,volume={vol},aresample=48000,aformat=channel_layouts=stereo[ia]"
                      ";[0:a]aresample=48000,aformat=channel_layouts=stereo[ca]"
                      ";[iv][ia][cv][ca]concat=n=2:v=1:a=1[vo][ao]")
            maps = ["-map", "[vo]", "-map", "[ao]"]
        else:
            graph += ";[iv][cv]concat=n=2:v=1:a=0[vo]"
            maps = ["-map", "[vo]"]

    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs,
           "-filter_complex", graph, *maps,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst)]
    if not _run(cmd):
        raise RuntimeError(f"ffmpeg could not convert {src.name}")
    return dst


def short_thumbnail(video: Path, dst: Path, at: float, text: str = "", font: str | None = None) -> bool:
    """JPEG thumbnail for a Short: the frame at `at` seconds (no static), optionally with a short
    punchy text in the blurred area under the clip."""
    raw = dst.with_suffix(".png")
    if not _run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{max(at, 0):.2f}", "-i", str(video),
                 "-frames:v", "1", str(raw)]) or not raw.exists():
        return False
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(raw).convert("RGB")
    text = (text or "").strip().upper()
    if text and font:
        d = ImageDraw.Draw(im)
        w, h = im.size
        size = int(w * 0.11)
        while size > 40:
            f = ImageFont.truetype(font, size)
            lines, cur = [], ""
            for word in text.split():
                t = f"{cur} {word}".strip()
                if d.textlength(t, font=f) <= w * 0.9:
                    cur = t
                else:
                    lines.append(cur)
                    cur = word
            lines.append(cur)
            if len(lines) <= 3 and all(d.textlength(x, font=f) <= w * 0.9 for x in lines):
                break
            size -= 6
        line_h = int(size * 1.15)
        block = line_h * len(lines)
        clip_bottom = (h + round(w * 9 / 16)) // 2 if h > w else int(h * 0.62)
        y = clip_bottom + max((h - clip_bottom - block) // 2, 20) if h > w else h - block - 60
        for ln in lines:
            tw = d.textlength(ln, font=f)
            d.text(((w - tw) / 2, y), ln, font=f, fill="white", stroke_width=max(size // 12, 4),
                   stroke_fill="black")
            y += line_h
    im.save(dst, "JPEG", quality=88)
    raw.unlink(missing_ok=True)
    return True
