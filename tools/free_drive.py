"""One-off: free Google Drive space.
CZ (ClipBot): delete uploaded videos except the top Short per streamer/day, folders older than 2 days.
GTA (ClipBotGTA): delete all compilation videos (never uploaded - no GTA token yet) and their queue items."""
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clipbot.__main__ import QUEUE, cleanup, empty_state, load_config, prune_uploaded  # noqa: E402
from clipbot.google_apis import Drive  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("free")


def quota(d):
    try:
        q = d.svc.about().get(fields="storageQuota").execute()["storageQuota"]
        gb = lambda k: f"{int(q.get(k, 0)) / 1e9:.1f} GB"
        log.info("Drive quota: used %s of %s (Drive files %s, trash %s)", gb("usage"), gb("limit"),
                 gb("usageInDrive"), gb("usageInDriveTrash"))
    except Exception as e:  # noqa: BLE001
        log.info("Quota not readable: %s", e)


cfg = load_config("config.yaml")
cz = Drive(cfg["drive_root_folder"])
quota(cz)
state = cz.read_json(QUEUE, empty_state())
prune_uploaded(cz, state)
cleanup(cfg, cz, state, datetime.now(ZoneInfo(cfg["timezone"])).date())
cz.write_json(QUEUE, state)

g = Drive(load_config("config_gta.yaml")["drive_root_folder"])
for f in g.children(g.root, folders_only=True):
    if f["name"] == "Compilations":
        log.info("Deleting ClipBotGTA/Compilations")
        g.delete(f["id"])
gs = g.read_json(QUEUE, empty_state())
before = len(gs["items"])
gs["items"] = [i for i in gs["items"] if i.get("status") == "posted"]
g.write_json(QUEUE, gs)
log.info("GTA queue: removed %d unuploaded videos", before - len(gs["items"]))
quota(cz)
