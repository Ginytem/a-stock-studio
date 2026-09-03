import type React from 'react';
import { useState } from 'react';
import { QRCodeSVG } from 'qrcode.react';
import { authApi, type TotpSetupResponse } from '../../api/auth';
import { getParsedApiError, isParsedApiError, type ParsedApiError } from '../../api/error';
import { useAuth } from '../../hooks';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { UiTextKey } from '../../i18n/uiText';
import { Badge, Button, Input } from '../common';
import { SettingsAlert } from './SettingsAlert';
import { SettingsSectionCard } from './SettingsSectionCard';

export const TotpCard: React.FC = () => {
  const { authEnabled, totpBound, refreshStatus } = useAuth();
  const { t } = useUiLanguage();

  const [setup, setSetup] = useState<TotpSetupResponse | null>(null);
  const [code, setCode] = useState('');
  const [currentPassword, setCurrentPassword] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | ParsedApiError | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);

  if (!authEnabled) {
    return null;
  }

  const startSetup = async () => {
    setError(null);
    setSuccessMessage(null);
    setIsSubmitting(true);
    try {
      const data = await authApi.totpSetup();
      setSetup(data);
      setCode('');
    } catch (err: unknown) {
      setError(getParsedApiError(err));
    } finally {
      setIsSubmitting(false);
    }
  };

  const confirmEnable = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSuccessMessage(null);
    if (!setup) return;
    if (!code.trim()) {
      setError(t('settings.totpCodeRequired' as UiTextKey));
      return;
    }
    setIsSubmitting(true);
    try {
      await authApi.totpEnable(setup.secret, code.trim());
      setSetup(null);
      setCode('');
      await refreshStatus();
      setSuccessMessage(t('settings.totpSuccess' as UiTextKey));
    } catch (err: unknown) {
      setError(getParsedApiError(err));
    } finally {
      setIsSubmitting(false);
    }
  };

  const cancelSetup = () => {
    setSetup(null);
    setCode('');
    setError(null);
  };

  const confirmDisable = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSuccessMessage(null);
    if (!currentPassword.trim()) {
      setError(t('settings.totpDisableRequiredPassword' as UiTextKey));
      return;
    }
    setIsSubmitting(true);
    try {
      await authApi.totpDisable(currentPassword.trim());
      setCurrentPassword('');
      await refreshStatus();
      setSuccessMessage(t('settings.totpDisabledSuccess' as UiTextKey));
    } catch (err: unknown) {
      setError(getParsedApiError(err));
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <SettingsSectionCard
      title={t('settings.totpTitle' as UiTextKey)}
      description={t('settings.totpDescription' as UiTextKey)}
      actions={
        totpBound ? (
          <Badge variant="success" size="sm">{t('settings.totpBound' as UiTextKey)}</Badge>
        ) : (
          <Badge variant="default" size="sm">{t('settings.totpNotBound' as UiTextKey)}</Badge>
        )
      }
    >
      {setup ? (
        <form onSubmit={confirmEnable} className="space-y-4">
          <div className="flex flex-col items-center gap-3 rounded-xl border border-[var(--settings-border)] bg-[var(--settings-surface)] p-4">
            <QRCodeSVG value={setup.uri} size={168} level="M" marginSize={1} />
            <p className="text-xs text-muted-text">{t('settings.totpScanHint' as UiTextKey)}</p>
            <div className="w-full rounded-lg border border-[var(--settings-border)] bg-[var(--settings-surface-hover)] p-2">
              <p className="mb-1 text-[10px] uppercase tracking-wider text-muted-text">{t('settings.totpManualSecret' as UiTextKey)}</p>
              <p className="break-all font-mono text-xs text-foreground">{setup.secret}</p>
            </div>
          </div>

          <Input
            id="totp-bind-code"
            type="text"
            inputMode="numeric"
            maxLength={6}
            label={t('settings.totpCodeLabel' as UiTextKey)}
            placeholder={t('settings.totpCodePlaceholder' as UiTextKey)}
            value={code}
            onChange={(e) => setCode(e.target.value)}
            disabled={isSubmitting}
            autoComplete="one-time-code"
          />

          {error ? (
            isParsedApiError(error) ? (
              <SettingsAlert title={t('settings.totpFailure' as UiTextKey)} message={error.message} variant="error" />
            ) : (
              <SettingsAlert title={t('settings.totpFailure' as UiTextKey)} message={error} variant="error" />
            )
          ) : null}

          <div className="flex flex-wrap gap-2">
            <Button type="submit" variant="settings-primary" isLoading={isSubmitting}>
              {t('settings.totpConfirmBind' as UiTextKey)}
            </Button>
            <Button type="button" variant="settings-secondary" onClick={cancelSetup} disabled={isSubmitting}>
              {t('settings.cancel' as UiTextKey)}
            </Button>
          </div>
        </form>
      ) : totpBound ? (
        <form onSubmit={confirmDisable} className="space-y-3">
          <p className="text-sm text-muted-text">{t('settings.totpBoundHint' as UiTextKey)}</p>
          <div className="space-y-3 md:max-w-md">
            <Input
              id="totp-disable-password"
              type="password"
              allowTogglePassword
              iconType="password"
              label={t('settings.authCurrentPassword')}
              placeholder={t('settings.authPasswordPlaceholder')}
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              disabled={isSubmitting}
              autoComplete="current-password"
            />
          </div>

          {error ? (
            isParsedApiError(error) ? (
              <SettingsAlert title={t('settings.totpFailure' as UiTextKey)} message={error.message} variant="error" />
            ) : (
              <SettingsAlert title={t('settings.totpFailure' as UiTextKey)} message={error} variant="error" />
            )
          ) : null}

          <Button type="submit" variant="settings-secondary" isLoading={isSubmitting}>
            {t('settings.totpDisable' as UiTextKey)}
          </Button>
        </form>
      ) : (
        <div className="space-y-3">
          <p className="text-sm text-muted-text">{t('settings.totpNotBoundHint' as UiTextKey)}</p>
          {error ? (
            isParsedApiError(error) ? (
              <SettingsAlert title={t('settings.totpFailure' as UiTextKey)} message={error.message} variant="error" />
            ) : (
              <SettingsAlert title={t('settings.totpFailure' as UiTextKey)} message={error} variant="error" />
            )
          ) : null}
          {successMessage ? (
            <SettingsAlert title={t('settings.actionSuccess' as UiTextKey)} message={successMessage} variant="success" />
          ) : null}
          <Button type="button" variant="settings-primary" onClick={startSetup} isLoading={isSubmitting}>
            {t('settings.totpEnable' as UiTextKey)}
          </Button>
        </div>
      )}
    </SettingsSectionCard>
  );
};
