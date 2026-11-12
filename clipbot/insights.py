"""What YouTube Analytics says about how people find the channel.

Used ONLY to write better titles/descriptions/tags (search phrases people actually use, title styles that
worked). It never influences which clips are chosen, so the channel stays diverse.

Needs the GitHub secret YOUTUBE_ANALYTICS_TOKEN (scopes yt-analytics.readonly + youtube.readonly).
Without it, everything here quietly returns nothing.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)
CACHE = "insights.json"
TEXT = ""  # prompt snippet for the AI, set by load()

SOURCE_NAMES = {
    "YT_SEARCH": "YouTube vyhledávání", "SHORTS": "Shorts feed", "RELATED_VIDEO": "Doporučená videa",
    "SUBSCRIBER": "Odběratelé", "YT_CHANNEL": "Stránka kanálu", "EXT_URL": "Externí weby",
    "NO_LINK_OTHER": "Přímý / neznámý", "PLAYLIST": "Playlisty", "NOTIFICATION": "Upozornění",
    "YT_OTHER_PAGE": "Jiné stránky YouTube", "END_SCREEN": "Závěrečné obrazovky", "HASHTAGS": "Hashtagy",
    "BROWSE": "Procházení / domovská stránka", "ADVERTISING": "Reklama",
}


def _fetch(days: int = 28) -> dict:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None, refresh_token=os.environ["YOUTUBE_ANALYTICS_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"], client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/yt-analytics.readonly",
                "https://www.googleapis.com/auth/youtube.readonly"])
    ya = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days)
    base = dict(ids="channel==MINE", startDate=start.isoformat(), endDate=end.isoformat())

    def rows(**kw):
        return ya.reports().query(**base, **kw).execute().get("rows") or []

    sources = rows(metrics="views,estimatedMinutesWatched", dimensions="insightTrafficSourceType", sort="-views")
    terms = rows(metrics="views", dimensions="insightTrafficSourceDetail",
                 filters="insightTrafficSourceType==YT_SEARCH", sort="-views", maxResults=25)
    videos = rows(metrics="views,averageViewPercentage,averageViewDuration", dimensions="video",
                  sort="-views", maxResults=50)
    titles = {}
    ids = [v[0] for v in videos]
    for i in range(0, len(ids), 50):
        res = yt.videos().list(part="snippet,contentDetails", id=",".join(ids[i:i + 50])).execute()
        for it in res.get("items", []):
            titles[it["id"]] = it["snippet"]["title"]
    vids = [{"id": v[0], "title": titles.get(v[0], "?"), "views": int(v[1]),
             "avg_view_pct": round(float(v[2]), 1), "avg_view_s": int(v[3])} for v in videos]
    return {
        "fetched": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "days": days,
        "sources": [{"source": s[0], "views": int(s[1]), "minutes": int(s[2])} for s in sources],
        "search_terms": [{"term": t[0], "views": int(t[1])} for t in terms],
        "videos": vids,
    }


def _prompt_text(data: dict) -> str:
    if not data or not (data.get("search_terms") or data.get("videos")):
        return ""
    parts = []
    terms = [t["term"] for t in data.get("search_terms", [])[:15]]
    if terms:
        parts.append("People find this channel on YouTube by searching for: " + ", ".join(terms) + ".")
    vids = [v for v in data.get("videos", []) if v["views"] >= 20]
    if len(vids) >= 4:
        best = sorted(vids, key=lambda v: -v["views"])[:5]
        weak = sorted(vids, key=lambda v: v["views"])[:3]
        parts.append("Titles of our videos that got the MOST views: " + " | ".join(v["title"] for v in best) + ".")
        parts.append("Titles that got the FEWEST views: " + " | ".join(v["title"] for v in weak) + ".")
    if not parts:
        return ""
    return ("\nChannel analytics (last %d days) - use it to word things the way viewers search, but only when it "
            "genuinely fits this clip; never invent content or force unrelated keywords:\n" % data.get("days", 28)
            + "\n".join(parts) + "\n")


def load(drive) -> dict:
    """Fetch once a day (cached on Drive), set TEXT for the AI prompts, return the data for the report."""
    global TEXT
    if not os.environ.get("YOUTUBE_ANALYTICS_TOKEN"):
        TEXT = ""
        return {}
    cached = drive.read_json(CACHE, {}) if drive is not None else {}
    fresh = cached.get("fetched", "")[:10] == datetime.now(timezone.utc).date().isoformat()
    data = cached
    if not fresh:
        try:
            data = _fetch()
            if drive is not None:
                drive.write_json(CACHE, data)
            log.info("YouTube Analytics: %d traffic sources, %d search terms, %d videos.",
                     len(data["sources"]), len(data["search_terms"]), len(data["videos"]))
        except Exception as e:  # noqa: BLE001 - analytics must never break a run
            log.warning("YouTube Analytics not available (%s: %s) — titles are written without it.",
                        type(e).__name__, str(e)[:200])
            data = cached or {}
    TEXT = _prompt_text(data)
    return data


def report_tables(report, data: dict) -> None:
    if not data:
        return
    total = sum(s["views"] for s in data.get("sources", [])) or 1
    report.table(f"Where viewers came from (last {data.get('days', 28)} days)", ["Source", "Views", "Share"],
                 [[SOURCE_NAMES.get(s["source"], s["source"]), s["views"], f"{100 * s['views'] / total:.0f} %"]
                  for s in data.get("sources", [])])
    report.table("Top search phrases (YouTube search)", ["Phrase", "Views"],
                 [[t["term"], t["views"]] for t in data.get("search_terms", [])[:15]])
    report.table("Best videos", ["Title", "Views", "Watched (avg %)", "Avg watch time"],
                 [[v["title"], v["views"], v["avg_view_pct"], f"{v['avg_view_s']} s"]
                  for v in data.get("videos", [])[:10]])
