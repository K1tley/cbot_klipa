"""Turn a clip title into a YouTube Shorts title, description and hashtags."""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata

from .sources import Clip

log = logging.getLogger(__name__)

PROMPT = """You write titles for a YouTube Shorts channel that reposts the best Twitch/Kick clips of Czech and Slovak streamers.

Clip info:
- Streamer: {creator}
- Platform: {platform}
- Original clip title (written by a viewer, may be slang, a meme, a typo or just emojis): {title!r}
- Game / category: {game}

Write in {language}. Return ONLY a JSON object with:
- "title": a catchy Shorts title, max 70 characters. Stay faithful to the original clip title's meaning -
  rephrase or tidy it, but don't invent events that the title doesn't suggest. Include the streamer's name
  if it fits naturally. No hashtags in the title. Max one emoji.
- "line": one short sentence for the description that sets up the moment (same rule: don't invent facts).
- "hashtags": 5-8 hashtags (without the # sign), mixing: the streamer's name, the game (if any),
  broad reach tags (e.g. shorts, twitchclips or kickclips, gaming), and 1-2 {language} tags. No spaces inside a tag.
"""


def _slug_tag(text: str) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]", "", t)


def _fallback(clip: Clip) -> dict:
    title = clip.title or f"{clip.creator} clip"
    tags = [clip.creator, clip.game, "shorts", f"{clip.platform}clips", "gaming", "cesko", "czstreamer"]
    return {"title": f"{title} | {clip.creator}", "line": "",
            "hashtags": [t for t in (_slug_tag(x) for x in tags if x) if t]}


def generate(clip: Clip, language: str, model: str) -> dict:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _fallback(clip)
    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=model, max_tokens=400,
            messages=[{"role": "user", "content": PROMPT.format(
                creator=clip.creator, platform=clip.platform.capitalize(), title=clip.title,
                game=clip.game or "unknown", language=language)}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        data = json.loads(text[text.index("{"): text.rindex("}") + 1])
        data["hashtags"] = [h for h in (_slug_tag(str(t).lstrip("#")) for t in data.get("hashtags", [])) if h]
        if not data.get("title"):
            raise ValueError("empty title")
        return data
    except Exception:  # noqa: BLE001
        log.exception("Caption generation failed for %s, using fallback", clip.clip_id)
        return _fallback(clip)


def build_youtube_fields(clip: Clip, cap: dict) -> tuple[str, str, list[str]]:
    tags = cap["hashtags"]
    if "shorts" not in [t.lower() for t in tags]:
        tags = tags + ["shorts"]
    title = re.sub(r"[<>]", "", cap["title"]).strip()[:100]
    platform = "Twitch" if clip.platform == "twitch" else "Kick"
    desc_lines = [
        cap.get("line", "").strip(),
        "",
        f"🎥 Klip: {clip.creator} na {platform}",
        f"👉 Sleduj ho živě: {'https://twitch.tv/' if clip.platform == 'twitch' else 'https://kick.com/'}{clip.channel}",
        f"🔗 Originál: {clip.url}",
        "",
        " ".join(f"#{t}" for t in tags),
    ]
    description = "\n".join(desc_lines).strip()[:4900]
    # YouTube 'tags' field (separate from hashtags), 500 chars total
    yt_tags, total = [], 0
    for t in [clip.creator, clip.game, *tags]:
        if t and t.lower() not in {x.lower() for x in yt_tags} and total + len(t) + 1 <= 480:
            yt_tags.append(t)
            total += len(t) + 1
    return title, description, yt_tags
