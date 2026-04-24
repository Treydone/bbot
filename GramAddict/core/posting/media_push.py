"""Push media files from WSL to the Android device and poke the MediaStore.

All commands inherit os.environ so ANDROID_ADB_SERVER_HOST/PORT are honored when
adb server lives on the Windows host. We avoid shell=True to stop surprises with
filenames containing spaces.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Iterable, List, Optional

from GramAddict.core.utils import _resolve_adb_path

logger = logging.getLogger(__name__)

TARGET_DIR = "/sdcard/DCIM/Camera"
MIN_FREE_MB_DEFAULT = 500


class PushError(Exception):
    """Raised when an adb push or shell call fails."""


def _adb_cmd(device_serial: Optional[str], *args: str) -> List[str]:
    cmd = [_resolve_adb_path()]
    if device_serial:
        cmd.extend(["-s", device_serial])
    cmd.extend(args)
    return cmd


def _run(cmd: List[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    logger.debug(f"[push] $ {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ,
        timeout=timeout,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise PushError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result


def check_free_storage(device_serial: Optional[str], min_free_mb: int = MIN_FREE_MB_DEFAULT) -> int:
    """Return free space in MB on /sdcard; raise if below min_free_mb.

    Different Android builds (Samsung Toybox, GKI, old BSD) accept different
    df flags. We try ``-h`` (human-readable, widely supported) first and fall
    back to plain ``df``. If neither works we log a warning and skip the check
    so a picky df doesn't block posting.
    """
    last_err: Optional[Exception] = None
    for flags in (("-h",), ()):
        try:
            result = _run(_adb_cmd(device_serial, "shell", "df", *flags, "/sdcard"), timeout=15)
        except PushError as exc:
            last_err = exc
            continue
        free_mb = _parse_df_free_mb(result.stdout)
        if free_mb < 0:
            continue
        if free_mb < min_free_mb:
            raise PushError(f"low storage on /sdcard: {free_mb} MB < {min_free_mb} MB required")
        logger.debug(f"[push] device free storage: {free_mb} MB")
        return free_mb
    logger.warning(f"[push] df preflight skipped (all forms failed): {last_err}")
    return -1


_SIZE_SUFFIXES = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}


def _parse_df_free_mb(stdout: str) -> int:
    """Parse ``df`` output (either ``-h`` or block form) and return free MB.

    Returns -1 when the output format is unrecognized.
    """
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[0].startswith("/"):
            continue
        raw = parts[3]  # 'Available' column on both forms
        # Human-readable form: '12G', '512M', '800K'
        if raw and raw[-1].upper() in _SIZE_SUFFIXES:
            try:
                value = float(raw[:-1])
            except ValueError:
                continue
            return int(value * _SIZE_SUFFIXES[raw[-1].upper()])
        # Plain form: block count (usually 1K blocks)
        try:
            blocks = int(raw)
            return blocks // 1024  # 1K → MB
        except ValueError:
            continue
    return -1


def _remote_path(persona: str, item_id: str, post_type: str, src: Path) -> str:
    # Prefix with ga_ and include persona/id so collisions are impossible.
    safe_stem = src.stem.replace(" ", "_")
    remote_name = f"ga_{persona}_{item_id}_{post_type}_{safe_stem}{src.suffix.lower()}"
    return f"{TARGET_DIR}/{remote_name}"


def push_media(
    device_serial: Optional[str],
    local_paths: Iterable[Path],
    persona: str,
    item_id: str,
    post_type: str,
    min_free_mb: int = MIN_FREE_MB_DEFAULT,
) -> List[str]:
    """Push each file and rescan MediaStore. Returns the list of remote paths."""
    local_paths = [Path(p).expanduser() for p in local_paths]
    for p in local_paths:
        if not p.exists() or not p.is_file():
            raise PushError(f"local media not found: {p}")

    check_free_storage(device_serial, min_free_mb)

    remotes: List[str] = []
    for src in local_paths:
        remote = _remote_path(persona, item_id, post_type, src)
        logger.info(f"[push] {src.name} → {remote}")
        _run(_adb_cmd(device_serial, "push", str(src), remote), timeout=180)
        _scan_media(device_serial, remote)
        remotes.append(remote)
    return remotes


def _scan_media(device_serial: Optional[str], remote: str) -> None:
    """Tell Android's MediaScanner to index a file."""
    try:
        _run(
            _adb_cmd(
                device_serial,
                "shell",
                "am",
                "broadcast",
                "-a",
                "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                "-d",
                f"file://{remote}",
            ),
            timeout=20,
        )
    except PushError as exc:
        # Rescan is best-effort; the file is still on disk.
        logger.warning(f"[push] media scan failed (non-fatal): {exc}")


def cleanup_media(device_serial: Optional[str], remote_paths: Iterable[str]) -> None:
    """Best-effort removal of remote files + MediaStore refresh."""
    for remote in remote_paths:
        try:
            _run(_adb_cmd(device_serial, "shell", "rm", "-f", remote), timeout=20)
            _scan_media(device_serial, remote)
            logger.debug(f"[push] cleaned {remote}")
        except PushError as exc:
            logger.warning(f"[push] cleanup failed for {remote} (non-fatal): {exc}")


def ensure_target_dir(device_serial: Optional[str]) -> None:
    """Create /sdcard/DCIM/Camera if missing. Some fresh devices lack it."""
    try:
        _run(_adb_cmd(device_serial, "shell", "mkdir", "-p", TARGET_DIR), timeout=10)
    except PushError as exc:
        logger.warning(f"[push] mkdir {TARGET_DIR} failed (may already exist): {exc}")
