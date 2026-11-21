"""ClipBot — daily Twitch/Kick clips -> Google Drive -> YouTube Shorts.

Usage:
    python -m clipbot fetch [--force]   # collect clips, store on Drive, schedule posts
    python -m clipbot post              # upload any clips that are due to YouTube
    python -m clipbot cleanup           # delete Drive folders older than retention_days
    python -m clipbot status            # print the queue
    python -m clipbot discover [--force]  # remember CZ/SK channels that are live right now
    python -m clipbot compile [--force]   # build the daily long compilation, queue it for upload_hour
"""
from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from . import captions, media, sources

log = logging.getLogger("clipbot")
QUEUE = "queue.json"
ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | None = None) -> dict:
    """config.yaml by default; another file with --config or the CLIPBOT_CONFIG env variable
    (e.g. config_gta.yaml for the English GTA channel)."""
    import os
    name = path or os.environ.get("CLIPBOT_CONFIG") or "config.yaml"
    with open(ROOT / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def empty_state() -> dict:
    return {"last_fetch": None, "items": [], "seen": {}}


# ---------------------------------------------------------------- cleanup
def cleanup(cfg: dict, drive, state: dict, today: date) -> None:
    keep_days = int(cfg["retention_days"])
    removed_dates: set[str] = set()
    for creator in drive.children(drive.root, folders_only=True):
        for day in drive.children(creator["id"], folders_only=True):
            try:
                d = date.fromisoformat(day["name"])
            except ValueError:
                continue
            if (today - d).days > keep_days:
                log.info("Deleting %s/%s from Drive", creator["name"], day["name"])
                drive.delete(day["id"])
                removed_dates.add(day["name"])
    before = len(state["items"])
    for it in state["items"]:
        if it["date"] in removed_dates and it["status"] == "pending":
            log.warning("Clip %s expired before it was posted", it["file_name"])
    state["items"] = [it for it in state["items"]
                      if (today - date.fromisoformat(it["date"])).days <= keep_days]
    # remember posted/seen clip ids a bit longer to avoid re-posting duplicates
    state["seen"] = {k: v for k, v in state.get("seen", {}).items()
                     if (today - date.fromisoformat(v)).days <= keep_days + 7}
    log.info("Cleanup done: %d old queue entries removed", before - len(state["items"]))


def prune_uploaded(drive, state: dict) -> int:
    """Drive is only a short backup: once a video is on YouTube its file is deleted, except the most
    viewed Short of each streamer per day (kept until retention_days removes the folder).
    Compilation videos and thumbnails are deleted right after the upload."""
    top: dict[tuple, dict] = {}
    for it in state["items"]:
        if it.get("kind") == "long":
            continue
        k = (it.get("creator"), it.get("date"))
        if k not in top or int(it.get("views") or 0) > int(top[k].get("views") or 0):
            top[k] = it
    freed = 0
    for it in state["items"]:
        if it.get("status") != "posted":
            continue
        if not it.get("video_pruned") and it.get("drive_file_id"):
            if it.get("kind") == "long" or top.get((it.get("creator"), it.get("date"))) is not it:
                try:
                    drive.delete(it["drive_file_id"])
                    freed += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("Could not delete %s from Drive: %s", it.get("file_name"), e)
                    continue
                it["video_pruned"] = True
        for key in ("thumb_drive_id", "thumb_file_id"):
            if it.get(key) and not it.get(key + "_pruned") and it.get("thumb") not in ("pending",):
                try:
                    drive.delete(it[key])
                except Exception:  # noqa: BLE001
                    continue
                it[key + "_pruned"] = True
    if freed:
        log.info("Drive: deleted %d uploaded video file(s) (only the top Short per streamer is kept).", freed)
    return freed


# ---------------------------------------------------------------- fetch
def in_quiet(local: datetime, quiet) -> bool:
    if not quiet:
        return False
    q0, q1 = int(quiet[0]), int(quiet[1])
    h = local.hour
    return q0 <= h < q1 if q0 < q1 else (h >= q0 or h < q1)


def next_allowed(t: datetime, cfg: dict, tz: ZoneInfo) -> datetime:
    """Move t forward to the end of the quiet hours if it falls inside them."""
    quiet = cfg["posting"].get("quiet_hours")
    local = t.astimezone(tz)
    if not in_quiet(local, quiet):
        return t
    end = local.replace(hour=int(quiet[1]), minute=0, second=0, microsecond=0)
    if end <= local:
        end += timedelta(days=1)
    return end.astimezone(timezone.utc)


def schedule_times(cfg: dict, state: dict, n: int, now: datetime, tz: ZoneInfo) -> list[datetime]:
    p = cfg["posting"]
    if n == 0:
        return []
    local = now.astimezone(tz)
    start = now
    if p.get("start_hour") is not None:
        first = local.replace(hour=int(p["start_hour"]), minute=0, second=0, microsecond=0)
        if first > local:
            start = first.astimezone(timezone.utc)
    start = next_allowed(start, cfg, tz)
    min_gap = timedelta(minutes=int(p["min_interval_minutes"]))
    pending = [parse_iso(i["scheduled_at"]) for i in state["items"]
               if i["status"] == "pending" and i.get("kind") != "long" and i.get("scheduled_at")]
    if pending:
        start = next_allowed(max(start, max(pending) + min_gap), cfg, tz)
    # length of the posting window: from start until the next quiet period begins
    quiet = p.get("quiet_hours")
    if quiet:
        s_local = start.astimezone(tz)
        q_start = s_local.replace(hour=int(quiet[0]), minute=0, second=0, microsecond=0)
        if q_start <= s_local:
            q_start += timedelta(days=1)
        window = q_start - s_local
    else:
        window = timedelta(hours=24)
    interval = max(min_gap, window / n)
    times, t = [], start
    for _ in range(n):
        t = next_allowed(t, cfg, tz)
        times.append(t)
        t = t + interval
    return times


def cmd_fetch(cfg: dict, force: bool) -> None:
    from .google_apis import Drive

    tz = ZoneInfo(cfg["timezone"])
    now = datetime.now(timezone.utc)
    local = now.astimezone(tz)
    # GitHub often starts scheduled runs hours late, so: run at fetch_hour OR LATER, once per day
    if not force and local.hour < int(cfg["fetch_hour"]):
        log.info("It's %s in %s, today's fetch starts at %02d:00 — nothing to do yet.",
                 local.strftime("%H:%M"), cfg["timezone"], int(cfg["fetch_hour"]))
        return

    drive = Drive(cfg["drive_root_folder"])
    state = drive.read_json(QUEUE, empty_state())
    state.setdefault("seen", {})
    if not force and state.get("last_fetch") == local.date().isoformat():
        log.info("Already fetched today — skipping.")
        return
    from . import insights
    insights.load(drive)

    cleanup(cfg, drive, state, local.date())

    lookback = timedelta(hours=float(cfg["lookback_hours"]))
    day_label = (local - lookback).date().isoformat()  # date the clips are from
    clips = sources.collect(cfg["creators"], now - lookback, now, int(cfg["clips_per_creator"]),
                            cfg.get("clip_length"))
    clips = [c for c in clips if f"{c.platform}:{c.clip_id}" not in state["seen"]]

    by_creator: dict[str, list] = defaultdict(list)
    for c in clips:
        by_creator[c.creator].append(c)

    new_items = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for creator, items in by_creator.items():
            folder = None
            for rank, clip in enumerate(items, start=1):
                name = f"{creator}-{day_label}-clip{rank}.mp4"
                raw, final = tmp / f"raw-{clip.clip_id}.mp4", tmp / name
                if not media.download(clip, raw):
                    log.error("Could not download %s (%s)", clip.url, creator)
                    continue
                real = media.duration(raw)
                if real and sources.length_factor(real, cfg.get("clip_length")) is None:
                    log.info("Skipping %s: %.0f s is over the length limit", clip.url, real)
                    raw.unlink(missing_ok=True)
                    continue
                try:
                    media.make_short(raw, final, bool(cfg["youtube"]["vertical"]),
                                     int(cfg["youtube"]["max_seconds"]), cfg.get("overlay"),
                                     float(cfg["youtube"].get("background_saturation", 1.0)),
                                     cfg.get("short_intro"))
                except RuntimeError:
                    log.exception("Conversion failed for %s", clip.url)
                    continue
                folder = folder or drive.folder_path(creator, day_label)
                file_id = drive.upload(final, name, folder)
                cap = captions.generate(clip, cfg["captions"]["language"], cfg["captions"]["model"])
                title, desc, tags = captions.build_youtube_fields(clip, cap)
                thumb_id = make_short_thumb(cfg, drive, final, real or media.duration(raw), cap, folder,
                                            name.replace(".mp4", "-thumb.jpg"), tmp)
                new_items.append({
                    "key": f"{clip.platform}:{clip.clip_id}", "creator": creator, "platform": clip.platform,
                    "channel": clip.channel, "ai_caption": bool(cap.get("ai")),
                    "rank": rank, "date": day_label, "file_name": name, "drive_file_id": file_id,
                    "source_url": clip.url, "source_title": clip.title, "views": clip.views,
                    "game": clip.game, "yt_title": title, "yt_description": desc, "yt_tags": tags,
                    "status": "pending", "attempts": 0, "scheduled_at": None, "youtube_id": None,
                    "thumb_file_id": thumb_id, "thumb_text": cap.get("thumb_text", ""),
                    "thumb": "pending" if thumb_id else None, "thumb_attempts": 0,
                })
                state["seen"][f"{clip.platform}:{clip.clip_id}"] = day_label
                log.info("Saved %s  ->  \"%s\"", name, title)
                raw.unlink(missing_ok=True)
                final.unlink(missing_ok=True)

    # posting order: every creator's #1 clip first (most viewed first), then the #2s, ...
    new_items.sort(key=lambda i: (i["rank"], -i["views"]))
    for item, when in zip(new_items, schedule_times(cfg, state, len(new_items), now, tz)):
        item["scheduled_at"] = iso(when)
    state["items"].extend(new_items)
    state["last_fetch"] = local.date().isoformat()
    drive.write_json(QUEUE, state)
    log.info("Fetch finished: %d new clips queued.", len(new_items))
    for i in new_items:
        log.info("  %s  %s", parse_iso(i["scheduled_at"]).astimezone(tz).strftime("%d.%m %H:%M"), i["file_name"])
    if batch_mode(cfg):
        cmd_post(cfg, everything=True)
    report_fetch(cfg, drive, day_label, [i["key"] for i in new_items])


def make_short_thumb(cfg: dict, drive, video: Path, clip_seconds: float, cap: dict, folder: str,
                     name: str, tmp: Path) -> str | None:
    """Thumbnail from the middle of the clip (after the static intro), with the AI's short text if it
    found the clip title interesting. Saved next to the Short on Drive; set on YouTube after upload."""
    t = cfg.get("short_thumbnail") or {}
    if not t.get("enabled", True):
        return None
    try:
        from .compilation import _font
        intro = cfg.get("short_intro") or {}
        start = float(intro.get("seconds", 0.5)) if intro.get("enabled") else 0.0
        clip_seconds = min(clip_seconds or 10, float(cfg["youtube"]["max_seconds"]))
        text = cap.get("thumb_text", "") if t.get("text", True) else ""
        out = tmp / name
        if not media.short_thumbnail(video, out, start + clip_seconds / 2, text, _font()):
            return None
        fid = drive.upload(out, name, folder, mimetype="image/jpeg")
        out.unlink(missing_ok=True)
        return fid
    except Exception as e:  # noqa: BLE001 - a missing thumbnail must never stop the Short
        log.warning("Could not make the thumbnail %s: %s", name, e)
        return None


def set_short_thumbnails(drive, yt, state: dict, tmp: Path, min_age_minutes: int = 3) -> None:
    """YouTube accepts a Short's thumbnail only after it has processed the video, so thumbnails are set
    a few minutes after the upload (and retried in later runs)."""
    from googleapiclient.errors import HttpError
    now = datetime.now(timezone.utc)
    todo = [i for i in state["items"] if i.get("kind") != "long" and i["status"] == "posted"
            and i.get("thumb") == "pending" and i.get("youtube_id") and i.get("thumb_file_id")
            and parse_iso(i["posted_at"]) <= now - timedelta(minutes=min_age_minutes)]
    for item in todo:
        path = tmp / f"thumb-{item['youtube_id']}.jpg"
        try:
            drive.download(item["thumb_file_id"], path)
            yt.set_thumbnail(item["youtube_id"], path)
            item["thumb"] = "set"
            log.info("Thumbnail set for %s", item["file_name"])
        except Exception as e:  # noqa: BLE001
            item["thumb_attempts"] = item.get("thumb_attempts", 0) + 1
            if isinstance(e, HttpError) and "quotaExceeded" in str(e):
                log.warning("Quota reached while setting thumbnails - will retry next run.")
                break
            if item["thumb_attempts"] >= 8:
                item["thumb"] = "failed"
            log.warning("Thumbnail for %s not set yet (attempt %d): %s", item["file_name"],
                        item["thumb_attempts"], str(e)[:200])
        finally:
            path.unlink(missing_ok=True)
    if todo:
        drive.write_json(QUEUE, state)


def report_fetch(cfg: dict, drive, day_label: str, keys: list[str]) -> None:
    from .report import Report
    state = drive.read_json(QUEUE, empty_state())
    by_key = {i["key"]: i for i in state["items"]}
    r = Report(f"Shorts {day_label}")
    r.table("Streamers (last 24 h)", ["Streamer", "Platform", "Clips in 24 h", "Over 60 s", "Best views", "Chosen"],
            [[x["name"], x["platform"], x["found"], x["too_long"], x["best_views"], x["chosen"]]
             for x in sources.LAST_STATS])
    rows = []
    for k in keys:
        i = by_key.get(k)
        if not i:
            continue
        yt = f"https://youtube.com/shorts/{i['youtube_id']}" if i.get("youtube_id") else ""
        rows.append([i["file_name"], i["creator"], i.get("views", ""), i.get("source_title", ""), i["yt_title"],
                     i["status"], yt, i.get("source_url", "")])
    r.table("Chosen Shorts", ["File", "Streamer", "Views", "Original title", "YouTube title", "Status",
                              "YouTube", "Original clip"], rows)
    r.publish(drive, day_label, f"Shorts {day_label}")


# ---------------------------------------------------------------- post
def recaption(cfg: dict, item: dict, channels: dict) -> None:
    """Retry the AI caption for a clip whose caption fell back to the raw title at fetch time."""
    clip = sources.Clip(
        platform=item["platform"], clip_id=item["key"].split(":", 1)[1], creator=item["creator"],
        channel=item.get("channel") or channels.get(item["creator"], item["creator"].lower()),
        title=item.get("source_title", ""), url=item["source_url"], views=item.get("views", 0),
        created_at="", game=item.get("game", ""),
    )
    cap = captions.generate(clip, cfg["captions"]["language"], cfg["captions"]["model"])
    title, desc, tags = captions.build_youtube_fields(clip, cap)
    item.update(yt_title=title, yt_description=desc, yt_tags=tags, ai_caption=bool(cap.get("ai")))
    log.info("Re-captioned %s -> \"%s\" (ai=%s)", item["file_name"], title, item["ai_caption"])


def batch_mode(cfg: dict) -> bool:
    return str(cfg["posting"].get("mode", "spread")).lower() == "batch"


def publish_time(cfg: dict, item: dict, now: datetime) -> str | None:
    """With youtube.schedule_publish: the time YouTube should publish this video by itself."""
    if not cfg["youtube"].get("schedule_publish"):
        return None
    if item.get("kind") == "long":
        tz = ZoneInfo(cfg["timezone"])
        local = now.astimezone(tz)
        t = local.replace(hour=int(cfg["compilation"].get("publish_hour", 18)), minute=0, second=0, microsecond=0)
        if t <= local:
            return None
        return iso(t)
    when = item.get("scheduled_at")
    if when and parse_iso(when) > now + timedelta(minutes=15):
        return iso(parse_iso(when))
    return None


def cmd_post(cfg: dict, everything: bool = False) -> None:
    """Upload pending videos. everything=True: all of them now (batch mode), else only those that are due."""
    from googleapiclient.errors import HttpError

    from .google_apis import Drive, YouTube

    drive = Drive(cfg["drive_root_folder"])
    state = drive.read_json(QUEUE, empty_state())
    now = datetime.now(timezone.utc)
    pending = sorted((i for i in state["items"] if i["status"] == "pending"),
                     key=lambda i: i.get("scheduled_at") or "")
    if everything or batch_mode(cfg):
        # batch: anything still waiting goes up now (also leftovers / retries of failed uploads)
        due = pending
    else:
        due = [i for i in pending if i.get("scheduled_at") and parse_iso(i["scheduled_at"]) <= now]
        local_now = now.astimezone(ZoneInfo(cfg["timezone"]))
        if in_quiet(local_now, cfg["posting"].get("quiet_hours")):
            due = [i for i in due if i.get("kind") == "long"]  # Shorts wait until morning
        longs = [i for i in due if i.get("kind") == "long"]
        due = longs + [i for i in due if i.get("kind") != "long"][: int(cfg["posting"]["max_per_run"])]
    if not due:
        log.info("Nothing due to post.")
        if any(i.get("thumb") == "pending" and i["status"] == "posted" for i in state["items"]):
            with tempfile.TemporaryDirectory() as tmp:
                set_short_thumbnails(drive, YouTube(), state, Path(tmp))
        prune_uploaded(drive, state)
        drive.write_json(QUEUE, state)
        return
    log.info("Uploading %d video(s)%s.", len(due), " (batch)" if everything or batch_mode(cfg) else "")

    yt = YouTube()
    y = cfg["youtube"]
    channels = {c["name"]: c["channel"] for c in cfg["creators"]}
    with tempfile.TemporaryDirectory() as tmp:
        for item in due:
            path = Path(tmp) / item["file_name"]
            if item.get("kind") == "long":
                post_long(cfg, drive, yt, state, item, Path(tmp))
                continue
            if not item.get("ai_caption"):
                recaption(cfg, item, channels)
            if captions.is_unsafe(item["yt_title"]):
                log.warning("Title of %s still contains a blocked word — skipping this clip", item["file_name"])
                item["status"] = "skipped"
                drive.write_json(QUEUE, state)
                continue
            try:
                drive.download(item["drive_file_id"], path)
                pub = publish_time(cfg, item, now)
                vid = yt.upload(path, item["yt_title"], item["yt_description"], item["yt_tags"],
                                y["category_id"], y["privacy"], publish_at=pub)
                item.update(status="posted", youtube_id=vid, posted_at=iso(datetime.now(timezone.utc)),
                            publish_at=pub)
                log.info("Uploaded %s -> https://youtube.com/shorts/%s%s", item["file_name"], vid,
                         f" (YouTube publishes it at {pub})" if pub else "")
            except HttpError as e:
                item["attempts"] += 1
                if "quotaExceeded" in str(e) or "uploadLimitExceeded" in str(e):
                    log.error("YouTube upload limit reached — will retry next run.")
                    break
                log.exception("YouTube upload failed for %s", item["file_name"])
            except Exception:  # noqa: BLE001
                item["attempts"] += 1
                log.exception("Posting failed for %s", item["file_name"])
            finally:
                if item["status"] == "pending" and item["attempts"] >= 3:
                    item["status"] = "failed"
                path.unlink(missing_ok=True)
                drive.write_json(QUEUE, state)
        set_short_thumbnails(drive, yt, state, Path(tmp))
    prune_uploaded(drive, state)
    drive.write_json(QUEUE, state)


def post_long(cfg: dict, drive, yt, state: dict, item: dict, tmp: Path) -> None:
    from googleapiclient.errors import HttpError
    comp = cfg["compilation"]
    from .compilation import episode_title
    path = tmp / item["file_name"]
    try:
        drive.download(item["drive_file_id"], path)
        # episode number is given only when the upload succeeds, so skipped days leave no gaps
        series = item.get("series")  # GTA channel: every group / the best-of has its own numbering
        fixed = item.get("replaces_episode")  # a re-cut of an already numbered episode keeps its number
        if fixed:
            episode = int(fixed)
            title = episode_title(item.get("base_title") or item["yt_title"], episode, item.get("title_suffix"))
        elif series:
            episode = int(state.setdefault("episodes", {}).get(series, 0)) + 1
            title = episode_title(item.get("base_title") or item["yt_title"], episode, item.get("title_suffix"))
        else:
            episode = int(state.get("episode", int(comp.get("first_episode", 1)) - 1)) + 1
            title = episode_title(item.get("base_title") or item["yt_title"], episode)
        pub = publish_time(cfg, item, datetime.now(timezone.utc))
        vid = yt.upload(path, title, item["yt_description"], item["yt_tags"],
                        cfg["youtube"]["category_id"], comp.get("privacy", "private"), publish_at=pub)
        if fixed:
            pass
        elif series:
            state["episodes"][series] = episode
        else:
            state["episode"] = episode
        item.update(status="posted", youtube_id=vid, posted_at=iso(datetime.now(timezone.utc)),
                    yt_title=title, episode=episode)
        log.info("Posted compilation #%d \"%s\" -> https://youtube.com/watch?v=%s", episode, title, vid)
        if item.get("thumb_drive_id"):
            thumb = drive.download(item["thumb_drive_id"], tmp / "thumb.jpg")
            try:
                from .compilation import add_episode_badge
                add_episode_badge(thumb, episode, cfg)
            except Exception as e:  # noqa: BLE001
                log.warning("Could not add the episode badge: %s", e)
            try:
                yt.set_thumbnail(vid, thumb)
                log.info("Thumbnail set.")
            except HttpError as e:
                log.warning("Could not set the thumbnail (is the channel phone-verified?): %s", e)
    except Exception:  # noqa: BLE001
        item["attempts"] += 1
        log.exception("Posting the compilation failed")
        if item["attempts"] >= 3:
            item["status"] = "failed"
    finally:
        path.unlink(missing_ok=True)
        drive.write_json(QUEUE, state)


def cmd_discover(cfg: dict, force: bool) -> None:
    from . import compilation
    from .google_apis import Drive
    if not cfg.get("compilation", {}).get("enabled"):
        return
    compilation.discover(cfg, Drive(cfg["drive_root_folder"]), force)


def cmd_compile(cfg: dict, force: bool) -> None:
    from . import compilation
    from .google_apis import Drive

    comp = cfg.get("compilation", {})
    if not comp.get("enabled"):
        log.info("Compilation is turned off in config.yaml.")
        return
    tz = ZoneInfo(cfg["timezone"])
    now = datetime.now(timezone.utc)
    local = now.astimezone(tz)
    if not force and local.hour < int(comp["hour"]):
        log.info("It's %s, today's compilation starts at %02d:00 — nothing to do yet.", local.strftime("%H:%M"),
                 int(comp["hour"]))
        return
    drive = Drive(cfg["drive_root_folder"])
    state = drive.read_json(QUEUE, empty_state())
    if not force and state.get("last_compilation") == local.date().isoformat():
        log.info("Today's compilation is already done — skipping.")
        return
    compilation.discover(cfg, drive, force=False)  # top up the channel list first
    from . import insights
    analytics = insights.load(drive)
    with tempfile.TemporaryDirectory() as tmp:
        item = compilation.build(cfg, drive, now, local, Path(tmp))
    state = drive.read_json(QUEUE, empty_state())  # re-read: other runs may have changed it
    state["last_compilation"] = local.date().isoformat()
    immediately = str(comp.get("upload", "immediately")).lower() == "immediately"
    if item:
        up = local.replace(hour=int(comp["upload_hour"]), minute=0, second=0, microsecond=0)
        if up < local or immediately:
            up = local
        item["scheduled_at"] = iso(up)
        state["items"] = [i for i in state["items"] if i.get("key") != item["key"]] + [item]
        log.info("Compilation queued for upload at %s (%s).", up.strftime("%d.%m %H:%M"),
                 comp.get("privacy", "private"))
    drive.write_json(QUEUE, state)
    if item and immediately:
        cmd_post(cfg, everything=True)
    report_compile(cfg, drive, item, analytics)


def report_compile(cfg: dict, drive, item: dict | None, analytics: dict | None = None) -> None:
    from . import compilation, insights
    from .report import Report
    L = compilation.LAST
    day = item["date"] if item else datetime.now(ZoneInfo(cfg["timezone"])).date().isoformat()
    r = Report(f"Compilation {day}")
    if item:
        state = drive.read_json(QUEUE, empty_state())
        cur = next((i for i in state["items"] if i.get("key") == item["key"]), item)
        link = f"https://youtube.com/watch?v={cur['youtube_id']}" if cur.get("youtube_id") else "not uploaded yet"
        streamers = len({o[2].lower() for o in L.get("order", [])})
        r.text(f"**{cur['yt_title']}** - {len(L.get('order', []))} clips from {streamers} streamers - {link}")
    if L.get("note"):
        r.text(L["note"])
    r.text(f"Scanned {L.get('scanned', '?')} channels, {L.get('candidates', '?')} candidate clips. "
           "Score = clip views / that streamer's usual clip views, adjusted for clip length.")
    w = L.get("why") or {}
    if w:
        r.text(f"Selection: {w.get('guaranteed', 0)} clips = 1 per chosen creator, {w.get('core_extra', 0)} extra clips "
               f"from chosen creators, {w.get('abnormal', 0)} abnormal clips from the CZ/SK top 10, "
               f"{w.get('fill', 0)} fill clips from the CZ/SK top (only when the chosen creators had too little).")
    if L.get("missing_core"):
        r.text("Chosen creators with no usable clip today: " + ", ".join(L["missing_core"]))
    r.table("Order in the video", ["#", "Starts at", "Streamer", "Why", "Score", "Views", "Length", "Clip title",
                                   "Clip"], L.get("order", []))
    r.table("CZ/SK Twitch top 10 today (logged in Drive/Stats)", ["#", "Streamer", "Views", "x vs top 10",
            "x vs usual", "Logs", "Abnormal", "Clip title", "Clip"], L.get("scene", []))
    chans = sorted(L.get("channels", []), key=lambda x: -x[5])
    r.table("Channels with clips (last 24 h)", ["Channel", "Platform", "Clips", "Best views", "Usual views",
                                                 "Best / usual", "Clips over min. views", "Peak live viewers",
                                                 "Size weight"], chans)
    insights.report_tables(r, analytics or {})
    r.publish(drive, day, f"Compilation {day}")


def cmd_gta(cfg: dict, job: str, group: str | None, force: bool) -> None:
    """English GTA channel: one compilation per streamer group (every gta.groups.every_hours) and
    a daily 'Best GTA 6 clips of the day'. Uses its own config (config_gta.yaml) and Drive folder."""
    from . import gta
    from .google_apis import Drive
    drive = Drive(cfg["drive_root_folder"])
    state = drive.read_json(QUEUE, empty_state())
    cleanup(cfg, drive, state, datetime.now(ZoneInfo(cfg["timezone"])).date())
    drive.write_json(QUEUE, state)
    import os
    items = gta.run(cfg, drive, job, group, force)
    can_post = bool(os.environ.get("YOUTUBE_REFRESH_TOKEN"))
    if not can_post:
        log.warning("YOUTUBE_REFRESH_TOKEN_GTA is not set - videos stay on Drive and go up once it is.")
    if not items:
        if can_post:
            cmd_post(cfg, everything=True)  # retry anything that failed to upload last time
        return
    state = drive.read_json(QUEUE, empty_state())
    now = iso(datetime.now(timezone.utc))
    for item in items:
        item["scheduled_at"] = now
        state["items"] = [i for i in state["items"] if i.get("key") != item["key"]] + [item]
    drive.write_json(QUEUE, state)
    if can_post:
        cmd_post(cfg, everything=True)
    gta.report(cfg, drive, items)


def cmd_cleanup(cfg: dict) -> None:
    from .google_apis import Drive
    drive = Drive(cfg["drive_root_folder"])
    state = drive.read_json(QUEUE, empty_state())
    state.setdefault("seen", {})
    cleanup(cfg, drive, state, datetime.now(ZoneInfo(cfg["timezone"])).date())
    drive.write_json(QUEUE, state)


def cmd_status(cfg: dict) -> None:
    from .google_apis import Drive
    tz = ZoneInfo(cfg["timezone"])
    state = Drive(cfg["drive_root_folder"]).read_json(QUEUE, empty_state())
    print(f"Last fetch: {state.get('last_fetch')}")
    for i in sorted(state["items"], key=lambda i: i.get("scheduled_at") or ""):
        when = parse_iso(i["scheduled_at"]).astimezone(tz).strftime("%d.%m %H:%M") if i.get("scheduled_at") else "-"
        print(f"{when}  {i['status']:<8} {i['file_name']:<40} {i['yt_title']}")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="clipbot")
    ap.add_argument("command", choices=["fetch", "post", "cleanup", "status", "discover", "compile", "gta"])
    ap.add_argument("job", nargs="?", default="all", help="gta only: groups | best | all")
    ap.add_argument("--group", help="gta groups: only this group (name)")
    ap.add_argument("--config", help="config file (default config.yaml or $CLIPBOT_CONFIG)")
    ap.add_argument("--force", action="store_true", help="fetch now, ignoring the clock and today's run")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    {"fetch": lambda: cmd_fetch(cfg, args.force), "post": lambda: cmd_post(cfg),
     "cleanup": lambda: cmd_cleanup(cfg), "status": lambda: cmd_status(cfg),
     "discover": lambda: cmd_discover(cfg, args.force),
     "compile": lambda: cmd_compile(cfg, args.force),
     "gta": lambda: cmd_gta(cfg, args.job, args.group, args.force)}[args.command]()


if __name__ == "__main__":
    sys.exit(main())
