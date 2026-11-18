"""English GTA channel (config_gta.yaml).

Two kinds of compilations, both uploaded right away:
  * groups:  one video per streamer group (CORE, Kai Cenat & AMP, ...) from their GTA V / GTA VI clips,
             every `gta.groups.every_hours` hours. Target ~12 min, max 25, shorter only if there's not enough.
  * best:    "Best GTA 6 clips of the day" - the most viewed English GTA VI clips from Twitch and Kick
             of the last 24 h, 15-20 min, once a day.

Every clip that went into a video is remembered in gta_used.json on Drive (also the moment it shows,
so a re-clip of the same moment is skipped too). Group videos never repeat a clip; the best-of only
never repeats its own clips (unless best.reuse_group_clips is false).
"""
from __future__ import annotations

import json
import logging
import os
import re
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import captions, sources
from .compilation import LAST, Pick, produce, who

log = logging.getLogger(__name__)
USED = "gta_used.json"
KICK_CHANNELS = "gta_kick_channels.json"
STATE = "gta_state.json"
RUN: dict = {}  # what happened in this run, for the report


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ---------------------------------------------------------------- history of used clips
class Used:
    def __init__(self, drive, keep_days: int):
        self.drive = drive
        self.data = drive.read_json(USED, {"clips": {}})
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
        self.data["clips"] = {k: v for k, v in self.data["clips"].items() if v.get("date", "9999") >= cutoff}
        self._as_clips = [self._clip(v) for v in self.data["clips"].values()]

    @staticmethod
    def _clip(v: dict) -> sources.Clip:
        return sources.Clip(platform=v.get("platform", ""), clip_id=v.get("id", ""), creator=v.get("creator", ""),
                            channel=v.get("channel", ""), title="", url="", views=0,
                            created_at=v.get("created_at", ""), duration=float(v.get("duration") or 30),
                            stream_id=v.get("stream_id") or "", offset=v.get("offset"))

    def blocked(self, c: sources.Clip, series: str | None = None) -> bool:
        """True if this clip (or the same moment) is already in a video. With `series`, only that
        series' videos count (the best-of may reuse clips that were in a group video)."""
        rec = self.data["clips"].get(f"{c.platform}:{c.clip_id}")
        if rec and (series is None or series in rec.get("series", [])):
            return True
        for v, u in zip(self.data["clips"].values(), self._as_clips):
            if series is not None and series not in v.get("series", []):
                continue
            if same_moment(c, u):
                return True
        return False

    def add(self, item: dict, series: str) -> None:
        day = date.today().isoformat()
        for c in item.get("clips", []):
            key = f"{c.get('platform')}:{c.get('id')}"
            rec = self.data["clips"].setdefault(key, {**{k: c.get(k) for k in (
                "platform", "id", "creator", "channel", "created_at", "stream_id", "offset", "duration")},
                "date": day, "series": []})
            if series not in rec["series"]:
                rec["series"].append(series)
        self.drive.write_json(USED, self.data)
        self._as_clips = [self._clip(v) for v in self.data["clips"].values()]


def same_moment(a: sources.Clip, b: sources.Clip, cross_seconds: float = 0) -> bool:
    """sources.same_moment + the same streamer on Twitch and Kick at once (simulcast), and - with
    cross_seconds - two streamers of one group clipped within that many seconds (same scene, two POVs)."""
    if sources.same_moment(a, b):
        return True
    ta, tb = sources._parse_time(a.created_at), sources._parse_time(b.created_at)
    if not (ta and tb):
        return False
    gap = abs((ta - tb).total_seconds())
    if a.creator.strip().lower() == b.creator.strip().lower() and a.platform != b.platform:
        return gap <= 90
    return bool(cross_seconds) and gap <= cross_seconds


def dedupe(picks: list[Pick], cross_seconds: float) -> list[Pick]:
    kept: list[Pick] = []
    for p in sorted(picks, key=lambda p: -p.score):
        if not any(same_moment(p.clip, k.clip, cross_seconds) for k in kept):
            kept.append(p)
    return kept


# ---------------------------------------------------------------- clips
def _is_gta(c: sources.Clip, games: list[str]) -> bool:
    g = (c.game or "").lower()
    return any(x.lower() in g for x in games) or ("grand theft auto" in g and not games)


def _to_pick(c: sources.Clip, rules: dict, min_views: int, norm: float = 1.0) -> Pick | None:
    if c.views < min_views or captions.is_unsafe(c.title or ""):
        return None
    factor = sources.length_factor(c.duration, rules)
    if factor is None:
        return None
    return Pick(clip=c, score=factor * c.views / max(norm, 1.0),
                duration=float(c.duration or rules.get("ideal_seconds", 30)))


