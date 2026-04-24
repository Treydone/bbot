"""post-content job: publish queued media to Instagram via UIAutomator2.

Reads items from a per-persona queue (see ``core.posting.queue``), pushes
each media file to the device, drives the IG composer, and archives each
item as ``posted/`` or ``failed/``.

Intended to run as the **only** job in a dedicated session:

    accounts/<user>/config.yml:
        post-content: true
        post-persona: luna_voss
        post-queue-dir: /home/devil/git/moneymaker2/queue/luna_voss
        post-types-allowed: [photo, carousel, story, reel]
        post-max-per-session: 1-2
        post-min-gap-minutes: 45-120
        post-dry-run: false
        shuffle-jobs: false
        total-sessions: 1

Do not mix with engagement jobs in the same run: posting right after/before a
burst of likes is a loud behavioral signal for Instagram's anti-bot heuristics.
"""

from __future__ import annotations

import logging
import random
from pathlib import Path
from time import sleep
from typing import List, Optional

from GramAddict.core.decorators import run_safely
from GramAddict.core.plugin_loader import Plugin
from GramAddict.core.posting import photo_flow, reel_flow, story_flow
from GramAddict.core.posting.caption import render_caption
from GramAddict.core.posting.queue import PostingQueue, QueueItem
from GramAddict.core.posting.safety import (
    REASON_UNKNOWN,
    SafetyHit,
    detect_challenge,
    detect_sensitive,
    scan_all,
)
from GramAddict.core.storage import Storage
from GramAddict.core.utils import get_value

logger = logging.getLogger(__name__)


class PostContent(Plugin):
    """Publish queued media to Instagram."""

    def __init__(self):
        super().__init__()
        self.description = "Publish queued media (photo/carousel/reel/story) from a moneymaker2-style queue"
        self.arguments = [
            {
                "arg": "--post-content",
                "nargs": None,
                "help": "enable the post-content job. Set to 'true' in YAML to register as a job.",
                "metavar": "true",
                "default": None,
                "operation": True,
            },
            {
                "arg": "--post-persona",
                "nargs": None,
                "help": "persona key; the queue root resolves to <queue-dir>/<persona>",
                "metavar": "luna_voss",
                "default": None,
            },
            {
                "arg": "--post-queue-dir",
                "nargs": None,
                "help": "absolute path to the queue ROOT (persona name is appended; layout is <dir>/<persona>/pending|posting|posted|failed)",
                "metavar": "/path/to/queue",
                "default": None,
            },
            {
                "arg": "--post-types-allowed",
                "nargs": "+",
                "help": "post types this session may publish",
                "metavar": "photo carousel reel story",
                "default": ["photo", "carousel", "reel", "story"],
            },
            {
                "arg": "--post-max-per-session",
                "nargs": None,
                "help": "cap on posts this session (number or range)",
                "metavar": "1-2",
                "default": "1",
            },
            {
                "arg": "--post-min-gap-minutes",
                "nargs": None,
                "help": "minimum random pause between posts within a session",
                "metavar": "45-120",
                "default": "20-60",
            },
            {
                "arg": "--post-dry-run",
                "help": "run the composer up to Share and back out — do not publish",
                "action": "store_true",
            },
            {
                "arg": "--post-upload-spinner-timeout",
                "nargs": None,
                "help": "seconds to wait for the IG upload spinner to disappear",
                "metavar": "90",
                "default": "90",
            },
            {
                "arg": "--post-caption-hashtag-count",
                "nargs": None,
                "help": "number of hashtags to append to the caption when the item provides a pool",
                "metavar": "10",
                "default": "10",
            },
            {
                "arg": "--post-hashtag-cooldown-hours",
                "nargs": None,
                "help": "hashtags reused within this many hours are penalized",
                "metavar": "48",
                "default": "48",
            },
        ]

    # ------------------------------------------------------------------ run
    def run(self, device, configs, storage, sessions, profile_filter, plugin):
        args = configs.args
        self.args = args
        self.session_state = sessions[-1]

        if not _truthy(args.post_content):
            logger.info("[post-content] not enabled for this session; skipping.")
            return

        queue_dir = self._resolve_queue_dir(args)
        persona = args.post_persona or Path(queue_dir).name
        if not persona:
            logger.error("[post-content] post-persona missing and queue-dir has no basename.")
            return

        max_posts = int(get_value(args.post_max_per_session, "[post-content] max/session: {}", 1))
        dry_run = bool(args.post_dry_run)
        allowed_types: List[str] = list(args.post_types_allowed or [])
        upload_timeout = float(
            get_value(args.post_upload_spinner_timeout, None, 90) or 90
        )

        queue = PostingQueue(queue_dir, persona)
        counts = queue.counts()
        logger.info(
            f"[post-content] persona={persona} queue={queue_dir} "
            f"pending={counts.get('pending', 0)} allowed={','.join(allowed_types)} "
            f"max={max_posts} dry_run={dry_run}"
        )
        if counts.get("pending", 0) == 0:
            logger.info("[post-content] nothing to post; exiting cleanly.")
            return

        device_serial = args.device
        hashtag_history = self._hashtag_history_path(storage)

        @run_safely(
            device=device,
            device_id=device_serial,
            sessions=sessions,
            session_state=self.session_state,
            screen_record=args.screen_record,
            configs=configs,
        )
        def _job():
            posted = 0
            for _ in range(max_posts):
                hit = scan_all(device)
                if hit is not None:
                    logger.error(f"[post-content] abort before claim: {hit.label}")
                    return

                item = queue.claim_next(types_allowed=allowed_types)
                if item is None:
                    logger.info("[post-content] no more eligible items; stopping.")
                    return

                try:
                    caption = _render_item_caption(
                        item,
                        hashtag_count=int(get_value(args.post_caption_hashtag_count, None, 10) or 10),
                        cooldown_hours=int(get_value(args.post_hashtag_cooldown_hours, None, 48) or 48),
                        history_path=hashtag_history,
                    )
                    self._publish_one(
                        device=device,
                        device_serial=device_serial,
                        item=item,
                        caption=caption,
                        dry_run=dry_run,
                        upload_timeout_s=upload_timeout,
                    )
                except Exception as exc:
                    reason = _classify_exception(exc)
                    logger.exception(f"[post-content] publish failed ({reason}): {exc}")
                    item.mark_failed(reason)
                    if reason.startswith("LOGIN_") or reason == "SOFT_BAN":
                        # Don't keep trying on a borked session.
                        return
                    continue

                if dry_run:
                    item.release_back_to_pending()
                    logger.info("[post-content] dry-run: item returned to pending/")
                else:
                    item.mark_posted()
                    posted += 1

                # Space posts out within the session
                if posted < max_posts:
                    gap = float(get_value(args.post_min_gap_minutes, None, 30) or 30)
                    jitter = random.uniform(0.85, 1.15)
                    wait_s = gap * 60 * jitter
                    logger.info(f"[post-content] inter-post pause ~{wait_s/60:.1f} min")
                    sleep(wait_s)

        _job()

    # ------------------------------------------------------------- helpers
    def _publish_one(
        self,
        device,
        device_serial: Optional[str],
        item: QueueItem,
        caption: str,
        dry_run: bool,
        upload_timeout_s: float,
    ) -> None:
        post_type = item.post_type
        logger.info(
            f"[post-content] publishing {item.id} ({post_type}) "
            f"{'[DRY-RUN] ' if dry_run else ''}caption={caption[:50]!r}..."
        )
        if post_type in ("photo", "carousel"):
            photo_flow.post_photo(
                device=device,
                item=item,
                caption=caption,
                device_serial=device_serial,
                dry_run=dry_run,
                upload_timeout_s=upload_timeout_s,
            )
        elif post_type == "reel":
            reel_flow.post_reel(
                device=device,
                item=item,
                caption=caption,
                device_serial=device_serial,
                dry_run=dry_run,
                upload_timeout_s=upload_timeout_s * 2,
            )
        elif post_type == "story":
            story_flow.post_story(
                device=device,
                item=item,
                device_serial=device_serial,
                dry_run=dry_run,
            )
        else:
            raise ValueError(f"unsupported post_type: {post_type!r}")

    def _resolve_queue_dir(self, args) -> str:
        """Return the queue ROOT (persona is appended by PostingQueue).

        Be forgiving: if the user accidentally pointed ``post-queue-dir`` at
        the persona subdirectory (``.../queue/<persona>``), strip that tail so
        we don't end up looking at ``.../queue/<persona>/<persona>/``.
        """
        if args.post_queue_dir:
            p = Path(args.post_queue_dir).expanduser()
            if args.post_persona and p.name == args.post_persona:
                logger.warning(
                    f"[post-content] post-queue-dir ends with '{args.post_persona}' — "
                    f"treating its parent as the queue root"
                )
                p = p.parent
            return str(p)
        if args.post_persona:
            return str(Path("accounts") / args.username / "queue")
        raise ValueError(
            "post-content requires either --post-queue-dir or --post-persona with fallback layout"
        )

    @staticmethod
    def _hashtag_history_path(storage: Optional[Storage]) -> Optional[Path]:
        if storage is None:
            return None
        acct = getattr(storage, "account_path", None)
        if not acct:
            return None
        return Path(acct) / "hashtag_history.json"


