"""Caption rendering: spintax expansion + hashtag rotation with history.

Spintax: ``{hey|hi|yo}`` → one of "hey" / "hi" / "yo". Nested supported.

Hashtag history is kept per account at ``<storage_path>/hashtag_history.json``
so we can penalize reuse within a configurable cooldown window (48h default)
without tying it to Instagram's own state.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import random
import re
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_SPINTAX_RE = re.compile(r"\{([^{}]*)\}")
_DEFAULT_COOLDOWN_H = 48


def expand_spintax(text: str, rng: Optional[random.Random] = None) -> str:
    """Recursively replace {a|b|c} groups with one of their options."""
    rng = rng or random
    while True:
        m = _SPINTAX_RE.search(text)
        if not m:
            break
        options = m.group(1).split("|")
        replacement = rng.choice(options) if options else ""
        text = text[: m.start()] + replacement + text[m.end() :]
    return text


def pick_hashtags(
    pool: List[str],
    n: int,
    history_path: Optional[Path] = None,
    cooldown_hours: int = _DEFAULT_COOLDOWN_H,
    rng: Optional[random.Random] = None,
) -> List[str]:
    """Select `n` hashtags from pool, preferring ones unused in the last N hours.

    Also persists the picks to history_path (if provided) with current timestamp.
    """
    rng = rng or random
    pool = [h.lstrip("#").lower() for h in pool if h.strip()]
    if not pool:
        return []

    history = _load_history(history_path) if history_path else {}
    cutoff = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) - _dt.timedelta(hours=cooldown_hours)

    fresh: List[str] = []
    stale: List[str] = []
    for h in pool:
        ts = history.get(h)
        if ts:
            try:
                last = _dt.datetime.fromisoformat(ts)
            except ValueError:
                last = _dt.datetime.min
        else:
            last = _dt.datetime.min
        (fresh if last < cutoff else stale).append(h)

    rng.shuffle(fresh)
    rng.shuffle(stale)
    chosen = (fresh + stale)[: max(0, min(n, len(pool)))]

    now_iso = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat()
    for h in chosen:
        history[h] = now_iso
    if history_path:
        _save_history(history_path, history)

    return chosen


def render_caption(
    template: str,
    hashtag_pool: Optional[List[str]] = None,
    hashtag_count: int = 10,
    history_path: Optional[Path] = None,
    rng: Optional[random.Random] = None,
) -> str:
    """Expand a spintax template and append hashtags."""
    body = expand_spintax(template or "", rng=rng)
    if hashtag_pool:
        tags = pick_hashtags(
            hashtag_pool,
            hashtag_count,
            history_path=history_path,
            rng=rng,
        )
        if tags:
            body = body.rstrip() + "\n\n" + " ".join(f"#{t}" for t in tags)
    return body


def _load_history(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, json.JSONDecodeError):
        logger.warning(f"[caption] could not read hashtag history at {path}; starting fresh")
        return {}


def _save_history(path: Path, history: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(history, fh, indent=2, ensure_ascii=False)
        import os as _os
        _os.replace(str(tmp), str(path))
    except OSError as exc:
        logger.warning(f"[caption] could not save hashtag history at {path}: {exc}")
