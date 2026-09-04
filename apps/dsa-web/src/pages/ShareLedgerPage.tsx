import { useEffect, useState } from 'react';
import { KeyRound, Loader2 } from 'lucide-react';
import { shareApi, type ShareLedgerResult } from '../api/share';
import { HoldingLedgerTable } from './MonthlyStatementPage';

function httpStatus(err: unknown): number | undefined {
  return (err as { response?: { status?: number } })?.response?.status;
}

/**
 * 对账单「个股流水」独立访客分享页。
 * - 极简无壳：无侧边栏 / 无设置 / 无其他导航。
 * - 固定口令访问：口令长期有效（管理员可改/清除）；验证后换取 12 小时会话，
 *   12 小时内刷新免输入，过期后需重新输入口令。
 * - 只展示白名单股票（后端接口层过滤，白名单外数据不返回）。
 * - 与管理员登录态严格隔离：不跳转 /login，不依赖 dsa_session。
 */
export default function ShareLedgerPage() {
  const [phase, setPhase] = useState<'checking' | 'gate' | 'table' | 'unavailable'>('checking');
  const [result, setResult] = useState<ShareLedgerResult | null>(null);
  const [password, setPassword] = useState('');
  const [gateError, setGateError] = useState('');
  const [gateLoading, setGateLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await shareApi.getLedger();
        if (!alive) return;
        setResult(res);
        setPhase('table');
      } catch (err) {
        if (!alive) return;
        const status = httpStatus(err);
        if (status === 401) setPhase('gate');
        else setPhase('unavailable');
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const submit = async () => {
    const p = password.trim();
    if (!p) {
      setGateError('请输入分享口令');
      return;
    }
    setGateLoading(true);
    setGateError('');
    try {
      const res = await shareApi.getLedger(p);
      setResult(res);
      setPhase('table');
    } catch (err) {
      const status = httpStatus(err);
      if (status === 401) setGateError('口令错误，请重试');
      else if (status === 403) setPhase('unavailable');
      else setGateError('服务异常，请稍后重试');
    } finally {
      setGateLoading(false);
    }
  };

  if (phase === 'checking') {
    return (
      <div className="flex min-h-screen items-center justify-center bg-base">
        <Loader2 className="h-6 w-6 animate-spin text-cyan" />
      </div>
    );
  }

  if (phase === 'unavailable') {
    return (
      <div className="flex min-h-screen items-center justify-center bg-base px-4">
        <div className="w-full max-w-md rounded-2xl border border-border bg-card p-6 text-center shadow-soft-card">
          <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-muted">
            <KeyRound className="h-6 w-6 text-muted-text" />
          </div>
          <h1 className="mt-4 text-lg font-semibold text-foreground">分享暂不可用</h1>
          <p className="mt-2 text-sm leading-6 text-muted-text">分享功能未开启，或服务暂不可用。请联系分享方确认。</p>
        </div>
      </div>
    );
  }

  if (phase === 'gate') {
    return (
      <div className="flex min-h-screen items-center justify-center bg-base px-4">
        <div className="w-full max-w-md rounded-2xl border border-border bg-card p-6 shadow-soft-card">
          <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-primary/10">
            <KeyRound className="h-6 w-6 text-primary" />
          </div>
          <h1 className="mt-4 text-center text-lg font-semibold text-foreground">访客口令</h1>
          <p className="mt-2 text-center text-sm leading-6 text-muted-text">
            请输入分享口令以查看个股流水。口令长期有效，验证后 12 小时内免重复输入。
          </p>
          <div className="mt-5 space-y-3">
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void submit();
              }}
              placeholder="请输入分享口令"
              autoFocus
              className="w-full rounded-xl border border-border bg-base px-4 py-2.5 text-sm text-foreground outline-none transition-colors focus:border-primary"
            />
            {gateError ? <p className="text-sm text-[#e04545]">{gateError}</p> : null}
            <button
              type="button"
              disabled={gateLoading}
              onClick={() => void submit()}
              className="btn-primary w-full"
            >
              {gateLoading ? '验证中…' : '进入查看'}
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-base">
      <div className="mx-auto w-full max-w-5xl px-4 py-6">
        <div className="mb-4 flex items-center justify-between gap-3">
          <div>
            <h1 className="text-lg font-semibold text-foreground">个股流水</h1>
            <p className="mt-0.5 text-xs text-muted-text">
              共展示 {result?.total ?? 0} 只股票 · 信息来自同花顺实时数据
            </p>
          </div>
          <button
            type="button"
            onClick={() => {
              setPhase('gate');
              setPassword('');
              setGateError('');
            }}
            className="inline-flex items-center gap-1.5 rounded-xl border border-border bg-card px-3 py-1.5 text-xs text-muted-text transition-colors hover:text-foreground"
          >
            <KeyRound className="h-3.5 w-3.5" />
            退出
          </button>
        </div>
        <HoldingLedgerTable
          stocks={result?.stocks ?? []}
          isLoading={false}
          error={null}
          onRetry={() => undefined}
          emptyTitle="暂无展示数据"
          emptyDescription="当前白名单内暂无持仓数据。"
        />
      </div>
    </div>
  );
}
