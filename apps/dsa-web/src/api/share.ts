import axios from 'axios';
import { API_BASE_URL } from '../utils/constants';
import { toCamelCase } from './utils';
import type { ThsHoldingLedgerItem } from './thsSync';

/**
 * 分享页独立 API 客户端。
 * 不复用全局 apiClient（其 401 拦截器会跳转 /login，与"访客与登录态隔离"冲突）。
 * 同源请求自动携带 cookie（withCredentials），支持分享会话 cookie。
 */

const shareClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000,
  withCredentials: true,
  headers: { 'Content-Type': 'application/json' },
});

export type ShareConfig = {
  enabled: boolean;
  symbols: string[];
  hasPassword: boolean;
  updatedAt?: string;
};

export type ShareLedgerResult = {
  total: number;
  stocks: ThsHoldingLedgerItem[];
};

/** 口令换取 12 小时会话后不再需要传 password；401 = 未认证（需输口令）。 */
export const shareApi = {
  getLedger: async (password?: string): Promise<ShareLedgerResult> => {
    const params = password ? `?password=${encodeURIComponent(password)}` : '';
    const r = await shareClient.get(`/api/v1/share/ledger${params}`);
    return toCamelCase<ShareLedgerResult>(r.data);
  },
  getConfig: async (): Promise<ShareConfig> => {
    const r = await shareClient.get('/api/v1/share/config');
    return toCamelCase<ShareConfig>(r.data);
  },
  saveConfig: async (body: { enabled?: boolean; password?: string | null; symbols?: string[] }): Promise<ShareConfig> => {
    const r = await shareClient.post('/api/v1/share/config', body);
    return toCamelCase<ShareConfig>(r.data);
  },
};
