"""Reel posting flow.

UI sequence (IG v410+):
    [any tab]
    → creation tab (+)
    → bottom sheet: tap 'Reel'
    → capture screen: tap the gallery icon (bottom-left)
    → pick a single .mp4
    → trim screen (if shown): tap Add → Next
    → audio/effects screen: tap Next (skip music to avoid copyright flags)
    → cover frame picker: tap Next (accept auto cover)
    → details screen: caption EditText; keep 'Also share to Feed' ON
    → tap Share

Reel uploads are significantly slower than photos (30-120s) so we use a
longer default timeout. IG also shows a progress banner at the top of the
feed while uploading — waiting for absence of *both* the modal spinner and
the feed banner is the most reliable completion signal.
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
    REASON_COPYRIGHT,
    REASON_UPLOAD_TIMEOUT,
    SafetyHit,
    detect_copyright,
    discard_draft_prompt,
    scan_all,
)
from GramAddict.core.utils import random_sleep, save_crash
from GramAddict.core.views import TabBarView

logger = logging.getLogger(__name__)


REEL_ADD_BUTTON = [
    C.make("text Add", textMatches=r"(?i)^(add|ajouter|añadir|hinzufügen)$"),
    C.make("resourceId add_clip_button", resourceIdMatches=r".*add_clip.*"),
]

REEL_FEED_UPLOAD_BANNER = [
    C.make("text Uploading", textMatches=r"(?i)(uploading|publishing|posting)\.*"),
    C.make("resourceId upload_banner", resourceIdMatches=r".*upload_banner.*"),
]


def post_reel(
    device,
    item: QueueItem,
    caption: str,
    device_serial: Optional[str],
    *,
    dry_run: bool = False,
    upload_timeout_s: float = 180.0,
) -> None:
    if item.post_type != "reel":
        raise ValueError(f"post_reel called with post_type={item.post_type!r}")
    if not item.media_paths or not str(item.media_paths[0]).lower().endswith((".mp4", ".mov", ".webm")):
        raise ValueError("reel media must be a video (mp4/mov/webm)")

    remote_paths: List[str] = []
    try:
        remote_paths = media_push.push_media(
            device_serial=device_serial,
            local_paths=item.media_paths[:1],  # Reels are single-clip only
            persona=item.persona,
            item_id=item.id,
            post_type="reel",
        )

        _open_creation(device)
        _choose_reel_option(device)
        _open_gallery_from_reel_camera(device)
        _select_first_video(device)
        _maybe_accept_trim(device)
        _skip_audio_effects(device)
        _accept_auto_cover(device)
        _write_caption(device, caption)

        if dry_run:
            logger.info("[reel] DRY-RUN — backing out before share")
            _back_out(device)
            return

        _share(device)
        _wait_upload_complete(device, timeout_s=upload_timeout_s)

    except Exception:
        save_crash(device)
        raise
    finally:
        if remote_paths:
            media_push.cleanup_media(device_serial, remote_paths)


def _open_creation(device) -> None:
    try:
        TabBarView(device).navigateToHome()
    except Exception:
        logger.warning("[reel] navigateToHome failed — continuing")
    random_sleep(inf=0.8, sup=1.5, modulable=False)
    hit = scan_all(device)
    if hit:
        raise RuntimeError(f"abort pre-reel: {hit.label}")
    C.click_first_of(
        device,
        C.CREATION_TAB,
        description="creation tab (+)",
        ui_timeout=Timeout.MEDIUM,
    )
    C.human_pause(1.0, 1.8)


def _choose_reel_option(device) -> None:
    C.click_first_of(
        device,
        C.REEL_OPTION_IN_SHEET,
        description="creation sheet → Reel",
        ui_timeout=Timeout.MEDIUM,
    )
    C.human_pause(1.5, 2.5)


def _open_gallery_from_reel_camera(device) -> None:
    # The reel camera view has its own gallery icon bottom-left.
    C.click_first_of(
        device,
        C.GALLERY_BUTTON_REEL,
        description="reel camera → gallery",
        ui_timeout=Timeout.MEDIUM,
    )
    C.human_pause(1.0, 1.6)
    C.dismiss_if_present(device, C.ALLOW_PHOTOS_PERMISSION, description="gallery permission")


def _select_first_video(device) -> None:
    v = device.find(resourceIdMatches=r".*(gallery_grid_item|thumbnail_item|media_picker_item).*")
    if not v.exists(Timeout.MEDIUM):
        save_crash(device)
        raise LookupError("no reel gallery thumbnails visible")
    v.click()
    C.human_pause(1.2, 2.0)


def _maybe_accept_trim(device) -> None:
    # Some versions route through an Add/trim screen before Next.
    C.click_first_of(
        device,
        REEL_ADD_BUTTON,
        description="trim screen Add",
        ui_timeout=Timeout.SHORT,
        required=False,
    )
    C.human_pause(0.6, 1.2)
    C.click_first_of(
        device,
        C.NEXT_BUTTON,
        description="Next after trim",
        ui_timeout=Timeout.MEDIUM,
    )
    C.human_pause(1.0, 1.6)


def _skip_audio_effects(device) -> None:
    # Do not attach music: licensed audio triggers copyright blocks on many
    # regions. Just hit Next.
    C.click_first_of(
        device,
        C.NEXT_BUTTON,
        description="Next (audio/effects)",
        ui_timeout=Timeout.MEDIUM,
    )
    C.human_pause(1.0, 1.8)

    hit = detect_copyright(device)
    if hit is not None:
        raise RuntimeError(REASON_COPYRIGHT)


def _accept_auto_cover(device) -> None:
    # Cover picker; accept the auto-generated frame and move on.
    C.click_first_of(
        device,
        C.NEXT_BUTTON,
        description="Next (cover)",
        ui_timeout=Timeout.MEDIUM,
    )
    C.human_pause(1.0, 1.8)


def _write_caption(device, caption: str) -> None:
    if not caption:
        logger.info("[reel] no caption — skipping text field")
        return
    for sel in C.CAPTION_INPUT:
        try:
            C.type_text(device, sel, caption, description="reel caption")
            return
        except LookupError:
            continue
    save_crash(device)
    raise LookupError("reel caption field not found")


def _share(device) -> None:
    C.click_first_of(
        device,
        C.SHARE_BUTTON,
        description="Share (reel)",
        ui_timeout=Timeout.MEDIUM,
    )


def _wait_upload_complete(device, timeout_s: float) -> None:
    def abort_if_bad() -> Optional[str]:
        hit: Optional[SafetyHit] = scan_all(device)
        return None if hit is None else hit.label

    try:
        C.wait_until_gone(
            device,
            C.UPLOAD_PROGRESS + REEL_FEED_UPLOAD_BANNER,
            description="reel upload banner/spinner",
            timeout_s=timeout_s,
            poll_s=1.5,
            abort_check=abort_if_bad,
        )
    except TimeoutError as exc:
        logger.warning(f"[reel] upload timeout after {timeout_s}s: {exc}")
        raise RuntimeError(REASON_UPLOAD_TIMEOUT) from exc

    logger.info("[reel] upload completed")


def _back_out(device) -> None:
    for _ in range(5):
        try:
            device.back(modulable=False)
        except Exception:
            break
        sleep(0.6)
        if discard_draft_prompt(device):
            break
