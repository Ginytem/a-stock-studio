import type React from 'react';
import type { ReportLanguage, ReportStrategy as ReportStrategyType } from '../../types/analysis';
import { Card } from '../common';
import { DashboardPanelHeader } from '../dashboard';
import { getReportText, normalizeReportLanguage } from '../../utils/reportLanguage';

interface ReportStrategyProps {
  strategy?: ReportStrategyType;
  language?: ReportLanguage;
}

interface StrategyItemProps {
  label: string;
  value?: string;
  tone: string;
}

const StrategyItem: React.FC<StrategyItemProps> = ({
  label,
  value,
  tone,
}) => (
  <div className="home-subpanel home-strategy-card p-3" style={{ ['--home-strategy-tone' as string]: `var(${tone})` }}>
    <div className="flex flex-col">
      <span className="home-strategy-label mb-0.5 text-xs">{label}</span>
      <span className="home-strategy-value text-lg font-bold font-mono" style={!value ? { color: 'var(--text-muted-text)' } : undefined}>
        {value || '—'}
      </span>
    </div>
    <div
      className="absolute bottom-0 left-0 right-0 h-0.5"
      style={{ background: `linear-gradient(90deg, transparent, var(${tone}), transparent)` }}
    />
  </div>
);

interface CciSignalBadgeProps {
  type?: string;
  label?: string;
  size?: 'sm' | 'md';
}

/** CCI 历史回测适用性徽章：适用=绿，一般=黄，不适用=红，信号较少=灰 */
const FitnessBadge: React.FC<{ fitness?: string }> = ({ fitness }) => {
  if (!fitness) return null;
  const map: Record<string, { color: string; bg: string }> = {
    适用: { color: '#52c41a', bg: 'rgba(82,196,26,0.12)' },
    一般: { color: '#faad14', bg: 'rgba(250,173,20,0.12)' },
    不适用: { color: '#f5222d', bg: 'rgba(245,34,45,0.12)' },
  };
  const s = map[fitness] || { color: '#8c8c8c', bg: 'rgba(140,140,140,0.12)' };
  return (
    <span
      className="inline-flex items-center rounded text-xs px-1.5 py-0.5 font-semibold"
      style={{ color: s.color, background: s.bg }}
    >
      {fitness}
    </span>
  );
};

/**
 * CCI 信号徽章：红色▲=买入信号，绿色▼=卖出信号，灰色●=观望
 * （与东财 K 线图中红箭头买入 / 绿箭头卖出口径一致）
 */
const CciSignalBadge: React.FC<CciSignalBadgeProps> = ({ type, label, size = 'md' }) => {
  const text =
    label ||
    (type === 'buy' ? '买入信号' : type === 'sell' ? '卖出信号' : '观望');
  const cls = size === 'sm' ? 'text-xs px-1.5 py-0.5' : 'text-sm px-2 py-0.5';
  if (type === 'buy') {
    return (
      <span
        className={`inline-flex items-center gap-1 rounded font-semibold ${cls}`}
        style={{ color: '#f5222d', background: 'rgba(245,34,45,0.12)' }}
      >
        ▲ {text}
      </span>
    );
  }
  if (type === 'sell') {
    return (
      <span
        className={`inline-flex items-center gap-1 rounded font-semibold ${cls}`}
        style={{ color: '#52c41a', background: 'rgba(82,196,26,0.12)' }}
      >
        ▼ {text}
      </span>
    );
  }
  return (
    <span
      className={`inline-flex items-center gap-1 rounded font-medium ${cls}`}
      style={{ color: 'var(--text-muted-text)', background: 'rgba(128,128,128,0.14)' }}
    >
      ● {text}
    </span>
  );
};

/** A 股交易时段判断（周一至周五 9:30-11:30 / 13:00-15:00，本地时间） */
function isAStockTradingTime(): boolean {
  const now = new Date();
  const day = now.getDay();
  if (day === 0 || day === 6) return false;
  const minutes = now.getHours() * 60 + now.getMinutes();
  return (minutes >= 9 * 60 + 30 && minutes <= 11 * 60 + 30) || (minutes >= 13 * 60 && minutes <= 15 * 60);
}

/**
 * 策略点位区组件 - 终端风格
 */