def group_clips(cfg: dict, group: dict, since: datetime, until: datetime, tw, kick) -> list[sources.Clip]:
    g = cfg["gta"]
    games = g.get("games") or ["Grand Theft Auto"]
    out: list[sources.Clip] = []
    stats = []
    members = group["members"]
    logins = [m["twitch"] for m in members if m.get("twitch")]
    ids = tw.user_ids(logins) if (tw and logins) else {}
    for m in members:
        name = m.get("name") or m.get("twitch") or m.get("kick")
        got, gta_n = 0, 0
        if m.get("twitch") and tw:
            bid = ids.get(m["twitch"].lower())
            if bid:
                try:
                    cl = tw.top_clips(name, m["twitch"].lower(), since, until, broadcaster_id=bid, limit=100)
                    got += len(cl)
                    cl = [c for c in cl if _is_gta(c, games)]
                    gta_n += len(cl)
                    out += cl
                except Exception as e:  # noqa: BLE001
                    log.warning("Twitch clips for %s failed: %s", name, e)
            else:
                log.info("  %s: not found on Twitch (%s)", name, m["twitch"])
        if m.get("kick"):
            try:
                kick = kick or sources.Kick()
                cl = kick.top_clips(name, m["kick"].lower(), since, until)
                got += len(cl)
                cl = [c for c in cl if _is_gta(c, games)]
                gta_n += len(cl)
                out += cl
            except Exception as e:  # noqa: BLE001
                log.warning("Kick clips for %s failed: %s", name, e)
        stats.append([name, got, gta_n, max((c.views for c in out if c.creator == name), default=0)])
        log.info("  %-18s %3d clips in window, %3d GTA", name, got, gta_n)
    RUN.setdefault("members", {})[group["name"]] = stats
    return out


def english_kick_channels(drive, kick, g: dict) -> set[str]:
    """Kick doesn't put a language on clips, so we remember which channels stream GTA in English
    (seen live in the GTA categories, language 'English', enough viewers)."""
    data = drive.read_json(KICK_CHANNELS, {"channels": {}})
    chans = data["channels"]
    b = g.get("best") or {}
    min_viewers = int(b.get("kick_min_viewers", 300))
    langs = [x.lower() for x in b.get("kick_languages", ["English"])]
    now = _iso(datetime.now(timezone.utc))
    for cat in b.get("kick_categories", ["grand-theft-auto-vi", "grand-theft-auto-v"]):
        for it in sources.kick_live(kick, cat):
            if it["language"].lower() not in langs or it["viewers"] < min_viewers or not it["slug"]:
                continue
            ch = chans.setdefault(it["slug"], {"name": it["name"], "seen": 0})
            ch.update(name=it["name"], last_seen=now, language=it["language"],
                      peak=max(int(ch.get("peak", 0)), it["viewers"]))
            ch["seen"] = int(ch.get("seen", 0)) + 1
    cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=int(b.get("kick_keep_days", 30))))
    data["channels"] = {k: v for k, v in chans.items() if v.get("last_seen", "") >= cutoff}
    drive.write_json(KICK_CHANNELS, data)
    known = {m["kick"].lower() for gr in g["groups"]["list"] for m in gr["members"] if m.get("kick")}
    known |= {x.lower() for x in b.get("extra_kick_channels", [])}
    return set(data["channels"]) | known


def best_clips(cfg: dict, drive, since: datetime, until: datetime, tw, kick) -> list[sources.Clip]:
    g = cfg["gta"]
    b = g["best"]
    out: list[sources.Clip] = []
    langs = [x.lower() for x in b.get("twitch_languages", ["en"])]
    if tw:
        ids = tw.game_ids([b.get("twitch_game", "Grand Theft Auto VI")])
        for gid in ids.values():
            cl = tw.game_clips(gid, since, until, limit=int(b.get("twitch_clips", 400)))
            en = [c for c in cl if c.language.lower() in langs]
            log.info("Twitch %s: %d clips in window, %d English", b.get("twitch_game"), len(cl), len(en))
            out += en
            RUN["best_twitch"] = [len(cl), len(en)]
    try:
        kick = kick or sources.Kick()
        english = english_kick_channels(drive, kick, g)
        cl = sources.kick_category_clips(kick, b.get("kick_category", "grand-theft-auto-vi"), since, until,
                                         pages=int(b.get("kick_pages", 8)))
        en = [c for c in cl if c.channel.lower() in english]
        log.info("Kick %s: %d clips in window, %d from English channels (%d known)", b.get("kick_category"),
                 len(cl), len(en), len(english))
        out += en
        RUN["best_kick"] = [len(cl), len(en), len(english)]
    except Exception as e:  # noqa: BLE001
        log.warning("Kick best-of clips failed: %s", e)
    return out


