"""Photo and carousel posting flow.

Entry point: ``post_photo(device, item, caption, device_serial, dry_run=False)``.

UI sequence (IG v410+):
    [any tab]
    → tap the '+' creation tab
    → bottom sheet: tap 'Post'
    → gallery grid (permission dialog may appear first)
    → carousel only: tap 'Select multiple'
    → tap N thumbnail(s)
    → tap 'Next'
    → (filters screen) tap 'Next' again
    → type caption
    → tap 'Share'
    → wait for upload spinner to vanish

Every step uses multiple fallback selectors (see ``composer.py``) so the flow
survives most IG minor updates.
"""

from __future__ import annotations

import logging
from pathlib import Path
from time import sleep
from typing import Iterable, List, Optional

from GramAddict.core.device_facade import Timeout
from GramAddict.core.posting import composer as C
from GramAddict.core.posting import media_push
from GramAddict.core.posting.queue import QueueItem
from GramAddict.core.posting.safety import (
    REASON_UPLOAD_TIMEOUT,
    SafetyHit,
    detect_challenge,
    detect_sensitive,
    discard_draft_prompt,
    scan_all,
)
from GramAddict.core.utils import random_sleep, save_crash
from GramAddict.core.views import TabBarView

logger = logging.getLogger(__name__)


def post_photo(
    device,
    item: QueueItem,
    caption: str,
    device_serial: Optional[str],
    *,
    dry_run: bool = False,
    upload_timeout_s: float = 90.0,
) -> None:
    """Publish a photo or carousel. Raises on failure; caller marks queue."""
    is_carousel = item.post_type == "carousel" or len(item.media_paths) > 1

    remote_paths: List[str] = []
    try:
        remote_paths = media_push.push_media(
            device_serial=device_serial,
            local_paths=item.media_paths,
            persona=item.persona,
            item_id=item.id,
            post_type=item.post_type,
        )

        _open_creation(device)
        _choose_post_option(device)
        _dismiss_permission_popup(device)
        _select_media(device, count=len(item.media_paths), is_carousel=is_carousel)
        _advance_past_filters(device)
        _write_caption(device, caption)

        if dry_run:
            logger.info("[photo] DRY-RUN — backing out before Share")
            _back_out_from_composer(device)
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
    # Always navigate to Home first — the creation entry lives there on both
    # old and new IG builds.
    try:
        TabBarView(device).navigateToHome()
    except Exception as exc:
        logger.warning(f"[photo] navigateToHome failed ({exc}) — trying creation entry anyway")
    random_sleep(inf=0.8, sup=1.6, modulable=False)

    hit = scan_all(device)
    if hit:
        raise RuntimeError(f"abort pre-creation: {hit.label}")

    C.click_creation_entry(device, description="creation entry (+)")
    C.human_pause(1.0, 2.0)


def _choose_post_option(device) -> None:
    """Pick 'Post' if IG shows a chooser sheet.

    IG ≤ 419 opened a bottom sheet with Post/Story/Reel/Live after tapping +.
    IG 420+ skips the sheet and lands directly on the 'New post' composer.
    We detect the new-composer header and skip the sheet tap in that case.
    """
    # Quick check: are we already on the New-post screen?
    if C.any_matches(device, C.NEW_POST_HEADER, ui_timeout=Timeout.SHORT):
        logger.info("[photo] already on 'New post' composer (IG 420+), skip bottom-sheet tap")
        return

    # Otherwise assume the old-style chooser sheet.
    if not C.click_first_of(
        device,
        C.POST_OPTION_IN_SHEET,
        description="creation sheet → Post",
        ui_timeout=Timeout.MEDIUM,
        required=False,
    ):
        # Sheet missing AND no new-composer header: something unexpected on screen.
        logger.warning("[photo] neither 'New post' header nor Post bottom-sheet found")
    C.human_pause(1.5, 2.5)


def _dismiss_permission_popup(device) -> None:
    C.dismiss_if_present(
        device,
        C.ALLOW_PHOTOS_PERMISSION,
        description="gallery permission 'Allow all'",
    )


def _select_media(device, count: int, is_carousel: bool) -> None:
    if is_carousel:
        if not C.click_first_of(
            device,
            C.SELECT_MULTIPLE_BUTTON,
            description="select multiple toggle",
            ui_timeout=Timeout.SHORT,
            required=False,
        ):
            logger.warning(
                "[photo] 'Select multiple' toggle not found — falling back to single photo"
            )
            is_carousel = False

    thumbnails = _gallery_thumbnails(device)
    if not thumbnails:
        save_crash(device)
        raise LookupError("no gallery thumbnails visible")

    for i, thumb in enumerate(thumbnails[: max(1, count)]):
        logger.info(f"[photo] select thumbnail #{i+1}")
        try:
            thumb.click()
        except Exception:
            save_crash(device)
            raise
        C.human_pause(0.6, 1.3)
        if not is_carousel:
            break

    C.click_first_of(device, C.NEXT_BUTTON, description="Next (after gallery)",
                     ui_timeout=Timeout.MEDIUM)
    C.human_pause(1.5, 2.5)


