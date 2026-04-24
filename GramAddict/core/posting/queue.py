"""Queue reader for the posting plugin.

Directory layout (owned by the generator side, e.g. moneymaker2):

    <queue_root>/<persona_key>/
        pending/   item_<id>.json + item_<id>.jpg|mp4 (+ carousel slides)
        posting/   items claimed by a running bot session
        posted/    terminal: successfully published
        failed/    terminal: give up (after retries exhausted or hard error)
        .lock      advisory fcntl lock, held only during the claim phase

Metadata JSON schema (strict keys; unknown keys preserved round-trip):

    {
        "id":            "2026-04-24T1015_0001",
        "persona":       "luna_voss",
        "post_type":     "photo|carousel|reel|story",
        "media":         ["item_<id>.jpg", "item_<id>_2.jpg"],
        "caption":       "…",
        "hashtags":      ["fitness", "miami"],
        "scheduled_at":  "2026-04-24T11:00:00Z",
        "priority":      0,
        "lora_version":  "luna_v3_2026-04-20",
        "source_prompt": "...",
        "nsfw_flag":     false,
        "cover_frame_ms": 800
    }

Ownership rules (non-negotiable):
  * The generator writes ONLY into pending/ and ONLY with os.replace (atomic).
  * instagrambot claims by os.rename'ing pending/* into posting/ under an
    exclusive .lock. Once in posting/ the item is owned by this session.
  * On success: move to posted/ with status/posted_at written back.
  * On failure: move to failed/ with status/failure_reason written back.
  * On crash mid-flight: item stays in posting/ — never auto-retry because a
    partial IG upload may have succeeded. Use `scripts/queue_inspect.py` to
    reconcile manually.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import logging
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional

logger = logging.getLogger(__name__)

_SUBDIRS = ("pending", "posting", "posted", "failed")
_ALLOWED_TYPES = {"photo", "carousel", "reel", "story"}


class QueueError(Exception):
    """Raised when the queue layout is invalid or an item cannot be read."""


@dataclass
class QueueItem:
    """A single queue entry (metadata JSON + media files)."""

    root: Path
    persona: str
    id: str
    data: dict
    json_path: Path
    media_paths: List[Path] = field(default_factory=list)

    @property
    def post_type(self) -> str:
        return self.data.get("post_type", "")

    @property
    def scheduled_at(self) -> Optional[_dt.datetime]:
        s = self.data.get("scheduled_at")
        if not s:
            return None
        try:
            return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None

    @property
    def priority(self) -> int:
        try:
            return int(self.data.get("priority", 0))
        except (TypeError, ValueError):
            return 0

    @property
    def is_ready(self) -> bool:
        when = self.scheduled_at
        if when is None:
            return True
        now = _dt.datetime.now(_dt.timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        return when <= now

    @property
    def caption(self) -> str:
        return self.data.get("caption", "")

    @property
    def hashtags(self) -> List[str]:
        return list(self.data.get("hashtags", []))

    def mark(self, status: str, extra: Optional[dict] = None) -> None:
        self.data["status"] = status
        self.data["status_updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        if extra:
            self.data.update(extra)
        _atomic_write_json(self.json_path, self.data)

    def _move_to(self, subdir: str) -> None:
        target_dir = self.root / self.persona / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        for p in [self.json_path, *self.media_paths]:
            if p.exists():
                dest = target_dir / p.name
                os.replace(str(p), str(dest))
                if p == self.json_path:
                    self.json_path = dest
        self.media_paths = [target_dir / p.name for p in self.media_paths]

    def mark_posted(self, permalink: Optional[str] = None) -> None:
        extra = {"posted_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}
        if permalink:
            extra["permalink"] = permalink
        self.mark("posted", extra)
        self._move_to("posted")
        logger.info(f"[queue] item {self.id} → posted/")

    def mark_failed(self, reason: str) -> None:
        self.mark("failed", {"failure_reason": reason})
        self._move_to("failed")
        logger.warning(f"[queue] item {self.id} → failed/ ({reason})")

    def release_back_to_pending(self) -> None:
        """Used by --dry-run: returns item to pending/."""
        self.mark("pending", {})
        self._move_to("pending")
        logger.info(f"[queue] item {self.id} → pending/ (dry-run release)")


class PostingQueue:
    """Scoped view of one persona's queue directory."""

    def __init__(self, queue_root: os.PathLike | str, persona: str):
        self.root = Path(queue_root).expanduser()
        self.persona = persona
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        base = self.root / self.persona
        base.mkdir(parents=True, exist_ok=True)
        for sub in _SUBDIRS:
            (base / sub).mkdir(exist_ok=True)
        lock = base / ".lock"
        if not lock.exists():
            lock.touch()

    @contextmanager
    def _lock(self):
        lock_path = self.root / self.persona / ".lock"
        with open(lock_path, "r+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def iter_pending(self, types_allowed: Optional[List[str]] = None) -> Iterator[QueueItem]:
        pending_dir = self.root / self.persona / "pending"
        for json_path in sorted(pending_dir.glob("*.json")):
            try:
                item = self._load_item(json_path, subdir="pending")
            except QueueError as exc:
                logger.warning(f"[queue] skip {json_path.name}: {exc}")
                continue
            if not item.is_ready:
                continue
            if types_allowed and item.post_type not in types_allowed:
                continue
            if item.post_type not in _ALLOWED_TYPES:
                logger.warning(f"[queue] {item.id} has unknown post_type={item.post_type!r}")
                continue
            yield item

    def claim_next(self, types_allowed: Optional[List[str]] = None) -> Optional[QueueItem]:
        """Atomically claim the next runnable item under lock."""
        with self._lock():
            candidates = list(self.iter_pending(types_allowed))
            if not candidates:
                return None
            candidates.sort(key=lambda i: (i.priority, i.scheduled_at or _dt.datetime.min, i.id))
            chosen = candidates[0]
            chosen._move_to("posting")
            chosen.mark("posting")
            logger.info(
                f"[queue] claimed {chosen.id} (type={chosen.post_type}, persona={self.persona})"
            )
            return chosen

    def _load_item(self, json_path: Path, subdir: str) -> QueueItem:
        try:
            with open(json_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise QueueError(f"cannot read {json_path}: {exc}") from exc

        item_id = data.get("id") or json_path.stem
        persona = data.get("persona") or self.persona

        if persona != self.persona:
            raise QueueError(f"persona mismatch: {persona} vs expected {self.persona}")

        media_dir = json_path.parent
        media_paths: List[Path] = []
        for name in data.get("media", []):
            p = media_dir / name
            if not p.exists():
                raise QueueError(f"missing media file {name} for item {item_id}")
            media_paths.append(p)

        if not media_paths:
            raise QueueError(f"no media files listed for item {item_id}")

        return QueueItem(
            root=self.root,
            persona=persona,
            id=item_id,
            data=data,
            json_path=json_path,
            media_paths=media_paths,
        )

    def counts(self) -> dict:
        base = self.root / self.persona
        return {
            sub: sum(1 for _ in (base / sub).glob("*.json"))
            for sub in _SUBDIRS
        }

    def list_items(self, subdir: str) -> List[QueueItem]:
        if subdir not in _SUBDIRS:
            raise QueueError(f"unknown subdir {subdir}")
        items: List[QueueItem] = []
        for json_path in sorted((self.root / self.persona / subdir).glob("*.json")):
            try:
                items.append(self._load_item(json_path, subdir))
            except QueueError as exc:
                logger.warning(f"[queue] skip {json_path.name}: {exc}")
        return items

    def reconcile_stale_posting(self, max_age_minutes: int = 120, dry_run: bool = True) -> List[str]:
        """Move 'posting/' items older than N minutes back to 'pending/'.

        Used by scripts/queue_inspect.py --reconcile. Default dry_run=True prints
        what would be moved; pass dry_run=False to actually do it. Caller MUST
        have verified no other bot session is mid-post.
        """
        now = _dt.datetime.now()
        moved: List[str] = []
        posting_dir = self.root / self.persona / "posting"
        for json_path in posting_dir.glob("*.json"):
            mtime = _dt.datetime.fromtimestamp(json_path.stat().st_mtime)
            age = (now - mtime).total_seconds() / 60.0
            if age < max_age_minutes:
                continue
            try:
                item = self._load_item(json_path, subdir="posting")
            except QueueError:
                continue
            if dry_run:
                logger.info(f"[queue] would reconcile {item.id} (age={age:.0f} min)")
            else:
                item.mark("pending", {"reconciled_from_posting_at": _dt.datetime.now(_dt.timezone.utc).isoformat()})
                item._move_to("pending")
                logger.info(f"[queue] reconciled {item.id} → pending/")
            moved.append(item.id)
        return moved


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(str(tmp), str(path))


def purge_old(queue_root: os.PathLike | str, persona: str, max_days: int = 30) -> int:
    """Delete posted/ and failed/ items older than N days. Returns files removed."""
    root = Path(queue_root).expanduser() / persona
    cutoff = _dt.datetime.now() - _dt.timedelta(days=max_days)
    removed = 0
    for sub in ("posted", "failed"):
        for f in (root / sub).iterdir():
            if f.is_file() and _dt.datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


def copy_example_item(queue_root: os.PathLike | str, persona: str, media_src: Path) -> str:
    """Testing helper: drop an example photo item into pending/."""
    queue = PostingQueue(queue_root, persona)
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    item_id = now_utc.strftime("%Y-%m-%dT%H%M_example")
    pending = queue.root / persona / "pending"
    media_dest = pending / f"{item_id}.jpg"
    shutil.copy(media_src, media_dest)
    meta = {
        "id": item_id,
        "persona": persona,
        "post_type": "photo",
        "media": [media_dest.name],
        "caption": "Example post — auto-generated",
        "hashtags": ["test", "example"],
        # scheduled_at must be a real UTC timestamp, not naive-local with a 'Z'
        # suffix — otherwise is_ready() treats it as ~2h in the future in CEST.
        "scheduled_at": now_utc.isoformat().replace("+00:00", "Z"),
        "priority": 0,
    }
    _atomic_write_json(pending / f"{item_id}.json", meta)
    return item_id