export const ReportStrategy: React.FC<ReportStrategyProps> = ({ strategy, language = 'zh' }) => {
  if (!strategy) {
    return null;
  }

  const reportLanguage = normalizeReportLanguage(language);
  const text = getReportText(reportLanguage);

  const strategyItems = [
    {
      label: text.idealBuy,
      value: strategy.idealBuy,
      tone: '--home-strategy-buy',
    },
    {
      label: text.secondaryBuy,
      value: strategy.secondaryBuy,
      tone: '--home-strategy-secondary',
    },
    {
      label: text.stopLoss,
      value: strategy.stopLoss,
      tone: '--home-strategy-stop',
    },
    {
      label: text.takeProfit,
      value: strategy.takeProfit,
      tone: '--home-strategy-take',
    },
  ];

  return (
    <Card variant="bordered" padding="md" className="home-panel-card">
      <DashboardPanelHeader
        eyebrow={text.strategyPoints}
        title={text.sniperLevels}
        className="mb-3"
      />
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {strategyItems.map((item) => (
          <StrategyItem key={item.label} {...item} />
        ))}
      </div>

      {/* CCI 指标点位 */}
      {strategy.cci != null && (
        <div className="mt-3 border-t pt-3 space-y-2">
          {/* 上半块：实时信号 */}
          <div className="flex items-center justify-between">
            <span className="text-xs text-[var(--text-muted-text)]">CCI 实时信号</span>
            <CciSignalBadge type={strategy.cciLiveType} label={strategy.cciLiveLabel} />
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-[var(--text-muted-text)]">当前 CCI(14)</span>
            <span className="font-mono text-lg font-bold">{strategy.cci}</span>
          </div>
          {strategy.cciSignal && (
            <div className="text-xs leading-relaxed text-[var(--text-muted-text)]">
              {strategy.cciSignal}
            </div>
          )}
          <div className="text-xs text-[var(--text-muted-text)] opacity-70">
            {isAStockTradingTime() ? '盘中实时 · 信号可能随价格波动' : '收盘确认 · 信号稳定'}
          </div>

          {/* 下半块：最近确认信号 */}
          {strategy.cciSignalDate ? (
            <div className="border-t pt-2 space-y-1">
              <div className="flex items-center justify-between">
                <span className="text-xs text-[var(--text-muted-text)]">最近确认信号</span>
                <CciSignalBadge type={strategy.cciSignalType} label={strategy.cciSignalLabel} size="sm" />
              </div>
              {strategy.cciSignalTrigger && (
                <div className="text-xs text-[var(--text-muted-text)]">{strategy.cciSignalTrigger}</div>
              )}
              <div className="flex items-center justify-between text-xs">
                <span className="text-[var(--text-muted-text)]">信号日期</span>
                <span className="font-mono">{strategy.cciSignalDate}</span>
              </div>
              {strategy.cciSignalPrice != null && strategy.cciSignalPrice > 0 && (
                <div className="flex items-center justify-between text-xs">
                  <span className="text-[var(--text-muted-text)]">触发日收盘价</span>
                  <span className="font-mono">{strategy.cciSignalPrice.toFixed(2)} 元</span>
                </div>
              )}
            </div>
          ) : (
            <div className="border-t pt-2 text-xs text-[var(--text-muted-text)]">
              观望 · 暂无明确买卖信号
            </div>
          )}

          {/* CCI 指标历史适用性回测 */}
          {strategy.cciFitness?.summary && (
            <div className="border-t pt-2 space-y-1">
              <div className="flex items-center justify-between">
                <span className="text-xs text-[var(--text-muted-text)]">CCI 历史回测适用性</span>
                <FitnessBadge fitness={strategy.cciFitness.fitness} />
              </div>
              <div className="text-xs leading-relaxed text-[var(--text-muted-text)]">
                {strategy.cciFitness.summary}
              </div>
              {strategy.cciFitness.signals != null && (
                <div className="flex items-center justify-between text-xs">
                  <span className="text-[var(--text-muted-text)]">近两年信号</span>
                  <span className="font-mono">
                    {strategy.cciFitness.wins}/{strategy.cciFitness.signals} 次盈利（胜率{' '}
                    {strategy.cciFitness.winRate}%）
                  </span>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </Card>
  );
};
