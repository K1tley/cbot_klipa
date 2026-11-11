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
                clips.append(Clip(
                    platform="twitch", clip_id=c["id"], creator=creator, channel=channel,
                    title=c.get("title", "").strip(), url=c["url"], views=int(c.get("view_count", 0)),
                    created_at=c.get("created_at", ""), game=self._game_name(c.get("game_id", "")),
                    duration=float(c.get("duration") or 0),
                ))
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
            ))
        log.debug("Kick %s: %d clips seen, %d in window", channel, len(raw), len(clips))
        return clips


DEFAULT_LENGTH = {"max_seconds": 60, "ideal_seconds": 30, "long_penalty": 0.5}


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


def collect(creators: list[dict], since: datetime, until: datetime, per_creator: int,
            length_rules: dict | None = None) -> list[Clip]:
    """Return up to `per_creator` best clips for every creator: views weighted by length, nothing over the limit."""
    twitch = kick = None
    result: list[Clip] = []
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
        except Exception:  # one broken creator must not stop the rest
            log.exception("Could not get clips for %s", cr["name"])
            continue
        ok = [(c, length_factor(c.duration, length_rules)) for c in found]
        too_long = sum(1 for _, f in ok if f is None)
        ok = [(c, f) for c, f in ok if f is not None]
        ok.sort(key=lambda cf: cf[0].views * cf[1], reverse=True)
        top = [c for c, _ in ok[:per_creator]]
        log.info("%s (%s): %d clips in window (%d too long), keeping %d %s", cr["name"], platform, len(found),
                 too_long, len(top), [f"{c.views}v/{c.duration:.0f}s" for c in top])
        result.extend(top)
    return result
