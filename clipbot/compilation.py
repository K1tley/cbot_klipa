"""Daily long-form compilation of the best CZ/SK clips.

Pipeline (python -m clipbot compile):
  1. channel pool  = CZ/SK Twitch channels found by `discover` + creators from config + extra_channels
  2. fetch the day's clips for every channel and score them: views / that channel's usual clip views
  3. pick the best clips until the video is long enough (5-20 min, depending on how good the day was)
  4. order them: a highlight first, other highlights spread through the video, never the same
     streamer twice in a row
  5. render 1080p30 with a name tag + logo, join, make a thumbnail, write title/description with AI
  6. store on Drive and queue the upload for upload_hour (private, so it can be checked first)
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import statistics
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import captions, insights, media, sources

log = logging.getLogger(__name__)
CHANNELS = "channels.json"
LAST: dict = {}  # filled by build(): numbers for the daily report
ROOT = Path(__file__).resolve().parent.parent
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _font() -> str | None:
    return next((f for f in FONT_CANDIDATES if os.path.exists(f)), None)


# ================================================================ discovery
def empty_channels() -> dict:
    return {"last_discovery": None, "channels": {}}


def discover(cfg: dict, drive, force: bool = False) -> None:
    """Remember every channel that is live in Czech/Slovak right now (cheap: a few API calls)."""
    comp = cfg["compilation"]
    disc = comp.get("discover", {})
    data = drive.read_json(CHANNELS, empty_channels())
    now = datetime.now(timezone.utc)
    last = data.get("last_discovery")
    every = timedelta(hours=float(disc.get("every_hours", 2)))
    if not force and last and now - datetime.fromisoformat(last.replace("Z", "+00:00")) < every:
        log.info("Discovery ran less than %s ago — skipping.", every)
        return
    tw = sources.Twitch()
    today = now.date().isoformat()
    min_viewers = int(disc.get("min_viewers", 20))
    found = 0
    for lang in disc.get("languages", ["cs", "sk"]):
        for s in tw.live_streams(lang, int(disc.get("max_pages", 10))):
            if int(s.get("viewer_count", 0)) < min_viewers:
                continue
            key = f"twitch:{s['user_login'].lower()}"
            ch = data["channels"].setdefault(key, {"first_seen": today, "history": []})
            ch.update(platform="twitch", channel=s["user_login"].lower(), name=s["user_name"],
                      id=s["user_id"], language=lang, last_seen=today,
                      peak_viewers=max(int(ch.get("peak_viewers", 0)), int(s.get("viewer_count", 0))))
            found += 1
    data["last_discovery"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    drive.write_json(CHANNELS, data)
    log.info("Discovery: %d live CZ/SK channels now, %d known in total.", found, len(data["channels"]))


def channel_pool(cfg: dict, data: dict, today) -> list[dict]:
    comp = cfg["compilation"]
    disc = comp.get("discover", {})
    active_days = int(disc.get("active_days", 14))
    pool: dict[str, dict] = {}
    for key, ch in data["channels"].items():
        try:
            seen = datetime.fromisoformat(ch.get("last_seen", "2000-01-01")).date()
        except ValueError:
            continue
        if (today - seen).days <= active_days and int(ch.get("peak_viewers", 0)) >= int(disc.get("min_viewers", 20)):
            pool[key] = dict(ch, key=key)
    ranked = sorted(pool.values(), key=lambda c: -int(c.get("peak_viewers", 0)))
    ranked = ranked[: int(disc.get("max_channels", 150))]
    pool = {c["key"]: c for c in ranked}
    # creators from the Shorts list + extra channels (Kick has no language search) are always included
    for c in list(cfg.get("creators", [])) + list(comp.get("extra_channels") or []):
        key = f"{c['platform'].lower()}:{c['channel'].lower()}"
        known = data["channels"].get(key, {})
        entry = pool.setdefault(key, dict(known, key=key, platform=c["platform"].lower(),
                                          channel=c["channel"].lower(), name=c.get("name", c["channel"])))
        entry["favorite"] = True
    return list(pool.values())  # same streamer on Twitch and Kick: both scanned, clips deduped by id


# ================================================================ scoring & selection
@dataclass(eq=False)
class Pick:
    clip: sources.Clip
    score: float
    duration: float
    path: Path | None = None
    extra: dict = field(default_factory=dict)


def size_factor(ch: dict, comp: dict) -> float:
    """Bigger / well-known streamers count more, so a tiny channel's '10x its usual 2 views' clip
    doesn't beat a real moment from a known personality. 1.0 = a channel with reference_viewers."""
    pop = comp.get("popularity") or {}
    weight = float(pop.get("weight", 0.5))
    if weight <= 0:
        return 1.0
    size = float(ch.get("peak_viewers") or 0)
    if ch.get("favorite"):
        size = max(size, float(pop.get("favorite_viewers", 2000)))
    ref = float(pop.get("reference_viewers", 500))
    f = (max(size, 1.0) / ref) ** weight
    return min(max(f, float(pop.get("min_factor", 0.15))), float(pop.get("max_factor", 1.5)))


