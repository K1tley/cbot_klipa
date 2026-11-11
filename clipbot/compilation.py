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

from . import captions, media, sources

log = logging.getLogger(__name__)
CHANNELS = "channels.json"
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
        if (today - seen).days <= active_days:
            pool[key] = dict(ch, key=key)
    ranked = sorted(pool.values(), key=lambda c: -int(c.get("peak_viewers", 0)))
    ranked = ranked[: int(disc.get("max_channels", 150))]
    pool = {c["key"]: c for c in ranked}
    # creators from the Shorts list + extra channels (Kick has no language search) are always included
    for c in list(cfg.get("creators", [])) + list(comp.get("extra_channels") or []):
        key = f"{c['platform'].lower()}:{c['channel'].lower()}"
        known = data["channels"].get(key, {})
        pool.setdefault(key, dict(known, key=key, platform=c["platform"].lower(),
                                  channel=c["channel"].lower(), name=c.get("name", c["channel"])))
    return list(pool.values())  # same streamer on Twitch and Kick: both scanned, clips deduped by id


# ================================================================ scoring & selection
@dataclass(eq=False)
class Pick:
    clip: sources.Clip
    score: float
    duration: float
    path: Path | None = None
    extra: dict = field(default_factory=dict)


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


def render_segment(src: Path, dst: Path, name: str, comp: dict, tmp: Path, max_seconds: float) -> bool:
    """Normalise one clip to 1920x1080 / 30 fps / 48 kHz stereo with a name tag and logo."""
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
    if logo and logo.exists():
        idx = len([a for a in inputs if a == "-i"])
        inputs += ["-i", str(logo)]
        w = int(logo_cfg.get("width", 160))
        op = float(logo_cfg.get("opacity", 0.9))
        graph += (f";[{idx}:v]scale={w}:-1,format=rgba,colorchannelmixer=aa={op}[lg]"
                  f";[{last}][lg]overlay=W-w-36:30[v2]")
        last = "v2"
    graph += f";[{last}]format=yuv420p[v]"
    audio_map = "0:a" if has_audio else "1:a"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-t", f"{max_seconds:.2f}",
           "-filter_complex", graph, "-map", "[v]", "-map", audio_map,
           "-af", "aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=11",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(comp.get("crf", 23)),
           "-g", "60", "-r", "30", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
           "-shortest", str(dst)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        log.warning("Segment render failed for %s: %s", src.name, p.stderr[-600:])
        return False
    return True


def concat(segments: list[Path], out: Path, tmp: Path) -> None:
    lst = tmp / "segments.txt"
    lst.write_text("".join(f"file '{s.as_posix()}'\n" for s in segments), encoding="utf-8")
    p = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c", "copy", "-movflags", "+faststart", str(out)], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"Joining the clips failed: {p.stderr[-600:]}")