# ---------------------------------------------------------------- selection
def choose(picks: list[Pick], job: dict, one_per_member: bool) -> list[Pick]:
    """Best clips first until target_minutes (never over max_minutes). Groups: every member with a
    clip gets their best one first."""
    target = float(job.get("target_minutes", job["min_minutes"])) * 60
    max_s = float(job["max_minutes"]) * 60
    cap = int(job.get("max_clips_per_streamer", 6))
    ranked = sorted(picks, key=lambda p: -p.score)
    chosen: list[Pick] = []
    per: dict[str, int] = {}
    total = 0.0

    def add(p: Pick) -> None:
        nonlocal total
        chosen.append(p)
        per[who(p)] = per.get(who(p), 0) + 1
        total += p.duration

    if one_per_member:
        firsts: dict[str, Pick] = {}
        for p in ranked:
            firsts.setdefault(who(p), p)
        for p in firsts.values():
            if total + p.duration <= max_s:
                add(p)
    for p in ranked:
        if total >= target:
            break
        if p in chosen or per.get(who(p), 0) >= cap or total + p.duration > max_s:
            continue
        add(p)
    return chosen


# ---------------------------------------------------------------- title / description
META_PROMPT = """You name a YouTube compilation of the best GTA clips from Twitch and Kick streamers.

Video: {kind}
Streamers in the video (most clips first): {streamers}
Some of the clip titles (written by random viewers, may be slang or jokes): {titles}

Write in English. Return ONLY a JSON object with:
- "title": a catchy, specific YouTube title, max 65 characters, based on the funniest/wildest clip titles above.
  Mention 1-2 of the biggest streamer names. No date, no episode number, no hashtags, no "compilation".
- "intro": 1-2 natural sentences for the start of the description. Don't invent specific events.
- "thumbnail_text": 2-4 punchy words for the thumbnail (no streamer names, no emojis).
- "hashtags": 5-8 hashtags without the # sign (e.g. gta6, gtavi, gta, twitchclips, kickclips, streamer names).

Safety: never use slurs, hate toward any group, sexual content or insults about real people, even if the clip
titles contain them.
"""


def metadata(seq: list[Pick], cfg: dict, kind: str, fallback_title: str) -> dict:
    counts: dict[str, int] = {}
    for p in seq:
        counts[p.clip.creator] = counts.get(p.clip.creator, 0) + 1
    streamers = sorted(counts, key=lambda s: -counts[s])
    out = {"title": fallback_title, "intro": f"The best GTA moments from {', '.join(streamers[:3])} and more.",
           "thumbnail_text": "INSANE MOMENTS", "hashtags": ["gta6", "gtavi", "gta", "twitchclips", "kickclips"]}
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            titles = [p.clip.title for p in sorted(seq, key=lambda p: -p.score)
                      if p.clip.title and not captions.is_unsafe(p.clip.title)][:15]
            msg = anthropic.Anthropic().messages.create(
                model=cfg["captions"]["model"], max_tokens=1500,
                system="Reply with one JSON object and nothing else - no prose, no code fences.",
                messages=[{"role": "user", "content": META_PROMPT.format(
                    kind=kind, streamers=", ".join(streamers), titles="; ".join(titles))}])
            text = "".join(getattr(b, "text", "") or "" for b in msg.content if getattr(b, "type", "") == "text")
            data = json.loads(text[text.find("{"): text.rfind("}") + 1])
            if data.get("title") and not captions.is_unsafe(" ".join(str(data.get(k, "")) for k in
                                                                     ("title", "intro", "thumbnail_text"))):
                out.update({k: v for k, v in data.items() if v})
        except Exception as e:  # noqa: BLE001
            log.warning("AI title failed (%s) - using the default title", e)
    tags = [t for t in (re.sub(r"[^A-Za-z0-9]", "", captions._ascii(str(x).lstrip("#"))) for x in out["hashtags"]) if t]
    links = []
    for s in streamers:
        p = next(p for p in seq if p.clip.creator == s)
        base = "https://twitch.tv/" if p.clip.platform == "twitch" else "https://kick.com/"
        links.append(f"• {s} – {base}{p.clip.channel}")
    description = "\n".join([out["intro"].strip(), "", "🎥 Streamers in this video:", *links, "",
                             "All rights to the clips belong to the streamers - go watch them live! ❤️", "",
                             " ".join(f"#{t}" for t in tags)])[:4900]
    yt_tags, total = [], 0
    for t in streamers + tags + ["GTA 6", "GTA VI", "GTA 6 clips", "GTA funny moments", "Twitch clips"]:
        if t.lower() not in {x.lower() for x in yt_tags} and total + len(t) + 1 <= 480:
            yt_tags.append(t)
            total += len(t) + 1
    title = re.sub(r"[<>]", "", str(out["title"])).strip().rstrip("|-– ").strip()
    return {"title": title, "description": description, "tags": yt_tags,
            "thumbnail_text": str(out["thumbnail_text"])}


