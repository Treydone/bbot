"""DM outbox reader (consumer side).

Mirror of moneymaker2/back/app/storage/dm_outbox.py. The back is the writer,
this module is the reader. Same directory convention:

    <queue_root>/<persona>/dm_outbox/
        pending/   dm_<id>.json (+ attachments)
        sending/
        sent/
        failed/
        .dm_lock

The layout is intentionally split from the posting queue so posting and DM
sending can run in separate sessions without stepping on each other.
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional

logger = logging.getLogger(__name__)

_SUBDIRS = ("pending", "sending", "sent", "failed")


class DmOutboxError(Exception):
    pass


@dataclass
class DmItem:
    root: Path
    persona: str
    id: str
    data: dict
    json_path: Path
    attachment_paths: List[Path] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.data.get("text", "")

    @property
    def recipient_username(self) -> Optional[str]:
        r = self.data.get("recipient") or {}
        return r.get("username")

    @property
    def recipient_user_id(self) -> Optional[str]:
        r = self.data.get("recipient") or {}
        return r.get("user_id")

    def mark(self, status: str, extra: Optional[dict] = None) -> None:
        self.data["status"] = status
        self.data["status_updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        if extra:
            self.data.update(extra)
        _atomic_write_json(self.json_path, self.data)

    def _move_to(self, subdir: str) -> None:
        target = self.root / self.persona / "dm_outbox" / subdir
        target.mkdir(parents=True, exist_ok=True)
        for p in [self.json_path, *self.attachment_paths]:
            if p.exists():
                dest = target / p.name
                os.replace(str(p), str(dest))
                if p == self.json_path:
                    self.json_path = dest
        self.attachment_paths = [target / p.name for p in self.attachment_paths]

    def mark_sent(self) -> None:
        self.mark("sent", {"sent_at": _dt.datetime.now(_dt.timezone.utc).isoformat()})
        self._move_to("sent")
        logger.info(f"[dm] {self.id} → sent/")

    def mark_failed(self, reason: str) -> None:
        self.mark("failed", {"failure_reason": reason})
        self._move_to("failed")
        logger.warning(f"[dm] {self.id} → failed/ ({reason})")


class DmOutbox:
    def __init__(self, queue_root: os.PathLike | str, persona: str):
        self.root = Path(queue_root).expanduser()
        self.persona = persona
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        base = self.root / self.persona / "dm_outbox"
        base.mkdir(parents=True, exist_ok=True)
        for sub in _SUBDIRS:
            (base / sub).mkdir(exist_ok=True)
        lock = base / ".dm_lock"
        if not lock.exists():
            lock.touch()

    @contextmanager
    def _lock(self):
        lock_path = self.root / self.persona / "dm_outbox" / ".dm_lock"
        with open(lock_path, "r+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def iter_pending(self) -> Iterator[DmItem]:
        pending = self.root / self.persona / "dm_outbox" / "pending"
        for json_path in sorted(pending.glob("dm_*.json")):
            try:
                yield self._load_item(json_path)
            except DmOutboxError as exc:
                logger.warning(f"[dm] skip {json_path.name}: {exc}")

    def claim_next(self) -> Optional[DmItem]:
        with self._lock():
            candidates = list(self.iter_pending())
            if not candidates:
                return None
            # FIFO by id (timestamp-prefixed).
            candidates.sort(key=lambda i: i.id)
            chosen = candidates[0]
            chosen._move_to("sending")
            chosen.mark("sending")
            logger.info(f"[dm] claimed {chosen.id} (persona={self.persona})")
            return chosen

    def _load_item(self, json_path: Path) -> DmItem:
        try:
            with open(json_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise DmOutboxError(f"cannot read {json_path}: {exc}") from exc

        item_id = data.get("id") or json_path.stem.removeprefix("dm_")
        persona = data.get("persona") or self.persona
        if persona != self.persona:
            raise DmOutboxError(f"persona mismatch: {persona} vs {self.persona}")

        dir_ = json_path.parent
        attachments: List[Path] = []
        for name in data.get("attachments", []) or []:
            p = dir_ / name
            if not p.exists():
                raise DmOutboxError(f"missing attachment {name}")
            attachments.append(p)

        return DmItem(
            root=self.root,
            persona=persona,
            id=item_id,
            data=data,
            json_path=json_path,
            attachment_paths=attachments,
        )

    def counts(self) -> dict:
        base = self.root / self.persona / "dm_outbox"
        return {sub: sum(1 for _ in (base / sub).glob("dm_*.json")) for sub in _SUBDIRS}


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(str(tmp), str(path))
