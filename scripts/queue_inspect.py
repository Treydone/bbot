#!/usr/bin/env python3
"""Read-only CLI for inspecting a moneymaker2-style posting queue.

Examples:
    # overview (counts per subdir) for all personas under the queue root
    ./scripts/queue_inspect.py --queue /home/devil/git/moneymaker2/queue

    # list pending items for one persona
    ./scripts/queue_inspect.py --queue .../queue --persona luna_voss --list pending

    # move back any 'posting/' item older than 2h to 'pending/' (dry-run first)
    ./scripts/queue_inspect.py --queue .../queue --persona luna_voss --reconcile
    ./scripts/queue_inspect.py --queue .../queue --persona luna_voss --reconcile --apply

    # drop an example photo into pending/ for smoke testing
    ./scripts/queue_inspect.py --queue .../queue --persona luna_voss --seed-example \\
        --media /path/to/test.jpg
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as a plain script from inside the repo without install.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from GramAddict.core.posting.queue import (  # noqa: E402
    PostingQueue,
    copy_example_item,
    purge_old,
)

_SUBDIRS = ("pending", "posting", "posted", "failed")


def _fmt_item(item) -> str:
    sched = item.scheduled_at.isoformat() if item.scheduled_at else "—"
    return (
        f"  {item.id:<32} type={item.post_type:<8} "
        f"media={len(item.media_paths):<2} sched={sched} prio={item.priority}"
    )


def cmd_overview(args) -> int:
    root = Path(args.queue).expanduser()
    if not root.exists():
        print(f"[inspect] queue root not found: {root}", file=sys.stderr)
        return 2
    personas = [p.name for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith(".")]
    if args.persona:
        personas = [args.persona]
    for persona in personas:
        if not (root / persona).is_dir():
            print(f"[inspect] persona {persona!r} not found under {root}")
            continue
        q = PostingQueue(root, persona)
        counts = q.counts()
        print(
            f"{persona:<20} "
            + " ".join(f"{k}={v}" for k, v in counts.items())
        )
    return 0


def cmd_list(args) -> int:
    if not args.persona:
        print("[inspect] --list requires --persona", file=sys.stderr)
        return 2
    subdir = args.list
    if subdir not in _SUBDIRS:
        print(f"[inspect] unknown subdir {subdir!r}, expected one of {_SUBDIRS}", file=sys.stderr)
        return 2
    q = PostingQueue(args.queue, args.persona)
    items = q.list_items(subdir)
    print(f"[{args.persona}/{subdir}] {len(items)} item(s)")
    for item in items:
        print(_fmt_item(item))
    return 0


def cmd_show(args) -> int:
    if not args.persona or not args.show:
        print("[inspect] --show requires --persona and item id", file=sys.stderr)
        return 2
    q = PostingQueue(args.queue, args.persona)
    found = None
    for sub in _SUBDIRS:
        for item in q.list_items(sub):
            if item.id == args.show:
                found = item
                break
        if found:
            break
    if not found:
        print(f"[inspect] item {args.show!r} not found", file=sys.stderr)
        return 3
    print(json.dumps(found.data, indent=2, ensure_ascii=False))
    print(f"\n-- located in {found.json_path.parent.name}/")
    print(f"-- media: {[str(p.name) for p in found.media_paths]}")
    return 0


def cmd_reconcile(args) -> int:
    if not args.persona:
        print("[inspect] --reconcile requires --persona", file=sys.stderr)
        return 2
    q = PostingQueue(args.queue, args.persona)
    dry = not args.apply
    moved = q.reconcile_stale_posting(max_age_minutes=args.stale_minutes, dry_run=dry)
    verb = "would move" if dry else "moved"
    print(f"[reconcile] {verb} {len(moved)} item(s): {', '.join(moved) if moved else '(none)'}")
    if dry and moved:
        print("[reconcile] re-run with --apply to actually perform the moves.")
        print("[reconcile] WARNING: make sure no bot session is currently mid-post.")
    return 0


def cmd_seed(args) -> int:
    if not args.persona or not args.media:
        print("[inspect] --seed-example requires --persona and --media", file=sys.stderr)
        return 2
    media = Path(args.media).expanduser()
    if not media.exists():
        print(f"[inspect] media file not found: {media}", file=sys.stderr)
        return 3
    item_id = copy_example_item(args.queue, args.persona, media)
    print(f"[seed] dropped example item: {item_id} (persona={args.persona})")
    return 0


def cmd_purge(args) -> int:
    if not args.persona:
        print("[inspect] --purge requires --persona", file=sys.stderr)
        return 2
    if not args.apply:
        # Count what would be purged without touching disk
        q = PostingQueue(args.queue, args.persona)
        counts = q.counts()
        print(f"[purge] would review {counts.get('posted', 0)} posted + {counts.get('failed', 0)} failed items older than {args.days} days")
        print("[purge] re-run with --apply to delete.")
        return 0
    removed = purge_old(args.queue, args.persona, max_days=args.days)
    print(f"[purge] removed {removed} file(s) older than {args.days} days")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--queue", required=True, help="root dir containing <persona>/ subdirs")
    p.add_argument("--persona", help="persona key; omit for overview across all personas")

    # Actions — mutually informative, pick one per invocation
    p.add_argument("--list", choices=_SUBDIRS, help="list items in the given subdir")
    p.add_argument("--show", help="print full metadata of the item with the given id")
    p.add_argument("--reconcile", action="store_true", help="scan posting/ for stale items and rollback")
    p.add_argument("--seed-example", dest="seed_example", action="store_true",
                   help="drop one example photo item into pending/ for smoke testing")
    p.add_argument("--media", help="local file path used with --seed-example")
    p.add_argument("--purge", action="store_true", help="delete posted/ and failed/ items older than N days")

    p.add_argument("--stale-minutes", type=int, default=120, help="how old a posting/ item must be to reconcile (default 120)")
    p.add_argument("--days", type=int, default=30, help="age threshold for --purge (default 30)")
    p.add_argument("--apply", action="store_true", help="actually perform destructive actions (otherwise dry-run)")

    args = p.parse_args()

    if args.show:
        return cmd_show(args)
    if args.list:
        return cmd_list(args)
    if args.reconcile:
        return cmd_reconcile(args)
    if args.seed_example:
        return cmd_seed(args)
    if args.purge:
        return cmd_purge(args)
    return cmd_overview(args)


if __name__ == "__main__":
    sys.exit(main())
