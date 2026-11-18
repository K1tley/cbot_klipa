"""Find the most-viewed recent clips on Twitch and Kick."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)


@dataclass
class Clip:
    platform: str          # "twitch" | "kick"
    clip_id: str
    creator: str           # display name from config
    channel: str           # login / slug
    title: str
    url: str               # public page URL of the clip
    views: int
    created_at: str        # ISO-8601 UTC
    game: str = ""
    duration: float = 0.0
    media_urls: list[str] = field(default_factory=list)  # direct video URLs, if known
    stream_id: str = ""        # the VOD / livestream the clip was cut from (to spot duplicate moments)
    offset: float | None = None  # where in that stream the clip starts, in seconds (Twitch only)
    language: str = ""           # stream language reported by the platform ("en", "cs", ...), if known


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------- Twitch
class Twitch:
    API = "https://api.twitch.tv/helix"

    def __init__(self) -> None:
        self.client_id = os.environ["TWITCH_CLIENT_ID"]
        secret = os.environ["TWITCH_CLIENT_SECRET"]
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            data={"client_id": self.client_id, "client_secret": secret,
                  "grant_type": "client_credentials"},
            timeout=30,
        )
        r.raise_for_status()
        self.headers = {"Client-Id": self.client_id,
                        "Authorization": f"Bearer {r.json()['access_token']}"}
        self._games: dict[str, str] = {}

    def _get(self, path: str, **params):
        r = requests.get(f"{self.API}/{path}", headers=self.headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _game_name(self, game_id: str) -> str:
        if not game_id:
            return ""
        if game_id not in self._games:
            data = self._get("games", id=game_id).get("data", [])
            self._games[game_id] = data[0]["name"] if data else ""
        return self._games[game_id]

    def game_ids(self, names: list[str]) -> dict[str, str]:
        """Twitch game/category name -> id (e.g. 'Grand Theft Auto VI')."""
        out: dict[str, str] = {}
        for n in names:
            data = self._get("games", name=n).get("data", [])
            if data:
                out[n] = data[0]["id"]
                self._games[data[0]["id"]] = data[0]["name"]
            else:
                log.warning("Twitch category %r not found", n)
        return out

    def _clip(self, c: dict, creator: str, channel: str) -> Clip:
        return Clip(
            platform="twitch", clip_id=c["id"], creator=creator, channel=channel,
            title=c.get("title", "").strip(), url=c["url"], views=int(c.get("view_count", 0)),
            created_at=c.get("created_at", ""), game=self._game_name(c.get("game_id", "")),
            duration=float(c.get("duration") or 0),
            stream_id=str(c.get("video_id") or ""),
            offset=float(c["vod_offset"]) if c.get("vod_offset") is not None else None,
            language=str(c.get("language") or ""),
        )

    def game_clips(self, game_id: str, since: datetime, until: datetime, limit: int = 300) -> list[Clip]:
        """Most viewed clips of a whole category (all streamers), newest window only."""
        clips: list[Clip] = []
        cursor = None
        while len(clips) < limit:
            params = dict(game_id=game_id, first=100, started_at=since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          ended_at=until.strftime("%Y-%m-%dT%H:%M:%SZ"))
            if cursor:
                params["after"] = cursor
            page = self._get("clips", **params)
            for c in page.get("data", []):
                created = _parse_time(c.get("created_at"))
                if created and not (since <= created <= until):
                    continue
                name = c.get("broadcaster_name") or ""
                login = name.lower() if name.isascii() else str(c.get("broadcaster_id"))
                clips.append(self._clip(c, name, login))
            cursor = page.get("pagination", {}).get("cursor")
            if not cursor:
                break
        return clips

    def user_ids(self, logins: list[str]) -> dict[str, str]:
        """login -> broadcaster id, looked up 100 at a time."""
        out: dict[str, str] = {}
        for i in range(0, len(logins), 100):
            r = requests.get(f"{self.API}/users", headers=self.headers,
                             params=[("login", l) for l in logins[i:i + 100]], timeout=30)
            r.raise_for_status()
            out.update({u["login"].lower(): u["id"] for u in r.json().get("data", [])})
        return out

    def live_streams(self, language: str, max_pages: int = 10) -> list[dict]:
        """Everyone streaming in `language` right now (Twitch language code, e.g. cs, sk)."""
        streams, cursor = [], None
        for _ in range(max_pages):
            params = {"language": language, "first": 100}
            if cursor:
                params["after"] = cursor
            page = self._get("streams", **params)
            streams += page.get("data", [])
            cursor = page.get("pagination", {}).get("cursor")
            if not cursor:
                break
        return streams

    def top_clips(self, creator: str, channel: str, since: datetime, until: datetime,
                  broadcaster_id: str | None = None, limit: int = 500) -> list[Clip]:
        if not broadcaster_id:
            users = self._get("users", login=channel).get("data", [])
            if not users:
                log.warning("Twitch channel %s not found", channel)
                return []
            broadcaster_id = users[0]["id"]
        clips: list[Clip] = []
        cursor = None
        while True:
            params = dict(broadcaster_id=broadcaster_id, first=min(100, limit),
                          started_at=since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          ended_at=until.strftime("%Y-%m-%dT%H:%M:%SZ"))
            if cursor:
                params["after"] = cursor
            page = self._get("clips", **params)
            for c in page.get("data", []):
                created = _parse_time(c.get("created_at"))
                if created and not (since <= created <= until):
                    continue
                clips.append(self._clip(c, creator, channel))
            cursor = page.get("pagination", {}).get("cursor")
            if not cursor or len(clips) >= limit:
                break
        return clips


# ---------------------------------------------------------------- Kick
class Kick:
    """Kick has no official 'list a channel's clips' endpoint, so this uses the
    same JSON endpoint the kick.com website uses. It sits behind Cloudflare, so
    requests impersonate a real Chrome browser (curl_cffi). If Kick changes the
    site this is the part most likely to need a fix."""

    def __init__(self) -> None:
        from curl_cffi import requests as cffi  # lazy import
        self.session = cffi.Session(impersonate="chrome")
        self.session.headers.update({"Accept": "application/json", "Referer": "https://kick.com/"})

    def _fetch(self, url: str):
        r = self.session.get(url, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Kick returned HTTP {r.status_code} for {url}")
        return r.json()

    def top_clips(self, creator: str, channel: str, since: datetime, until: datetime) -> list[Clip]:
        """Kick's 'time=day' seems to mean the calendar day, not the last 24 h, so we collect from
        several views (top of day, top of week, newest first - paged) and filter to our own window."""
        base = f"https://kick.com/api/v2/channels/{channel}/clips"
        raw: dict[str, dict] = {}
        queries = [f"{base}?sort=view&time=day", f"{base}?sort=view&time=week"]
        for url in queries:
            try:
                data = self._fetch(url)
                for c in data.get("clips") or data.get("data") or []:
                    raw[str(c.get("id"))] = c
            except Exception as e:  # noqa: BLE001
                log.warning("Kick request failed (%s): %s", url, e)
        cursor = None
        for _ in range(5):  # newest first, until we are past the start of our window
            url = f"{base}?sort=date&time=all" + (f"&cursor={cursor}" if cursor else "")
            try:
                data = self._fetch(url)
            except Exception as e:  # noqa: BLE001
                log.warning("Kick request failed (%s): %s", url, e)
                break
            page = data.get("clips") or data.get("data") or []
            for c in page:
                raw[str(c.get("id"))] = c
            cursor = data.get("nextCursor") or data.get("next_cursor")
            oldest = min((_parse_time(c.get("created_at")) for c in page if c.get("created_at")), default=None)
            if not cursor or not page or (oldest and oldest < since):
                break

        clips: list[Clip] = []
        for c in raw.values():
            created = _parse_time(c.get("created_at") or c.get("started_at"))
            if created and not (since <= created <= until):
                continue
            cid = str(c.get("id") or c.get("clip_id") or "")
            if not cid or cid == "None":
                continue
            cat = c.get("category") or {}
            media = [u for u in (c.get("video_url"), c.get("clip_url")) if u]
            dur = float(c.get("duration") or 0)
            if dur > 1000:  # some Kick responses give milliseconds
                dur /= 1000
            clips.append(Clip(
                platform="kick", clip_id=cid, creator=creator, channel=channel,
                title=(c.get("title") or "").strip(),
                url=f"https://kick.com/{channel}/clips/{cid}",
                views=int(c.get("view_count") or c.get("views") or 0),
                created_at=c.get("created_at") or "",
                game=cat.get("name", "") if isinstance(cat, dict) else "",
                duration=dur,
                media_urls=media,
                stream_id=str(c.get("livestream_id") or ""),
            ))
        log.debug("Kick %s: %d clips seen, %d in window", channel, len(raw), len(clips))
        return clips


def _kick_clip(c: dict, creator: str | None = None, channel: str | None = None) -> Clip | None:
    cid = str(c.get("id") or c.get("clip_id") or "")
    if not cid or cid == "None":
        return None
    ch = c.get("channel") or {}
    channel = channel or ch.get("slug") or ""
    creator = creator or ch.get("username") or channel
    cat = c.get("category") or {}
    dur = float(c.get("duration") or 0)
    if dur > 1000:  # some Kick responses give milliseconds
        dur /= 1000
    return Clip(
        platform="kick", clip_id=cid, creator=creator, channel=channel,
        title=(c.get("title") or "").strip(), url=f"https://kick.com/{channel}/clips/{cid}",
        views=int(c.get("view_count") or c.get("views") or 0), created_at=c.get("created_at") or "",
        game=cat.get("name", "") if isinstance(cat, dict) else "", duration=dur,
        media_urls=[u for u in (c.get("video_url"), c.get("clip_url")) if u],
        stream_id=str(c.get("livestream_id") or ""),
    )


def kick_category_clips(kick: "Kick", slug: str, since: datetime, until: datetime, pages: int = 8) -> list[Clip]:
    """Most viewed clips of a whole Kick category (e.g. grand-theft-auto-vi), filtered to our window."""
    raw: dict[str, dict] = {}
    for time_range, n in (("day", pages), ("week", max(2, pages // 2))):
        cursor = None
        for _ in range(n):
            url = f"https://kick.com/api/v2/categories/{slug}/clips?sort=view&time={time_range}"
            if cursor:
                url += f"&cursor={cursor}"
            try:
                data = kick._fetch(url)
            except Exception as e:  # noqa: BLE001
                log.warning("Kick category request failed (%s): %s", url, e)
                break
            for c in data.get("clips") or []:
                raw[str(c.get("id"))] = c
            cursor = data.get("nextCursor")
            if not cursor:
                break
    out = []
    for c in raw.values():
        created = _parse_time(c.get("created_at"))
        if created and not (since <= created <= until):
            continue
        clip = _kick_clip(c)
        if clip:
            out.append(clip)
    return out


def kick_live(kick: "Kick", slug: str, pages: int = 5) -> list[dict]:
    """Who is live in a Kick category right now: [{slug, name, language, viewers}]."""
    out = []
    for page in range(1, pages + 1):
        try:
            data = kick._fetch(f"https://kick.com/stream/livestreams/en?page={page}&limit=24&subcategory={slug}&sort=desc")
        except Exception as e:  # noqa: BLE001
            log.warning("Kick live list failed: %s", e)
            break
        items = data.get("data") or []
        for it in items:
            ch = it.get("channel") or {}
            if not any((c or {}).get("slug") == slug for c in it.get("categories") or []):
                continue
            out.append({"slug": ch.get("slug", ""), "name": (ch.get("user") or {}).get("username") or ch.get("slug", ""),
                        "language": it.get("language") or "", "viewers": int(it.get("viewer_count") or 0)})
        if len(items) < 24:
            break
    return out


DEFAULT_LENGTH = {"max_seconds": 60, "ideal_seconds": 30, "long_penalty": 0.5}
LAST_STATS: list[dict] = []  # filled by collect(): per-streamer numbers for the daily report


def length_factor(duration: float, rules: dict | None = None) -> float | None:
    """How much a clip's views count, by length. None = too long (hard limit).
    Up to ideal_seconds: 1.0. Then linearly down to long_penalty at max_seconds
    (0.5 = a 60 s clip needs twice the views of a 30 s clip)."""
    r = {**DEFAULT_LENGTH, **(rules or {})}
    mx, ideal, pen = float(r["max_seconds"]), float(r["ideal_seconds"]), float(r["long_penalty"])
    d = float(duration or 0) or ideal  # unknown length: assume ideal, checked again after download
    if d > mx + 0.5:
        return None
    if d <= ideal:
        return 1.0
    return 1.0 - (1.0 - pen) * (d - ideal) / max(mx - ideal, 1.0)


def same_moment(a: Clip, b: Clip) -> bool:
    """Two clips of the same streamer that show the same moment (clipped by different viewers)."""
    if a.creator.strip().lower() != b.creator.strip().lower():
        return False
    if a.stream_id and a.stream_id == b.stream_id and a.offset is not None and b.offset is not None:
        da, db = a.duration or 30, b.duration or 30
        overlap = min(a.offset + da, b.offset + db) - max(a.offset, b.offset)
        return overlap > 0.3 * min(da, db)  # they share at least 30 % of the shorter clip
    ta, tb = _parse_time(a.created_at), _parse_time(b.created_at)
    if ta and tb and (not a.stream_id or not b.stream_id or a.stream_id == b.stream_id):
        return abs((ta - tb).total_seconds()) <= 90  # clipped within 90 s of each other
    return False


def drop_duplicates(clips: list[Clip], key=lambda c: c.views) -> tuple[list[Clip], int]:
    """Keep the best clip of every moment (by `key`), drop the other clips of the same moment."""
    kept: list[Clip] = []
    for c in sorted(clips, key=key, reverse=True):
        if not any(same_moment(c, k) for k in kept):
            kept.append(c)
    return kept, len(clips) - len(kept)


def collect(creators: list[dict], since: datetime, until: datetime, per_creator: int,
            length_rules: dict | None = None) -> list[Clip]:
    """Return up to `per_creator` best clips for every creator: views weighted by length, nothing over the limit."""
    twitch = kick = None
    result: list[Clip] = []
    LAST_STATS.clear()
    for cr in creators:
        platform = cr["platform"].lower()
        try:
            if platform == "twitch":
                twitch = twitch or Twitch()
                found = twitch.top_clips(cr["name"], cr["channel"], since, until)
            elif platform == "kick":
                kick = kick or Kick()
                found = kick.top_clips(cr["name"], cr["channel"], since, until)
            else:
                log.error("Unknown platform %r for %s", platform, cr["name"])
                continue
        except Exception as e:  # one broken creator must not stop the rest
            log.exception("Could not get clips for %s", cr["name"])
            LAST_STATS.append({"name": cr["name"], "platform": platform, "found": "ERROR", "too_long": "",
                               "best_views": "", "chosen": str(e)[:80]})
            continue
        found, dupes = drop_duplicates(found)
        if dupes:
            log.info("%s: %d clip(s) dropped as the same moment as a better clip", cr["name"], dupes)
        ok = [(c, length_factor(c.duration, length_rules)) for c in found]
        too_long = sum(1 for _, f in ok if f is None)
        ok = [(c, f) for c, f in ok if f is not None]
        ok.sort(key=lambda cf: cf[0].views * cf[1], reverse=True)
        top = [c for c, _ in ok[:per_creator]]
        LAST_STATS.append({"name": cr["name"], "platform": platform, "found": len(found), "too_long": too_long,
                           "best_views": max((c.views for c in found), default=0),
                           "chosen": ", ".join(f"{c.views} views / {c.duration:.0f} s" for c in top) or "-"})
        log.info("%s (%s): %d clips in window (%d too long), keeping %d %s", cr["name"], platform, len(found),
                 too_long, len(top), [f"{c.views}v/{c.duration:.0f}s" for c in top])
        result.extend(top)
    return result