def baseline(ch: dict, today_views: list[int], day_label: str) -> float:
    """A channel's 'normal' clip views: average of its daily medians over the previous days."""
    hist = [v for d, v in ch.get("history", []) if d != day_label]
    if hist:
        return max(statistics.mean(hist[-14:]), 1.0)
    return max(statistics.median(today_views), 1.0) if today_views else 1.0


def select(picks: list[Pick], comp: dict) -> list[Pick]:
    min_s, max_s = float(comp["min_minutes"]) * 60, float(comp["max_minutes"]) * 60
    max_per = int(comp.get("max_clips_per_streamer", 4))
    thr = float(comp.get("score_threshold", 2.0))
    max_avg = float(comp.get("max_average_seconds", 40))
    ranked = sorted(picks, key=lambda p: -p.score)
    chosen: list[Pick] = []
    per: dict[str, int] = {}
    total = 0.0
    for t in (thr, thr * 0.75, thr * 0.5, 0.0):  # relax the bar only if the day is too thin
        for p in ranked:
            if p in chosen or p.score < t or per.get(who(p), 0) >= max_per:
                continue
            if total + p.duration > max_s:
                continue
            # keep the average clip short: a long clip only gets in if it's a real top highlight
            if chosen and (total + p.duration) / (len(chosen) + 1) > max_avg and p.score < 1.5 * thr:
                continue
            chosen.append(p)
            per[who(p)] = per.get(who(p), 0) + 1
            total += p.duration
        if total >= min_s:
            break
    return limit_dominance(chosen)


def limit_dominance(chosen: list[Pick]) -> list[Pick]:
    """Make "never the same streamer twice in a row" possible: no streamer may have more than half
    of the clips (drops that streamer's weakest clips until it fits)."""
    chosen = list(chosen)
    while len(chosen) > 1:
        counts: dict[str, int] = {}
        for p in chosen:
            counts[who(p)] = counts.get(who(p), 0) + 1
        top, n = max(counts.items(), key=lambda kv: kv[1])
        if len(counts) > 1 and n <= math.ceil(len(chosen) / 2):
            break
        if len(counts) == 1 and len(chosen) <= 1:
            break
        weakest = min((p for p in chosen if who(p) == top), key=lambda p: p.score)
        chosen.remove(weakest)
    return chosen


def who(p: Pick) -> str:
    return p.clip.creator.strip().lower()


def _adjacent_ok(seq: list[Pick]) -> bool:
    return all(who(seq[i]) != who(seq[i - 1]) for i in range(1, len(seq)))


def order(chosen: list[Pick], rng: random.Random) -> list[Pick]:
    """Highlight first, other highlights spread evenly (with jitter), rest shuffled,
    never two clips from the same streamer back to back."""
    n = len(chosen)
    if n <= 1:
        return list(chosen)
    ranked = sorted(chosen, key=lambda p: -p.score)
    k = max(1, round(n / 3))
    highlights, rest = ranked[:k], ranked[k:]
    for _attempt in range(60):
        hl = [highlights[0]] + rng.sample(highlights[1:], len(highlights) - 1)
        others = rng.sample(rest, len(rest))
        slots: list[Pick | None] = [None] * n
        slots[0] = hl[0]
        used = {0}
        for i, h in enumerate(hl[1:], start=1):
            ideal = i * n / k + rng.uniform(-n / (3 * k), n / (3 * k))
            pos = min(max(1, round(ideal)), n - 1)
            while pos in used:
                pos = pos + 1 if pos + 1 < n else 1
            slots[pos] = h
            used.add(pos)
        it = iter(others)
        seq = [s if s is not None else next(it) for s in slots]
        # repair: swap a clashing clip with another position where both fit
        for _ in range(n * 3):
            bad = next((i for i in range(1, n) if who(seq[i]) == who(seq[i - 1])), None)
            if bad is None:
                break
            hl_set = set(map(id, hl))
            same = lambda j: (id(seq[j]) in hl_set) == (id(seq[bad]) in hl_set)  # noqa: E731
            cands = rng.sample(range(1, n), n - 1)
            cands.sort(key=lambda j: (not same(j), abs(j - bad)))  # keep highlights where they are
            for j in cands:
                if j == bad:
                    continue
                trial = seq[:]
                trial[bad], trial[j] = trial[j], trial[bad]
                ok = all(who(trial[x]) != who(trial[x - 1])
                         for x in {bad, bad + 1, j, j + 1} if 0 < x < n)
                if ok:
                    seq = trial
                    break
        if _adjacent_ok(seq):
            return seq
    # fallback that always works when no streamer has more than half: greedy by remaining count
    remaining = rest + highlights[1:]
    rng.shuffle(remaining)
    seq = [highlights[0]]
    while remaining:
        counts: dict[str, int] = {}
        for p in remaining:
            counts[who(p)] = counts.get(who(p), 0) + 1
        options = [p for p in remaining if who(p) != who(seq[-1])]
        if not options:
            options = remaining
        best = max(options, key=lambda p: (counts[who(p)], rng.random()))
        seq.append(best)
        remaining.remove(best)
    return seq


