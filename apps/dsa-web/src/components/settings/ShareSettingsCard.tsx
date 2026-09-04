import { useCallback, useEffect, useState } from 'react';
import { CheckCircle2, ClipboardCopy, Link2, LockKeyhole } from 'lucide-react';
import { shareApi, type ShareConfig } from '../../api/share';
import { ApiErrorAlert } from '../common';
import { SettingsSectionCard } from './SettingsSectionCard';

/**
 * 对账单「个股流水」独立访客分享页配置卡片。
 * - 展示白名单：仅白名单内股票会出现在分享页（后端接口层过滤）。
 * - 固定口令：长期有效，可随时修改/清除，保存后即时生效。
 * - 访客输入口令后换取 12 小时会话；与管理员登录态严格隔离。
 */
export function ShareSettingsCard() {
  const [config, setConfig] = useState<ShareConfig | null>(null);
  const [symbolsText, setSymbolsText] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const cfg = await shareApi.getConfig();
      setConfig(cfg);
      setSymbolsText(cfg.symbols.join(', '));
    } catch (err) {
      setError('读取分享配置失败，请确认已登录。');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const shareUrl =
    typeof window !== 'undefined' ? `${window.location.origin}/share/ledger` : '/share/ledger';

  const copyUrl = async () => {
    try {
      await navigator.clipboard.writeText(shareUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      setError('复制失败，请手动复制链接。');
    }
  };

  const save = async (body: { enabled?: boolean; password?: string | null; symbols?: string[] }) => {
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      const cfg = await shareApi.saveConfig(body);
      setConfig(cfg);
      setSymbolsText(cfg.symbols.join(', '));
      if (body.password === '') setNewPassword('');
      setSuccess('已保存，即时生效。');
    } catch (err) {
      setError('保存失败，请稍后重试。');
    } finally {
      setSaving(false);
    }
  };

  const handleSaveAll = () => {
    const symbols = symbolsText
      .split(/[,，、\s]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    void save({
      enabled: config?.enabled ?? false,
      symbols,
      password: newPassword.trim() ? newPassword.trim() : undefined,
    });
  };

  return (
    <SettingsSectionCard
      title="访客分享页"
      description="将对账单「个股流水」做成独立访客分享页：展示白名单 + 固定口令访问。仅白名单内股票可见，访客无法进入本应用其他页面或接口。"
      actions={
        config?.enabled ? (
          <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-500/10 px-3 py-1 text-xs font-medium text-emerald-600 dark:text-emerald-400">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
            已开启
          </span>
        ) : (
          <span className="inline-flex items-center gap-1.5 rounded-full bg-muted px-3 py-1 text-xs font-medium text-muted-text">
            <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/50" />
            未开启
          </span>
        )
      }
    >
      {loading ? (
        <p className="text-sm text-muted-text">加载中…</p>
      ) : (
        <>
          {/* 开启/关闭 */}
          <div className="flex flex-col gap-3 rounded-2xl border settings-border bg-background/35 px-4 py-4 md:flex-row md:items-center md:justify-between">
            <div>
              <p className="text-sm font-semibold text-foreground">
                {config?.enabled ? '分享页已开启' : '分享页未开启'}
              </p>
              <p className="mt-1 text-xs leading-6 text-muted-text">
                开启后访客可通过分享链接 + 固定口令查看白名单股票的持仓与流水。
              </p>
            </div>
            <button
              type="button"
              onClick={() => void save({ enabled: !config?.enabled })}
              disabled={saving}
              className="btn-primary inline-flex items-center gap-2 px-4 py-2 text-sm"
            >
              {config?.enabled ? '关闭分享' : '开启分享'}
            </button>
          </div>

          {/* 分享链接 */}
          <div className="rounded-2xl border settings-border bg-background/35 px-4 py-4">
            <p className="text-sm font-semibold text-foreground">分享链接</p>
            <p className="mt-1 text-xs leading-6 text-muted-text">
              将以下链接发给访客，访客输入口令后即可查看（口令长期有效，验证后 12 小时内免重复输入）。
            </p>
            <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center">
              <code className="flex-1 break-all rounded-xl border settings-border bg-base px-3 py-2 font-mono text-xs text-foreground">
                {shareUrl}
              </code>
              <button
                type="button"
                onClick={() => void copyUrl()}
                className="inline-flex items-center justify-center gap-1.5 rounded-xl border settings-border bg-card px-3 py-2 text-xs font-medium text-foreground transition-colors hover:bg-hover"
              >
                {copied ? <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" /> : <ClipboardCopy className="h-3.5 w-3.5" />}
                {copied ? '已复制' : '复制链接'}
              </button>
            </div>
          </div>

          {/* 展示白名单 */}
          <div className="rounded-2xl border settings-border bg-background/35 px-4 py-4">
            <p className="text-sm font-semibold text-foreground">展示股票白名单</p>
            <p className="mt-1 text-xs leading-6 text-muted-text">
              仅白名单内股票的持仓与流水会展示给访客，白名单外数据完全不可见（后端接口层过滤）。多个代码用逗号/顿号分隔，例如：000630, 000999。
            </p>
            <input
              type="text"
              value={symbolsText}
              onChange={(e) => setSymbolsText(e.target.value)}
              placeholder="000630, 000999"
              className="mt-3 w-full rounded-xl border settings-border bg-base px-4 py-2.5 text-sm text-foreground outline-none transition-colors focus:border-primary"
            />
          </div>

          {/* 固定口令 */}
          <div className="rounded-2xl border settings-border bg-background/35 px-4 py-4">
            <div className="flex items-center gap-2">
              <LockKeyhole className="h-4 w-4 text-muted-text" />
              <p className="text-sm font-semibold text-foreground">固定口令</p>
            </div>
            <p className="mt-1 text-xs leading-6 text-muted-text">
              访客进入分享页需输入此口令。口令长期有效，可随时修改或清除；修改/清除后旧口令立即失效。
              {config?.hasPassword ? '（当前已设置口令）' : '（当前未设置口令，访客将无法进入）'}
            </p>
            <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center">
              <input
                type="text"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                placeholder="输入新口令（留空表示不修改）"
                className="flex-1 rounded-xl border settings-border bg-base px-4 py-2.5 text-sm text-foreground outline-none transition-colors focus:border-primary"
              />
              <button
                type="button"
                onClick={() => void save({ password: '' })}
                disabled={saving}
                className="inline-flex items-center justify-center gap-1.5 rounded-xl border settings-border bg-card px-3 py-2 text-xs font-medium text-foreground transition-colors hover:bg-hover"
              >
                清除口令
              </button>
            </div>
          </div>

          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-2 text-xs text-muted-text">
              <Link2 className="h-3.5 w-3.5" />
              保存后即时生效，无需重启。
            </div>
            <button
              type="button"
              onClick={handleSaveAll}
              disabled={saving}
              className="btn-primary inline-flex items-center gap-2 px-4 py-2 text-sm"
            >
              {saving ? '保存中…' : '保存配置'}
            </button>
          </div>

          {error ? (
            <div className="mt-2">
              <ApiErrorAlert
                error={{ title: '操作失败', message: error, rawMessage: error, category: 'unknown' }}
              />
            </div>
          ) : null}
          {success ? <p className="text-sm text-emerald-600 dark:text-emerald-400">{success}</p> : null}
        </>
      )}
    </SettingsSectionCard>
  );
}