def _gallery_thumbnails(device) -> list:
    """Best-effort grab of first gallery grid items. IG exposes them under
    various resourceIds depending on version; we try the stable ones."""
    view_sets = [
        dict(resourceIdMatches=r".*gallery_grid_item_thumbnail.*"),            # IG 420+
        dict(resourceIdMatches=r".*(gallery_grid_item|thumbnail_item|media_picker_item).*"),
        dict(classNameMatches=r".*(ImageView|FrameLayout)", clickable=True, descriptionContains="Photo"),
    ]
    for kwargs in view_sets:
        v = device.find(**kwargs)
        if v.exists(Timeout.SHORT):
            count = v.count_items()
            if count > 0:
                # Build per-index Views (indexing is handled by DeviceFacade.find)
                return [device.find(index=i, **kwargs) for i in range(min(count, 20))]
    return []


def _advance_past_filters(device) -> None:
    """Walk through intermediate screens until the caption composer appears.

    IG ≤ 419 had one ``Next`` between gallery and caption. IG 420+ inserted a
    crop/trim screen plus an Audio/Text/Overlay/Filter/Edit screen before the
    caption — that's up to 2 more ``Next`` taps. We keep tapping until the
    caption field is visible or a safety cap is reached.
    """
    for attempt in range(4):
        if C.any_matches(device, C.CAPTION_INPUT, ui_timeout=Timeout.TINY):
            logger.info(f"[photo] caption composer reached after {attempt} post-gallery Next tap(s)")
            return
        if not C.click_first_of(
            device,
            C.NEXT_BUTTON,
            description=f"Next (intermediate #{attempt + 1})",
            ui_timeout=Timeout.SHORT,
            required=False,
        ):
            logger.warning(f"[photo] no Next button after {attempt} taps — giving up on _advance")
            return
        C.human_pause(1.0, 2.0)
    logger.warning("[photo] caption composer not reached after 4 Next taps — continuing anyway")


def _write_caption(device, caption: str) -> None:
    if not caption:
        logger.info("[photo] no caption — skipping text field")
        return
    try:
        C.type_text(
            device,
            C.CAPTION_INPUT[0],
            caption,
            description="caption EditText",
        )
    except LookupError:
        # Some versions put the caption above the fold; try alternatives.
        for alt in C.CAPTION_INPUT[1:]:
            try:
                C.type_text(device, alt, caption, description="caption (fallback)")
                return
            except LookupError:
                continue
        raise


def _share(device) -> None:
    # After the caption-typing step the keyboard or the autocomplete suggestion
    # dropdown can still cover the Share button. One back-press closes either
    # without leaving the composer.
    try:
        device.back(modulable=False)
        C.human_pause(0.6, 1.2)
    except Exception:
        pass
    C.click_first_of(
        device,
        C.SHARE_BUTTON,
        description="Share",
        ui_timeout=Timeout.MEDIUM,
    )


def _wait_upload_complete(device, timeout_s: float) -> None:
    def abort_if_bad() -> Optional[str]:
        hit: Optional[SafetyHit] = scan_all(device)
        return None if hit is None else hit.label

    try:
        C.wait_until_gone(
            device,
            C.UPLOAD_PROGRESS,
            description="upload spinner",
            timeout_s=timeout_s,
            abort_check=abort_if_bad,
        )
    except TimeoutError as exc:
        logger.warning(f"[photo] upload did not finish in {timeout_s}s ({exc})")
        raise RuntimeError(REASON_UPLOAD_TIMEOUT) from exc

    # Upload spinner gone = IG accepted the post. The post-share toast and
    # feed-return are nice-to-haves but IG 420+ shows neither reliably; if we
    # can confirm within a short budget, great, otherwise we trust the spinner
    # as the real success signal.
    try:
        C.wait_for_first_of(
            device,
            C.POST_SHARED_TOAST,
            description="post-share toast",
            timeout_s=3.0,
            poll_s=0.5,
        )
    except TimeoutError:
        logger.info("[photo] no post-shared toast (IG 420+ usually skips it); spinner-gone is success")
    logger.info("[photo] post upload completed")


def _back_out_from_composer(device) -> None:
    # Walk back out of the composer with the device's back key; if IG asks
    # 'Save draft?', discard it so we don't leave ghosts.
    for _ in range(4):
        try:
            device.back(modulable=False)
        except Exception:
            break
        sleep(0.6)
        if discard_draft_prompt(device):
            break
