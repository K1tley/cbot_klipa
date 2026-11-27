"""One-off: items that failed only because of YouTube's upload limit go back to 'pending' with 0 attempts."""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clipbot.__main__ import QUEUE, empty_state, load_config  # noqa: E402
from clipbot.google_apis import Drive  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
cfg = load_config()
d = Drive(cfg["drive_root_folder"])
st = d.read_json(QUEUE, empty_state())
n = 0
for i in st["items"]:
    if i.get("status") in ("pending", "failed") and i.get("drive_file_id") and not i.get("video_pruned"):
        if i["status"] == "failed" or i.get("attempts"):
            i.update(status="pending", attempts=0)
            n += 1
d.write_json(QUEUE, st)
pend = [i for i in st["items"] if i.get("status") == "pending"]
logging.info("Reset %d item(s). Waiting for upload: %d (%d compilation)", n, len(pend),
             sum(i.get("kind") == "long" for i in pend))
for i in pend:
    logging.info("  %s  %s", i.get("kind", "short"), i.get("yt_title", "")[:80])
