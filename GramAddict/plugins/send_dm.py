"""send-dm job: deliver queued Instagram DMs from the back via app UI.

Reads items from a per-persona outbox (see ``core.posting.dm_outbox``),
opens each recipient's thread in the IG Direct inbox, types the message,
and archives the item as ``sent/`` or ``failed/``.

Config (usually its own run, separate from engagement):

    accounts/<user>/config-dm.yml:
        send-dm: true
        dm-persona: glowofsin
        dm-queue-dir: /home/devil/git/moneymaker2/queue
        dm-max-per-session: 3-5
        dm-min-gap-seconds: 30-90
        dm-dry-run: false
        shuffle-jobs: false
        total-sessions: 1

The UI flow is intentionally kept minimal and defensive:

    1. Open Direct inbox (main nav, top-right).
    2. Search recipient by `username` if provided, else fail (user_id-only
       items can't drive the UI without an extra resolution step — the
       back is responsible for providing a username, or we mark failed).
    3. Tap the first matching user → open thread.
    4. Type text, tap Send.
    5. If attachments: tap gallery icon → pick from device.

Robustness:
    * If Instagram presents a message request instead of a regular inbox
      entry, we skip and mark failed (never auto-accept — that's the
      user's trust decision).
    * On any unexpected screen, abort the item (failed) and return to the
      home feed before the next claim.
"""

from __future__ import annotations

import logging
import random
from pathlib import Path
from time import sleep
from typing import List, Optional

from GramAddict.core.decorators import run_safely
from GramAddict.core.device_facade import Timeout
from GramAddict.core.plugin_loader import Plugin
from GramAddict.core.posting.dm_outbox import DmOutbox, DmItem
from GramAddict.core.posting import media_push
from GramAddict.core.posting.safety import scan_all
from GramAddict.core.resources import ClassName
from GramAddict.core.utils import get_value
from GramAddict.core import views as _views
from GramAddict.core.views import ProfileView, TabBarView

logger = logging.getLogger(__name__)


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "on")
    return bool(v)