# ================================================================ rendering
def _probe(path: Path) -> tuple[float, bool]:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                        "-of", "json", str(path)], capture_output=True, text=True)
    try:
        info = json.loads(p.stdout)
        dur = float(info["format"]["duration"])
        has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
        return dur, has_audio
    except (KeyError, ValueError):
        return 0.0, False


def render_segment(src: Path, dst: Path, name: str, comp: dict, tmp: Path, max_seconds: float,
                   fade_out: float = 0.0) -> bool:
    """Normalise one clip to 1920x1080 / 30 fps / 48 kHz stereo with a name tag and logo.
    fade_out > 0: fade picture and sound to black/silence over the last N seconds (end of the video)."""
    _, has_audio = _probe(src)
    inputs = ["-i", str(src)]
    if not has_audio:
        inputs += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    graph = ("[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
             "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v0]")
    last = "v0"
    font = _font()
    tag_secs = float(comp.get("name_tag_seconds", 5))
    if comp.get("name_tag", True) and font:
        txt = tmp / f"{dst.stem}.txt"
        txt.write_text(name, encoding="utf-8")
        enable = f":enable='lt(t,{tag_secs})'" if tag_secs > 0 else ""
        graph += (f";[{last}]drawtext=fontfile={font}:textfile={txt}:fontsize=44:fontcolor=white"
                  f":box=1:boxcolor=black@0.55:boxborderw=16:x=48:y=h-th-60{enable}[v1]")
        last = "v1"
    logo_cfg = comp.get("logo") or {}
    logo = ROOT / logo_cfg["image"] if logo_cfg.get("image") else None
    if logo and logo.exists() and logo_cfg.get("corner", False):
        idx = len([a for a in inputs if a == "-i"])
        inputs += ["-i", str(logo)]
        w = int(logo_cfg.get("width", 160))
        op = float(logo_cfg.get("opacity", 0.9))
        graph += (f";[{idx}:v]scale={w}:-1,format=rgba,colorchannelmixer=aa={op}[lg]"
                  f";[{last}][lg]overlay=W-w-36:30[v2]")
        last = "v2"
    clip_len = min(max_seconds, _probe(src)[0] or max_seconds)
    fade_at = max(clip_len - fade_out, 0)
    if fade_out > 0:
        graph += f";[{last}]fade=t=out:st={fade_at:.2f}:d={fade_out:.2f}[fo]"
        last = "fo"
    graph += f";[{last}]format=yuv420p[v]"
    audio_map = "0:a" if has_audio else "1:a"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-t", f"{max_seconds:.2f}",
           "-filter_complex", graph, "-map", "[v]", "-map", audio_map,
           "-af", "aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=11"
           + (f",afade=t=out:st={fade_at:.2f}:d={fade_out:.2f}" if fade_out > 0 else ""),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(comp.get("crf", 23)),
           "-g", "60", "-r", "30", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
           "-shortest", str(dst)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        log.warning("Segment render failed for %s: %s", src.name, p.stderr[-600:])
        return False
    return True


def render_static(dst: Path, comp: dict) -> bool:
    """Short TV-static transition with the logo in the centre, encoded exactly like the clip segments
    so everything can be joined without re-encoding."""
    t = comp.get("transition") or {}
    secs = float(t.get("seconds", 0.6))
    inputs = ["-f", "lavfi", "-i", f"color=c=0x808080:s=1920x1080:r=30:d={secs}",
              "-f", "lavfi", "-i", f"anoisesrc=d={secs}:c=white:a=1:r=48000"]
    # strong per-frame noise + grain flicker + faint scanlines = old TV static
    graph = ("[0:v]noise=alls=100:allf=t+u,hue=s=0,eq=contrast=2.3:brightness=-0.02,"
             "drawgrid=w=1920:h=4:t=1:c=black@0.25,setsar=1[st]")
    last = "st"
    logo_cfg = comp.get("logo") or {}
    logo = ROOT / logo_cfg["image"] if logo_cfg.get("image") else None
    if t.get("logo", True) and logo and logo.exists():
        inputs += ["-loop", "1", "-t", f"{secs}", "-i", str(logo)]
        w = int(t.get("logo_width", 460))
        graph += f";[2:v]scale={w}:-1,format=rgba[lg];[{last}][lg]overlay=(W-w)/2:(H-h)/2:shortest=1[sl]"
        last = "sl"
    graph += f";[{last}]format=yuv420p[v]"
    vol = float(t.get("volume", 0.12))
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", graph,
           "-map", "[v]", "-map", "1:a", "-af", f"highpass=f=300,volume={vol},aresample=48000",
           "-t", f"{secs}", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(comp.get("crf", 23)),
           "-g", "60", "-r", "30", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", str(dst)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        log.warning("Static transition render failed: %s", p.stderr[-600:])
        return False
    return True


