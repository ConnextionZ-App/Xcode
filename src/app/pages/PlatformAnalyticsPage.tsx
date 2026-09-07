import { useEffect, useState } from "react";
import { ArrowLeft, RefreshCw, ShieldAlert } from "lucide-react";
import { graphqlRequestResult } from "../profile-graphql";
import { ACCENT, useTokens } from "../settings-ui";
import { useTheme } from "../ThemeContext";
import type { PageProps } from "./settingsPages.types";

type Range = "today" | "7d" | "28d" | "90d";
const ranges: { id: Range; label: string; days: number }[] = [
  { id: "today", label: "Today", days: 1 },
  { id: "7d", label: "7 Days", days: 7 },
  { id: "28d", label: "28 Days", days: 28 },
  { id: "90d", label: "90 Days", days: 90 },
];

type PlatformData = {
  platformAnalytics: {
    totalUsers: number; newUsers: number; activeUsers: number; totalViews: number;
    totalPublishedVideos: number; engagementRate: number; followsCreated: number;
    collabsCreated: number; totalLikes: number; totalComments: number; totalShares: number;
    totalSaves: number; averageWatchTime: number | null; completionRate: number | null;
  };
  platformAnalyticsTrends: { date: string; views: number; uploads: number; publishedVideos: number; engagement: number }[];
  platformTopContent: { post: { id: string; caption: string; thumbnail: string }; views: number; likes: number; comments: number; shares: number; saves: number; engagementRate: number }[];
};

export function PlatformAnalyticsPage({ onBack, t }: PageProps) {
  const isDark = useTheme();
  const tokens = useTokens(isDark);
  const [range, setRange] = useState<Range>("28d");
  const [data, setData] = useState<PlatformData | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");

  const load = async (next: Range) => {
    setStatus("loading");
    const option = ranges.find((item) => item.id === next)!;
    const end = new Date();
    const start = new Date(end.getTime() - option.days * 86_400_000);
    const result = await graphqlRequestResult<PlatformData>(`
      query PlatformAnalytics($period: AnalyticsPeriod!) {
        platformAnalytics(period: $period) { totalUsers newUsers activeUsers totalViews totalPublishedVideos engagementRate followsCreated collabsCreated totalLikes totalComments totalShares totalSaves averageWatchTime completionRate }
        platformAnalyticsTrends(period: $period) { date views uploads publishedVideos engagement }
        platformTopContent(period: $period, sortBy: "views") { post { id caption thumbnail } views likes comments shares saves engagementRate }
      }
    `, { period: { start: start.toISOString(), end: end.toISOString() } });
    if (!result.ok) { setStatus("error"); return; }
    setData(result.value);
    setStatus("ready");
  };

  useEffect(() => { void load(range); }, [range]);

  return (
    <div className="absolute inset-0 z-30 flex flex-col" style={{ background: tokens.bg }}>
      <header className="flex items-center gap-3 px-5 pt-14 pb-4" style={{ borderBottom: `1px solid ${tokens.divider}` }}>
        <button onClick={onBack} aria-label="Back" className="w-9 h-9 rounded-full flex items-center justify-center" style={{ background: tokens.backBtnBg, border: tokens.cardBorder }}><ArrowLeft className="w-4 h-4" /></button>
        <div className="flex-1"><h1 className="font-extrabold text-[22px]" style={{ color: tokens.heading }}>Platform Analytics</h1><p className="text-[12px]" style={{ color: tokens.sub }}>Internal administrator view</p></div>
        <button onClick={() => void load(range)} aria-label="Refresh" className="w-9 h-9 rounded-full flex items-center justify-center" style={{ background: tokens.chipBg, border: tokens.chipBorder }}><RefreshCw className={status === "loading" ? "animate-spin" : ""} /></button>
      </header>
      <div className="flex gap-2 px-5 py-3">
        {ranges.map((option) => <button key={option.id} onClick={() => setRange(option.id)} className="px-3 py-1.5 rounded-full text-[12px] font-bold" style={{ color: range === option.id ? ACCENT : tokens.sub, border: `1px solid ${range === option.id ? ACCENT : tokens.divider}` }}>{option.label}</button>)}
      </div>
      <main className="flex-1 overflow-y-auto px-5 pb-10">
        {status === "loading" && <p className="py-12 text-center" style={{ color: tokens.sub }}>Loading platform analytics...</p>}
        {status === "error" && <div className="py-12 text-center"><ShieldAlert className="mx-auto mb-3" style={{ color: "#f87171" }} /><p className="font-bold" style={{ color: tokens.heading }}>Platform analytics unavailable</p><p className="text-[13px] mt-1" style={{ color: tokens.sub }}>You may not have administrator access, or the service is unavailable.</p></div>}
        {status === "ready" && data && <>
          <section className="grid grid-cols-2 gap-3 mb-4">
            {[["Total users", data.platformAnalytics.totalUsers], ["Active users", data.platformAnalytics.activeUsers], ["New users", data.platformAnalytics.newUsers], ["Views", data.platformAnalytics.totalViews], ["Published videos", data.platformAnalytics.totalPublishedVideos], ["Engagement", `${data.platformAnalytics.engagementRate.toFixed(1)}%`], ["Follows", data.platformAnalytics.followsCreated], ["Collabs", data.platformAnalytics.collabsCreated]].map(([label, value]) => <div key={String(label)} className="rounded-2xl p-3.5" style={{ background: tokens.groupBg, border: tokens.groupBorder }}><p className="font-extrabold text-[20px]" style={{ color: tokens.heading }}>{value}</p><p className="text-[12px]" style={{ color: tokens.sub }}>{label}</p></div>)}
          </section>
          <section className="rounded-2xl p-4 mb-4" style={{ background: tokens.groupBg, border: tokens.groupBorder }}><h2 className="font-bold mb-3" style={{ color: tokens.heading }}>Activity trend</h2>{data.platformAnalyticsTrends.length === 0 ? <p className="text-[13px]" style={{ color: tokens.sub }}>No activity in this period.</p> : data.platformAnalyticsTrends.slice(-14).map((point) => <div key={point.date} className="flex justify-between text-[12px] py-1" style={{ color: tokens.sub }}><span>{point.date}</span><span>{point.views} views · {point.engagement} engagements</span></div>)}</section>
          <section><h2 className="font-bold mb-3" style={{ color: tokens.heading }}>Top content</h2>{data.platformTopContent.length === 0 ? <p className="text-[13px]" style={{ color: tokens.sub }}>No published content activity in this period.</p> : <div className="space-y-2">{data.platformTopContent.map((item) => <div key={item.post.id} className="rounded-2xl p-3" style={{ background: tokens.groupBg, border: tokens.groupBorder }}><p className="font-semibold text-[13px] truncate" style={{ color: tokens.heading }}>{item.post.caption || "Untitled post"}</p><p className="text-[12px] mt-1" style={{ color: tokens.sub }}>{item.views} views · {item.likes} likes · {item.engagementRate.toFixed(1)}% engagement</p></div>)}</div>}</section>
        </>}
      </main>
    </div>
  );
}
