"""Posting module for GramAddict.

Adds a `post-content` plugin and the supporting primitives:
    queue        — claim items under flock, archive after post
    media_push   — adb push + MediaStore rescan + storage preflight
    composer     — shared UIA2 waiters and fallback selector helpers
    caption      — spintax rendering + hashtag rotation with history
    safety       — detectors for challenge, sensitive-content, copyright, uploads
    photo_flow   — photo / carousel posting steps
    reel_flow    — reel posting steps
    story_flow   — story posting steps

Contract with moneymaker2 (or any content generator) is in queue.py docstring.
"""