# ---------------------------------------------------------------- utilities
def _truthy(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in {"1", "true", "yes", "on"}


def _render_item_caption(
    item: QueueItem,
    *,
    hashtag_count: int,
    cooldown_hours: int,
    history_path: Optional[Path],
) -> str:
    # The generator can either hand us a fully-rendered caption OR a spintax
    # template + hashtag pool. We support both shapes transparently.
    template = item.caption
    pool = item.hashtags
    return render_caption(
        template=template,
        hashtag_pool=pool,
        hashtag_count=hashtag_count,
        history_path=history_path,
    )


def _classify_exception(exc: BaseException) -> str:
    from GramAddict.core.device_facade import DeviceFacade

    msg = str(exc) or exc.__class__.__name__

    if isinstance(exc, SafetyHit):
        return exc.code

    # Known reason codes bubbled up from flow modules:
    for known in (
        "LOGIN_CHALLENGE",
        "SOFT_BAN",
        "SENSITIVE_GATE",
        "COPYRIGHT_REJECTED",
        "UPLOAD_TIMEOUT",
        "DEVICE_STORAGE_LOW",
        "ATX_AGENT_DIED",
    ):
        if known in msg:
            return known

    if isinstance(exc, (DeviceFacade.JsonRpcError, DeviceFacade.AppHasCrashed)):
        return "ATX_AGENT_DIED"

    if isinstance(exc, FileNotFoundError):
        return "MEDIA_MISSING"

    if isinstance(exc, TimeoutError):
        return "UPLOAD_TIMEOUT"

    return REASON_UNKNOWN