def render_teaser_part(seg: Path, dst: Path, start: float, secs: float, text: str, comp: dict, tmp: Path) -> bool:
    """A short cut from an already-rendered segment with a 'V dnešním videu uvidíte' banner,
    encoded like everything else so it can be joined without re-encoding the whole video."""
    font = _font()
    graph = "[0:v]setsar=1"
    if font and text:
        txt = tmp / "teaser_text.txt"
        txt.write_text(text.upper(), encoding="utf-8")
        graph += (f",drawtext=fontfile={font}:textfile={txt}:fontsize=64:fontcolor=white:box=1"
                  f":boxcolor=black@0.6:boxborderw=24:x=(w-tw)/2:y=70")
    graph += ",format=yuv420p[v]"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", str(seg), "-t", f"{secs:.2f}",
           "-filter_complex", graph, "-map", "[v]", "-map", "0:a",
           "-af", f"afade=t=in:d=0.15,afade=t=out:st={max(secs - 0.25, 0):.2f}:d=0.25",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(comp.get("crf", 23)),
           "-g", "60", "-r", "30", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", str(dst)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        log.warning("Teaser part failed: %s", p.stderr[-400:])
        return False
    return True


def build_teaser(seq: list[Pick], seg_of: dict, comp: dict, tmp: Path, rng: random.Random) -> list[Path]:
    """'V dnešním videu uvidíte': quick cuts from the strongest clips (not the opening one, so it isn't
    shown twice in a row), each from the middle of the clip so the punchline isn't spoiled."""
    t = comp.get("teaser") or {}
    if not t.get("enabled", True) or len(seq) < 3:
        return []
    n, secs = int(t.get("clips", 4)), float(t.get("seconds", 2.5))
    pool = [p for p in sorted(seq[1:], key=lambda p: -p.score) if id(p) in seg_of][: n]
    rng.shuffle(pool)
    parts = []
    tag = float(comp.get("name_tag_seconds", 5))
    for k, p in enumerate(pool):
        dur = p.duration
        if dur < secs + 0.5:
            continue
        start = min(max(tag + 0.3, dur * 0.35), dur - secs - 0.2) if dur > tag + secs + 1 else max(dur * 0.3, 0)
        part = tmp / f"teaser_{k}.mp4"
        if render_teaser_part(seg_of[id(p)], part, start, secs, t.get("text", "V dnešním videu uvidíte"), comp, tmp):
            parts.append(part)
    return parts


def concat(segments: list[Path], out: Path, tmp: Path) -> None:
    lst = tmp / "segments.txt"
    lst.write_text("".join(f"file '{s.as_posix()}'\n" for s in segments), encoding="utf-8")
    p = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c", "copy", "-movflags", "+faststart", str(out)], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"Joining the clips failed: {p.stderr[-600:]}")


FRAME_PROMPT = """These are {n} numbered frames from one Twitch/Kick clip. Which ONE would make the most eye-catching
YouTube thumbnail? Prefer: a clearly visible face with a strong emotion (shock, laughing, shouting), or an
obvious moment of action. Avoid: blurry frames, loading/black screens, menus, frames where nothing happens.
Reply with JSON only: {{"best": <number>}}"""


def _extract_frames(clip: Path, tmp: Path, tag: str, n: int = 6) -> list[Path]:
    dur, _ = _probe(clip)
    frames = []
    for i in range(n):
        t = dur * (0.15 + 0.7 * i / max(n - 1, 1)) if dur else i
        f = tmp / f"cand_{tag}_{i}.jpg"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(clip),
                        "-frames:v", "1", "-q:v", "3", str(f)], capture_output=True)
        if f.exists():
            frames.append(f)
    return frames


