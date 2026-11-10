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

    def top_clips(self, creator: str, channel: str, since: datetime, until: datetime) -> list[Clip]:
        users = self._get("users", login=channel).get("data", [])
        if not users:
            log.warning("Twitch channel %s not found", channel)
            return []
        clips: list[Clip] = []
        cursor = None
        while True:
            params = dict(broadcaster_id=users[0]["id"], first=100,
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
            if not cursor or len(clips) >= 500:
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
        urls = [
            f"https://kick.com/api/v2/channels/{channel}/clips?sort=view&time=day",
            f"https://kick.com/api/v2/channels/{channel}/clips?sort=date&time=day",
        ]
        raw: list[dict] = []
        for url in urls:
            try:
                data = self._fetch(url)
            except Exception as e:  # noqa: BLE001
                log.warning("Kick request failed (%s): %s", url, e)
                continue
            raw = data.get("clips") or data.get("data") or (data if isinstance(data, list) else [])
            if raw:
                break

        clips: list[Clip] = []
        for c in raw:
            created = _parse_time(c.get("created_at") or c.get("started_at"))
            if created and not (since <= created <= until):
                continue
            cid = str(c.get("id") or c.get("clip_id") or "")
            if not cid:
                continue
            cat = c.get("category") or {}
            media = [u for u in (c.get("video_url"), c.get("clip_url")) if u]
            clips.append(Clip(
                platform="kick", clip_id=cid, creator=creator, channel=channel,
                title=(c.get("title") or "").strip(),
                url=f"https://kick.com/{channel}/clips/{cid}",
                views=int(c.get("view_count") or c.get("views") or 0),
                created_at=c.get("created_at") or "",
                game=cat.get("name", "") if isinstance(cat, dict) else "",
                duration=float(c.get("duration") or 0),
                media_urls=media,
            ))
        return clips


def collect(creators: list[dict], since: datetime, until: datetime, per_creator: int) -> list[Clip]:
    """Return up to `per_creator` most-viewed clips for every creator."""
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
        found.sort(key=lambda c: c.views, reverse=True)
        top = found[:per_creator]
        log.info("%s (%s): %d clips in window, keeping %d", cr["name"], platform, len(found), len(top))
        result.extend(top)
    return result
