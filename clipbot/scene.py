"""Daily log of the top CZ/SK Twitch clips ("the scene").

Every compilation run writes the day's top N clips (by views) of all scanned CZ/SK Twitch channels
to scene_top10.json on Drive and to two Google Sheets in Drive/<root>/Stats:
  - "CZSK top 10 - log"       every logged clip, newest day first
  - "CZSK top 10 - streameri" one row per streamer: how many logs, usual views, best clip

A clip is "abnormal" when its views are far above the other top clips of that day, or far above
what that streamer's clips usually get in this log. A streamer outside the chosen creators may only
appear in the compilation with an abnormal clip once they have at least `min_logs` entries here.
"""
from __future__ import annotations

import csv
import logging
import statistics
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger(__name__)
LOG = "scene_top10.json"


def _settings(comp: dict) -> dict:
    s = comp.get("scene") or {}
    return {
        "top": int(s.get("top_clips", 10)),
        "min_logs": int(s.get("min_logs", 5)),
        "vs_top": float(s.get("abnormal_vs_top10", 3.0)),
        "vs_person": float(s.get("abnormal_vs_person", 4.0)),
        "person_min_history": int(s.get("person_min_history", 3)),
        "keep_days": int(s.get("keep_days", 120)),
    }


def update(drive, comp: dict, day_label: str, clips: list, tmp: Path) -> dict:
    """clips: sources.Clip objects from CZ/SK Twitch (duplicates of the same moment already removed).
    Returns {"entries": [...], "logs": {channel: count incl. today}, "by_clip": {clip_id: entry}}."""
    st = _settings(comp)
    data = drive.read_json(LOG, {"days": {}})
    days = data.setdefault("days", {})
    days.pop(day_label, None)  # a re-run of the same day replaces that day
    prior: dict[str, list[int]] = {}
    for d in sorted(days):
        for e in days[d]:
            prior.setdefault(e["channel"], []).append(int(e["views"]))

    top = sorted(clips, key=lambda c: -c.views)[: st["top"]]
    entries = []
    for rank, c in enumerate(top, 1):
        others = [x.views for x in top if x is not c]
        med_top = statistics.median(others) if others else 0
        vs_top = c.views / med_top if med_top else None
        hist = prior.get(c.channel.lower(), [])
        vs_person = c.views / statistics.median(hist) if len(hist) >= st["person_min_history"] else None
        reasons = []
        if vs_top is not None and vs_top >= st["vs_top"]:
            reasons.append(f"{vs_top:.1f}x the other top clips")
        if vs_person is not None and vs_person >= st["vs_person"]:
            reasons.append(f"{vs_person:.1f}x this streamer's usual")
        entries.append({
            "date": day_label, "rank": rank, "channel": c.channel.lower(), "name": c.creator,
            "views": int(c.views), "seconds": round(float(c.duration or 0)), "title": c.title,
            "url": c.url, "clip_id": c.clip_id,
            "vs_top10": round(vs_top, 2) if vs_top is not None else None,
            "vs_person": round(vs_person, 2) if vs_person is not None else None,
            "abnormal": bool(reasons), "why": "; ".join(reasons),
            "logs": len(hist) + 1,  # including this one
        })
    days[day_label] = entries
    cutoff = (date.fromisoformat(day_label) - timedelta(days=st["keep_days"])).isoformat()
    for d in [d for d in days if d < cutoff]:
        days.pop(d)
    drive.write_json(LOG, data)

    counts: dict[str, int] = {}
    for d in days.values():
        for e in d:
            counts[e["channel"]] = counts.get(e["channel"], 0) + 1
    for e in entries:
        e["logs"] = counts.get(e["channel"], 0)
    try:
        _write_sheets(drive, days, counts, tmp)
    except Exception as e:  # noqa: BLE001 - the stats sheet must never stop the compilation
        log.warning("Could not update the scene stats sheets: %s", e)
    n_abn = sum(e["abnormal"] for e in entries)
    log.info("Scene log: %d top clips logged for %s (%d abnormal), %d streamers in the log.",
             len(entries), day_label, n_abn, len(counts))
    return {"entries": entries, "logs": counts, "by_clip": {e["clip_id"]: e for e in entries},
            "min_logs": st["min_logs"]}


def _write_sheets(drive, days: dict, counts: dict, tmp: Path) -> None:
    folder = drive.folder_path("Stats")
    rows = [e for d in sorted(days, reverse=True) for e in days[d]]
    p1 = tmp / "scene_log.csv"
    with p1.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Datum", "Pořadí", "Streamer", "Kanál", "Zhlédnutí", "Délka (s)", "x vs. top 10",
                    "x vs. jeho obvyklé", "Abnormální", "Proč", "Počet logů", "Název klipu", "Odkaz"])
        for e in rows:
            w.writerow([e["date"], e["rank"], e["name"], e["channel"], e["views"], e["seconds"],
                        e["vs_top10"] if e["vs_top10"] is not None else "",
                        e["vs_person"] if e["vs_person"] is not None else "",
                        "ANO" if e["abnormal"] else "", e["why"], e.get("logs", ""), e["title"], e["url"]])
    drive.upload_sheet(p1, "CZSK top 10 - log", folder)

    per: dict[str, list[dict]] = {}
    for e in rows:
        per.setdefault(e["channel"], []).append(e)
    p2 = tmp / "scene_streamers.csv"
    with p2.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Streamer", "Kanál", "Počet logů", "Medián zhlédnutí", "Nejlepší zhlédnutí",
                    "Abnormálních klipů", "Poprvé", "Naposledy", "Nejlepší klip"])
        for ch, es in sorted(per.items(), key=lambda kv: -counts.get(kv[0], 0)):
            best = max(es, key=lambda e: e["views"])
            w.writerow([es[0]["name"], ch, counts.get(ch, 0), statistics.median(e["views"] for e in es),
                        best["views"], sum(e["abnormal"] for e in es), min(e["date"] for e in es),
                        max(e["date"] for e in es), best["url"]])
    drive.upload_sheet(p2, "CZSK top 10 - streameri", folder)
