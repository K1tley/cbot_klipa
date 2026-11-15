"""Turn a clip title into a YouTube Shorts title, description and hashtags."""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata

from . import insights
from .sources import Clip

log = logging.getLogger(__name__)

PROMPT = """You write titles for a YouTube Shorts channel that reposts the best Twitch/Kick clips of Czech and Slovak streamers.

Clip info:
- Streamer: {creator}
- Platform: {platform}
- Original clip title (written by a random viewer - may be slang, a meme, a typo, an inside joke or just gibberish): {title!r}
- Game / category: {game}

Write in natural, fluent, grammatically correct {language}, the way a Czech/Slovak gaming fan would write
(casual is fine, slang is fine). Proofread names: the streamer is "{creator}" - spell it exactly like that.
Return ONLY a JSON object with:
- "title": a catchy Shorts title, max 70 characters. Stay faithful to the original clip title's meaning -
  rephrase or tidy it, but don't invent events that the title doesn't suggest. Include the streamer's name
  if it fits naturally. No hashtags in the title. Max one emoji. Never put a non-game stream category
  ("Just Chatting", IRL etc.) in the title - in the hashtags it is fine.
- "line": one short, natural sentence for the description that adds something the title doesn't
  (context, a reaction, a question to viewers). If you have nothing meaningful to add, return "".
  Never write filler like "Clip from the stream of X playing Y".
- "hashtags": 5-8 hashtags (without the # sign), mixing: the streamer's name, the game (if any),
  broad reach tags (e.g. shorts, twitchclips or kickclips, gaming), and 1-2 {language} tags. No spaces inside a tag.

Safety rules (YouTube will penalise the channel otherwise):
- Never repeat slurs, hate toward any group (ethnicity, religion, race, sexuality), sexual content or insults
  aimed at a real person. If the original title has any of that, drop it and write a neutral, fun title
  about the streamer and the game instead.
- If the original title is gibberish (e.g. "dsadas", "xd", "wowcs02"), write a neutral title from the
  streamer and game without inventing what happens.
"""

# Words that must never reach YouTube when the AI step is unavailable (compared without accents, lowercase).
BLOCKED = {
    "nword", "nigga", "nigger", "negr", "negri", "cigan", "cikan", "cigani", "cikani", "zid", "zide", "zidi",
    "zidak", "buzna", "buzerant", "teplous", "retard", "rasa", "sex", "sexy", "porn", "porno", "naha", "nahy",
    "kurva", "pica", "pici", "kokot", "debil", "hitler", "nazi",
}


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _slug_tag(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", _ascii(text))


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _ascii(text).lower())


def is_unsafe(text: str) -> bool:
    words = _words(text)
    joined = "".join(words)
    return any(w in BLOCKED for w in words) or "nword" in joined or "nigg" in joined


# Twitch/Kick categories that are not a game - never mentioned in titles, descriptions or tags.
NON_GAME = {"justchatting", "irl", "chatting", "talkshowspodcasts", "talkshows", "podcasts", "pools hottubsandbeaches",
            "poolshottubsandbeaches", "music", "art", "asmr", "travel outdoors", "travelandoutdoors", "travel",
            "slots", "slotscasino", "sports", "foodanddrink", "makingcrafting", "specialevents", "scienceandtechnology"}
_CATEGORY_RE = re.compile(r"\s*(?:\b(?:v|ve|na|pri|při|u|in|during)\s+)?just\s*chatting\b", re.I)


def game_of(clip: Clip) -> str:
    """The clip's game, or "" when the category is not a game (Just Chatting, IRL, ...)."""
    g = (clip.game or "").strip()
    return "" if re.sub(r"[^a-z]", "", _ascii(g).lower()) in {re.sub(r"[^a-z]", "", x) for x in NON_GAME} else g


def strip_category(text: str) -> str:
    """Remove any mention of 'Just Chatting' that slipped through (e.g. 'Agraelus v Just Chatting: ...')."""
    out = _CATEGORY_RE.sub(" ", text or "")
    out = re.sub(r"\s+([:,!?.])", r"\1", out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return re.sub(r"^[\s:–\-|,]+", "", out).strip()


def _is_gibberish(text: str) -> bool:
    real = [w for w in _words(text) if len(w) >= 3 and re.search(r"[aeiouy]", w)]
    return len(real) < 2


def _fallback(clip: Clip) -> dict:
    title = (clip.title or "").strip()
    if not title or is_unsafe(title) or _is_gibberish(title):
        title = f"{clip.creator} – {game_of(clip)}" if game_of(clip) else f"{clip.creator} – moment ze streamu"
    else:
        title = f"{title} | {clip.creator}"
    tags = [clip.creator, clip.game, "shorts", f"{clip.platform}clips", "gaming", "cesko", "czstreamer"]
    return {"title": title, "line": "", "ai": False,
            "hashtags": [t for t in (_slug_tag(x) for x in tags if x) if t]}


def generate(clip: Clip, language: str, model: str) -> dict:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _fallback(clip)
    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=model, max_tokens=2000,
            system="You are a caption writer. Reply with one JSON object and nothing else - no prose, no code fences.",
            messages=[{"role": "user", "content": PROMPT.format(
                creator=clip.creator, platform=clip.platform.capitalize(), title=clip.title,
                game=clip.game or "unknown", language=language) + insights.TEXT}],
        )
        text = "".join(getattr(b, "text", "") or "" for b in msg.content if getattr(b, "type", "") == "text")
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            log.warning("No JSON in caption reply for %s (stop_reason=%s, blocks=%s): %r",
                        clip.clip_id, msg.stop_reason, [getattr(b, "type", "?") for b in msg.content], text[:300])
            return _fallback(clip)
        data = json.loads(text[start: end + 1])
        data["hashtags"] = [h for h in (_slug_tag(str(t).lstrip("#")) for t in data.get("hashtags", [])) if h]
        data["title"] = strip_category(str(data.get("title", "")))
        if not data.get("title"):
            raise ValueError("empty title")
        if is_unsafe(data["title"]) or is_unsafe(data.get("line", "")):
            log.warning("AI caption for %s still contained a blocked word, using neutral title", clip.clip_id)
            return _fallback(Clip(**{**clip.__dict__, "title": ""}))
        data["ai"] = True
        return data
    except Exception as e:  # noqa: BLE001
        log.warning("Caption generation failed for %s (%s: %s), using fallback", clip.clip_id, type(e).__name__, e)
        return _fallback(clip)


def build_youtube_fields(clip: Clip, cap: dict) -> tuple[str, str, list[str]]:
    tags = cap["hashtags"]
    if "shorts" not in [t.lower() for t in tags]:
        tags = tags + ["shorts"]
    title = re.sub(r"[<>]", "", strip_category(cap["title"])).strip()[:100]
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
