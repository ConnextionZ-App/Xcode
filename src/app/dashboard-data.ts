// ─── DASHBOARD DATA ──────────────────────────────────────────────────────────
//
// Dashboard analytics are loaded from the authenticated creator GraphQL queries.
// The screen owns presentation only; aggregation and date filtering stay in the backend.

import { type Result } from "./auth-store";
import { analyticsPeriod } from "./analytics-utils";
import { type ContentItem } from "./creators";
import { graphqlRequestResult } from "./profile-graphql";

// ─── SHAPES ──────────────────────────────────────────────────────────────────

export type Range = "7d" | "30d" | "90d";

export const RANGES: { id: Range; label: string; days: number }[] = [
  { id: "7d", label: "7 days", days: 7 },
  { id: "30d", label: "30 days", days: 30 },
  { id: "90d", label: "90 days", days: 90 },
];

export type MetricKey = "views" | "likes" | "comments" | "shares" | "followers";

export interface Metric {
  key: MetricKey;
  label: string;
  /** Total across the range. */
  value: number;
  /** Change against the previous range of the same length, as a percentage. */
  deltaPct: number;
  /** One point per day, oldest first — what the sparkline and bars draw. */
  series: number[];
}

export interface CollabStats {
  totalRequests: number;
  pending: number;
  accepted: number;
  completed: number;
  active: number;
  successRatePct: number;
  avgResponseHours: number;
  collabScore: number;
  repeatCollaborators: number;
  freelanceOpportunities: number;
  jobOffers: number;
  brandInvitations: number;
  adOpportunities: number;
}

/** A row in the content-management list. */
export interface ContentRow extends ContentItem {
  comments: number;
  shares: number;
  /** Present only for posts the viewer uploaded in this prototype. */
  own?: OwnPost;
  createdAt?: number;
}

export interface DashboardData {
  range: Range;
  days: number;
  metrics: Metric[];
  collab: CollabStats;
  content: ContentRow[];
  /** Best-performing post in the range — the "what worked" callout. */
  best?: ContentRow;
  quality: {
    uniqueViewers: number;
    saves: number;
    avgWatchTime: number | null;
    completionRate: number | null;
    engagementRate: number;
  };
  generatedAt: number;
}

const METRIC_LABELS: Record<MetricKey, string> = {
  views: "Views",
  likes: "Likes",
  comments: "Comments",
  shares: "Shares",
  followers: "New followers",
};

export interface CreatorAnalyticsResponse {
  creatorAnalytics: {
    totalViews: number; uniqueViewers: number; totalLikes: number; totalComments: number;
    totalShares: number; totalSaves: number; followerGrowth: number; newFollowers: number;
    lostFollowers: number; avgWatchTime: number | null; completionRate: number | null;
    engagementRate: number; totalPosts: number;
  };
  creatorVideoAnalytics: { post: ContentItem; views: number; likes: number; comments: number; shares: number; saves: number }[];
  creatorAnalyticsTrends: { date: string; views: number; likes: number; comments: number; shares: number; saves: number; followersGained: number }[];
}

export async function fetchCreatorAnalytics(range: Range): Promise<Result<CreatorAnalyticsResponse>> {
  return graphqlRequestResult<CreatorAnalyticsResponse>(`
    query CreatorAnalytics($period: AnalyticsPeriod!) {
      creatorAnalytics(period: $period) {
        totalPosts totalViews uniqueViewers totalLikes totalComments totalShares totalSaves
        followerGrowth newFollowers lostFollowers avgWatchTime completionRate engagementRate
      }
      creatorVideoAnalytics(period: $period, sortBy: "views") {
        post { id thumbnail caption views likes status scheduledAt collabWith }
        views likes comments shares saves
      }
      creatorAnalyticsTrends(period: $period) { date views likes comments shares saves followersGained }
    }
  `, { period: analyticsPeriod(range) });
}

// ─── FETCH ───────────────────────────────────────────────────────────────────

/**
 * The network seam. Latency is real enough that the screen's skeleton is worth
 * having, and an offline browser fails the way a fetch would — the dashboard is
 * the screen where a silent stale number would be most misleading.
 */
export async function fetchDashboard(range: Range): Promise<Result<DashboardData>> {
  const days = RANGES.find((r) => r.id === range)!.days;
  const result = await fetchCreatorAnalytics(range);
  if (!result.ok) return result;

  const summary = result.value.creatorAnalytics;
  const trends = result.value.creatorAnalyticsTrends;
  const seriesFor = (key: "views" | "likes" | "comments" | "shares" | "followers") =>
    trends.map((point) => key === "followers" ? point.followersGained : point[key]);
  const metric = (key: MetricKey, value: number): Metric => ({
    key, label: METRIC_LABELS[key], value, deltaPct: 0, series: seriesFor(key),
  });
  const content = result.value.creatorVideoAnalytics.map((row) => ({
    ...row.post, views: row.views, likes: row.likes, comments: row.comments, shares: row.shares,
  } as ContentRow));
  const collab: CollabStats = {
    totalRequests: 0, pending: 0, accepted: 0, completed: 0, active: 0, successRatePct: 0,
    avgResponseHours: 0, collabScore: 0, repeatCollaborators: 0, freelanceOpportunities: 0,
    jobOffers: 0, brandInvitations: 0, adOpportunities: 0,
  };
  return {
    ok: true,
    value: {
      range, days,
      metrics: [metric("views", summary.totalViews), metric("likes", summary.totalLikes), metric("comments", summary.totalComments), metric("shares", summary.totalShares), metric("followers", summary.newFollowers)],
      collab, content, best: content[0], quality: {
        uniqueViewers: summary.uniqueViewers,
        saves: summary.totalSaves,
        avgWatchTime: summary.avgWatchTime,
        completionRate: summary.completionRate,
        engagementRate: summary.engagementRate,
      }, generatedAt: Date.now(),
    },
  };
}

// ─── FORMATTING ──────────────────────────────────────────────────────────────

/** Axis labels: "Mon", or a date once the window is longer than a fortnight. */
export function axisLabels(days: number, now = Date.now()): string[] {
  return Array.from({ length: days }, (_, i) => {
    const date = new Date(now - (days - 1 - i) * 86_400_000);
    return days <= 14
      ? date.toLocaleDateString(undefined, { weekday: "short" })
      : date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  });
}

export const signed = (n: number) => `${n > 0 ? "+" : ""}${n}%`;