def thumbnail(segment: Path, out: Path, text: str, comp: dict, tmp: Path) -> Path | None:
    """Frame from the middle of the opening highlight + big text + logo, 1280x720 JPEG."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageEnhance

    dur, _ = _probe(segment)
    frame = tmp / "thumb_frame.png"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{max(dur * 0.5, 0):.2f}", "-i", str(segment),
                    "-frames:v", "1", str(frame)], capture_output=True)
    if not frame.exists():
        return None
    im = Image.open(frame).convert("RGB").resize((1280, 720))
    im = ImageEnhance.Color(im).enhance(1.25)
    im = ImageEnhance.Contrast(im).enhance(1.1)
    # dark gradient at the bottom so the text reads well
    grad = Image.new("L", (1, 720))
    for y in range(720):
        grad.putpixel((0, y), int(max(0, (y - 330) / 390) * 200))
    im = Image.composite(Image.new("RGB", im.size, (0, 0, 0)), im, grad.resize(im.size))
    draw = ImageDraw.Draw(im)
    font_path = _font()
    text = text.upper().strip()[:28]
    size = 120
    while size > 50:
        font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=8)
        if bbox[2] - bbox[0] <= 1180:
            break
        size -= 6
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((1280 - tw) // 2, 720 - th - 70), text, font=font, fill=(255, 230, 0),
              stroke_width=8, stroke_fill=(0, 0, 0))
    logo_cfg = comp.get("logo") or {}
    logo = ROOT / logo_cfg["image"] if logo_cfg.get("image") else None
    if logo and logo.exists():
        lg = Image.open(logo).convert("RGBA")
        lg.thumbnail((200, 200))
        im.paste(lg, (1280 - lg.width - 30, 24), lg)
    im.save(out, "JPEG", quality=90)
    return out


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
                    titles="; ".join(titles), language=cfg["captions"]["language"])}])
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

    min_views = int(comp.get("min_views", 30))
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
        kept = sum(1 for x in clips if x.views >= min_views)
        log.info("  %-8s %-20s %3d clips in window, top views %5s, usual %6.0f, %d with >= %d views",
                 c["platform"], c["channel"][:20], found, views[0] if views else "-", base, kept, min_views)
        if views:  # remember today's median for this channel's future baseline
            ch = data["channels"].setdefault(c["key"], {k: v for k, v in c.items() if k != "key"})
            hist = [h for h in ch.get("history", []) if h[0] != day_label]
            hist.append([day_label, statistics.median(views)])
            ch["history"] = hist[-14:]
        for x in clips:
            if x.views < min_views or captions.is_unsafe(x.title or "") or x.clip_id in seen_clips:
                continue
            seen_clips.add(x.clip_id)
            picks.append(Pick(clip=x, score=x.views / max(base, float(comp.get("min_baseline", 10))),
                              duration=min(float(x.duration or 30), float(comp.get("max_clip_seconds", 60)))))
    drive.write_json(CHANNELS, data)
    log.info("Compilation: %d candidate clips.", len(picks))

    chosen = select(picks, comp)
    if not chosen or sum(p.duration for p in chosen) < float(comp["min_minutes"]) * 60 * 0.6:
        log.info("Not enough good clips today (%d) — no compilation.", len(chosen))
        return None

    # download; drop failures, then order
    ready: list[Pick] = []
    for p in chosen:
        raw = tmp / f"raw_{re.sub(r'[^A-Za-z0-9_-]', '', p.clip.clip_id)[:60]}.mp4"
        if media.download(p.clip, raw):
            dur, _ = _probe(raw)
            p.duration = min(dur or p.duration, float(comp.get("max_clip_seconds", 60)))
            p.path = raw
            ready.append(p)
        else:
            log.warning("Download failed: %s", p.clip.url)
    ready = limit_dominance(ready)  # re-check the diversity rule after failed downloads
    seq = order(ready, random.Random(day_label))
    total = sum(p.duration for p in seq)
    if total < float(comp["min_minutes"]) * 60 * 0.6:
        log.info("Only %.1f min of clips downloaded — no compilation today.", total / 60)
        return None
    log.info("Compilation order (%d clips, %.1f min):", len(seq), total / 60)
    for i, p in enumerate(seq, 1):
        log.info("  %2d. %-14s score %.1f  %4d views  %4.0fs  %s", i, p.clip.creator, p.score, p.clip.views,
                 p.duration, p.clip.title[:60])

    meta = metadata(seq, cfg, day_label)
    # thumbnail from the original opening clip (no name tag / logo baked in yet)
    thumb = thumbnail(seq[0].path, tmp / f"thumbnail-{day_label}.jpg", meta["thumbnail_text"], comp, tmp)

    segments = []
    for i, p in enumerate(seq):
        seg = tmp / f"seg_{i:03d}.mp4"
        if render_segment(p.path, seg, p.clip.creator, comp, tmp, p.duration):
            segments.append(seg)
        p.path.unlink(missing_ok=True)
    if not segments:
        return None
    out = tmp / f"compilation-{day_label}.mp4"
    concat(segments, out, tmp)

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
