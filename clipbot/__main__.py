"""ClipBot — daily Twitch/Kick clips -> Google Drive -> YouTube Shorts.

Usage:
    python -m clipbot fetch [--force]   # collect clips, store on Drive, schedule posts
    python -m clipbot post              # upload any clips that are due to YouTube
    python -m clipbot cleanup           # delete Drive folders older than retention_days
    python -m clipbot status            # print the queue
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


def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as fh:
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
    pending = [parse_iso(i["scheduled_at"]) for i in state["items"] if i["status"] == "pending"]
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
    if not force and local.hour != int(cfg["fetch_hour"]):
        log.info("It's %s in %s, fetch runs at %02d:00 — nothing to do.",
                 local.strftime("%H:%M"), cfg["timezone"], int(cfg["fetch_hour"]))
        return

    drive = Drive(cfg["drive_root_folder"])
    state = drive.read_json(QUEUE, empty_state())
    state.setdefault("seen", {})
    if not force and state.get("last_fetch") == local.date().isoformat():
        log.info("Already fetched today — skipping.")
        return

    cleanup(cfg, drive, state, local.date())

    lookback = timedelta(hours=float(cfg["lookback_hours"]))
    day_label = (local - lookback).date().isoformat()  # date the clips are from
    clips = sources.collect(cfg["creators"], now - lookback, now, int(cfg["clips_per_creator"]))
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
                try:
                    media.make_short(raw, final, bool(cfg["youtube"]["vertical"]),
                                     int(cfg["youtube"]["max_seconds"]), cfg.get("overlay"),
                                     float(cfg["youtube"].get("background_saturation", 1.0)))
                except RuntimeError:
                    log.exception("Conversion failed for %s", clip.url)
                    continue
                folder = folder or drive.folder_path(creator, day_label)
                file_id = drive.upload(final, name, folder)
                cap = captions.generate(clip, cfg["captions"]["language"], cfg["captions"]["model"])
                title, desc, tags = captions.build_youtube_fields(clip, cap)
                new_items.append({
                    "key": f"{clip.platform}:{clip.clip_id}", "creator": creator, "platform": clip.platform,
                    "channel": clip.channel, "ai_caption": bool(cap.get("ai")),
                    "rank": rank, "date": day_label, "file_name": name, "drive_file_id": file_id,
                    "source_url": clip.url, "source_title": clip.title, "views": clip.views,
                    "game": clip.game, "yt_title": title, "yt_description": desc, "yt_tags": tags,
                    "status": "pending", "attempts": 0, "scheduled_at": None, "youtube_id": None,
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


def cmd_post(cfg: dict) -> None:
    from googleapiclient.errors import HttpError

    from .google_apis import Drive, YouTube

    drive = Drive(cfg["drive_root_folder"])
    state = drive.read_json(QUEUE, empty_state())
    now = datetime.now(timezone.utc)
    due = sorted((i for i in state["items"] if i["status"] == "pending" and i["scheduled_at"]
                  and parse_iso(i["scheduled_at"]) <= now), key=lambda i: i["scheduled_at"])
    due = due[: int(cfg["posting"]["max_per_run"])]
    local_now = now.astimezone(ZoneInfo(cfg["timezone"]))
    if due and in_quiet(local_now, cfg["posting"].get("quiet_hours")):
        log.info("Quiet hours (%s) — %d clip(s) wait until morning.", local_now.strftime("%H:%M"), len(due))
        return
    if not due:
        log.info("Nothing due to post.")
        return

    yt = YouTube()
    y = cfg["youtube"]
    channels = {c["name"]: c["channel"] for c in cfg["creators"]}
    with tempfile.TemporaryDirectory() as tmp:
        for item in due:
            path = Path(tmp) / item["file_name"]
            if not item.get("ai_caption"):
                recaption(cfg, item, channels)
            if captions.is_unsafe(item["yt_title"]):
                log.warning("Title of %s still contains a blocked word — skipping this clip", item["file_name"])
                item["status"] = "skipped"
                drive.write_json(QUEUE, state)
                continue
            try:
                drive.download(item["drive_file_id"], path)
                vid = yt.upload(path, item["yt_title"], item["yt_description"], item["yt_tags"],
                                y["category_id"], y["privacy"])
                item.update(status="posted", youtube_id=vid, posted_at=iso(datetime.now(timezone.utc)))
                log.info("Posted %s -> https://youtube.com/shorts/%s", item["file_name"], vid)
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
    ap.add_argument("command", choices=["fetch", "post", "cleanup", "status"])
    ap.add_argument("--force", action="store_true", help="fetch now, ignoring the clock and today's run")
    args = ap.parse_args(argv)
    cfg = load_config()
    {"fetch": lambda: cmd_fetch(cfg, args.force), "post": lambda: cmd_post(cfg),
     "cleanup": lambda: cmd_cleanup(cfg), "status": lambda: cmd_status(cfg)}[args.command]()


if __name__ == "__main__":
    sys.exit(main())
