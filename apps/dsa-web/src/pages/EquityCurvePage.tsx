import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { LineChart as LineIcon, TrendingDown, TrendingUp } from 'lucide-react';
import { portfolioApi } from '../api/portfolio';
import type { ParsedApiError } from '../api/error';
import { ApiErrorAlert, AppPage, Card, EmptyState, PageHeader, StatCard } from '../components/common';
import { cn } from '../utils/cn';

type CurvePoint = {
  date: string;
  totalEquity: number;
  totalMarketValue: number;
  totalCash: number;
  drawdownPct: number;
};

type CurveData = {
  series?: CurvePoint[];
  summary?: {
    beginEquity?: number | null;
    endEquity?: number | null;
    returnPct?: number | null;
    maxDrawdownPct?: number | null;
    points?: number;
  };
};

const DAY_OPTIONS = [
  { label: '近 30 天', value: 30 },
  { label: '近 90 天', value: 90 },
  { label: '近 180 天', value: 180 },
  { label: '近 365 天', value: 365 },
  { label: '全部', value: 0 },
];

const TOOLTIP_STYLE = {
  borderRadius: 12,
  fontSize: 12,
  backgroundColor: 'hsl(var(--card))',
  border: '1px solid hsl(var(--border))',
  color: 'hsl(var(--foreground))',
};

function fmt(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '--';
  return v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtSigned(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '--';
  const s = v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return v > 0 ? `+${s}` : s;
}

export const EquityCurvePage: React.FC = () => {
  const [days, setDays] = useState(180);
  const [data, setData] = useState<CurveData | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);

  const load = useCallback(async (target: number) => {
    setIsLoading(true);
    setError(null);
    try {
      const result = await portfolioApi.getEquityCurve({ days: target, costMethod: 'fifo' });
      setData(result as CurveData);
    } catch (err) {
      setError({ title: '加载失败', message: '加载资产曲线失败', rawMessage: String(err), category: 'unknown' });
      setData(null);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(days);
  }, [load, days]);

  const series = data?.series ?? [];
  const summary = data?.summary ?? {};
  const retPct = summary.returnPct;
  const retColor = retPct === null || retPct === undefined || retPct === 0 ? 'text-foreground' : retPct > 0 ? 'text-[#0a8f5c]' : 'text-[#e04545]';

  const chartData = useMemo(
    () =>
      series.map((p) => ({
        date: p.date,
        总资产: p.totalEquity,
        回撤: p.drawdownPct,
      })),
    [series],
  );

  return (
    <AppPage>
      <PageHeader
        eyebrow="Portfolio"
        title="资产曲线"
        description="基于每日快照的总资产走势与最大回撤监控"
      />

      <div className="mb-4 flex flex-wrap items-center gap-2">
        {DAY_OPTIONS.map((opt) => (
          <button
            key={opt.value}
            type="button"
            onClick={() => setDays(opt.value)}
            className={cn(
              'rounded-lg px-3 py-1.5 text-sm transition-colors',
              days === opt.value
                ? 'bg-primary-gradient text-[hsl(var(--primary-foreground))]'
                : 'border border-border bg-card text-muted-text hover:text-foreground',
            )}
          >
            {opt.label}
          </button>
        ))}
        {isLoading ? <span className="text-xs text-muted-text">加载中…</span> : null}
      </div>

      {error ? (
        <ApiErrorAlert error={error} actionLabel="重试" onAction={() => void load(days)} dismissLabel="关闭" onDismiss={() => setError(null)} />
      ) : null}

      {!isLoading && !error && !data ? (
        <Card variant="bordered" padding="lg">
          <EmptyState
            title="暂无资产曲线数据"
            description="每日快照尚未积累数据。多打开几次持仓页刷新快照后，这里会展示总资产走势。"
          />
        </Card>
      ) : null}

      {data ? (
        <div className="space-y-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatCard label="期初总资产" value={`¥${fmt(summary.beginEquity)}`} icon={<LineIcon className="h-4 w-4 text-muted-text" />} />
            <StatCard label="期末总资产" value={`¥${fmt(summary.endEquity)}`} icon={<LineIcon className="h-4 w-4 text-muted-text" />} />
            <StatCard
              label="区间收益率"
              value={retPct === null || retPct === undefined ? '--' : `${fmtSigned(retPct)}%`}
              className={retColor}
              icon={<TrendingUp className="h-4 w-4 text-muted-text" />}
            />
            <StatCard
              label="最大回撤"
              value={summary.maxDrawdownPct === null || summary.maxDrawdownPct === undefined ? '--' : `${fmtSigned(summary.maxDrawdownPct)}%`}
              tone="danger"
              icon={<TrendingDown className="h-4 w-4 text-muted-text" />}
            />
          </div>

          <Card variant="bordered" padding="md">
            <h2 className="mb-2 text-sm font-semibold text-foreground">
              总资产走势（{summary.points ?? 0} 个快照点）
            </h2>
            {chartData.length ? (
              <div className="h-[360px] w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={chartData} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                    <XAxis
                      dataKey="date"
                      tick={{ fontSize: 11, fill: 'hsl(var(--muted-text))' }}
                      tickFormatter={(v: string) => v.slice(5)}
                      minTickGap={32}
                    />
                    <YAxis
                      tick={{ fontSize: 11, fill: 'hsl(var(--muted-text))' }}
                      tickFormatter={(v: number) => `¥${(v / 10000).toFixed(1)}万`}
                      width={72}
                      domain={['auto', 'auto']}
                    />
                    <Tooltip
                      formatter={(value) => [`¥${Number(value).toLocaleString('zh-CN')}`, '总资产']}
                      labelFormatter={(label) => `日期 ${String(label)}`}
                      contentStyle={TOOLTIP_STYLE}
                      itemStyle={{ color: 'hsl(var(--foreground))' }}
                      labelStyle={{ color: 'hsl(var(--foreground))' }}
                    />
                    <Line type="monotone" dataKey="总资产" stroke="#2563eb" strokeWidth={2.5} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            ) : (
              <p className="py-8 text-center text-sm text-muted-text">暂无快照点</p>
            )}
          </Card>

          <Card variant="bordered" padding="md">
            <h2 className="mb-2 text-sm font-semibold text-foreground">最大回撤</h2>
            {chartData.length ? (
              <div className="h-[180px] w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={chartData} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                    <XAxis dataKey="date" tick={{ fontSize: 11, fill: 'hsl(var(--muted-text))' }} tickFormatter={(v: string) => v.slice(5)} minTickGap={48} />
                    <YAxis
                      tick={{ fontSize: 11, fill: 'hsl(var(--muted-text))' }}
                      tickFormatter={(v: number) => `${v.toFixed(1)}%`}
                      width={48}
                      domain={['auto', 0]}
                    />
                    <Tooltip
                      formatter={(value) => [`${Number(value).toFixed(2)}%`, '回撤']}
                      labelFormatter={(label) => `日期 ${String(label)}`}
                      contentStyle={TOOLTIP_STYLE}
                      itemStyle={{ color: 'hsl(var(--foreground))' }}
                      labelStyle={{ color: 'hsl(var(--foreground))' }}
                    />
                    <Line type="monotone" dataKey="回撤" stroke="#e04545" strokeWidth={2} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            ) : (
              <p className="py-8 text-center text-sm text-muted-text">暂无快照点</p>
            )}
          </Card>
        </div>
      ) : null}
    </AppPage>
  );
};

export default EquityCurvePage;
