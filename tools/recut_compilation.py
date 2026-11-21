"""Re-cut an uploaded compilation without some clips (e.g. nudity) and upload it again as private,
with the same episode number. The old video has to be deleted by hand in YouTube Studio.

    python tools/recut_compilation.py <queue key> <positions to drop, 1-based, comma separated>
    e.g. python tools/recut_compilation.py compilation:2026-11-20 3,7,8,15,21,25,27
"""
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clipbot import compilation as C, sources  # noqa: E402
from clipbot.__main__ import QUEUE, cmd_post, empty_state, load_config  # noqa: E402
from clipbot.google_apis import Drive  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("recut")

key, drop = sys.argv[1], {int(x) for x in sys.argv[2].split(",") if x.strip()}
cfg = load_config()
drive = Drive(cfg["drive_root_folder"])
state = drive.read_json(QUEUE, empty_state())
old = next(i for i in state["items"] if i.get("key") == key)
log.info("Re-cutting %s (#%s, %d clips), dropping positions %s", key, old.get("episode"), len(old["clips"]), sorted(drop))
picks = []
for n, c in enumerate(old["clips"], 1):
    if n in drop:
        log.info("  drop %2d. %s  %s", n, c["creator"], c.get("title", "")[:60])
        continue
    clip = sources.Clip(platform=c["platform"], clip_id=str(c["id"]), creator=c["creator"], channel=c.get("channel", ""),
                        title=c.get("title", ""), url=c["url"], views=int(c.get("views") or 0),
                        created_at=c.get("created_at") or "", duration=float(c.get("duration") or 30),
                        stream_id=c.get("stream_id") or "", offset=c.get("offset"))
    picks.append(C.Pick(clip=clip, score=float(c.get("score") or 1), duration=float(c.get("duration") or 30)))
C.LAST.clear()
C.LAST.update(channels=[], order=[], candidates=len(picks), note="")
with tempfile.TemporaryDirectory() as tmp:
    def meta(seq):
        m = C.metadata(seq, cfg, old["date"])
        m["title"] = old.get("base_title") or m["title"]
        return m
    new = C.produce(cfg, drive, picks, Path(tmp), old["date"], label=old["date"] + "-recut", meta_fn=meta,
                    folder=("Compilations", old["date"]), key=key + ":recut")
if not new:
    sys.exit("Re-cut failed (not enough clips downloaded)")
new["replaces_episode"] = old.get("episode")
state = drive.read_json(QUEUE, empty_state())
state["items"] = [i for i in state["items"] if i.get("key") != new["key"]] + [new]
drive.write_json(QUEUE, state)
cmd_post(cfg, everything=True)
