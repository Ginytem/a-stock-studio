import apiClient from './index';

export type AuthStatusResponse = {
  authEnabled: boolean;
  loggedIn: boolean;
  passwordSet?: boolean;
  passwordChangeable?: boolean;
  setupState: 'enabled' | 'password_retained' | 'no_password';
  totpBound?: boolean;
  totpRequired?: boolean;
};

export type TotpSetupResponse = {
  secret: string;
  uri: string;
  issuer: string;
  account: string;
};

export const authApi = {
  async getStatus(): Promise<AuthStatusResponse> {
    const { data } = await apiClient.get<AuthStatusResponse>('/api/v1/auth/status');
    return data;
  },

  async updateSettings(
    authEnabled: boolean,
    password?: string,
    passwordConfirm?: string,
    currentPassword?: string
  ): Promise<AuthStatusResponse> {
    const body: {
      authEnabled: boolean;
      password?: string;
      passwordConfirm?: string;
      currentPassword?: string;
    } = { authEnabled };
    if (password !== undefined) {
      body.password = password;
    }
    if (passwordConfirm !== undefined) {
      body.passwordConfirm = passwordConfirm;
    }
    if (currentPassword !== undefined) {
      body.currentPassword = currentPassword;
    }
    const { data } = await apiClient.post<AuthStatusResponse>('/api/v1/auth/settings', body);
    return data;
  },

  async login(password: string, passwordConfirm?: string, totpCode?: string): Promise<void> {
    const body: { password: string; passwordConfirm?: string; totpCode?: string } = { password };
    if (passwordConfirm !== undefined) {
      body.passwordConfirm = passwordConfirm;
    }
    if (totpCode !== undefined) {
      body.totpCode = totpCode;
    }
    await apiClient.post('/api/v1/auth/login', body);
  },

  async changePassword(
    currentPassword: string,
    newPassword: string,
    newPasswordConfirm: string
  ): Promise<void> {
    await apiClient.post('/api/v1/auth/change-password', {
      currentPassword,
      newPassword,
      newPasswordConfirm,
    });
  },

  async logout(): Promise<void> {
    await apiClient.post('/api/v1/auth/logout');
  },

  async totpSetup(): Promise<TotpSetupResponse> {
    const { data } = await apiClient.post<TotpSetupResponse>('/api/v1/auth/totp/setup');
    return data;
  },

  async totpEnable(secret: string, code: string): Promise<void> {
    await apiClient.post('/api/v1/auth/totp/enable', { secret, code });
  },

  async totpDisable(currentPassword: string): Promise<void> {
    await apiClient.post('/api/v1/auth/totp/disable', { currentPassword });
  },
};
