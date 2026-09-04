import React, { useCallback, useEffect, useState } from 'react';
import {
  ArrowDownCircle,
  ArrowUpCircle,
  Banknote,
  CalendarDays,
  ChevronDown,
  ChevronUp,
  Coins,
  ReceiptText,
} from 'lucide-react';
import { portfolioApi } from '../api/portfolio';
import { thsApi, type ThsStockLedgerItem } from '../api/thsSync';
import type { ParsedApiError } from '../api/error';
import { ApiErrorAlert, AppPage, Card, EmptyState, PageHeader, StatCard } from '../components/common';
import { cn } from '../utils/cn';

type TradeDetail = {
  date: string;
  symbol: string;
  side: 'buy' | 'sell';
  quantity: number;
  price: number;
  amount: number;
  fee: number;
};

type StatementData = {
  month?: string;
  source?: 'local' | 'ths';
  trades?: {
    buyCount?: number;
    buyAmount?: number;
    buyFee?: number;
    sellCount?: number;
    sellAmount?: number;
    sellFee?: number;
    netCashOutflow?: number;
  };
  cash?: { inflow?: number; outflow?: number; net?: number };
  dividends?: { count?: number; items?: Array<{ date: string; symbol: string; name?: string; amount: number; type?: string }> };
  asset?: { beginEquity?: number | null; endEquity?: number | null; returnPct?: number | null };
  details?: TradeDetail[];
};