def best_frame(clip: Path, tmp: Path, tag: str, cfg: dict):
    """Pick the best thumbnail frame of a clip. With an Anthropic key, Claude looks at a contact sheet of
    6 candidates and chooses; otherwise the frame from the middle of the clip is used."""
    from PIL import Image, ImageDraw, ImageFont
    frames = _extract_frames(clip, tmp, tag)
    if not frames:
        return None
    pick = len(frames) // 2
    use_ai = (cfg["compilation"].get("thumbnail") or {}).get("ai_frame_pick", True)
    if use_ai and os.environ.get("ANTHROPIC_API_KEY") and len(frames) > 1:
        try:
            import anthropic
            import base64
            cols, w, h = 3, 426, 240
            sheet = Image.new("RGB", (cols * w, ((len(frames) + cols - 1) // cols) * h), "black")
            d = ImageDraw.Draw(sheet)
            font = ImageFont.truetype(_font(), 34) if _font() else ImageFont.load_default()
            for i, f in enumerate(frames):
                im = Image.open(f).convert("RGB").resize((w, h))
                sheet.paste(im, ((i % cols) * w, (i // cols) * h))
                d.text(((i % cols) * w + 10, (i // cols) * h + 6), str(i + 1), font=font, fill="yellow",
                       stroke_width=4, stroke_fill="black")
            sheet_path = tmp / f"sheet_{tag}.jpg"
            sheet.save(sheet_path, "JPEG", quality=80)
            data = base64.b64encode(sheet_path.read_bytes()).decode()
            msg = anthropic.Anthropic().messages.create(
                model=cfg["captions"]["model"], max_tokens=200,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
                    {"type": "text", "text": FRAME_PROMPT.format(n=len(frames))}]}])
            text = "".join(getattr(b, "text", "") or "" for b in msg.content if getattr(b, "type", "") == "text")
            n = int(json.loads(text[text.find("{"): text.rfind("}") + 1])["best"])
            if 1 <= n <= len(frames):
                pick = n - 1
                log.info("Thumbnail frame for %s: AI picked %d/%d", tag, n, len(frames))
        except Exception as e:  # noqa: BLE001
            log.warning("AI frame pick failed (%s) — using the middle frame", str(e)[:150])
    return Image.open(frames[pick]).convert("RGB")


def _cover(im, w: int, h: int):
    """Resize + centre-crop to exactly w x h."""
    from PIL import Image
    s = max(w / im.width, h / im.height)
    im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.LANCZOS)
    x, y = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((x, y, x + w, y + h))


def _fit_font(draw, text: str, max_w: int, start: int, stroke: int):
    from PIL import ImageFont
    font_path = _font()
    size = start
    while True:
        font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
        if bbox[2] - bbox[0] <= max_w or size <= 40:
            return font, bbox
        size -= 6


def thumbnail(picks: list, out: Path, text: str, cfg: dict, tmp: Path) -> Path | None:
    """1280x720 collage of the strongest clips (one per streamer, slanted panels), big text,
    streamer names and the logo. The episode badge is added at upload time (add_episode_badge)."""
    from PIL import Image, ImageDraw, ImageEnhance
    comp = cfg["compilation"]
    tcfg = comp.get("thumbnail") or {}
    W, H = 1280, 720
    chosen, seen = [], set()
    for p in sorted(picks, key=lambda p: -p.score):
        if p.path and who(p) not in seen:
            chosen.append(p)
            seen.add(who(p))
        if len(chosen) >= int(tcfg.get("panels", 3)):
            break
    frames = [(p, best_frame(p.path, tmp, f"t{i}", cfg)) for i, p in enumerate(chosen)]
    frames = [(p, f) for p, f in frames if f is not None]
    if not frames:
        return None
    n, slant = len(frames), 70
    canvas = Image.new("RGB", (W, H), "black")
    bounds = [round(W * k / n) for k in range(n + 1)]
    for k, (p, im) in enumerate(frames):
        x0, x1 = bounds[k], bounds[k + 1]
        panel_w = x1 - x0 + 2 * slant
        tile = _cover(im, panel_w, H)
        layer = Image.new("RGB", (W, H))
        layer.paste(tile, (x0 - slant, 0))
        mask = Image.new("L", (W, H), 0)
        top0 = x0 + (slant if k else 0)
        bot0 = x0 - (slant if k else 0)
        top1 = x1 + (slant if k < n - 1 else 0)
        bot1 = x1 - (slant if k < n - 1 else 0)
        ImageDraw.Draw(mask).polygon([(top0, 0), (top1, 0), (bot1, H), (bot0, H)], fill=255)
        canvas.paste(layer, (0, 0), mask)
    d = ImageDraw.Draw(canvas)
    for k in range(1, n):  # white slanted dividers
        d.line([(bounds[k] + slant, 0), (bounds[k] - slant, H)], fill="white", width=10)
    canvas = ImageEnhance.Color(canvas).enhance(float(tcfg.get("saturation", 1.35)))
    canvas = ImageEnhance.Contrast(canvas).enhance(float(tcfg.get("contrast", 1.15)))
    # dark gradient at the bottom for the text
    grad = Image.linear_gradient("L").resize((W, H))
    grad = grad.point(lambda v: max(0, int((v - 110) * 1.45)) if v > 110 else 0)
    canvas = Image.composite(Image.new("RGB", (W, H), "black"), canvas, grad)
    d = ImageDraw.Draw(canvas)
    names_h = 0
    if tcfg.get("names", True):
        names = "  •  ".join(p.clip.creator for p, _ in frames)
        font, bbox = _fit_font(d, names, W - 120, 44, 5)
        names_h = bbox[3] - bbox[1] + 18
        d.text(((W - (bbox[2] - bbox[0])) // 2, H - names_h - 22), names, font=font, fill="white",
               stroke_width=5, stroke_fill="black")
    big = text.upper().strip()[:28]
    font, bbox = _fit_font(d, big, W - 100, int(tcfg.get("text_size", 132)), 11)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((W - tw) // 2, H - names_h - th - 60), big, font=font, fill=tuple(tcfg.get("text_color", [255, 222, 0])),
           stroke_width=11, stroke_fill="black")
    logo_cfg = comp.get("logo") or {}
    logo = ROOT / logo_cfg["image"] if logo_cfg.get("image") else None
    if logo and logo.exists():
        lg = Image.open(logo).convert("RGBA")
        size = int(tcfg.get("logo_size", 230))
        lg.thumbnail((size, size))
        canvas.paste(lg, (W - lg.width - 26, 20), lg)
    canvas.save(out, "JPEG", quality=92)
    return out


def add_episode_badge(path: Path, episode: int, cfg: dict) -> Path:
    """Draw '#N' in the top-left corner (done at upload time, when the episode number is known)."""
    from PIL import Image, ImageDraw
    tcfg = cfg["compilation"].get("thumbnail") or {}
    if not tcfg.get("episode_badge", True):
        return path
    im = Image.open(path).convert("RGB")
    d = ImageDraw.Draw(im)
    label = f"#{episode}"
    font, bbox = _fit_font(d, label, 400, 96, 0)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 22
    d.rounded_rectangle([24, 22, 24 + tw + 2 * pad, 22 + th + 2 * pad + 6], radius=22,
                        fill=tuple(tcfg.get("badge_color", [229, 37, 33])), outline="white", width=5)
    d.text((24 + pad - bbox[0], 22 + pad - bbox[1]), label, font=font, fill="white")
    im.save(path, "JPEG", quality=92)
    return path


# ================================================================ title / description
META_PROMPT = """You name a daily YouTube compilation of the best Twitch/Kick clips of Czech and Slovak streamers.

Date: {date}
Streamers in the video (most clips first): {streamers}
Some of the clip titles (written by random viewers, may be slang or jokes): {titles}

Write in {language}. Return ONLY a JSON object with:
- "title": a catchy, specific video title, max 70 characters, based on the best clip titles above.
  Mention 1-3 of the biggest streamer names. No date, no episode number, no hashtags (they are added automatically).
- "intro": 1-2 natural sentences for the start of the description. Don't invent specific events.
- "thumbnail_text": 2-4 punchy words for the thumbnail (no streamer names, no emojis).
- "hashtags": 5-8 hashtags without the # sign (e.g. czstreamer, twitchcz, kickcz, cesko, gaming, streamer names).

Safety: never use slurs, hate toward any group, sexual content or insults about real people, even if the clip
titles contain them.
"""


def metadata(picks: list[Pick], cfg: dict, day_label: str) -> dict:
    counts: dict[str, int] = {}
    for p in picks:
        counts[p.clip.creator] = counts.get(p.clip.creator, 0) + 1
    streamers = sorted(counts, key=lambda s: -counts[s])
    d = datetime.fromisoformat(day_label)
    top_names = " & ".join(streamers[:2]) if streamers else "CZ/SK streameři"
    fallback = {"title": f"{top_names} a další nejlepší momenty dne",
                "intro": "Nejlepší a nejsledovanější momenty z českých a slovenských streamů za poslední den.",
                "thumbnail_text": "TOP KLIPY DNE",
                "hashtags": ["czstreamer", "twitchcz", "kickcz", "cesko", "gaming", "klipy"]}
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            titles = [p.clip.title for p in sorted(picks, key=lambda p: -p.score)
                      if p.clip.title and not captions.is_unsafe(p.clip.title)][:15]
            msg = anthropic.Anthropic().messages.create(
                model=cfg["captions"]["model"], max_tokens=2000,
                system="Reply with one JSON object and nothing else - no prose, no code fences.",
                messages=[{"role": "user", "content": META_PROMPT.format(
                    date=f"{d.day}. {d.month}. {d.year}", streamers=", ".join(streamers),
                    titles="; ".join(titles), language=cfg["captions"]["language"]) + insights.TEXT}])
            text = "".join(getattr(b, "text", "") or "" for b in msg.content if getattr(b, "type", "") == "text")
            data = json.loads(text[text.find("{"): text.rfind("}") + 1])
            if data.get("title") and not captions.is_unsafe(data["title"] + " " + data.get("intro", "")
                                                            + " " + data.get("thumbnail_text", "")):
                fallback.update({k: v for k, v in data.items() if v})
            else:
                log.warning("AI compilation title was empty or unsafe — using the default title")
        except Exception as e:  # noqa: BLE001
            log.warning("AI compilation title failed (%s: %s) — using the default title", type(e).__name__, e)
    tags = [re.sub(r"[^A-Za-z0-9]", "", captions._ascii(str(t).lstrip("#"))) for t in fallback["hashtags"]]
    tags = [t for t in tags if t]
    links = []
    for s in streamers:
        p = next(p for p in picks if p.clip.creator == s)
        base = "https://twitch.tv/" if p.clip.platform == "twitch" else "https://kick.com/"
        links.append(f"• {s} – {base}{p.clip.channel}")
    description = "\n".join([
        fallback["intro"].strip(), "",
        "🎥 Streameři v dnešním videu:", *links, "",
        "Všechna práva k obsahu patří streamerům. Sledujte je naživo! ❤️", "",
        " ".join(f"#{t}" for t in tags),
    ])[:4900]
    yt_tags, total = [], 0
    for t in streamers + tags + ["CZ klipy", "SK klipy", "Twitch CZ", "Kick CZ"]:
        if t.lower() not in {x.lower() for x in yt_tags} and total + len(t) + 1 <= 480:
            yt_tags.append(t)
            total += len(t) + 1
    title = re.sub(r"[<>]", "", fallback["title"]).strip().rstrip("|-– ").strip()
    return {"title": title, "description": description, "tags": yt_tags,
            "thumbnail_text": fallback["thumbnail_text"]}


def episode_title(base: str, episode: int | None) -> str:
    """'<AI title> | Klipy dne #N', shortened so it fits YouTube's 100-character limit."""
    suffix = f" | Klipy dne #{episode}" if episode else " | Klipy dne"
    room = 100 - len(suffix)
    base = base.strip()
    if len(base) > room:
        base = base[: room - 1].rsplit(" ", 1)[0].rstrip(",.;:-–|") + "…"
    return base + suffix


# ================================================================ main step
def build(cfg: dict, drive, now: datetime, local: datetime, tmp: Path) -> dict | None:
    """Make today's compilation. Returns a queue item (without scheduled_at) or None."""
    comp = cfg["compilation"]
    data = drive.read_json(CHANNELS, empty_channels())
    lookback = timedelta(hours=float(cfg["lookback_hours"]))
    day_label = (local - lookback).date().isoformat()
    since, until = now - lookback, now
    pool = channel_pool(cfg, data, local.date())
    log.info("Compilation: scanning %d channels for clips from the last %s.", len(pool), lookback)

    tw = kick = None
    tw_pool = [c for c in pool if c["platform"] == "twitch"]
    if tw_pool:
        tw = sources.Twitch()
        missing = [c["channel"] for c in tw_pool if not c.get("id")]
        if missing:
            ids = tw.user_ids(missing)
            for c in tw_pool:
                c["id"] = c.get("id") or ids.get(c["channel"])

    LAST.clear()
    LAST.update(channels=[], order=[], candidates=0, note="")
    min_views = int(comp.get("min_views", 30))
    length_rules = {**sources.DEFAULT_LENGTH, **(cfg.get("clip_length") or {})}
    per_channel = int(comp.get("clips_per_channel", 10))
    picks: list[Pick] = []
    seen_clips: set[str] = set()
    for c in pool:
        try:
            if c["platform"] == "twitch":
                if not c.get("id"):
                    log.info("  twitch   %-20s not found on Twitch (check the channel name)", c["channel"][:20])
                    continue
                clips = tw.top_clips(c.get("name", c["channel"]), c["channel"], since, until,
                                     broadcaster_id=c["id"], limit=100)
            elif c["platform"] == "kick":
                kick = kick or sources.Kick()
                clips = kick.top_clips(c.get("name", c["channel"]), c["channel"], since, until)
            else:
                continue
        except Exception as e:  # noqa: BLE001
            log.warning("Clips for %s failed: %s", c["channel"], e)
            continue
        clips.sort(key=lambda x: -x.views)
        found = len(clips)
        clips = clips[:per_channel]
        views = [x.views for x in clips]
        base = baseline(c, views, day_label)
        sf = size_factor(c, comp)
        kept = sum(1 for x in clips if x.views >= min_views)
        log.info("  %-8s %-20s %3d clips in window, top views %5s, usual %6.0f, size x%.2f, %d with >= %d views",
                 c["platform"], c["channel"][:20], found, views[0] if views else "-", base, sf, kept, min_views)
        if found:
            LAST["channels"].append([c.get("name", c["channel"]), c["platform"], found, views[0] if views else 0,
                                     round(base, 1), round((views[0] if views else 0) / max(base, 1), 1), kept,
                                     int(c.get("peak_viewers") or 0), round(sf, 2)])
        if views:  # remember today's median for this channel's future baseline
            ch = data["channels"].setdefault(c["key"], {k: v for k, v in c.items() if k != "key"})
            hist = [h for h in ch.get("history", []) if h[0] != day_label]
            hist.append([day_label, statistics.median(views)])
            ch["history"] = hist[-14:]
        for x in clips:
            if x.views < min_views or captions.is_unsafe(x.title or "") or x.clip_id in seen_clips:
                continue
            factor = sources.length_factor(x.duration, length_rules)
            if factor is None:  # over the hard length limit
                continue
            seen_clips.add(x.clip_id)
            picks.append(Pick(clip=x, score=sf * factor * x.views / max(base, float(comp.get("min_baseline", 10))),
                              duration=float(x.duration or length_rules.get("ideal_seconds", 30))))
    drive.write_json(CHANNELS, data)
    # the same moment clipped by several viewers -> keep only the best-scoring one
    by_clip = {id(p.clip): p for p in picks}
    kept, dupes = sources.drop_duplicates([p.clip for p in picks], key=lambda c: by_clip[id(c)].score)
    picks = [by_clip[id(c)] for c in kept]
    if dupes:
        log.info("Compilation: %d duplicate clip(s) of the same moment dropped.", dupes)
    log.info("Compilation: %d candidate clips.", len(picks))
    LAST["candidates"] = len(picks)
    LAST["scanned"] = len(pool)

    chosen = select(picks, comp)
    hard_min = float(comp.get("absolute_min_minutes", float(comp["min_minutes"]) * 0.6)) * 60
    if not chosen or sum(p.duration for p in chosen) < hard_min:
        log.info("Not enough good clips today (%d) — no compilation.", len(chosen))
        LAST["note"] = f"Not enough good clips today ({len(chosen)}, {sum(p.duration for p in chosen) / 60:.1f} min) - no video."
        return None

    # download; drop failures, then order
    ready: list[Pick] = []
    for p in chosen:
        raw = tmp / f"raw_{re.sub(r'[^A-Za-z0-9_-]', '', p.clip.clip_id)[:60]}.mp4"
        if media.download(p.clip, raw):
            dur, _ = _probe(raw)
            if dur and sources.length_factor(dur, length_rules) is None:
                log.info("Skipping %s: %.0f s is over the length limit", p.clip.url, dur)
                raw.unlink(missing_ok=True)
                continue
            p.duration = dur or p.duration
            p.path = raw
            ready.append(p)
        else:
            log.warning("Download failed: %s", p.clip.url)
    ready = limit_dominance(ready)  # re-check the diversity rule after failed downloads
    seq = order(ready, random.Random(day_label))
    total = sum(p.duration for p in seq)
    if total < hard_min:
        log.info("Only %.1f min of clips downloaded — no compilation today.", total / 60)
        return None
    log.info("Compilation order (%d clips, %.1f min):", len(seq), total / 60)
    tcfg = comp.get("teaser") or {}
    t = (float(tcfg.get("clips", 4)) * float(tcfg.get("seconds", 2.5))
         + float((comp.get("transition") or {}).get("seconds", 0.6))) if tcfg.get("enabled", True) and len(seq) >= 3 else 0.0
    for i, p in enumerate(seq, 1):
        LAST["order"].append([i, f"{int(t // 60)}:{int(t % 60):02d}", p.clip.creator, round(p.score, 1), p.clip.views,
                              f"{p.duration:.0f} s", p.clip.title[:70], p.clip.url])
        t += p.duration + float((comp.get("transition") or {}).get("seconds", 0.6))
    for i, p in enumerate(seq, 1):
        log.info("  %2d. %-14s score %.1f  %4d views  %4.0fs  %s", i, p.clip.creator, p.score, p.clip.views,
                 p.duration, p.clip.title[:60])

    meta = metadata(seq, cfg, day_label)
    # thumbnail from the original opening clip (no name tag / logo baked in yet)
    thumb = thumbnail(seq, tmp / f"thumbnail-{day_label}.jpg", meta["thumbnail_text"], cfg, tmp)

    segments = []
    seg_of: dict = {}
    fade = float(comp.get("fade_out_seconds", 1.5))
    for i, p in enumerate(seq):
        seg = tmp / f"seg_{i:03d}.mp4"
        last_clip = i == len(seq) - 1
        if render_segment(p.path, seg, p.clip.creator, comp, tmp, p.duration, fade if last_clip else 0.0):
            segments.append(seg)
            seg_of[id(p)] = seg
        p.path.unlink(missing_ok=True)
    if not segments:
        return None
    teaser = build_teaser(seq, seg_of, comp, tmp, random.Random(day_label + "teaser"))
    if teaser:
        log.info("Teaser: %d quick cuts at the start.", len(teaser))
    # short TV static with the logo between every two clips
    if (comp.get("transition") or {}).get("enabled", True) and len(segments) > 1:
        static = tmp / "static.mp4"
        if render_static(static, comp):
            joined = []
            for i, seg in enumerate(segments):
                if i:
                    joined.append(static)
                joined.append(seg)
            segments_to_join = (teaser + [static] if teaser else []) + joined
        else:
            segments_to_join = teaser + segments
    else:
        segments_to_join = teaser + segments
    out = tmp / f"compilation-{day_label}.mp4"
    concat(segments_to_join, out, tmp)

    for s in segments:
        s.unlink(missing_ok=True)

    folder = drive.folder_path("Compilations", day_label)
    video_id = drive.upload(out, out.name, folder)
    thumb_id = drive.upload(thumb, thumb.name, folder, mimetype="image/jpeg") if thumb else None
    log.info("Compilation saved to Drive: %s (%.0f MB) -> \"%s\"", out.name, out.stat().st_size / 1e6, meta["title"])
    return {
        "kind": "long", "key": f"compilation:{day_label}", "creator": "Compilation", "platform": "mix",
        "date": day_label, "file_name": out.name, "drive_file_id": video_id, "thumb_drive_id": thumb_id,
        "source_url": "", "source_title": "", "views": sum(p.clip.views for p in seq),
        "base_title": meta["title"], "yt_title": episode_title(meta["title"], None),
        "yt_description": meta["description"], "yt_tags": meta["tags"],
        "ai_caption": True, "status": "pending", "attempts": 0, "youtube_id": None,
        "clips": [{"creator": p.clip.creator, "url": p.clip.url, "views": p.clip.views,
                   "score": round(p.score, 2)} for p in seq],
    }