class SendDm(Plugin):
    """Deliver queued Instagram DMs."""

    def __init__(self):
        super().__init__()
        self.description = "Deliver queued DMs from <queue>/<persona>/dm_outbox/pending/"
        self.arguments = [
            {
                "arg": "--send-dm",
                "nargs": None,
                "help": "enable the send-dm job. Set to 'true' in YAML.",
                "metavar": "true",
                "default": None,
                "operation": True,
            },
            {
                "arg": "--dm-persona",
                "nargs": None,
                "help": "persona key; outbox resolves to <dm-queue-dir>/<persona>/dm_outbox",
                "metavar": "glowofsin",
                "default": None,
            },
            {
                "arg": "--dm-queue-dir",
                "nargs": None,
                "help": "absolute path to the queue ROOT (persona is appended internally)",
                "metavar": "/path/to/queue",
                "default": None,
            },
            {
                "arg": "--dm-max-per-session",
                "nargs": None,
                "help": "cap on DMs this session (number or range)",
                "metavar": "3-5",
                "default": "3",
            },
            {
                "arg": "--dm-min-gap-seconds",
                "nargs": None,
                "help": "minimum random pause between DMs within a session",
                "metavar": "30-90",
                "default": "30-90",
            },
            {
                "arg": "--dm-dry-run",
                "help": "navigate up to the Send button but do not actually send",
                "action": "store_true",
            },
        ]

    def run(self, device, configs, storage, sessions, profile_filter, plugin):
        class_name = self.__class__.__name__
        args = configs.args
        if not _truthy(args.send_dm):
            return

        queue_dir = self._resolve_queue_dir(args)
        if not queue_dir:
            logger.error("[send-dm] dm-queue-dir is required")
            return
        persona = args.dm_persona or Path(queue_dir).name
        max_dms = int(get_value(args.dm_max_per_session, "[send-dm] max/session: {}", 3))
        dry_run = bool(args.dm_dry_run)

        outbox = DmOutbox(queue_dir, persona)
        counts = outbox.counts()
        logger.info(
            f"[send-dm] persona={persona} queue={queue_dir} "
            f"pending={counts.get('pending', 0)} max={max_dms} dry_run={dry_run}"
        )
        if counts.get("pending", 0) == 0:
            logger.info("[send-dm] nothing to send; exiting cleanly.")
            return

        device_serial = args.device

        @run_safely(
            device=device,
            device_id=device_serial,
            sessions=sessions,
            session_state=self.session_state,
            screen_record=args.screen_record,
            configs=configs,
        )
        def _job():
            sent = 0
            for _ in range(max_dms):
                hit = scan_all(device)
                if hit is not None:
                    logger.error(f"[send-dm] abort before claim: {hit.label}")
                    return

                item = outbox.claim_next()
                if item is None:
                    logger.info("[send-dm] no more eligible items; stopping.")
                    return

                try:
                    self._deliver_one(
                        device=device,
                        item=item,
                        dry_run=dry_run,
                        device_serial=device_serial,
                    )
                except Exception as exc:
                    reason = exc.__class__.__name__
                    logger.exception(f"[send-dm] delivery failed ({reason}): {exc}")
                    item.mark_failed(reason)
                    continue

                if not dry_run:
                    item.mark_sent()
                    sent += 1

                if sent < max_dms:
                    gap = float(get_value(args.dm_min_gap_seconds, None, 45) or 45)
                    jitter = random.uniform(0.85, 1.3)
                    wait_s = gap * jitter
                    logger.info(f"[send-dm] inter-DM pause ~{wait_s:.0f}s")
                    sleep(wait_s)

        _job()

    # --------------------------------------------------------------- helpers

    def _resolve_queue_dir(self, args) -> Optional[str]:
        if args.dm_queue_dir:
            p = Path(args.dm_queue_dir).expanduser()
            if args.dm_persona and p.name == args.dm_persona:
                logger.warning(
                    f"[send-dm] dm-queue-dir ends with '{args.dm_persona}' — "
                    f"persona is appended internally; using parent {p.parent}"
                )
                return str(p.parent)
            return str(p)
        return None

    def _deliver_one(
        self,
        device,
        item: DmItem,
        dry_run: bool,
        device_serial: Optional[str] = None,
    ) -> None:
        username = item.recipient_username
        if not username:
            raise RuntimeError(
                "recipient.username missing — back must resolve user_id→username "
                "before enqueuing (UI-driven DM sending can't use Instagram-scoped ids)"
            )
        text = item.text
        if not text and not item.attachment_paths:
            raise RuntimeError("item has neither text nor attachments")

        logger.info(
            f"[send-dm] delivering {item.id} to @{username} "
            f"{'[DRY-RUN] ' if dry_run else ''}text={text[:40]!r}..."
        )

        # 1. Search and open @username's profile — same primitive the
        #    `nav_to_blogger` flow uses.
        search_view = TabBarView(device).navigateToSearch()
        if not search_view.navigate_to_target(username, "account"):
            raise RuntimeError(f"cannot open profile @{username}")

        # 2. Tap the "Message" button on the profile. On recent IG builds
        #    it's a TextView labeled "Message" inside the profile header.
        #    Fall back to case-insensitive match.
        profile = ProfileView(device, is_own_profile=False)
        profile.wait_profile_header_loaded(retries=3)

        message_btn = device.find(
            classNameMatches=ClassName.BUTTON_OR_TEXTVIEW_REGEX,
            textMatches="(?i)^message$",
        )
        if not message_btn.exists(Timeout.LONG):
            raise RuntimeError(f"Message button not found on @{username}'s profile")
        message_btn.click()

        # 3. Push attachments to the device first (if any) so they show up in
        #    the IG gallery picker. We also delete them after the DM is sent.
        pushed_remotes: list[str] = []
        if item.attachment_paths:
            try:
                pushed_remotes = media_push.push_media(
                    device_serial,
                    [Path(p) for p in item.attachment_paths],
                    persona=item.persona,
                    item_id=item.id,
                    post_type="dm",
                )
            except Exception as exc:
                raise RuntimeError(f"adb push of DM attachments failed: {exc}") from exc

        # 4. Type the text first (Instagram lets you type before/after attaching).
        if text:
            composer = device.find(
                resourceId=_views.ResourceID.ROW_THREAD_COMPOSER_EDITTEXT,
                className=ClassName.EDIT_TEXT,
            )
            if not composer.exists(Timeout.LONG):
                raise RuntimeError("DM composer not available (message-request thread?)")
            composer.click()
            composer.set_text(text)
        else:
            composer = device.find(
                resourceId=_views.ResourceID.ROW_THREAD_COMPOSER_EDITTEXT,
                className=ClassName.EDIT_TEXT,
            )

        # 5. Attach images via the gallery picker, one by one.
        if pushed_remotes:
            try:
                self._attach_images_via_gallery(device, len(pushed_remotes))
            finally:
                # Always clean up the device-side files; the IG draft has
                # already loaded them into memory by this point.
                if not dry_run:
                    media_push.cleanup_media(device_serial, pushed_remotes)

        # 6. Tap Send.
        send_btn = device.find(
            resourceId=_views.ResourceID.ROW_THREAD_COMPOSER_BUTTON_SEND,
        )
        if not send_btn.exists(Timeout.MEDIUM):
            raise RuntimeError("Send button not found in DM composer")
        if dry_run:
            logger.info("[send-dm] dry-run: not tapping Send; returning to home.")
            if pushed_remotes:
                media_push.cleanup_media(device_serial, pushed_remotes)
        else:
            send_btn.click()
            sleep(1.5)
            if composer.exists() and (composer.get_text() or "").strip():
                raise RuntimeError("composer still has text after Send — delivery unconfirmed")

        # 7. Return to home feed to reset the navigation stack.
        try:
            device.back()
            device.back()
        except Exception:
            pass
        try:
            TabBarView(device).navigateToHome()
        except Exception:
            pass

    def _attach_images_via_gallery(self, device, count: int) -> None:
        """Open the inline gallery picker in the DM composer and select the
        N most-recently-pushed images.

        UI flow on IG 426 (verified empirically — adapt if a build changes):
          1. The composer row has a "Gallery" / "Media" icon (image_button)
             to the left of the text field. Tap it.
          2. A bottom sheet appears with a horizontal grid of recent photos.
             Tap each desired photo to select it. The first image we pushed
             is the most recent (highest mtime), so they appear at index 0.
          3. The selected photos enqueue as draft attachments above the
             composer; closing the sheet (back) returns focus to the
             composer with the attachments queued.
        """
        # Step 1: tap the gallery / image icon.
        # On IG 420+ the icon is an image_button without a stable text label;
        # we fall back to content-desc matching in any of: "Gallery", "Photo",
        # "Add a photo or video".
        gallery_btn = device.find(
            classNameMatches=ClassName.BUTTON_OR_FRAME_LAYOUT_REGEX,
            descriptionMatches="(?i)(gallery|photo|add a photo|media|attachment)",
        )
        if not gallery_btn.exists(Timeout.MEDIUM):
            # Fallback: try the resource id matching the image button next to
            # the composer (often the first IMAGE_BUTTON sibling of the
            # composer edit text).
            gallery_btn = device.find(
                resourceId=_views.ResourceID.IMAGE_BUTTON,
                index=0,
            )
        if not gallery_btn.exists(Timeout.SHORT):
            raise RuntimeError(
                "DM gallery icon not found — IG layout may have changed; "
                "see _attach_images_via_gallery()"
            )
        gallery_btn.click()
        sleep(1.0)

        # Step 2: pick `count` photos from the recent grid.
        # The recent items live in a RecyclerView; the first child (index 0)
        # is the camera shortcut, real photos start at index 1.
        for i in range(count):
            child_index = 1 + i  # skip "take a photo" tile
            tile = device.find(
                resourceId=_views.ResourceID.RECYCLER_VIEW,
                index=0,
            ).child(index=child_index)
            if not tile.exists(Timeout.MEDIUM):
                raise RuntimeError(f"could not find recent-photo tile at index {child_index}")
            tile.click()
            sleep(0.4)

        # Step 3: close the picker — IG keeps the selection live and the
        # composer regains focus.
        device.back()
        sleep(0.5)
