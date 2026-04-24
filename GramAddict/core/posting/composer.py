"""Shared UIA2 helpers for the posting flows.

Instagram changes the Creation UI roughly every quarter: resource IDs get
renamed, bottom sheets become modals, the cover picker moves. We deal with
this by never relying on a single selector — every step tries multiple
strategies (resourceId, content-desc, text, text-contains) and logs which
one matched so we can refactor when things break.

Every ``click_first_of`` call dumps the hierarchy to a crash zip on failure
so the operator can see what Instagram was actually showing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence

from GramAddict.core.device_facade import DeviceFacade, Timeout
from GramAddict.core.utils import random_sleep, save_crash

logger = logging.getLogger(__name__)


@dataclass
class Selector:
    """One attempt at finding a view. Kwargs go straight to ``device.find``."""

    label: str
    kwargs: dict


def make(label: str, **kwargs) -> Selector:
    """Short-hand for building a selector."""
    return Selector(label=label, kwargs=kwargs)


def find_first(device, selectors: Sequence[Selector], ui_timeout: Timeout = Timeout.SHORT):
    """Try each selector in order; return the first matching View (or None)."""
    for sel in selectors:
        view = device.find(**sel.kwargs)
        if view.exists(ui_timeout):
            logger.debug(f"[composer] matched '{sel.label}' with {sel.kwargs}")
            return view
    return None


def click_first_of(
    device,
    selectors: Sequence[Selector],
    *,
    description: str,
    ui_timeout: Timeout = Timeout.MEDIUM,
    required: bool = True,
) -> bool:
    """Click the first matching selector.

    Returns True on click, False on miss (unless required=True which raises).
    On miss + required, saves a crash dump so we can diagnose later.
    """
    view = find_first(device, selectors, ui_timeout=ui_timeout)
    if view is None:
        msg = (
            f"could not find '{description}' — tried selectors: "
            + ", ".join(s.label for s in selectors)
        )
        logger.warning(f"[composer] {msg}")
        if required:
            save_crash(device)
            raise LookupError(msg)
        return False
    try:
        view.click()
    except Exception:
        logger.warning(f"[composer] click failed on '{description}' — saving crash dump")
        save_crash(device)
        raise
    logger.info(f"[composer] clicked '{description}'")
    return True


def wait_for_first_of(
    device,
    selectors: Sequence[Selector],
    *,
    description: str,
    timeout_s: float = 15.0,
    poll_s: float = 0.7,
):
    """Poll for any of the selectors to appear. Return the matched View."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        view = find_first(device, selectors, ui_timeout=Timeout.TINY)
        if view is not None:
            return view
        time.sleep(poll_s)
    raise TimeoutError(
        f"timed out waiting for '{description}' after {timeout_s:.0f}s"
    )


def wait_until_gone(
    device,
    selectors: Sequence[Selector],
    *,
    description: str,
    timeout_s: float = 90.0,
    poll_s: float = 1.0,
    abort_check: Optional[Callable[[], Optional[str]]] = None,
) -> None:
    """Poll until none of the selectors is visible or timeout expires.

    ``abort_check`` is called every iteration; if it returns a non-empty string,
    we raise TimeoutError immediately with that reason (used for early-abort
    on challenge / sensitive-content dialogs).
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if abort_check is not None:
            reason = abort_check()
            if reason:
                raise TimeoutError(f"aborted while waiting for '{description}' gone: {reason}")
        view = find_first(device, selectors, ui_timeout=Timeout.TINY)
        if view is None:
            return
        time.sleep(poll_s)
    raise TimeoutError(f"'{description}' still visible after {timeout_s:.0f}s")


def dismiss_if_present(
    device,
    selectors: Sequence[Selector],
    *,
    description: str,
) -> bool:
    """Click a selector only if it's present; swallow errors. Good for popups."""
    view = find_first(device, selectors, ui_timeout=Timeout.TINY)
    if view is None:
        return False
    try:
        view.click()
        logger.info(f"[composer] dismissed '{description}'")
        random_sleep(inf=0.8, sup=1.5, modulable=False)
        return True
    except Exception as exc:
        logger.warning(f"[composer] dismiss of '{description}' failed: {exc}")
        return False


