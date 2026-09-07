"""Database-backed creator analytics aggregation.

All creator metrics in this service are derived from ``AnalyticsEvent`` rows.
The queries group in the database and never expose raw events to callers.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics import AnalyticsEvent, EventType
from app.models.content import ContentStatus, Post


ENGAGEMENT_EVENTS = (
    EventType.LIKE_CREATED,
    EventType.COMMENT_CREATED,
    EventType.SHARE_CREATED,
    EventType.SAVE_CREATED,
)


class CreatorAnalyticsService:
    """Aggregates analytics for one authenticated creator."""

    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _window(stmt, start: datetime, end: datetime):
        return stmt.where(AnalyticsEvent.created_at >= start, AnalyticsEvent.created_at <= end)

    async def _event_rows(
        self,
        creator_id: uuid.UUID,
        start: datetime,
        end: datetime,
        post_ids: list[uuid.UUID] | None = None,
    ):
        stmt = (
            select(
                AnalyticsEvent.post_id,
                AnalyticsEvent.event_type,
                func.count().label("count"),
                func.count(distinct(AnalyticsEvent.user_id)).label("unique_users"),
                func.sum(AnalyticsEvent.duration_ms).label("duration_ms"),
            )
            .join(Post, Post.id == AnalyticsEvent.post_id)
            .where(Post.user_id == creator_id, Post.deleted_at.is_(None))
            .where(AnalyticsEvent.post_id.is_not(None))
            .group_by(AnalyticsEvent.post_id, AnalyticsEvent.event_type)
        )
        stmt = self._window(stmt, start, end)
        if post_ids:
            stmt = stmt.where(AnalyticsEvent.post_id.in_(post_ids))
        result = await self.db.execute(stmt)
        return list(result.all())

    async def _posts(self, creator_id: uuid.UUID) -> list[Post]:
        result = await self.db.execute(
            select(Post)
            .where(Post.user_id == creator_id, Post.deleted_at.is_(None), Post.status == ContentStatus.PUBLISHED)
            .order_by(Post.published_at.desc(), Post.created_at.desc())
        )
        return list(result.scalars().all())

    async def overview(self, creator_id: uuid.UUID, start: datetime, end: datetime) -> dict:
        posts = await self._posts(creator_id)
        rows = await self._event_rows(creator_id, start, end, [post.id for post in posts])
        totals = defaultdict(int)
        watch_ms = 0
        qualifying_views = 0
        completions = 0
        uploads = 0
        published = 0
        for row in rows:
            event_type = row.event_type
            count = int(row.count or 0)
            if event_type == EventType.VIDEO_VIEWED:
                totals["views"] += count
                qualifying_views += count
            elif event_type == EventType.VIDEO_UPLOADED:
                uploads += count
            elif event_type == EventType.VIDEO_PUBLISHED:
                published += count
            elif event_type == EventType.VIDEO_WATCHED:
                watch_ms += int(row.duration_ms or 0)
            elif event_type == EventType.VIDEO_COMPLETED:
                completions += count
            elif event_type == EventType.LIKE_CREATED:
                totals["likes"] += count
            elif event_type == EventType.COMMENT_CREATED:
                totals["comments"] += count
            elif event_type == EventType.SHARE_CREATED:
                totals["shares"] += count
            elif event_type == EventType.SAVE_CREATED:
                totals["saves"] += count
            if event_type == EventType.VIDEO_VIEWED:
                totals["unique_viewers"] += int(row.unique_users or 0)

        follow_stmt = select(
            AnalyticsEvent.event_type, func.count().label("count")
        ).where(
            AnalyticsEvent.target_user_id == creator_id,
            AnalyticsEvent.event_type.in_((EventType.FOLLOW_CREATED, EventType.FOLLOW_REMOVED)),
        )
        follow_rows = await self.db.execute(self._window(follow_stmt, start, end).group_by(AnalyticsEvent.event_type))
        follow_values = list(follow_rows.all())
        gained = sum(int(row.count) for row in follow_values if row.event_type == EventType.FOLLOW_CREATED)
        lost = sum(int(row.count) for row in follow_values if row.event_type == EventType.FOLLOW_REMOVED)
        unique_stmt = select(func.count(distinct(AnalyticsEvent.user_id))).join(
            Post, Post.id == AnalyticsEvent.post_id
        ).where(
            Post.user_id == creator_id,
            Post.deleted_at.is_(None),
            AnalyticsEvent.event_type == EventType.VIDEO_VIEWED,
        )
        unique_result = await self.db.execute(self._window(unique_stmt, start, end))
        views = totals["views"]
        engagements = totals["likes"] + totals["comments"] + totals["shares"] + totals["saves"]
        return {
            "total_posts": len(posts),
            "total_uploads": uploads,
            "total_published_videos": published,
            "total_views": views,
            "unique_viewers": int(unique_result.scalar_one() or 0),
            "total_likes": totals["likes"],
            "total_comments": totals["comments"],
            "total_shares": totals["shares"],
            "total_saves": totals["saves"],
            "new_followers": gained,
            "lost_followers": lost,
            "follower_growth": gained - lost,
            "avg_watch_time": watch_ms / qualifying_views / 1000 if qualifying_views and watch_ms >= 0 else None,
            "completion_rate": completions / qualifying_views * 100 if qualifying_views else None,
            "engagement_rate": engagements / views * 100 if views else 0.0,
        }

    async def video_performance(self, creator_id: uuid.UUID, start: datetime, end: datetime) -> list[dict]:
        posts = await self._posts(creator_id)
        rows = await self._event_rows(creator_id, start, end, [post.id for post in posts])
        by_post: dict[uuid.UUID, dict] = defaultdict(lambda: {"views": 0, "unique_viewers": 0, "likes": 0, "comments": 0, "shares": 0, "saves": 0, "watch_ms": 0, "completed": 0})
        for row in rows:
            item = by_post[row.post_id]
            count = int(row.count or 0)
            if row.event_type == EventType.VIDEO_VIEWED:
                item["views"] += count
                item["unique_viewers"] += int(row.unique_users or 0)
            elif row.event_type == EventType.VIDEO_WATCHED:
                item["watch_ms"] += int(row.duration_ms or 0)
            elif row.event_type == EventType.VIDEO_COMPLETED:
                item["completed"] += count
            elif row.event_type == EventType.LIKE_CREATED:
                item["likes"] += count
            elif row.event_type == EventType.COMMENT_CREATED:
                item["comments"] += count
            elif row.event_type == EventType.SHARE_CREATED:
                item["shares"] += count
            elif row.event_type == EventType.SAVE_CREATED:
                item["saves"] += count
        result = []
        for post in posts:
            item = by_post[post.id]
            views = item["views"]
            engagements = sum(item[key] for key in ("likes", "comments", "shares", "saves"))
            item.update(
                post=post,
                avg_watch_time=item["watch_ms"] / views / 1000 if views and item["watch_ms"] >= 0 else None,
                completion_rate=item["completed"] / views * 100 if views else None,
                engagement_rate=engagements / views * 100 if views else 0.0,
                followers_generated=None,
            )
            result.append(item)
        return result

    async def daily_trends(self, creator_id: uuid.UUID, start: datetime, end: datetime) -> list[dict]:
        """Return one grouped row per UTC day and tracked metric."""
        day = func.date(AnalyticsEvent.created_at).label("day")
        stmt = (
            select(day, AnalyticsEvent.event_type, func.count().label("count"))
            .join(Post, Post.id == AnalyticsEvent.post_id, isouter=True)
            .where(
                AnalyticsEvent.created_at >= start,
                AnalyticsEvent.created_at <= end,
                (Post.user_id == creator_id)
                | (AnalyticsEvent.target_user_id == creator_id),
                AnalyticsEvent.event_type.in_(ENGAGEMENT_EVENTS + (EventType.VIDEO_VIEWED, EventType.FOLLOW_CREATED)),
            )
            .group_by(day, AnalyticsEvent.event_type)
        )
        result = await self.db.execute(stmt)
        grouped: dict[str, dict] = defaultdict(lambda: {"views": 0, "likes": 0, "comments": 0, "shares": 0, "saves": 0, "followers_gained": 0})
        keys = {
            EventType.VIDEO_VIEWED: "views",
            EventType.LIKE_CREATED: "likes",
            EventType.COMMENT_CREATED: "comments",
            EventType.SHARE_CREATED: "shares",
            EventType.SAVE_CREATED: "saves",
            EventType.FOLLOW_CREATED: "followers_gained",
        }
        for row in result.all():
            grouped[str(row.day)][keys[row.event_type]] = int(row.count or 0)
        return [{"date": date, **values} for date, values in sorted(grouped.items())]