"""
Tests for the creator dashboard analytics resolvers in api/graphql.py.

Covers:
- Auth/ownership requirements for creator_analytics and post_analytics
- Aggregation of views/watch time/completions/rewatches from InteractionSignal
- Follower growth, likes/shares (signal-based) and comments (join-based) totals
- Engagement rate calculation and top-post ranking
- Per-post analytics (avg watch time, completion rate)

Follows the pattern established in test_social_interactions.py: resolvers are
called directly with a lightweight AppContext, and repository methods are
monkeypatched so no real (Postgres-only) database is required.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.graphql import AppContext, _creator_analytics, _creator_video_analytics, _post_analytics
from app.models.analytics import SignalType
from services.creator_analytics_service import CreatorAnalyticsService


def make_ctx(user_id: uuid.UUID | None = None) -> AppContext:
    user = SimpleNamespace(id=user_id or uuid.uuid4())
    return AppContext(db=AsyncMock(), current_user=user)


def make_post(user_id, **overrides) -> SimpleNamespace:
    defaults = dict(
        id=uuid.uuid4(),
        user_id=user_id,
        like_count=0,
        comment_count=0,
        share_count=0,
        view_count=0,
        content_type=None,
        status=None,
        title=None,
        body=None,
        caption=None,
        tags=None,
        mentions=None,
        sound_track=None,
        scheduled_at=None,
        published_at=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestCreatorAnalytics:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        ctx = AppContext(db=AsyncMock(), current_user=None)
        period = SimpleNamespace(start=datetime.now(timezone.utc), end=datetime.now(timezone.utc))
        with pytest.raises(PermissionError):
            await _creator_analytics(ctx, period)

    @pytest.mark.asyncio
    async def test_aggregates_from_existing_data(self, monkeypatch):
        ctx = make_ctx()
        posts = [
            make_post(ctx.user.id, like_count=10, comment_count=2, share_count=1, view_count=100),
            make_post(ctx.user.id, like_count=1, comment_count=0, share_count=0, view_count=5),
        ]

        async def fake_get_by_user_id(self, user_id, limit=500):
            return posts

        signals = {
            SignalType.VIEW: {"count": 8, "total": 8.0},
            SignalType.REWATCH: {"count": 2, "total": 2.0},
            SignalType.LIKE: {"count": 11, "total": 11.0},
            SignalType.SHARE: {"count": 1, "total": 1.0},
            SignalType.WATCH_DURATION: {"count": 10, "total": 500.0},
            SignalType.COMPLETION: {"count": 4, "total": 4.0},
        }

        async def fake_signal_totals(self, *, creator_id=None, post_id=None, start=None, end=None):
            assert creator_id == ctx.user.id
            return signals

        async def fake_count_for_creator(self, creator_id, start=None, end=None):
            return 3

        async def fake_count_followers_since(self, user_id, start=None, end=None):
            return 7

        monkeypatch.setattr(
            "repositories.content_repository.PostRepository.get_by_user_id", fake_get_by_user_id
        )
        monkeypatch.setattr(
            "repositories.analytics_repository.AnalyticsRepository.signal_totals",
            fake_signal_totals,
        )
        monkeypatch.setattr(
            "repositories.content_repository.CommentRepository.count_for_creator",
            fake_count_for_creator,
        )
        monkeypatch.setattr(
            "repositories.social_repository.FollowRepository.count_followers_since",
            fake_count_followers_since,
        )

        period = SimpleNamespace(
            start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 1, 31, tzinfo=timezone.utc),
        )
        result = await _creator_analytics(ctx, period)

        assert result.total_posts == 2
        assert result.total_views == 10  # VIEW + REWATCH
        assert result.total_likes == 11
        assert result.total_shares == 1
        assert result.total_comments == 3
        assert result.follower_growth == 7
        assert result.new_followers == 7
        assert result.lost_followers == 0
        assert result.engagement_rate == pytest.approx((11 + 3 + 1) / 10 * 100)
        assert len(result.top_posts) == 2
        assert result.top_posts[0].likes == 10  # highest-engagement post first

    @pytest.mark.asyncio
    async def test_no_views_gives_zero_engagement_rate(self, monkeypatch):
        ctx = make_ctx()

        async def fake_get_by_user_id(self, user_id, limit=500):
            return []

        async def fake_signal_totals(self, **kwargs):
            return {}

        async def fake_count_for_creator(self, creator_id, start=None, end=None):
            return 0

        async def fake_count_followers_since(self, user_id, start=None, end=None):
            return 0

        monkeypatch.setattr(
            "repositories.content_repository.PostRepository.get_by_user_id", fake_get_by_user_id
        )
        monkeypatch.setattr(
            "repositories.analytics_repository.AnalyticsRepository.signal_totals",
            fake_signal_totals,
        )
        monkeypatch.setattr(
            "repositories.content_repository.CommentRepository.count_for_creator",
            fake_count_for_creator,
        )
        monkeypatch.setattr(
            "repositories.social_repository.FollowRepository.count_followers_since",
            fake_count_followers_since,
        )

        period = SimpleNamespace(start=datetime.now(timezone.utc), end=datetime.now(timezone.utc))
        result = await _creator_analytics(ctx, period)

        assert result.total_views == 0
        assert result.engagement_rate == 0.0
        assert result.top_posts == []

    @pytest.mark.asyncio
    async def test_net_follower_growth_and_unfollows(self, monkeypatch):
        ctx = make_ctx()
        start = datetime(2026, 2, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 28, tzinfo=timezone.utc)

        async def fake_get_by_user_id(self, user_id, limit=500):
            return []

        signals = {
            SignalType.VIEW: {"count": 10, "total": 10.0},
            SignalType.FOLLOW: {"count": 15, "total": 15.0},
            SignalType.UNFOLLOW: {"count": 3, "total": 3.0},
            SignalType.SAVE: {"count": 2, "total": 2.0},
            SignalType.UNSAVE: {"count": 1, "total": 1.0},
            SignalType.LIKE: {"count": 5, "total": 5.0},
            SignalType.UNLIKE: {"count": 1, "total": 1.0},
        }

        async def fake_signal_totals(self, *, creator_id=None, post_id=None, start=None, end=None):
            return signals

        async def fake_count_for_creator(self, creator_id, start=None, end=None):
            return 1

        async def fake_count_followers_since(self, user_id, start=None, end=None):
            return 15

        monkeypatch.setattr("repositories.content_repository.PostRepository.get_by_user_id", fake_get_by_user_id)
        monkeypatch.setattr("repositories.analytics_repository.AnalyticsRepository.signal_totals", fake_signal_totals)
        monkeypatch.setattr("repositories.content_repository.CommentRepository.count_for_creator", fake_count_for_creator)
        monkeypatch.setattr("repositories.social_repository.FollowRepository.count_followers_since", fake_count_followers_since)

        period = SimpleNamespace(start=start, end=end)
        result = await _creator_analytics(ctx, period)

        assert result.new_followers == 15
        assert result.lost_followers == 3
        assert result.follower_growth == 12
        assert result.total_likes == 4
        assert result.total_saves == 1

    @pytest.mark.asyncio
    async def test_creator_video_analytics_authorization(self):
        ctx = AppContext(db=AsyncMock(), current_user=None)
        period = SimpleNamespace(start=datetime.now(timezone.utc), end=datetime.now(timezone.utc))
        with pytest.raises(PermissionError):
            await _creator_video_analytics(ctx, period, "views")

    @pytest.mark.asyncio
    async def test_creator_analytics_service_overview_and_per_post(self, monkeypatch):
        creator_id = uuid.uuid4()
        post1 = make_post(creator_id, like_count=10, view_count=50)
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 1, 31, tzinfo=timezone.utc)

        db = AsyncMock()
        service = CreatorAnalyticsService(db)

        async def fake_posts(c_id):
            return [post1]

        signals = {
            SignalType.VIEW: {"count": 20, "total": 20.0},
            SignalType.REWATCH: {"count": 5, "total": 5.0},
            SignalType.WATCH_DURATION: {"count": 25, "total": 500.0},
            SignalType.COMPLETION: {"count": 10, "total": 10.0},
            SignalType.LIKE: {"count": 8, "total": 8.0},
            SignalType.SHARE: {"count": 2, "total": 2.0},
            SignalType.SAVE: {"count": 3, "total": 3.0},
            SignalType.FOLLOW: {"count": 5, "total": 5.0},
            SignalType.UNFOLLOW: {"count": 1, "total": 1.0},
        }

        async def fake_signal_totals(*args, **kwargs):
            return signals

        per_post = {
            post1.id: {
                SignalType.VIEW: {"count": 20, "total": 20.0},
                SignalType.REWATCH: {"count": 5, "total": 5.0},
                SignalType.WATCH_DURATION: {"count": 25, "total": 500.0},
                SignalType.COMPLETION: {"count": 10, "total": 10.0},
                SignalType.LIKE: {"count": 8, "total": 8.0},
                SignalType.SHARE: {"count": 2, "total": 2.0},
                SignalType.SAVE: {"count": 3, "total": 3.0},
            }
        }

        async def fake_per_post(*args, **kwargs):
            return per_post

        async def fake_count_for_creator(*args, **kwargs):
            return 0

        monkeypatch.setattr(service, "_posts", fake_posts)
        monkeypatch.setattr(service.analytics_repo, "signal_totals", fake_signal_totals)
        monkeypatch.setattr(service.analytics_repo, "per_post_signal_totals", fake_per_post)
        monkeypatch.setattr("repositories.content_repository.CommentRepository.count_for_creator", fake_count_for_creator)

        overview = await service.overview(creator_id, start, end)

        assert overview["total_posts"] == 1
        assert overview["total_views"] == 25
        assert overview["total_likes"] == 8
        assert overview["total_shares"] == 2
        assert overview["total_saves"] == 3
        assert overview["new_followers"] == 5
        assert overview["lost_followers"] == 1
        assert overview["follower_growth"] == 4
        assert overview["avg_watch_time"] == pytest.approx(500.0 / 25)
        assert overview["completion_rate"] == pytest.approx(10 / 25 * 100)
        assert len(overview["top_posts"]) == 1


class TestPostAnalytics:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        ctx = AppContext(db=AsyncMock(), current_user=None)
        with pytest.raises(PermissionError):
            await _post_analytics(ctx, uuid.uuid4())

    @pytest.mark.asyncio
    async def test_rejects_non_owner(self, monkeypatch):
        ctx = make_ctx()
        post = make_post(uuid.uuid4())  # owned by someone else

        async def fake_get_by_id(self, post_id):
            return post

        monkeypatch.setattr(
            "repositories.content_repository.PostRepository.get_by_id", fake_get_by_id
        )

        with pytest.raises(PermissionError):
            await _post_analytics(ctx, post.id)

    @pytest.mark.asyncio
    async def test_returns_none_when_post_missing(self, monkeypatch):
        ctx = make_ctx()

        async def fake_get_by_id(self, post_id):
            return None

        monkeypatch.setattr(
            "repositories.content_repository.PostRepository.get_by_id", fake_get_by_id
        )

        assert await _post_analytics(ctx, uuid.uuid4()) is None

    @pytest.mark.asyncio
    async def test_computes_watch_time_and_completion_rate(self, monkeypatch):
        ctx = make_ctx()
        post = make_post(
            ctx.user.id, like_count=5, comment_count=1, share_count=0, view_count=20
        )

        async def fake_get_by_id(self, post_id):
            return post

        signals = {
            SignalType.VIEW: {"count": 15, "total": 15.0},
            SignalType.REWATCH: {"count": 5, "total": 5.0},
            SignalType.WATCH_DURATION: {"count": 20, "total": 1000.0},
            SignalType.COMPLETION: {"count": 10, "total": 10.0},
        }

        async def fake_signal_totals(self, *, creator_id=None, post_id=None, start=None, end=None):
            assert post_id == post.id
            return signals

        monkeypatch.setattr(
            "repositories.content_repository.PostRepository.get_by_id", fake_get_by_id
        )
        monkeypatch.setattr(
            "repositories.analytics_repository.AnalyticsRepository.signal_totals",
            fake_signal_totals,
        )

        result = await _post_analytics(ctx, post.id)

        assert result.views == 20
        assert result.likes == 5
        assert result.avg_watch_time == pytest.approx(1000.0 / 20)
        assert result.completion_rate == pytest.approx(10 / 20 * 100)

    @pytest.mark.asyncio
    async def test_no_watch_events_gives_none_rates(self, monkeypatch):
        ctx = make_ctx()
        post = make_post(ctx.user.id)

        async def fake_get_by_id(self, post_id):
            return post

        async def fake_signal_totals(self, **kwargs):
            return {}

        monkeypatch.setattr(
            "repositories.content_repository.PostRepository.get_by_id", fake_get_by_id
        )
        monkeypatch.setattr(
            "repositories.analytics_repository.AnalyticsRepository.signal_totals",
            fake_signal_totals,
        )

        result = await _post_analytics(ctx, post.id)

        assert result.avg_watch_time is None
        assert result.completion_rate is None