def type_text(device, selector: Selector, text: str, *, description: str) -> None:
    """Focus the text field and set its content.

    We reach the underlying uiautomator2 selector via ``viewV2`` and call
    ``set_text`` directly. DeviceFacade's wrapper toggles the FastInput IME
    and falls back to ``send_keys`` on a ``focused=True`` selector — on IG 420+
    the caption is an ``AutoCompleteTextView`` whose suggestion dropdown
    closes when the IME changes, so the focused-element lookup then 404s with
    ``-32002 Selector [focused=True]``. A raw ``set_text`` uses Accessibility
    SET_TEXT which doesn't need the keyboard at all, and it writes in one shot.
    """
    view = find_first(device, [selector], ui_timeout=Timeout.MEDIUM)
    if view is None:
        save_crash(device)
        raise LookupError(f"could not find '{description}' text field")

    v2 = getattr(view, "viewV2", None)
    if v2 is None:
        # Very unexpected — DeviceFacade.View always holds a viewV2 handle
        logger.warning(f"[composer] no viewV2 handle on '{description}', falling back to wrapper set_text")
        view.click()
        random_sleep(inf=0.4, sup=1.0, modulable=False)
        from GramAddict.core.device_facade import Mode
        view.set_text(text, mode=Mode.PASTE)
    else:
        # Click + set_text directly on uiautomator2 — bypasses the
        # DeviceFacade IME switch that breaks AutoCompleteTextView.
        try:
            v2.click()
        except Exception as exc:
            logger.debug(f"[composer] direct click on '{description}' raised {exc} — continuing to set_text")
        random_sleep(inf=0.4, sup=0.9, modulable=False)
        v2.set_text(text)
    logger.info(f"[composer] typed {len(text)} chars into '{description}'")


def human_pause(min_s: float = 1.0, max_s: float = 2.5) -> None:
    """Sleep a randomized short duration to look less bot-like between taps."""
    import random as _r

    time.sleep(_r.uniform(min_s, max_s))


# --- Common selector libraries (IG v410+, English-first with loose regex fallback) ---

CREATION_TAB = [
    make("resourceId creation_tab", resourceIdMatches=r".*creation_tab.*"),
    make("desc Create", descriptionMatches=r"(?i)^(create|créer|creer|crear|erstellen)$"),
    make("desc contains Create", descriptionContains="Create"),
]


def click_creation_entry(device, *, description: str = "creation entry (+)") -> bool:
    """Tap the entry that opens the New-post composer.

    IG ≤ 419 had a labelled creation tab in the bottom bar (``creation_tab``
    resource-id / ``desc="Create"``). IG 420+ removed it and exposed an
    unlabelled ImageView inside ``action_bar_buttons_container_left`` at the
    top-left of the Home screen. Try both paths so the bot works on either.
    Returns True on click.
    """
    # Path 1: labelled selectors (old IG).
    if click_first_of(
        device,
        CREATION_TAB,
        description=description,
        ui_timeout=Timeout.SHORT,
        required=False,
    ):
        return True

    # Path 2: IG 420+ unlabelled ImageView child of the left action-bar container.
    try:
        btn = device.deviceV2.xpath(
            '//*[@resource-id="com.instagram.android:id/action_bar_buttons_container_left"]'
            '/android.widget.ImageView'
        )
        if btn.exists:
            btn.click()
            logger.info(f"[composer] clicked '{description}' via IG 420+ xpath")
            return True
    except Exception as exc:
        logger.debug(f"[composer] xpath creation probe failed: {exc}")

    # Path 3: last-resort coordinate tap for the left action-bar icon. Works on
    # most phones: the button center is always within the top ~10% left corner.
    try:
        info = device.deviceV2.info
        w = int(info["displayWidth"])
        h = int(info["displayHeight"])
        x = int(w * 0.07)
        y = int(h * 0.08)
        logger.warning(f"[composer] tapping creation entry by coordinates ({x},{y}) — selectors all missed")
        device.deviceV2.click(x, y)
        return True
    except Exception as exc:
        logger.error(f"[composer] coord-click fallback failed: {exc}")

    save_crash(device)
    raise LookupError(
        f"could not find '{description}' via any strategy (labelled, xpath, coord)"
    )