# ---------------------------------------------------------------- jobs
def _render(cfg: dict, drive, chosen: list[Pick], job: dict, label: str, series: str, suffix: str,
            kind: str, fallback_title: str, tmp: Path) -> dict | None:
    comp = {**cfg["compilation"], **{k: job[k] for k in ("min_minutes", "max_minutes", "absolute_min_minutes")
                                     if k in job}, "strict_alternation": False}
    day = datetime.now(ZoneInfo(cfg["timezone"])).date().isoformat()
    item = produce(cfg, drive, chosen, tmp, day, label=label,
                   meta_fn=lambda seq: metadata(seq, cfg, kind, fallback_title),
                   folder=("Compilations", day, series), key=f"gta:{label}", comp=comp)
    if item:
        from .compilation import episode_title
        item.update(series=series, title_suffix=suffix, creator=series,
                    yt_title=episode_title(item["base_title"], None, suffix))
    return item


def run_group(cfg: dict, drive, group: dict, used: Used, tw, kick, tmp: Path) -> dict | None:
    g = cfg["gta"]
    job = g["groups"]
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=float(job.get("lookback_hours", 24)))
    log.info("GTA group %s: looking for clips since %s", group["name"], _iso(since))
    clips = group_clips(cfg, group, since, now, tw, kick)
    rules = {**sources.DEFAULT_LENGTH, **(cfg.get("clip_length") or {})}
    by_creator: dict[str, list[int]] = {}
    for c in clips:
        by_creator.setdefault(c.creator.lower(), []).append(c.views)
    picks = []
    for c in clips:
        if used.blocked(c):
            continue
        norm = statistics.median(by_creator[c.creator.lower()]) ** 0.5 if job.get("normalise", True) else 1.0
        p = _to_pick(c, rules, int(job.get("min_views", 20)), norm)
        if p:
            picks.append(p)
    picks = dedupe(picks, float(job.get("same_scene_seconds", 45)))
    chosen = choose(picks, job, one_per_member=True)
    mins = sum(p.duration for p in chosen) / 60
    RUN.setdefault("jobs", []).append([group["name"], len(clips), len(picks), len(chosen), f"{mins:.1f}"])
    log.info("GTA group %s: %d GTA clips, %d new candidates, %d chosen (%.1f min)", group["name"], len(clips),
             len(picks), len(chosen), mins)
    if mins < float(job.get("absolute_min_minutes", 3)):
        log.info("Not enough new clips for %s - no video this time.", group["name"])
        return None
    series = slug(group["name"])
    LAST.clear()
    LAST.update(channels=[], order=[], candidates=len(picks), note="")
    item = _render(cfg, drive, chosen, job, f"{series}-{now.strftime('%Y%m%d-%H%M')}", series,
                   group.get("suffix") or f"{group['name']} GTA Highlights",
                   f"highlights of the streamer group {group['name']} playing GTA",
                   "Insane GTA Moments", tmp)
    if item:
        used.add(item, series)
        item["order"] = list(LAST.get("order", []))
    return item


def run_best(cfg: dict, drive, used: Used, tw, kick, tmp: Path) -> dict | None:
    g = cfg["gta"]
    job = g["best"]
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=float(job.get("lookback_hours", 24)))
    clips = best_clips(cfg, drive, since, now, tw, kick)
    rules = {**sources.DEFAULT_LENGTH, **(cfg.get("clip_length") or {})}
    series = "best-of"
    only_own = job.get("reuse_group_clips", True)
    picks = []
    for c in clips:
        if used.blocked(c, series if only_own else None):
            continue
        p = _to_pick(c, rules, int(job.get("min_views", 200)))
        if p:
            picks.append(p)
    picks = dedupe(picks, 0)
    chosen = choose(picks, job, one_per_member=False)
    mins = sum(p.duration for p in chosen) / 60
    RUN.setdefault("jobs", []).append(["Best of the day", len(clips), len(picks), len(chosen), f"{mins:.1f}"])
    log.info("GTA best-of: %d English clips, %d new candidates, %d chosen (%.1f min)", len(clips), len(picks),
             len(chosen), mins)
    if mins < float(job.get("absolute_min_minutes", 8)):
        log.info("Not enough clips for the best-of - no video this time.")
        return None
    LAST.clear()
    LAST.update(channels=[], order=[], candidates=len(picks), note="")
    item = _render(cfg, drive, chosen, job, f"best-{now.strftime('%Y%m%d')}", series,
                   job.get("suffix", "GTA 6 Best Clips of the Day"),
                   "the best (most viewed) English GTA 6 clips of the last 24 hours",
                   "Best GTA 6 Clips of the Day", tmp)
    if item:
        used.add(item, series)
        item["order"] = list(LAST.get("order", []))
    return item


