"""Unified recommendation-ready engagement signal log.

See ``app.models.analytics.InteractionSignal`` for the rationale — this
repository is the single write/read path for that append-only event
stream, kept separate from the toggle-state repositories in
``social_repository`` so feature-extraction queries don't have to touch
five different tables.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics import InteractionSignal, SignalType
from repositories.base import BaseRepository


class AnalyticsRepository(BaseRepository[InteractionSignal]):
    def __init__(self, db: AsyncSession):
        super().__init__(db, InteractionSignal)

    async def record(
        self,
        *,
        user_id: uuid.UUID,
        creator_id: uuid.UUID,
        signal_type: SignalType,
        post_id: uuid.UUID | None = None,
        value: float = 1.0,
    ) -> InteractionSignal:
        signal = InteractionSignal(
            user_id=user_id,
            post_id=post_id,
            creator_id=creator_id,
            signal_type=signal_type,
            value=value,
        )
        self.db.add(signal)
        await self.db.flush()
        return signal

    async def get_for_post(self, post_id: uuid.UUID, limit: int = 500) -> list[InteractionSignal]:
        result = await self.db.execute(
            select(InteractionSignal)
            .where(InteractionSignal.post_id == post_id)
            .order_by(InteractionSignal.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_for_user(self, user_id: uuid.UUID, limit: int = 500) -> list[InteractionSignal]:
        result = await self.db.execute(
            select(InteractionSignal)
            .where(InteractionSignal.user_id == user_id)
            .order_by(InteractionSignal.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def signal_totals(
        self,
        *,
        creator_id: uuid.UUID | None = None,
        post_id: uuid.UUID | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[SignalType, dict[str, float]]:
        """Per-signal-type event count + summed value, scoped to a creator and/or
        post and/or date range — powers the creator/post analytics dashboard."""
        stmt = select(
            InteractionSignal.signal_type,
            func.count().label("cnt"),
            func.sum(InteractionSignal.value).label("total"),
        ).group_by(InteractionSignal.signal_type)
        if creator_id is not None:
            stmt = stmt.where(InteractionSignal.creator_id == creator_id)
        if post_id is not None:
            stmt = stmt.where(InteractionSignal.post_id == post_id)
        if start is not None:
            stmt = stmt.where(InteractionSignal.created_at >= start)
        if end is not None:
            stmt = stmt.where(InteractionSignal.created_at <= end)
        result = await self.db.execute(stmt)
        return {
            row.signal_type: {"count": row.cnt, "total": float(row.total or 0.0)}
            for row in result.all()
        }

    async def creator_affinity(self, user_id: uuid.UUID, limit: int = 20) -> list[tuple[uuid.UUID, float]]:
        """Creators this user engages with most, weighted by signal value."""
        result = await self.db.execute(
            select(InteractionSignal.creator_id, func.sum(InteractionSignal.value).label("score"))
            .where(InteractionSignal.user_id == user_id)
            .group_by(InteractionSignal.creator_id)
            .order_by(func.sum(InteractionSignal.value).desc())
            .limit(limit)
        )
        return [(row[0], float(row[1])) for row in result.all()]

    async def viewer_post_history(
        self, user_id: uuid.UUID, post_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, "ViewerPostSignals"]:
        """The viewer's past behavior on a candidate set of posts, for ranking.

        One grouped query over the unified signal log, aggregated in Python
        into ``ViewerPostSignals`` per post: the single best watch event
        (repeat ``post_watches`` rows are collapsed so one post contributes
        at most its max watch to ranking), whether any completion fired, and
        whether the viewer has a net like/save or any share. Posts with no
        signals are simply absent from the returned dict.
        """
        from repositories.feed_ranking import ViewerPostSignals

        if not post_ids:
            return {}

        stmt = (
            select(
                InteractionSignal.post_id,
                InteractionSignal.signal_type,
                func.count().label("cnt"),
                func.max(InteractionSignal.value).label("max_value"),
            )
            .where(InteractionSignal.user_id == user_id)
            .where(InteractionSignal.post_id.in_(post_ids))
            .group_by(InteractionSignal.post_id, InteractionSignal.signal_type)
        )
        result = await self.db.execute(stmt)

        watched: dict[uuid.UUID, float] = {}
        completed: set[uuid.UUID] = set()
        net_likes: dict[uuid.UUID, int] = {}
        net_saves: dict[uuid.UUID, int] = {}
        shared: set[uuid.UUID] = set()
        for post_id, signal_type, cnt, max_value in result.all():
            if signal_type == SignalType.WATCH_DURATION:
                watched[post_id] = max(watched.get(post_id, 0.0), float(max_value or 0.0))
            elif signal_type == SignalType.COMPLETION:
                completed.add(post_id)
            elif signal_type == SignalType.LIKE:
                net_likes[post_id] = net_likes.get(post_id, 0) + cnt
            elif signal_type == SignalType.UNLIKE:
                net_likes[post_id] = net_likes.get(post_id, 0) - cnt
            elif signal_type == SignalType.SAVE:
                net_saves[post_id] = net_saves.get(post_id, 0) + cnt
            elif signal_type == SignalType.UNSAVE:
                net_saves[post_id] = net_saves.get(post_id, 0) - cnt
            elif signal_type == SignalType.SHARE:
                shared.add(post_id)

        post_ids_with_signals = (
            set(watched) | completed | set(net_likes) | set(net_saves) | shared
        )
        return {
            post_id: ViewerPostSignals(
                watched_seconds=watched.get(post_id, 0.0),
                completed=post_id in completed,
                engaged=(
                    net_likes.get(post_id, 0) > 0
                    or net_saves.get(post_id, 0) > 0
                    or post_id in shared
                ),
            )
            for post_id in post_ids_with_signals
        }
