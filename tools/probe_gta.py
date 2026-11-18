"""One-off probe: which GTA streamers exist on Twitch/Kick, are they playing GTA, and which Kick
category endpoints work. Prints a plain report to the Actions log. Safe to delete later."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import requests
from curl_cffi import requests as cffi

GROUPS = {
    "CORE": ["stableronaldo", "jasontheween", "lacy", "marlon", "silky", "adapt", "clix"],
    "AMP": ["kaicenat", "fanum", "dukedennis", "agent00", "imdavisss", "chrisnxtdoor"],
    "KICK": ["adinross", "n3on", "sweatergxd", "xqc", "trainwreckstv", "cuffem"],
    "RP": ["buddha", "lord_kebun", "dasmehdi", "penta", "kaysan", "burn", "willneff", "zerkaa", "nmplol"],
    "OTK": ["zackrawrr", "esfandtv", "mizkif", "emiru", "cyr", "tectone", "sodapoppin"],
    "VARIETY": ["summit1g", "lirik", "shroud", "caseoh_", "jynxzi", "hasanabi", "ludwig", "jasontheween"],
}
now = datetime.now(timezone.utc)
since = now - timedelta(hours=48)
Z = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")

tok = requests.post("https://id.twitch.tv/oauth2/token", data={
    "client_id": os.environ["TWITCH_CLIENT_ID"], "client_secret": os.environ["TWITCH_CLIENT_SECRET"],
    "grant_type": "client_credentials"}, timeout=30).json()["access_token"]
H = {"Client-Id": os.environ["TWITCH_CLIENT_ID"], "Authorization": f"Bearer {tok}"}
tw = lambda p, **q: requests.get(f"https://api.twitch.tv/helix/{p}", headers=H, params=q, timeout=30).json()

print("=== Twitch games")
games = {}
for name in ["Grand Theft Auto V", "Grand Theft Auto VI"]:
    d = tw("games", name=name).get("data", [])
    print(name, d)
    if d:
        games[d[0]["id"]] = name
for gid, name in games.items():
    d = tw("clips", game_id=gid, started_at=Z(now - timedelta(hours=24)), ended_at=Z(now), first=40).get("data", [])
    print(f"--- top {name} clips 24h: {len(d)}")
    for c in d[:40]:
        print(f"   {c['view_count']:>7}  {c['language']:<5} {c['broadcaster_name']:<20} {c['duration']:>4}s  {c['title'][:60]}")
    s = tw("streams", game_id=gid, first=30).get("data", [])
    print(f"--- live {name}: " + ", ".join(f"{x['user_login']}({x['viewer_count']},{x['language']})" for x in s))

allu = sorted({u for g in GROUPS.values() for u in g})
users = {}
for i in range(0, len(allu), 100):
    r = requests.get("https://api.twitch.tv/helix/users", headers=H, params=[("login", u) for u in allu[i:i + 100]], timeout=30).json()
    users.update({u["login"]: u for u in r.get("data", [])})
print("\n=== Twitch members")
for g, mem in GROUPS.items():
    for m in mem:
        u = users.get(m)
        if not u:
            print(f"{g:<8} {m:<16} NOT ON TWITCH")
            continue
        fol = tw("channels/followers", broadcaster_id=u["id"]).get("total")
        cl = tw("clips", broadcaster_id=u["id"], started_at=Z(since), ended_at=Z(now), first=100).get("data", [])
        gta = [c for c in cl if c.get("game_id") in games]
        print(f"{g:<8} {m:<16} followers {fol}  clips48h {len(cl):>3}  gta {len(gta):>3}  best {max([c['view_count'] for c in gta], default=0)}")

print("\n=== Kick members")
k = cffi.Session(impersonate="chrome")
k.headers.update({"Accept": "application/json", "Referer": "https://kick.com/"})
def kj(url):
    try:
        r = k.get(url, timeout=30)
        return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:200])
    except Exception as e:  # noqa: BLE001
        return -1, str(e)[:200]
for g, mem in GROUPS.items():
    for m in mem:
        st, ch = kj(f"https://kick.com/api/v2/channels/{m}")
        if st != 200 or not isinstance(ch, dict):
            print(f"{g:<8} {m:<16} kick HTTP {st}")
            continue
        fol = ch.get("followers_count") or ch.get("followersCount")
        st2, cl = kj(f"https://kick.com/api/v2/channels/{m}/clips?sort=view&time=week")
        clips = (cl.get("clips") if isinstance(cl, dict) else None) or []
        cats = {}
        for c in clips:
            n = (c.get("category") or {}).get("name", "?")
            cats[n] = cats.get(n, 0) + 1
        live = (ch.get("livestream") or {})
        lcat = ((live.get("categories") or [{}])[0] or {}).get("name") if live else None
        print(f"{g:<8} {m:<16} kick followers {fol} live={bool(live)} {lcat or ''}  clips(week) {len(clips)} {cats}")

print("\n=== Kick category endpoints")
for slug in ["grand-theft-auto-vi", "gta-vi", "grand-theft-auto-6", "gta-6", "grand-theft-auto-v", "gta-v"]:
    for url in [f"https://kick.com/api/v1/subcategories/{slug}",
                f"https://kick.com/api/v2/categories/{slug}/clips?sort=view&time=day",
                f"https://kick.com/api/v2/subcategories/{slug}/clips?sort=view&time=day",
                f"https://kick.com/api/v1/subcategories/{slug}/clips?sort=view&time=day",
                f"https://kick.com/stream/livestreams/en?page=1&limit=24&subcategory={slug}&sort=desc"]:
        st, body = kj(url)
        txt = json.dumps(body)[:300] if not isinstance(body, str) else body[:200]
        print(f"{st:>4} {url}\n      {txt}")
for url in ["https://kick.com/api/v1/subcategories?limit=30&page=1", "https://kick.com/api/v2/clips?sort=view&time=day",
            "https://web.kick.com/api/v1/livestreams?limit=24&sort=viewer_count_desc",
            "https://kick.com/api/v1/categories/top"]:
    st, body = kj(url)
    print(f"{st:>4} {url}\n      {(json.dumps(body) if not isinstance(body, str) else body)[:600]}")