def run(cfg: dict, drive, job: str, only_group: str | None, force: bool) -> list[dict]:
    """Run whatever is due (or everything asked for with --force). Returns new queue items."""
    import tempfile
    g = cfg["gta"]
    st = drive.read_json(STATE, {"groups": {}, "best": None})
    now = datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(cfg["timezone"]))
    used = Used(drive, int(g.get("history_days", 60)))
    RUN.clear()
    tw = None
    if os.environ.get("TWITCH_CLIENT_ID"):
        tw = sources.Twitch()
    kick = None
    items: list[dict] = []
    every = float(g["groups"].get("every_hours", 8))
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if job in ("groups", "all") and g["groups"].get("enabled", True):
            for group in g["groups"]["list"]:
                if only_group and slug(only_group) != slug(group["name"]):
                    continue
                last = _parse(st["groups"].get(slug(group["name"])))
                if not force and last and now - last < timedelta(hours=every - 0.75):
                    log.info("GTA group %s: last video %s - next one after %.0f h.", group["name"],
                             last.strftime("%d.%m %H:%M"), every)
                    continue
                try:
                    item = run_group(cfg, drive, group, used, tw, kick, tmp)
                except Exception:  # noqa: BLE001 - one group failing must not stop the others
                    log.exception("GTA group %s failed", group["name"])
                    continue
                st["groups"][slug(group["name"])] = _iso(now)
                drive.write_json(STATE, st)
                if item:
                    items.append(item)
        b = g.get("best") or {}
        if job in ("best", "all") and b.get("enabled", True) and not only_group:
            due = local.hour >= int(b.get("hour", 6)) and st.get("best") != local.date().isoformat()
            if force or due:
                try:
                    item = run_best(cfg, drive, used, tw, kick, tmp)
                    st["best"] = local.date().isoformat()
                    drive.write_json(STATE, st)
                    if item:
                        items.append(item)
                except Exception:  # noqa: BLE001
                    log.exception("GTA best-of failed")
            else:
                log.info("GTA best-of: already done today or before %02d:00.", int(b.get("hour", 6)))
    return items


def report(cfg: dict, drive, items: list[dict]) -> None:
    from .report import Report
    day = datetime.now(ZoneInfo(cfg["timezone"])).date().isoformat()
    r = Report(f"GTA {day}")
    r.table("Jobs", ["Video", "Clips found", "New candidates", "Chosen", "Minutes"], RUN.get("jobs", []))
    for name, rows in (RUN.get("members") or {}).items():
        r.table(f"{name}: members", ["Streamer", "Clips in window", "GTA clips", "Best views"], rows)
    if RUN.get("best_twitch"):
        r.text(f"Best-of sources: Twitch {RUN['best_twitch'][0]} clips ({RUN['best_twitch'][1]} English); "
               + (f"Kick {RUN['best_kick'][0]} clips ({RUN['best_kick'][1]} from {RUN['best_kick'][2]} English channels)."
                  if RUN.get("best_kick") else "Kick: failed."))
    state = drive.read_json("queue.json", {"items": []})
    by_key = {i.get("key"): i for i in state.get("items", [])}
    for it in items:
        cur = by_key.get(it["key"], it)
        link = f"https://youtube.com/watch?v={cur['youtube_id']}" if cur.get("youtube_id") else "not uploaded yet"
        r.text(f"**{cur.get('yt_title')}** - {len(it.get('clips', []))} clips - {link}")
        r.table("Order", ["#", "Starts at", "Streamer", "Why", "Score", "Views", "Length", "Clip title", "Clip"],
                it.get("order", []))
    r.publish(drive, day, f"GTA {day} {datetime.now(ZoneInfo(cfg['timezone'])).strftime('%H%M')}")
