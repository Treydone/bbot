"""Story posting flow.

UI sequence (IG v410+):
    [home]
    → swipe right from left edge (stable) OR tap the user story ring
    → capture screen: tap gallery thumbnail (bottom-left)
    → pick one media
    → skip stickers / text
    → tap "Your story" to share

The story composer is much simpler than the post/reel composer — no caption,
no filters, no audio picker. Its main failure mode is the capture screen
showing up in a language where our text regex miss.
"""

from __future__ import annotations

import logging
from time import sleep
from typing import List, Optional

from GramAddict.core.device_facade import Timeout
from GramAddict.core.posting import composer as C
from GramAddict.core.posting import media_push
from GramAddict.core.posting.queue import QueueItem
from GramAddict.core.posting.safety import (
    REASON_UPLOAD_TIMEOUT,
    discard_draft_prompt,
    scan_all,
)
from GramAddict.core.utils import random_sleep, save_crash
from GramAddict.core.views import TabBarView

logger = logging.getLogger(__name__)


STORY_CAMERA_ENTRY = [
    C.make("desc Your story", descriptionMatches=r"(?i)(your story|votre story|ta story)"),
    C.make("resourceId story_avatar", resourceIdMatches=r".*self_profile_picture.*"),
]

STORY_GALLERY_THUMB = [
    C.make("resourceId gallery_thumbnail", resourceIdMatches=r".*gallery_thumbnail.*"),
    C.make("desc Gallery", descriptionMatches=r"(?i)(gallery|galerie|galer[ií]a)"),
]


def post_story(
    device,
    item: QueueItem,
    device_serial: Optional[str],
    *,
    dry_run: bool = False,
) -> None:
    if item.post_type != "story":
        raise ValueError(f"post_story called with post_type={item.post_type!r}")

    remote_paths: List[str] = []
    try:
        remote_paths = media_push.push_media(
            device_serial=device_serial,
            local_paths=item.media_paths,
            persona=item.persona,
            item_id=item.id,
            post_type="story",
        )

        _open_story_camera(device)
        _pick_gallery(device)
        _select_first_thumbnail(device)
        if dry_run:
            logger.info("[story] DRY-RUN — backing out before share")
            _back_out(device)
            return
        _share_to_your_story(device)

    except Exception:
        save_crash(device)
        raise
    finally:
        if remote_paths:
            media_push.cleanup_media(device_serial, remote_paths)


def _open_story_camera(device) -> None:
    try:
        TabBarView(device).navigateToHome()
    except Exception:
        logger.warning("[story] navigateToHome failed — continuing")
    random_sleep(inf=0.8, sup=1.5, modulable=False)

    hit = scan_all(device)
    if hit:
        raise RuntimeError(f"abort pre-story: {hit.label}")

    # Strategy 1: creation tab (+) → Story in the bottom sheet. Most version-stable.
    try:
        C.click_first_of(
            device,
            C.CREATION_TAB,
            description="creation tab (+)",
            ui_timeout=Timeout.MEDIUM,
        )
        C.human_pause(0.8, 1.4)
        if C.click_first_of(
            device,
            C.STORY_OPTION_IN_SHEET,
            description="creation sheet → Story",
            ui_timeout=Timeout.SHORT,
            required=False,
        ):
            C.human_pause(1.0, 1.8)
            return
        # Sheet had no Story option on this version; back out and try the other entry.
        device.back(modulable=False)
    except LookupError:
        logger.info("[story] creation tab route unavailable — falling back")

    # Strategy 2: tap own story ring on Home (works when Story is not in the sheet).
    C.click_first_of(
        device,
        STORY_CAMERA_ENTRY,
        description="own story ring / camera entry",
        ui_timeout=Timeout.MEDIUM,
    )
    C.human_pause(1.0, 1.8)


def _pick_gallery(device) -> None:
    C.click_first_of(
        device,
        STORY_GALLERY_THUMB,
        description="story camera → gallery thumbnail",
        ui_timeout=Timeout.MEDIUM,
    )
    C.human_pause(1.0, 1.6)
    C.dismiss_if_present(device, C.ALLOW_PHOTOS_PERMISSION, description="gallery permission")


def _select_first_thumbnail(device) -> None:
    # Re-use gallery thumb locators — the picker grid layout is the same on
    # many IG versions for the story chooser.
    v = device.find(resourceIdMatches=r".*(gallery_grid_item|thumbnail_item|media_picker_item).*")
    if not v.exists(Timeout.MEDIUM):
        save_crash(device)
        raise LookupError("no story gallery thumbnails visible")
    v.click()
    C.human_pause(1.2, 2.0)


def _share_to_your_story(device) -> None:
    C.click_first_of(
        device,
        C.YOUR_STORY_BUTTON,
        description="Your story (share)",
        ui_timeout=Timeout.MEDIUM,
    )

    # Story uploads are fast but still show a brief spinner on some versions.
    try:
        C.wait_until_gone(
            device,
            C.UPLOAD_PROGRESS,
            description="story upload spinner",
            timeout_s=45.0,
            abort_check=lambda: (scan_all(device).label if scan_all(device) else None),
        )
    except TimeoutError as exc:
        logger.warning(f"[story] upload timeout: {exc}")
        raise RuntimeError(REASON_UPLOAD_TIMEOUT) from exc

    logger.info("[story] posted")


def _back_out(device) -> None:
    for _ in range(4):
        try:
            device.back(modulable=False)
        except Exception:
            break
        sleep(0.5)
        if discard_draft_prompt(device):
            break
