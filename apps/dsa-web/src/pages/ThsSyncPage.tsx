import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  FileSpreadsheet,
  FolderCog,
  Loader2,
  LogOut,
  QrCode,
  RefreshCw,
  ShieldCheck,
  Upload,
} from 'lucide-react';
import { thsApi, thsExportApi } from '../api/thsSync';
import type {
  ThsStatus,
  ThsSyncResult,
  ThsTrade,
  ThsReconcileResult,
  ThsExportConfig,
  ThsExportImportResult,
  ThsExportParseResult,
} from '../api/thsSync';
import type { ParsedApiError } from '../api/error';
import { ApiErrorAlert, AppPage, Card, EmptyState, PageHeader, StatCard } from '../components/common';
import { cn } from '../utils/cn';

function fmt(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '--';
  return v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export const ThsSyncPage: React.FC = () => {
  const [status, setStatus] = useState<ThsStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [error, setError] = useState<ParsedApiError | null>(null);

  const [qrImage, setQrImage] = useState<string | null>(null);
  const [qrLoading, setQrLoading] = useState(false);
  const [polling, setPolling] = useState(false);
  const pollTimer = useRef<number | null>(null);

  const [syncing, setSyncing] = useState(false);
  const [syncResult, setSyncResult] = useState<ThsSyncResult | null>(null);

  const [reconcile, setReconcile] = useState<ThsReconcileResult | null>(null);
  const [reconcileLoading, setReconcileLoading] = useState(false);

  const loadReconcile = useCallback(async () => {
    if (!status?.loggedIn) return;
    setReconcileLoading(true);
    try {
      const r = await thsApi.reconcile();
      setReconcile(r);
    } catch {
      setReconcile(null);
    } finally {
      setReconcileLoading(false);
    }
  }, [status?.loggedIn]);

  useEffect(() => {
    if (status?.loggedIn) void loadReconcile();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status?.loggedIn]);

  const loadStatus = useCallback(async () => {
    setStatusLoading(true);
    setError(null);
    try {
      const s = await thsApi.getStatus();
      setStatus(s);
    } catch (err) {
      setError({ title: '加载失败', message: '无法获取数据同步状态', rawMessage: String(err), category: 'unknown' });
    } finally {
      setStatusLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
    return () => {
      if (pollTimer.current) window.clearTimeout(pollTimer.current);
    };
  }, [loadStatus]);

  const createQr = useCallback(async () => {
    setQrLoading(true);
    setError(null);
    setSyncResult(null);
    try {
      const q = await thsApi.createQrCode();
      setQrImage(q.qrImage);
      setPolling(true);
      pollTimer.current = window.setTimeout(() => {
        void (async () => {
          try {
            const res = await thsApi.pollLogin(q.qrid, 170);
            if (res.loggedIn) {
              setPolling(false);
              setQrImage(null);
              await loadStatus();
            }
          } catch (pollErr) {
            setPolling(false);
            setError({
              title: '扫码失败',
              message: '二维码可能已过期，请重新获取',
              rawMessage: String(pollErr),
              category: 'unknown',
            });
          }
        })();
      }, 1500);
    } catch (err) {
      setError({ title: '获取二维码失败', message: String(err), rawMessage: String(err), category: 'unknown' });
    } finally {
      setQrLoading(false);
    }
  }, [loadStatus]);

  const handleSync = useCallback(async () => {
    setSyncing(true);
    setError(null);
    setSyncResult(null);
    try {
      const result = await thsApi.sync(true);
      setSyncResult(result);
      await loadStatus();
      await loadReconcile();
    } catch (err) {
      setError({ title: '同步失败', message: String(err), rawMessage: String(err), category: 'unknown' });
    } finally {
      setSyncing(false);
    }
  }, [loadStatus, loadReconcile]);

  const handleLogout = useCallback(async () => {
    try {
      await thsApi.logout();
      setStatus(null);
      setQrImage(null);
      setSyncResult(null);
      setReconcile(null);
      await loadStatus();
    } catch (err) {
      setError({ title: '登出失败', message: String(err), rawMessage: String(err), category: 'unknown' });
    }
  }, [loadStatus]);

  // ---------- 账本交易流水 ----------
  type TradeRange = 'month' | 'quarter' | 'half' | 'year' | 'custom';
  const [tradeRange, setTradeRange] = useState<TradeRange>('year');
  const [customStart, setCustomStart] = useState('');
  const [customEnd, setCustomEnd] = useState('');
  const [trades, setTrades] = useState<ThsTrade[]>([]);
  const [tradesTotal, setTradesTotal] = useState(0);
  const [tradesLoading, setTradesLoading] = useState(false);
  const [tradeSource, setTradeSource] = useState<'ths' | 'local'>('local');

  const fmtYmd = (d: Date): string => {
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${d.getFullYear()}${m}${day}`;
  };

  const loadTrades = useCallback(async () => {
    if (tradeSource === 'ths' && !status?.loggedIn) return;
    setTradesLoading(true);
    try {
      const now = new Date();
      const end = fmtYmd(now);
      let start: string | undefined;
      if (tradeRange === 'month') {
        start = fmtYmd(new Date(now.getFullYear(), now.getMonth(), 1));
      } else if (tradeRange === 'quarter') {
        const d = new Date(now); d.setDate(d.getDate() - 90); start = fmtYmd(d);
      } else if (tradeRange === 'half') {
        const d = new Date(now); d.setDate(d.getDate() - 180); start = fmtYmd(d);
      } else if (tradeRange === 'year') {
        start = `${now.getFullYear()}0101`;
      } else {
        start = customStart ? customStart.replace(/-/g, '') : undefined;
      }
      const customEndDate = tradeRange === 'custom' && customEnd ? customEnd.replace(/-/g, '') : end;
      if (tradeSource === 'local') {
        // 本地导入流水：日期参数用 YYYY-MM-DD，含逆回购/分红/银证转账等全类别
        const dash = (s: string) => `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
        const res = await thsApi.getImportRecords(start ? dash(start) : undefined, customEndDate ? dash(customEndDate) : undefined);
        setTrades(res.records as unknown as ThsTrade[]);
        setTradesTotal(res.total);
      } else {
        const res = await thsApi.getTrades(start, customEndDate);
        setTrades(res.trades);
        setTradesTotal(res.total);
      }
    } catch (err) {
      setError({ title: '查询交易流水失败', message: String(err), rawMessage: String(err), category: 'unknown' });
    } finally {
      setTradesLoading(false);
    }
  }, [status?.loggedIn, tradeRange, customStart, customEnd, tradeSource]);

  // 页面进入 / 切换数据来源时自动查询一次（本地导入流水无需登录）
  useEffect(() => {
    void loadTrades();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tradeSource, status?.loggedIn]);

  // ---------- 账本导出文件（汇总持仓.xlsx）同步 ----------
  const [exportFile, setExportFile] = useState<File | null>(null);
  const [exportFileError, setExportFileError] = useState('');
  const [parseResult, setParseResult] = useState<ThsExportParseResult | null>(null);
  const [parsing, setParsing] = useState(false);
  const [importing, setImporting] = useState(false);
  const [exportResult, setExportResult] = useState<ThsExportImportResult | null>(null);
  const [exportConfig, setExportConfig] = useState<ThsExportConfig | null>(null);
  const [configDir, setConfigDir] = useState('');
  const [autoSync, setAutoSync] = useState(false);
  const [detecting, setDetecting] = useState(false);

  const loadExportConfig = useCallback(async () => {
    try {
      const cfg = await thsExportApi.getConfig();
      setExportConfig(cfg);
      setConfigDir(cfg.directory);
      setAutoSync(cfg.autoSync);
    } catch {
      /* 配置加载失败不阻断页面 */
    }
  }, []);

  useEffect(() => {
    void loadExportConfig();
  }, [loadExportConfig]);

  const handleExportFileChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0] ?? null;
    setExportFile(f);
    setExportFileError('');
    setParseResult(null);
    setExportResult(null);
  }, []);

  const handleParseExport = useCallback(async () => {
    if (!exportFile) {
      setExportFileError('请先选择账本导出的 汇总持仓.xlsx 文件');
      return;
    }
    setParsing(true);
    setExportFileError('');
    setExportResult(null);
    try {
      const res = await thsExportApi.parse(exportFile);
      setParseResult(res);
    } catch (err) {
      setExportFileError(`解析失败：${String(err)}`);
    } finally {
      setParsing(false);
    }
  }, [exportFile]);

  const handleImportExport = useCallback(async () => {
    if (!exportFile) return;
    setImporting(true);
    setExportFileError('');
    try {
      const res = await thsExportApi.import(exportFile);
      setExportResult(res);
      setParseResult(null);
      await loadExportConfig();
      await loadReconcile();
    } catch (err) {
      setExportFileError(`导入失败：${String(err)}`);
    } finally {
      setImporting(false);
    }
  }, [exportFile, loadExportConfig, loadReconcile]);

  const handleSaveExportConfig = useCallback(async () => {
    try {
      const cfg = await thsExportApi.saveConfig(configDir.trim() || undefined, autoSync);
      setExportConfig(cfg);
    } catch (err) {
      setError({ title: '保存目录配置失败', message: String(err), rawMessage: String(err), category: 'unknown' });
    }
  }, [configDir, autoSync]);

  const handleDetectExport = useCallback(async () => {
    setDetecting(true);
    setExportFileError('');
    try {
      const res = await thsExportApi.detect();
      setExportResult(res);
      await loadExportConfig();
      await loadReconcile();
    } catch (err) {
      setExportFileError(`检测同步失败：${String(err)}`);
    } finally {
      setDetecting(false);
    }
  }, [loadExportConfig, loadReconcile]);

  const syncResultView = syncResult ? (
    <Card className="mt-4 p-5">
      <div className="mb-3 flex items-center gap-2">
        <CheckCircle2 className="h-5 w-5 text-emerald-500" />
        <h3 className="text-base font-semibold text-foreground">同步完成</h3>
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard label="汇总持仓" value={`${syncResult.positionsApplied} 只`} />
        <StatCard label="持仓成本" value="已对齐账本" />
        <StatCard label="账本现金" value={`¥${fmt(syncResult.totalCash)}`} />
        <StatCard label="资产曲线" value={`${syncResult.assetWritten} 天`} />
      </div>
      <ul className="mt-3 space-y-1 text-sm text-secondary-text">
        <li>扫描账本账户：{syncResult.accountsScanned} 个 · 同步持仓：{syncResult.rebuilt.join('、')}</li>
        <li>
          现金校正：
          {syncResult.cashImport.adjusted
            ? `${syncResult.cashImport.direction === 'in' ? '入金' : '出金'} ¥${fmt(syncResult.cashImport.amount)}`
            : '无需调整'}
        </li>
      </ul>
      {(syncResult.rebuildErrors.length > 0) && (
        <div className="mt-3 rounded-xl border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-700 dark:text-amber-400">
          <p className="mb-1 font-medium">部分持仓同步异常</p>
          {syncResult.rebuildErrors.slice(0, 8).map((e, i) => (
            <p key={i} className="truncate">{e}</p>
          ))}
        </div>
      )}
    </Card>
  ) : null;

  const qrView = status && !status.loggedIn ? (
    <Card className="mt-4 p-6">
      <div className="mb-3 flex items-center gap-2">
        <QrCode className="h-5 w-5 text-[hsl(var(--primary))]" />
        <h3 className="text-base font-semibold text-foreground">扫码登录同花顺账本</h3>
      </div>
      {qrImage ? (
        <div className="flex flex-col items-center gap-3">
          {/* eslint-disable-next-line jsx-a11y/alt-text */}
          <img
            src={`data:image/png;base64,${qrImage}`}
            alt="同花顺登录二维码"
            className="h-56 w-56 rounded-xl border border-border object-contain"
          />
          <p className="flex items-center gap-2 text-sm text-secondary-text">
            {polling && <Loader2 className="h-4 w-4 animate-spin" />}
            请用同花顺 App / 手机客户端扫码，等待自动确认…
          </p>
          <button
            type="button"
            className="btn-secondary text-sm"
            onClick={() => void createQr()}
          >
            二维码已过期？重新获取
          </button>
        </div>
      ) : (
        <div className="flex flex-col items-center gap-3">
          <p className="text-sm text-secondary-text">使用同花顺账本自动同步持仓与交易，替代手动录入。</p>
          <button
            type="button"
            className="btn-primary"
            disabled={qrLoading}
            onClick={() => void createQr()}
          >
            {qrLoading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <QrCode className="mr-2 h-4 w-4" />}
            获取扫码二维码
          </button>
        </div>
      )}
    </Card>
  ) : null;

  return (
    <AppPage>
      <PageHeader
        title="数据同步"
        description="从同花顺投资账本同步持仓、交易与资产数据；账目不一致时可导出汇总持仓.xlsx 核对同步"
      />

      {statusLoading ? (
        <div className="flex h-40 items-center justify-center text-secondary-text">
          <Loader2 className="mr-2 h-5 w-5 animate-spin" /> 加载中…
        </div>
      ) : (
        <>
          {error && (
            <div className="mt-4">
              <ApiErrorAlert error={error} />
            </div>
          )}

          <Card className="mt-4 p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <ShieldCheck
                  className={cn('h-5 w-5', status?.loggedIn ? 'text-emerald-500' : 'text-muted-foreground')}
                />
                <span className="text-sm text-foreground">
                  {status?.loggedIn ? '已登录同花顺账本' : '未登录同花顺账本'}
                </span>
                {status?.loggedIn && (
                  <span className="inline-flex items-center rounded-full bg-emerald-500/10 px-2 py-0.5 text-xs text-emerald-600 dark:text-emerald-400">
                    登录态有效
                  </span>
                )}
              </div>
              <div className="flex items-center gap-2">
                <button type="button" className="btn-secondary text-sm" onClick={() => void loadStatus()}>
                  <RefreshCw className="mr-1.5 h-4 w-4" /> 刷新状态
                </button>
                {status?.loggedIn && (
                  <button type="button" className="btn-secondary text-sm" onClick={() => void handleLogout()}>
                    <LogOut className="mr-1.5 h-4 w-4" /> 登出
                  </button>
                )}
              </div>
            </div>
          </Card>

          {/* 账本导出文件同步（A：手动导入 / B：目录自动检测） */}
          <Card className="mt-4 p-5">
            <div className="mb-4 flex items-center gap-2">
              <FileSpreadsheet className="h-5 w-5 text-[hsl(var(--primary))]" />
              <div>
                <h3 className="text-base font-semibold text-foreground">账本导出文件同步</h3>
                <p className="mt-0.5 text-sm text-secondary-text">
                  在投资账本「汇总持仓 → 数据导出」下载「汇总持仓.xlsx」后，可手动导入或配置目录自动检测同步
                </p>
              </div>
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              {/* A 手动导入 */}
              <div className="rounded-xl border border-border p-4">
                <div className="mb-3 flex items-center gap-2">
                  <Upload className="h-4 w-4 text-secondary-text" />
                  <h4 className="text-sm font-semibold text-foreground">手动导入</h4>
                </div>
                <input
                  type="file"
                  accept=".xlsx"
                  className="input-surface input-focus-glow block w-full rounded-xl border bg-transparent px-3 py-2 text-sm"
                  onChange={handleExportFileChange}
                />
                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    type="button"
                    className="btn-secondary text-sm"
                    disabled={parsing || !exportFile}
                    onClick={() => void handleParseExport()}
                  >
                    {parsing ? <Loader2 className="mr-1.5 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-1.5 h-4 w-4" />}
                    解析预览
                  </button>
                  <button
                    type="button"
                    className="btn-primary text-sm"
                    disabled={importing || !exportFile}
                    onClick={() => void handleImportExport()}
                  >
                    {importing ? <Loader2 className="mr-1.5 h-4 w-4 animate-spin" /> : <Upload className="mr-1.5 h-4 w-4" />}
                    {importing ? '导入中…' : '导入同步'}
                  </button>
                </div>
                {exportFileError && <p className="mt-2 text-xs text-red-600 dark:text-red-400">{exportFileError}</p>}
                {parseResult && (
                  <div className="mt-3 rounded-xl bg-muted/40 p-3 text-xs text-secondary-text">
                    <p className="mb-1 font-medium text-foreground">解析预览（不会写入数据）</p>
                    <p>工作表：{parseResult.sheets.join('、')}</p>
                    <p>有效持仓 {parseResult.positionCount} 只 · 市值 ¥{fmt(parseResult.marketValue)}</p>
                    <p>
                      交易记录：买入 {parseResult.tradeStats.tradeBuy} 笔 / 卖出 {parseResult.tradeStats.tradeSell} 笔
                    </p>
                    <p>
                      银证转账：入金 {parseResult.tradeStats.cashIn} 笔（¥{fmt(parseResult.cashInTotal)}）/ 出金{' '}
                      {parseResult.tradeStats.cashOut} 笔（¥{fmt(parseResult.cashOutTotal)}）
                    </p>
                    {parseResult.tradeStats.firstDate && (
                      <p>流水区间：{parseResult.tradeStats.firstDate} ~ {parseResult.tradeStats.lastDate}</p>
                    )}
                  </div>
                )}
              </div>

              {/* B 目录自动检测 */}
              <div className="rounded-xl border border-border p-4">
                <div className="mb-3 flex items-center gap-2">
                  <FolderCog className="h-4 w-4 text-secondary-text" />
                  <h4 className="text-sm font-semibold text-foreground">下载目录自动检测</h4>
                </div>
                <input
                  type="text"
                  className="input-surface input-focus-glow block w-full rounded-xl border bg-transparent px-3 py-2 text-sm"
                  placeholder="例如：C:\Users\admin\Downloads"
                  value={configDir}
                  onChange={(e) => setConfigDir(e.target.value)}
                />
                <div className="mt-3 flex flex-wrap items-center gap-3">
                  <button type="button" className="btn-secondary text-sm" onClick={() => void handleSaveExportConfig()}>
                    保存目录
                  </button>
                  <label className="flex cursor-pointer items-center gap-2 text-sm text-secondary-text">
                    <input
                      type="checkbox"
                      className="h-4 w-4 rounded border-border"
                      checked={autoSync}
                      onChange={(e) => setAutoSync(e.target.checked)}
                    />
                    定时自动检测（开启后每 10 分钟扫描目录）
                  </label>
                  <button
                    type="button"
                    className="btn-secondary text-sm"
                    disabled={detecting}
                    onClick={() => void handleDetectExport()}
                  >
                    {detecting ? <Loader2 className="mr-1.5 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-1.5 h-4 w-4" />}
                    立即检测并同步
                  </button>
                </div>
                {exportConfig?.lastFile && (
                  <p className="mt-2 text-xs text-secondary-text">
                    最近同步：{exportConfig.lastFile}
                    {exportConfig.lastSyncedAt ? `（${exportConfig.lastSyncedAt}）` : ''}
                  </p>
                )}
                {autoSync && !configDir.trim() && (
                  <p className="mt-2 text-xs text-amber-600 dark:text-amber-400">开启自动检测前请先填写下载目录并保存</p>
                )}
              </div>
            </div>

            {exportResult && (
              <div className="mt-4 rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-4">
                {exportResult.changed === false ? (
                  <div className="flex items-center gap-2">
                    <CheckCircle2 className="h-4 w-4 text-emerald-500" />
                    <span className="text-sm font-medium text-foreground">
                      已是最新 · {exportResult.message}
                      {exportResult.lastFile ? `（${exportResult.lastFile}）` : ''}
                    </span>
                  </div>
                ) : (
                  <>
                    <div className="mb-2 flex items-center gap-2">
                      <CheckCircle2 className="h-4 w-4 text-emerald-500" />
                      <span className="text-sm font-medium text-foreground">
                        {exportResult.detected ? '自动检测同步完成' : '导入同步完成'}
                        {exportResult.file ? ` · ${exportResult.file}` : ''}
                      </span>
                    </div>
                    <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                      <StatCard label="同步持仓" value={`${exportResult.positionsApplied} 只`} />
                      <StatCard label="持仓市值" value={`¥${fmt(exportResult.marketValue)}`} />
                      <StatCard label="买卖流水" value={`${exportResult.tradeStats.tradeBuy + exportResult.tradeStats.tradeSell} 笔`} />
                      <StatCard label="银证转账" value={`入${exportResult.tradeStats.cashIn} / 出${exportResult.tradeStats.cashOut} 笔`} />
                    </div>
                    <ul className="mt-3 space-y-1 text-sm text-secondary-text">
                      <li>同步代码：{exportResult.rebuilt.join('、')}</li>
                      <li>
                        银证转账金额：入金 ¥{fmt(exportResult.cashInTotal)} / 出金 ¥{fmt(exportResult.cashOutTotal)}
                      </li>
                      {exportResult.cashImport?.skipped ? (
                        <li>现金校正：{exportResult.cashImport.note ?? '跳过'}</li>
                      ) : exportResult.cashImport?.error ? (
                        <li>现金校正：失败（{exportResult.cashImport.error}）</li>
                      ) : (
                        <li>
                          现金校正：
                          {exportResult.cashImport?.adjusted
                            ? `${exportResult.cashImport.direction === 'in' ? '入金' : '出金'} ¥${fmt(exportResult.cashImport.amount)}`
                            : '无需调整'}
                        </li>
                      )}
                    </ul>
                  </>
                )}
              </div>
            )}
          </Card>

          {!status?.loggedIn ? (
            <>
              {qrView}
              <Card className="mt-4 p-5 text-sm text-secondary-text">
                <p className="mb-1 font-medium text-foreground">登录说明</p>
                <ul className="list-inside list-disc space-y-1">
                  <li>扫码后需在手机上确认登录，登录态会保存在本地（data/ths_cookies.json）。</li>
                  <li>登录成功后即可一键同步「汇总持仓 + 交易流水 + 资产历史曲线」。</li>
                  <li>如果登录态失效，页面会提示重新扫码，无需重复录入。</li>
                </ul>
              </Card>
            </>
          ) : (
            <>
              <Card className="mt-4 p-5">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h3 className="text-base font-semibold text-foreground">同步持仓数据</h3>
                    <p className="mt-1 text-sm text-secondary-text">
                      将账本全部券商账户的汇总持仓同步到本地「百福具臻」账户（数量与成本以账本为准），可重复执行。
                    </p>
                  </div>
                  <button
                    type="button"
                    className="btn-primary"
                    disabled={syncing}
                    onClick={() => void handleSync()}
                  >
                    {syncing ? (
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    ) : (
                      <RefreshCw className="mr-2 h-4 w-4" />
                    )}
                    {syncing ? '同步中…' : '一键同步'}
                  </button>
                </div>
              </Card>
              {syncResultView}

              {/* 账目对账状态：网页账本 vs 本地账户 */}
              {reconcile && reconcile.available && (
                <Card className="mt-4 p-5">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="flex items-center gap-2">
                      {reconcile.aligned ? (
                        <ShieldCheck className="h-5 w-5 text-emerald-500" />
                      ) : (
                        <AlertTriangle className="h-5 w-5 text-amber-500" />
                      )}
                      <h3 className="text-base font-semibold text-foreground">
                        {reconcile.aligned ? '账目一致' : '账目存在差异'}
                      </h3>
                    </div>
                    <button
                      type="button"
                      className="btn-secondary text-sm"
                      disabled={reconcileLoading}
                      onClick={() => void loadReconcile()}
                    >
                      {reconcileLoading ? (
                        <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />
                      ) : (
                        <RefreshCw className="mr-1.5 h-4 w-4" />
                      )}
                      {reconcileLoading ? '对账中…' : '重新对账'}
                    </button>
                  </div>
                  <p className="mt-1 text-sm text-secondary-text">
                    网页汇总持仓 vs 本地持仓（数量 + 成本逐只核对；市值差异为价格源不同）
                  </p>
                  <div className="mt-3 grid grid-cols-2 gap-3 md:grid-cols-4">
                    <StatCard label="网页持仓市值" value={`¥${fmt(reconcile.position?.webTotalValue)}`} />
                    <StatCard label="本地持仓市值" value={`¥${fmt(reconcile.position?.localTotalValue)}`} />
                    <StatCard
                      label="市值差额"
                      value={`${reconcile.position && reconcile.position.valueDiff > 0 ? '+' : ''}¥${fmt(reconcile.position?.valueDiff)}`}
                    />
                    <StatCard label="现金差额" value={`${reconcile.cash && reconcile.cash.diff > 0 ? '+' : ''}¥${fmt(reconcile.cash?.diff)}`} />
                  </div>
                  {!reconcile.aligned && (
                    <div className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-sm">
                      <p className="font-medium text-amber-700 dark:text-amber-400">
                        检测到持仓数量或成本与账本不一致，建议导出 汇总持仓.xlsx 进行核对同步
                      </p>
                      {(reconcile.reasons ?? []).length > 0 && (
                        <ul className="mt-1 list-inside list-disc space-y-0.5 text-secondary-text">
                          {reconcile.reasons.map((r, i) => (
                            <li key={i}>{r}</li>
                          ))}
                        </ul>
                      )}
                      {reconcile.position && reconcile.position.diffPositions.length > 0 && (
                        <div className="mt-2 space-y-1">
                          {reconcile.position.diffPositions.map((d, i) => (
                            <p key={i} className="text-xs text-secondary-text">
                              {d.code}：{d.issue}
                              {d.web && d.local
                                ? `（网页 ${d.web.quantity}股 @ ${d.web.cost} / 本地 ${d.local.quantity}股 @ ${d.local.cost}）`
                                : ''}
                            </p>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                  {reconcile.aligned && reconcile.cash && Math.abs(reconcile.cash.diff) > 0.01 && (
                    <div className="mt-3 rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-3 text-sm text-secondary-text">
                      持仓数据与账本一致；现金存在 ¥{fmt(Math.abs(reconcile.cash.diff))} 差额（可能是银证转账，可导出文件核对）
                    </div>
                  )}
                </Card>
              )}

              {/* 交易流水（来源可切换：本地导入流水 / 账本实时） */}
              <Card className="mt-4 p-5">
                <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h3 className="text-base font-semibold text-foreground">
                      {tradeSource === 'local' ? '交易流水（本地导入数据）' : '交易流水（账本实时）'}
                    </h3>
                    <p className="mt-1 text-sm text-secondary-text">
                      {tradeSource === 'local'
                        ? `从本地导入的账本导出流水查询（共 ${tradesTotal} 笔），含逆回购/分红/银证转账等全类别，无需登录`
                        : `从同花顺账本实时查询（共 ${tradesTotal} 笔），仅供查看，不影响本地持仓成本`}
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <select
                      className="input-surface input-focus-glow h-10 rounded-xl border bg-transparent px-3 text-sm"
                      value={tradeSource}
                      onChange={(e) => setTradeSource(e.target.value as 'ths' | 'local')}
                    >
                      <option value="local">本地导入流水</option>
                      <option value="ths">账本实时流水</option>
                    </select>
                    <select
                      className="input-surface input-focus-glow h-10 rounded-xl border bg-transparent px-3 text-sm"
                      value={tradeRange}
                      onChange={(e) => setTradeRange(e.target.value as TradeRange)}
                    >
                      <option value="month">本月</option>
                      <option value="quarter">近三月</option>
                      <option value="half">近半年</option>
                      <option value="year">今年</option>
                      <option value="custom">自定义</option>
                    </select>
                    {tradeRange === 'custom' && (
                      <>
                        <input
                          type="date"
                          className="input-surface input-focus-glow h-10 rounded-xl border bg-transparent px-3 text-sm"
                          value={customStart}
                          onChange={(e) => setCustomStart(e.target.value)}
                        />
                        <span className="text-xs text-secondary-text">至</span>
                        <input
                          type="date"
                          className="input-surface input-focus-glow h-10 rounded-xl border bg-transparent px-3 text-sm"
                          value={customEnd}
                          onChange={(e) => setCustomEnd(e.target.value)}
                        />
                      </>
                    )}
                    <button type="button" className="btn-secondary text-sm" onClick={() => void loadTrades()}>
                      {tradesLoading ? <Loader2 className="mr-1.5 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-1.5 h-4 w-4" />}
                      查询
                    </button>
                  </div>
                </div>
                {trades.length > 0 ? (
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="border-b border-border text-left text-xs text-secondary-text">
                          <th className="py-2 pr-3">日期</th>
                          <th className="py-2 pr-3">代码</th>
                          <th className="py-2 pr-3">名称</th>
                          <th className="py-2 pr-3">{tradeSource === 'local' ? '类别' : '方向'}</th>
                          <th className="py-2 pr-3 text-right">数量</th>
                          <th className="py-2 pr-3 text-right">价格</th>
                          <th className="py-2 pr-3 text-right">金额</th>
                          <th className="py-2 pr-3 text-right">费用</th>
                        </tr>
                      </thead>
                      <tbody>
                        {trades.slice(0, 50).map((t, i) => {
                          const rtype = (t as unknown as { recordType?: string }).recordType;
                          const label = rtype ?? (t.side === 'buy' ? '买入' : '卖出');
                          const isBuy = label === '买入';
                          const isSell = label === '卖出';
                          return (
                            <tr key={i} className="border-b border-border/60">
                              <td className="whitespace-nowrap py-2 pr-3">{t.tradeDate}{t.tradeTime ? ` ${t.tradeTime}` : ''}</td>
                              <td className="py-2 pr-3">{t.code}</td>
                              <td className="py-2 pr-3">{t.name}</td>
                              <td className={cn('py-2 pr-3 font-medium', isBuy ? 'text-red-600 dark:text-red-400' : isSell ? 'text-emerald-600 dark:text-emerald-400' : 'text-secondary-text')}>
                                {label}
                              </td>
                              <td className="py-2 pr-3 text-right">{fmt(t.quantity)}</td>
                              <td className="py-2 pr-3 text-right">{fmt(t.price)}</td>
                              <td className="py-2 pr-3 text-right">{fmt(t.amount)}</td>
                              <td className="py-2 pr-3 text-right">{fmt(t.fee)}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="py-6 text-center text-sm text-secondary-text">
                    {tradesLoading
                      ? '加载中…'
                      : tradeSource === 'local'
                        ? '暂无本地导入流水，先在「导出数据」里导入一份汇总持仓.xlsx'
                        : '暂无账本交易记录，点击「查询」拉取账本流水'}
                  </p>
                )}
              </Card>
            </>
          )}

          {!status?.loggedIn && !qrImage && (
            <div className="mt-6">
              <EmptyState
                icon={<QrCode className="h-8 w-8" />}
                title="自动同步持仓"
                description="扫码登录后即可从同花顺账本自动拉取持仓、交易与资产曲线，无需手动录入"
              />
            </div>
          )}
        </>
      )}

      {!statusLoading && error && !status && !qrImage && (
        <div className="mt-4">
          <div className="flex items-start gap-2 text-amber-600 dark:text-amber-400">
            <AlertTriangle className="mt-0.5 h-4 w-4" />
            <span className="text-sm">无法连接后端服务，请确认 daily_stock_analysis 后端已启动。</span>
          </div>
        </div>
      )}
    </AppPage>
  );
};

export default ThsSyncPage;
