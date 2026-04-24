"""Detectors for the posting flows.

These never click anything on their own — they just observe the UI and return
signals for the flow modules to act on. All returns are enum strings so they
round-trip easily into JSON metadata on failed items.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from GramAddict.core.device_facade import Timeout
from GramAddict.core.posting.composer import Selector, any_matches, find_first, make

logger = logging.getLogger(__name__)


# Reason codes written into failed items — stable contract, don't rename.
REASON_CHALLENGE = "LOGIN_CHALLENGE"
REASON_SOFT_BAN = "SOFT_BAN"
REASON_SENSITIVE = "SENSITIVE_GATE"
REASON_COPYRIGHT = "COPYRIGHT_REJECTED"
REASON_UPLOAD_TIMEOUT = "UPLOAD_TIMEOUT"
REASON_DEVICE_STORAGE = "DEVICE_STORAGE_LOW"
REASON_ATX_AGENT = "ATX_AGENT_DIED"
REASON_UNKNOWN = "UNKNOWN_ERROR"


@dataclass(frozen=True)
class SafetyHit:
    code: str
    label: str


_CHALLENGE = [
    make("text Challenge", textMatches=r"(?i)(challenge|checkpoint|we ?ve detected|security check)"),
    make("text Unusual login", textMatches=r"(?i)(unusual login|suspicious activity|verify your account)"),
    make("text Login", textMatches=r"(?i)^(log ?in|connexion|anmelden|iniciar sesi[oó]n)$"),
]

_SOFT_BAN = [
    make("text Action blocked", textMatches=r"(?i)(action blocked|try again later|temporarily blocked)"),
    make("text Rate limited", textMatches=r"(?i)(rate limited|we limit how often)"),
]

_SENSITIVE = [
    make(
        "text Sensitive",
        textMatches=r"(?i)(sensitive content|not safe for most|reconsider|may be inappropriate)",
    ),
    make("text Your post may", textMatches=r"(?i)(your post may (go|violate)|community guidelines)"),
]

_COPYRIGHT = [
    make(
        "text Copyright",
        textMatches=r"(?i)(copyright|music|audio unavailable|song not available|this audio isn)",
    ),
]

_UPLOADING = [
    make("resourceId progress_bar", resourceIdMatches=r".*upload_progress_bar.*"),
    make("resourceId progress_text", resourceIdMatches=r".*progress_text.*"),
    make("text Posting", textMatches=r"(?i)(posting|uploading|publishing)"),
]

_SAVE_DRAFT_PROMPT = [
    make("text Save draft", textMatches=r"(?i)(save (as )?draft|save for later)"),
    make("text Discard", textMatches=r"(?i)(discard|delete draft|delete post)"),
]


def detect_challenge(device) -> Optional[SafetyHit]:
    if any_matches(device, _CHALLENGE, ui_timeout=Timeout.TINY):
        return SafetyHit(REASON_CHALLENGE, "login challenge / checkpoint")
    if any_matches(device, _SOFT_BAN, ui_timeout=Timeout.TINY):
        return SafetyHit(REASON_SOFT_BAN, "action blocked / rate limited")
    return None


def detect_sensitive(device) -> Optional[SafetyHit]:
    if any_matches(device, _SENSITIVE, ui_timeout=Timeout.TINY):
        return SafetyHit(REASON_SENSITIVE, "sensitive content warning")
    return None


def detect_copyright(device) -> Optional[SafetyHit]:
    if any_matches(device, _COPYRIGHT, ui_timeout=Timeout.TINY):
        return SafetyHit(REASON_COPYRIGHT, "copyright / audio rejection")
    return None


def is_upload_in_progress(device) -> bool:
    return any_matches(device, _UPLOADING, ui_timeout=Timeout.TINY)


def discard_draft_prompt(device) -> bool:
    """If we bailed from the composer and IG asks 'Save as draft?', tap Discard.

    Returns True iff we actually clicked Discard (so caller knows a draft was
    not left behind).
    """
    discard = find_first(
        device,
        [make("text Discard", textMatches=r"(?i)^(discard|delete draft|delete post|don't save)$")],
        ui_timeout=Timeout.SHORT,
    )
    if discard is None:
        return False
    try:
        discard.click()
        logger.info("[safety] discarded draft after composer abort")
        return True
    except Exception as exc:
        logger.warning(f"[safety] discard click failed: {exc}")
        return False


def scan_all(device) -> Optional[SafetyHit]:
    """Combined scan used as abort_check callback inside wait_until_gone."""
    for probe in (detect_challenge, detect_sensitive, detect_copyright):
        hit = probe(device)
        if hit is not None:
            return hit
    return None
