"""One-off: print the shape of Kick category clips and English livestreams."""
import json
from curl_cffi import requests as cffi
k = cffi.Session(impersonate="chrome")
k.headers.update({"Accept": "application/json", "Referer": "https://kick.com/"})
def j(u):
    r = k.get(u, timeout=30); print(r.status_code, u); return r.json()
d = j("https://kick.com/api/v2/categories/grand-theft-auto-vi/clips?sort=view&time=day")
print("keys:", list(d.keys()))
cl = d.get("clips") or []
print("n clips:", len(cl), "nextCursor:", d.get("nextCursor"))
print(json.dumps(cl[0], indent=1)[:3000] if cl else "-")
for c in cl[:25]:
    ch = c.get("channel") or {}
    print(c.get("view_count"), c.get("duration"), c.get("created_at"), ch.get("slug"), ch.get("username"), (c.get("title") or "")[:50])
if d.get("nextCursor"):
    d2 = j(f"https://kick.com/api/v2/categories/grand-theft-auto-vi/clips?sort=view&time=day&cursor={d['nextCursor']}")
    print("page2:", len(d2.get("clips") or []), d2.get("nextCursor"))
d = j("https://kick.com/api/v2/categories/grand-theft-auto-vi/clips?sort=view&time=week")
print("week:", len(d.get("clips") or []))
d = j("https://kick.com/stream/livestreams/en?page=1&limit=24&subcategory=grand-theft-auto-vi&sort=desc")
items = d.get("data") or []
print("live keys:", list(items[0].keys()) if items else "-", "last_page", d.get("last_page"), "total", d.get("total"))
print(json.dumps(items[0], indent=1)[:2500] if items else "-")
for it in items[:24]:
    ch = it.get("channel") or {}
    cats = [c.get("name") for c in it.get("categories") or []]
    print(it.get("viewer_count"), it.get("language"), ch.get("slug"), cats, (it.get("session_title") or "")[:40])
d = j("https://kick.com/stream/livestreams/en?page=1&limit=24&subcategory=grand-theft-auto-v&sort=desc")
for it in (d.get("data") or [])[:10]:
    ch = it.get("channel") or {}
    print(it.get("viewer_count"), it.get("language"), ch.get("slug"), [c.get("name") for c in it.get("categories") or []])
for s in ["lordkebun", "lord_kebun", "kebun", "penta", "pentapenguin", "burn", "zerkaa"]:
    try:
        x = j(f"https://kick.com/api/v2/channels/{s}"); print("  ", s, x.get("followers_count"))
    except Exception as e:
        print("  ", s, e)