function fmt(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '--';
  return v.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

// 按原始精度展示，保留到实际存储位数（去多余尾零），不做四舍五入缩位
function fmtPrice(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '--';
  return String(Number(v.toFixed(4)));
}

function fmtSigned(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '--';
  const s = v.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return v > 0 ? `+${s}` : s;
}

function currentMonth(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

// ---------------- 个股流水 ---------------- //
function recordCategory(item: ThsStockLedgerItem['records'][number]) {
  const t = item.recordType;
  if (t === '买入') return { label: '买入', cls: 'text-[#e04545] bg-[#e04545]/10' };
  if (t === '卖出') return { label: '卖出', cls: 'text-[#0a8f5c] bg-[#0a8f5c]/10' };
  if (t === '除权除息') {
    return item.amount > 0
      ? { label: '分红', cls: 'text-amber-600 bg-amber-500/10 dark:text-amber-400' }
      : { label: '除权调整', cls: 'text-muted-text bg-base/70' };
  }
  if (t.includes('股息个税') || t === '缴税') return { label: '股息个税', cls: 'text-muted-text bg-base/70' };
  return { label: t || '其他', cls: 'text-muted-text bg-base/70' };
}

function StockLedgerView() {
  const [ledger, setLedger] = useState<ThsStockLedgerItem[] | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const result = await thsApi.getStockLedger();
      setLedger(result.stocks ?? []);
    } catch (err) {
      setError({ title: '加载失败', message: '加载个股流水失败', rawMessage: String(err), category: 'unknown' });
      setLedger(null);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) {
    return (
      <ApiErrorAlert error={error} actionLabel="重试" onAction={() => void load()} dismissLabel="关闭" onDismiss={() => setError(null)} />
    );
  }

  if (isLoading && !ledger) {
    return <div className="text-sm text-muted-text">加载中…</div>;
  }

  if (!ledger || ledger.length === 0) {
    return (
      <Card variant="bordered" padding="lg">
        <EmptyState
          title="暂无个股流水"
          description="本地没有导入的账本导出流水。请先在「数据同步」中导入汇总持仓.xlsx，个股明细才会显示。"
        />
      </Card>
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-text">
        数据来自账本导出的「汇总持仓.xlsx」，含买入/卖出/分红/股息个税等全类别。点击股票展开明细。
      </p>
      {ledger.map((st) => {
        const isOpen = expanded === st.symbol;
        const totalFee = st.buyFee + st.sellFee + st.otherFee;
        return (
          <Card key={st.symbol} variant="bordered" padding="md">
            <button
              type="button"
              className="flex w-full items-center justify-between gap-3 text-left"
              onClick={() => setExpanded(isOpen ? null : st.symbol)}
            >
              <div className="flex min-w-0 items-center gap-3">
                <span className="font-medium text-foreground">{st.symbol}</span>
                {st.name ? <span className="truncate text-sm text-muted-text">{st.name}</span> : null}
                {st.latestDate ? (
                  <span className="hidden text-xs text-muted-text sm:inline">最近 {st.latestDate}</span>
                ) : null}
              </div>
              <div className="flex shrink-0 items-center gap-4 text-xs text-muted-text">
                <span>
                  买 <span className="text-foreground">{st.buyCount}</span>
                </span>
                <span>
                  卖 <span className="text-foreground">{st.sellCount}</span>
                </span>
                <span>
                  分红 <span className="text-amber-600 dark:text-amber-400">{st.dividendCount}</span>
                </span>
                {isOpen ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
              </div>
            </button>

            {isOpen ? (
              <div className="mt-4 space-y-4">
                <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                  <StatCard
                    label="累计买入"
                    value={`${st.buyCount} 笔 · ¥${fmt(st.buyAmount)}`}
                    icon={<ArrowDownCircle className="h-4 w-4 text-[#e04545]" />}
                  />
                  <StatCard
                    label="累计卖出"
                    value={`${st.sellCount} 笔 · ¥${fmt(st.sellAmount)}`}
                    icon={<ArrowUpCircle className="h-4 w-4 text-[#0a8f5c]" />}
                  />
                  <StatCard
                    label="累计费用"
                    value={`¥${fmt(totalFee)}`}
                    icon={<Banknote className="h-4 w-4 text-muted-text" />}
                  />
                  <StatCard
                    label="累计分红"
                    value={`${st.dividendCount} 笔 · ¥${fmt(st.dividendAmount)}`}
                    icon={<Coins className="h-4 w-4 text-muted-text" />}
                  />
                </div>

                {st.records.length ? (
                  <div className="overflow-x-auto">
                    <table className="w-full text-left text-sm">
                      <thead>
                        <tr className="border-b border-border text-xs text-muted-text">
                          <th className="pb-2 pr-3 font-normal">日期</th>
                          <th className="pb-2 pr-3 font-normal">类别</th>
                          <th className="pb-2 pr-3 text-right font-normal">数量</th>
                          <th className="pb-2 pr-3 text-right font-normal">价格</th>
                          <th className="pb-2 pr-3 text-right font-normal">金额</th>
                          <th className="pb-2 pr-3 text-right font-normal">费用</th>
                          {st.records.some((r) => r.note) ? (
                            <th className="pb-2 text-right font-normal">备注</th>
                          ) : null}
                        </tr>
                      </thead>
                      <tbody>
                        {st.records.map((r, idx) => {
                          const cat = recordCategory(r);
                          return (
                            <tr key={idx} className="border-b border-border/60 last:border-0">
                              <td className="py-2 pr-3 text-muted-text">
                                {r.date}
                                {r.time ? <span className="ml-1 text-xs text-muted-text/70">{r.time}</span> : null}
                              </td>
                              <td className="py-2 pr-3">
                                <span className={cn('inline-flex rounded-full px-2 py-0.5 text-xs', cat.cls)}>{cat.label}</span>
                              </td>
                              <td className="py-2 pr-3 text-right text-foreground">{fmt(r.quantity)}</td>
                              <td className="py-2 pr-3 text-right text-foreground">{fmtPrice(r.price)}</td>
                              <td className="py-2 pr-3 text-right font-medium text-foreground">¥{fmt(r.amount)}</td>
                              <td className="py-2 pr-3 text-right text-muted-text">¥{fmt(r.fee)}</td>
                              {st.records.some((rr) => rr.note) ? (
                                <td className="py-2 text-right text-xs text-muted-text">{r.note || ''}</td>
                              ) : null}
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="text-xs text-muted-text">该股票暂无明细记录。</p>
                )}
              </div>
            ) : null}
          </Card>
        );
      })}
    </div>
  );
}

// ---------------- 月度对账单 ---------------- //
function MonthlyView() {
  const [month, setMonth] = useState(currentMonth());
  const [useThs, setUseThs] = useState(true);
  const [data, setData] = useState<StatementData | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);

  const load = useCallback(async (target: string) => {
    setIsLoading(true);
    setError(null);
    try {
      const result = await portfolioApi.getMonthlyStatement(target, { costMethod: 'fifo', useThs });
      setData(result as StatementData);
    } catch (err) {
      setError({ title: '加载失败', message: '加载对账单失败', rawMessage: String(err), category: 'unknown' });
      setData(null);
    } finally {
      setIsLoading(false);
    }
  }, [useThs]);

  useEffect(() => {
    void load(month);
  }, [load, month]);

  const trades = data?.trades ?? {};
  const cash = data?.cash ?? {};
  const dividends = data?.dividends ?? {};
  const asset = data?.asset ?? {};
  const details = data?.details ?? [];
  const retPct = asset.returnPct;
  const retColor = retPct === null || retPct === undefined || retPct === 0 ? 'text-foreground' : retPct > 0 ? 'text-[#0a8f5c]' : 'text-[#e04545]';

  return (
    <div className="space-y-4">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2 rounded-xl border border-border bg-card px-3 py-2">
          <CalendarDays className="h-4 w-4 text-muted-text" />
          <input
            type="month"
            value={month}
            onChange={(e) => e.target.value && setMonth(e.target.value)}
            className="bg-transparent text-sm text-foreground outline-none"
          />
        </div>
        <button
          type="button"
          onClick={() => setUseThs((v) => !v)}
          className={cn(
            'inline-flex items-center gap-2 rounded-xl border px-3 py-2 text-sm transition-colors',
            useThs
              ? 'border-primary/40 bg-primary/10 text-foreground'
              : 'border-border bg-card text-muted-text hover:text-foreground',
          )}
        >
          <span
            className={cn(
              'relative inline-flex h-4 w-7 items-center rounded-full transition-colors',
              useThs ? 'bg-primary' : 'bg-border',
            )}
          >
            <span
              className={cn(
                'inline-block h-3 w-3 transform rounded-full bg-white transition-transform',
                useThs ? 'translate-x-3.5' : 'translate-x-0.5',
              )}
            />
          </span>
          使用同花顺交易数据（含国债逆回购）
        </button>
        {data?.source === 'ths' ? (
          <span className="inline-flex items-center rounded-full bg-emerald-500/10 px-2 py-0.5 text-xs text-emerald-600 dark:text-emerald-400">
            数据来源：同花顺
          </span>
        ) : null}
        {isLoading ? <span className="text-xs text-muted-text">加载中…</span> : null}
      </div>

      {error ? (
        <ApiErrorAlert error={error} actionLabel="重试" onAction={() => void load(month)} dismissLabel="关闭" onDismiss={() => setError(null)} />
      ) : null}

      {!isLoading && !error && !data ? (
        <Card variant="bordered" padding="lg">
          <EmptyState
            title="暂无对账单数据"
            description="当前月份没有交易、资金或分红流水。请先录入交易或切换月份。"
          />
        </Card>
      ) : null}

      {data ? (
        <>
          {/* 汇总卡片 */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatCard
              label="本月买入"
              value={`${trades.buyCount ?? 0} 笔 · ¥${fmt(trades.buyAmount)}`}
              icon={<ArrowDownCircle className="h-4 w-4 text-[#e04545]" />}
            />
            <StatCard
              label="本月卖出"
              value={`${trades.sellCount ?? 0} 笔 · ¥${fmt(trades.sellAmount)}`}
              icon={<ArrowUpCircle className="h-4 w-4 text-[#0a8f5c]" />}
            />
            <StatCard
              label="资金净流入"
              value={`¥${fmtSigned(cash.net)}`}
              icon={<Banknote className="h-4 w-4 text-muted-text" />}
            />
            <StatCard
              label="现金分红"
              value={`${dividends.count ?? 0} 笔`}
              icon={<Coins className="h-4 w-4 text-muted-text" />}
            />
          </div>

          {/* 交易与费用 */}
          <Card variant="bordered" padding="md">
            <h2 className="mb-3 text-sm font-semibold text-foreground">交易与费用</h2>
            <div className="grid grid-cols-2 gap-3 text-sm lg:grid-cols-4">
              <div className="rounded-lg bg-base/60 p-3">
                <p className="text-xs text-muted-text">买入金额</p>
                <p className="mt-1 font-medium text-foreground">¥{fmt(trades.buyAmount)}</p>
                <p className="text-xs text-muted-text">手续费/税费 ¥{fmt(trades.buyFee)}</p>
              </div>
              <div className="rounded-lg bg-base/60 p-3">
                <p className="text-xs text-muted-text">卖出金额</p>
                <p className="mt-1 font-medium text-foreground">¥{fmt(trades.sellAmount)}</p>
                <p className="text-xs text-muted-text">手续费/税费 ¥{fmt(trades.sellFee)}</p>
              </div>
              <div className="rounded-lg bg-base/60 p-3">
                <p className="text-xs text-muted-text">资金流入</p>
                <p className="mt-1 font-medium text-[#0a8f5c]">¥{fmt(cash.inflow)}</p>
              </div>
              <div className="rounded-lg bg-base/60 p-3">
                <p className="text-xs text-muted-text">资金流出</p>
                <p className="mt-1 font-medium text-[#e04545]">¥{fmt(cash.outflow)}</p>
              </div>
            </div>
          </Card>

          {/* 资产与收益 */}
          <Card variant="bordered" padding="md">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-sm font-semibold text-foreground">资产与收益</h2>
              <span className={`text-lg font-semibold ${retColor}`}>
                {retPct === null || retPct === undefined ? '月收益率 --' : `月收益率 ${fmtSigned(retPct)}%`}
              </span>
            </div>
            <div className="mt-3 grid grid-cols-2 gap-3 text-sm">
              <div className="rounded-lg bg-base/60 p-3">
                <p className="text-xs text-muted-text">期初总资产</p>
                <p className="mt-1 font-medium text-foreground">¥{fmt(asset.beginEquity)}</p>
              </div>
              <div className="rounded-lg bg-base/60 p-3">
                <p className="text-xs text-muted-text">期末总资产</p>
                <p className="mt-1 font-medium text-foreground">¥{fmt(asset.endEquity)}</p>
              </div>
            </div>
            {!asset.beginEquity && !asset.endEquity ? (
              <p className="mt-3 text-xs text-muted-text">
                提示：期初/期末资产来自每日快照，若当月或月初没有快照记录则显示 --。
              </p>
            ) : null}
          </Card>

          {/* 分红明细 */}
          {dividends.count ? (
            <Card variant="bordered" padding="md">
              <h2 className="mb-3 text-sm font-semibold text-foreground">分红明细</h2>
              <div className="space-y-1.5 text-sm">
                {(dividends.items ?? []).map((item, idx) => (
                  <div key={idx} className="flex items-center justify-between rounded-lg bg-base/60 px-3 py-2">
                    <span className="text-foreground">
                      {item.symbol}
                      {item.name ? <span className="ml-1 text-xs text-muted-text">{item.name}</span> : null}
                    </span>
                    <span className="text-xs text-muted-text">{item.date}</span>
                    <span
                      className={
                        'font-medium ' +
                        (item.type === '除权调整' ? 'text-[#e04545]' : 'text-[#0a8f5c]')
                      }
                    >
                      {item.type ? `${item.type} ` : ''}
                      {item.amount >= 0 ? '+' : ''}¥{fmt(Math.abs(item.amount))}
                    </span>
                  </div>
                ))}
              </div>
            </Card>
          ) : null}

          {/* 交易明细 */}
          {details.length ? (
            <Card variant="bordered" padding="md">
              <h2 className="mb-3 text-sm font-semibold text-foreground">交易明细</h2>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead>
                    <tr className="border-b border-border text-xs text-muted-text">
                      <th className="pb-2 pr-3 font-normal">日期</th>
                      <th className="pb-2 pr-3 font-normal">代码</th>
                      <th className="pb-2 pr-3 font-normal">方向</th>
                      <th className="pb-2 pr-3 text-right font-normal">数量</th>
                      <th className="pb-2 pr-3 text-right font-normal">价格</th>
                      <th className="pb-2 pr-3 text-right font-normal">金额</th>
                      <th className="pb-2 text-right font-normal">费用</th>
                    </tr>
                  </thead>
                  <tbody>
                    {details.map((row, idx) => (
                      <tr key={idx} className="border-b border-border/60 last:border-0">
                        <td className="py-2 pr-3 text-muted-text">{row.date}</td>
                        <td className="py-2 pr-3 font-medium text-foreground">{row.symbol}</td>
                        <td className="py-2 pr-3">
                          <span className={row.side === 'buy' ? 'text-[#e04545]' : 'text-[#0a8f5c]'}>
                            {row.side === 'buy' ? '买入' : '卖出'}
                          </span>
                        </td>
                        <td className="py-2 pr-3 text-right text-foreground">{fmt(row.quantity)}</td>
                        <td className="py-2 pr-3 text-right text-foreground">{fmtPrice(row.price)}</td>
                        <td className="py-2 pr-3 text-right font-medium text-foreground">¥{fmt(row.amount)}</td>
                        <td className="py-2 text-right text-muted-text">¥{fmt(row.fee)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

// ---------------- 页面入口 ---------------- //
export const MonthlyStatementPage: React.FC = () => {
  const [tab, setTab] = useState<'ledger' | 'monthly'>('ledger');

  return (
    <AppPage>
      <PageHeader
        eyebrow="Portfolio"
        title="对账单"
        description="个股流水：按股票查看完整交易、分红与税费明细；月度对账单：按月聚合买卖与资金流水"
      />

      <div className="mb-4 flex items-center gap-1 rounded-xl border border-border bg-card p-1">
        <button
          type="button"
          onClick={() => setTab('ledger')}
          className={cn(
            'inline-flex flex-1 items-center justify-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
            tab === 'ledger' ? 'bg-primary text-primary-foreground' : 'text-muted-text hover:text-foreground',
          )}
        >
          <ReceiptText className="h-4 w-4" />
          个股流水
        </button>
        <button
          type="button"
          onClick={() => setTab('monthly')}
          className={cn(
            'inline-flex flex-1 items-center justify-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
            tab === 'monthly' ? 'bg-primary text-primary-foreground' : 'text-muted-text hover:text-foreground',
          )}
        >
          <CalendarDays className="h-4 w-4" />
          月度对账单
        </button>
      </div>

      {tab === 'ledger' ? <StockLedgerView /> : <MonthlyView />}
    </AppPage>
  );
};

export default MonthlyStatementPage;
