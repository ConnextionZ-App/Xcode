"""Deterministic scoring/diversity for the personalized "For You" feed.

Pure functions only (no DB access) so the ranking behavior can be unit
tested and tuned independently of the ``_for_you_feed`` resolver. Inputs are
values already available from existing denormalized post counters, the
follow graph, ``AnalyticsRepository.creator_affinity`` (which itself
aggregates likes/saves/shares/watch-time/completion/rewatch/follow signals
from the unified ``InteractionSignal`` log), and the viewer's own per-post
watch/engagement history (``AnalyticsRepository.viewer_post_history``) —
no new signal sources.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# ── Candidate pool sizing (bounded so scoring stays O(pool), no pagination
# of the underlying query beyond these caps) ────────────────────────────────
FOR_YOU_PERSONAL_POOL_SIZE = 100
FOR_YOU_DISCOVERY_POOL_SIZE = 150
FOR_YOU_DISCOVERY_LOOKBACK_DAYS = 14

# ── Diversity ────────────────────────────────────────────────────────────────
MAX_CONSECUTIVE_PER_CREATOR = 2

# ── Scoring weights (tunable constants, deliberately simple/transparent) ────
ENGAGEMENT_SMOOTHING = 100.0
LIKE_WEIGHT = 3.0
COMMENT_WEIGHT = 4.0
SHARE_WEIGHT = 6.0
SAVE_WEIGHT = 5.0
ENGAGEMENT_SCORE_WEIGHT = 100.0
REACH_WEIGHT = 2.0
FRESHNESS_WINDOW_DAYS = 30.0
FRESHNESS_WEIGHT = 10.0
FOLLOW_BOOST = 30.0
AFFINITY_WEIGHT = 6.0

# ── Viewer-history penalties (already-consumed content is demoted, never
# hard-excluded, so small/cold-start pools still have something to show) ────
SEEN_PENALTY = 15.0        # any partial view/watch of the post
COMPLETED_PENALTY = 40.0   # watched to completion (replaces SEEN_PENALTY)
ENGAGED_PENALTY = 12.0     # viewer already liked/saved/shared the post


@dataclass(frozen=True)
class ViewerPostSignals:
    """The viewer's own past behavior on a single post, distilled from the
    unified interaction-signal log (``AnalyticsRepository.viewer_post_history``).

    ``watched_seconds`` is the viewer's single best watch event (repeat
    ``post_watches`` rows are collapsed upstream — one post contributes at
    most its best watch to ranking). ``completed`` means any completion
    signal; ``engaged`` means a net like/save or any share.
    """

    watched_seconds: float = 0.0
    completed: bool = False
    engaged: bool = False


def score_post(
    *,
    post: Any,
    now: datetime,
    is_followed: bool,
    creator_affinity: float,
    viewer_history: ViewerPostSignals | None = None,
) -> float:
    """Deterministic relevance score for a single candidate post.

    Combines: engagement rate (likes/comments/shares/saves normalized by
    views), log-scaled reach, freshness decay, a follow-graph boost, the
    viewer's per-creator affinity (derived from their own past
    likes/saves/shares/watch-time/completion/rewatch/follow signals), and
    penalties when the viewer has already consumed the post (partial watch,
    completion, or a prior like/save/share).
    """
    views = max(getattr(post, "view_count", 0) or 0, 0)
    likes = max(getattr(post, "like_count", 0) or 0, 0)
    comments = max(getattr(post, "comment_count", 0) or 0, 0)
    shares = max(getattr(post, "share_count", 0) or 0, 0)
    saves = max(getattr(post, "save_count", 0) or 0, 0)

    engagement_rate = (
        likes * LIKE_WEIGHT
        + comments * COMMENT_WEIGHT
        + shares * SHARE_WEIGHT
        + saves * SAVE_WEIGHT
    ) / (views + ENGAGEMENT_SMOOTHING)

    created_at = getattr(post, "created_at", None) or now
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_hours = max((now - created_at).total_seconds() / 3600.0, 0.0)
    freshness = max(0.0, 1.0 - age_hours / (FRESHNESS_WINDOW_DAYS * 24.0))

    history_penalty = 0.0
    if viewer_history is not None:
        if viewer_history.completed:
            history_penalty += COMPLETED_PENALTY
        elif viewer_history.watched_seconds > 0:
            history_penalty += SEEN_PENALTY
        if viewer_history.engaged:
            history_penalty += ENGAGED_PENALTY

    return (
        engagement_rate * ENGAGEMENT_SCORE_WEIGHT
        + math.log1p(views) * REACH_WEIGHT
        + freshness * FRESHNESS_WEIGHT
        + (FOLLOW_BOOST if is_followed else 0.0)
        + math.log1p(max(creator_affinity, 0.0)) * AFFINITY_WEIGHT
        - history_penalty
    )


def diversify_by_creator(
    scored: list[tuple[Any, float]],
    max_consecutive: int = MAX_CONSECUTIVE_PER_CREATOR,
) -> list[Any]:
    """Reorder score-sorted candidates so no creator dominates a run.

    Greedily takes the highest-scoring available post at each step, unless
    doing so would extend the same creator's streak past ``max_consecutive``,
    in which case the next-best post from a different creator is taken
    instead (falling back to the same creator if no other candidates
    remain). Never drops a candidate, only reorders. Deterministic: ties are
    broken by post id so output is stable given identical inputs.
    """
    order = sorted(scored, key=lambda item: (item[1], str(item[0].id)), reverse=True)

    buckets: dict[uuid.UUID, deque[tuple[Any, float]]] = defaultdict(deque)
    for post, score in order:
        buckets[post.user_id].append((post, score))

    result: list[Any] = []
    last_creator: uuid.UUID | None = None
    streak = 0
    while buckets:
        ordered_creators = sorted(
            buckets.keys(), key=lambda uid: buckets[uid][0][1], reverse=True
        )
        chosen = next(
            (
                uid
                for uid in ordered_creators
                if not (uid == last_creator and streak >= max_consecutive)
            ),
            ordered_creators[0],
        )
        post, _score = buckets[chosen].popleft()
        if not buckets[chosen]:
            del buckets[chosen]
        streak = streak + 1 if chosen == last_creator else 1
        last_creator = chosen
        result.append(post)
    return result