NEW_POST_HEADER = [
    make("text New post", textMatches=r"(?i)^(new post|nouveau(x|) post|neuer post|nuevo post)$"),
    make("resourceId new_post_button", resourceIdMatches=r".*(new_post|post_creation).*"),
]

POST_OPTION_IN_SHEET = [
    make("resourceId menu_post", resourceIdMatches=r".*creation_menu_post.*"),
    make("text Post", textMatches=r"(?i)^(post|publication)$"),
    make("desc Post", descriptionMatches=r"(?i)^post$"),
]

REEL_OPTION_IN_SHEET = [
    make("resourceId menu_reel", resourceIdMatches=r".*creation_menu_reel.*"),
    make("text Reel", textMatches=r"(?i)^reel$"),
    make("desc Reel", descriptionMatches=r"(?i)^reel$"),
]

STORY_OPTION_IN_SHEET = [
    make("resourceId menu_story", resourceIdMatches=r".*creation_menu_story.*"),
    make("text Story", textMatches=r"(?i)^(story|story)$"),
    make("desc Story", descriptionMatches=r"(?i)^story$"),
]

NEXT_BUTTON = [
    # IG 420+ creation flow uses ``creation_next_button``; older builds used
    # ``next_button_textview``. ``.*next_button.*`` matches both.
    make("resourceId creation_next_button", resourceIdMatches=r".*creation_next_button.*"),
    make("resourceId next_button_textview", resourceIdMatches=r".*next_button.*"),
    make("text Next", textMatches=r"(?i)^(next|suivant|weiter|siguiente)$"),
    make("desc Next", descriptionMatches=r"(?i)^next$"),
]

SHARE_BUTTON = [
    make("resourceId share_footer_button", resourceIdMatches=r".*share_(footer_)?button.*"),
    make("text Share", textMatches=r"(?i)^(share|partager|teilen|compartir)$"),
    make("desc Share", descriptionMatches=r"(?i)^share$"),
]

UPLOAD_PROGRESS = [
    make("resourceId upload_progress_bar", resourceIdMatches=r".*upload_progress_bar.*"),
    make("resourceId progress_text", resourceIdMatches=r".*progress_text.*"),
    make("text Posting", textMatches=r"(?i)(posting|uploading|publishing)"),
]

POST_SHARED_TOAST = [
    make("text Post shared", textMatches=r"(?i)(post shared|your post has been shared|shared)"),
    make("text Reel shared", textMatches=r"(?i)(reel (shared|posted)|your reel)"),
]

ALLOW_PHOTOS_PERMISSION = [
    make("text Allow all", textMatches=r"(?i)(allow all|allow full access|autoriser tout|zulassen)"),
    make("text Allow", textMatches=r"(?i)^(allow|autoriser|erlauben|permitir)$"),
    make("resourceId permission_allow_button", resourceIdMatches=r".*permission_allow.*"),
]

CAPTION_INPUT = [
    make("resourceId caption_input_text_view", resourceIdMatches=r".*caption_input_text_view.*"),
    make("resourceId caption_input", resourceIdMatches=r".*caption_input.*"),
    make("resourceId caption_edit_text", resourceIdMatches=r".*caption_edit_text.*"),
]

SELECT_MULTIPLE_BUTTON = [
    make("desc Select multiple", descriptionMatches=r"(?i)select multiple"),
    make("resourceId carousel_button", resourceIdMatches=r".*(multiple|carousel).*"),
]

GALLERY_BUTTON_REEL = [
    make("resourceId gallery_button", resourceIdMatches=r".*gallery_button.*"),
    make("desc Gallery", descriptionMatches=r"(?i)gallery"),
]

YOUR_STORY_BUTTON = [
    make("resourceId send_button", resourceIdMatches=r".*send_button.*"),
    make("desc Your story", descriptionMatches=r"(?i)(your story|votre story)"),
]


def any_matches(device, selectors: Iterable[Selector], ui_timeout: Timeout = Timeout.TINY) -> bool:
    """True iff at least one selector matches right now."""
    return find_first(device, list(selectors), ui_timeout=ui_timeout) is not None
