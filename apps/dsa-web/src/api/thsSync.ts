import apiClient from './index';
import { toCamelCase } from './utils';

export type ThsStatus = {
  loggedIn: boolean;
  platform: string;
  cookieFile: string;
};

export type ThsQrCode = {
  qrid: string;
  qrImage: string; // base64 png
};

export type ThsPollResult = {
  success: boolean;
  loggedIn: boolean;
};

export type ThsSyncResult = {
  account: { id: number; name: string };
  accountsScanned: number;
  tradesFetched: number;
  tradesImported: number;
  tradesSkipped?: number;
  importErrors?: string[];
  positionsMerged: number;
  positionsApplied: number;
  rebuilt: string[];
  rebuildErrors: string[];
  cashImport: {
    adjusted: boolean;
    direction?: string;
    amount?: number;
    currentCash: number;
    deletedOld: number;
  };
  totalCash: number;
  assetPoints: number;
  assetWritten: number;
};

export type ThsTrade = {
  code: string;
  name: string;
  tradeDate: string;
  tradeTime: string;
  side: 'buy' | 'sell';
  opName?: string;
  quantity: number;
  price: number;
  amount: number;
  fee: number;
  transNo?: string;
};

export type ThsTradesResult = {
  total: number;
  trades: ThsTrade[];
};

export type ThsImportRecord = {
  code: string;
  name: string;
  recordType: string;
  tradeDate: string;
  tradeTime: string;
  quantity: number;
  price: number;
  amount: number;
  fee: number;
  note: string;
};

export type ThsImportRecordsResult = {
  total: number;
  records: ThsImportRecord[];
};

export type ThsStockLedgerRecord = {
  recordType: string;
  date: string;
  time: string;
  quantity: number;
  price: number;
  amount: number;
  fee: number;
  note: string;
};

export type ThsStockLedgerItem = {
  symbol: string;
  name: string;
  buyCount: number;
  buyAmount: number;
  buyFee: number;
  sellCount: number;
  sellAmount: number;
  sellFee: number;
  dividendCount: number;
  dividendAmount: number;
  adjustCount: number;
  adjustAmount: number;
  otherFee: number;
  latestDate: string;
  records: ThsStockLedgerRecord[];
};

export type ThsStockLedgerResult = {
  totalStocks: number;
  stocks: ThsStockLedgerItem[];
};

export type ThsReconcileResult = {
  available: boolean;
  reason?: string;
  aligned: boolean | null;
  position?: {
    webCount: number;
    localCount: number;
    webTotalValue: number;
    localTotalValue: number;
    valueDiff: number;
    diffPositions: {
      code: string;
      issue: string;
      web?: { quantity: number; cost: number };
      local?: { quantity: number; cost: number };
    }[];
    aligned: boolean;
  };
  cash?: {
    web: number;
    local: number;
    diff: number;
  };
  reasons: string[];
  suggestExport: boolean;
};

const LONG_TIMEOUT = 190000;

export const thsApi = {
  getStatus: () =>
    apiClient.get<ThsStatus>('/api/v1/ths/status').then((r) => toCamelCase<ThsStatus>(r.data)),
  createQrCode: () =>
    apiClient.post<ThsQrCode>('/api/v1/ths/qr-code').then((r) => toCamelCase<ThsQrCode>(r.data)),
  pollLogin: (qrid: string, timeout = 180) =>
    apiClient
      .post<ThsPollResult>(`/api/v1/ths/poll?qrid=${qrid}&timeout=${timeout}`, null, {
        timeout: LONG_TIMEOUT,
      })
      .then((r) => toCamelCase<ThsPollResult>(r.data)),
  sync: (importAsset = true, startDate?: string, endDate?: string) => {
    const params = new URLSearchParams();
    params.set('import_asset', String(importAsset));
    if (startDate) params.set('start_date', startDate);
    if (endDate) params.set('end_date', endDate);
    return apiClient
      .post<ThsSyncResult>(`/api/v1/ths/sync?${params.toString()}`, null, {
        timeout: LONG_TIMEOUT,
      })
      .then((r) => toCamelCase<ThsSyncResult>(r.data));
  },
  getTrades: (startDate?: string, endDate?: string) => {
    const params = new URLSearchParams();
    if (startDate) params.set('start_date', startDate);
    if (endDate) params.set('end_date', endDate);
    return apiClient
      .get<ThsTradesResult>(`/api/v1/ths/trades?${params.toString()}`, { timeout: LONG_TIMEOUT })
      .then((r) => toCamelCase<ThsTradesResult>(r.data));
  },
  getImportRecords: (startDate?: string, endDate?: string) => {
    const params = new URLSearchParams();
    if (startDate) params.set('start_date', startDate);
    if (endDate) params.set('end_date', endDate);
    return apiClient
      .get<ThsImportRecordsResult>(`/api/v1/ths/import-records?${params.toString()}`, { timeout: LONG_TIMEOUT })
      .then((r) => toCamelCase<ThsImportRecordsResult>(r.data));
  },
  getStockLedger: (startDate?: string, endDate?: string) => {
    const params = new URLSearchParams();
    if (startDate) params.set('start_date', startDate);
    if (endDate) params.set('end_date', endDate);
    return apiClient
      .get<ThsStockLedgerResult>(`/api/v1/ths/stock-ledger?${params.toString()}`, { timeout: LONG_TIMEOUT })
      .then((r) => toCamelCase<ThsStockLedgerResult>(r.data));
  },
  reconcile: () =>
    apiClient
      .get<ThsReconcileResult>('/api/v1/ths/reconcile', { timeout: LONG_TIMEOUT })
      .then((r) => toCamelCase<ThsReconcileResult>(r.data)),
  logout: () => apiClient.post('/api/v1/ths/logout').then((r) => r.data),
};

// ---------- 账本导出文件（汇总持仓.xlsx）同步 ----------
export type ThsExportStats = {
  tradeBuy: number;
  tradeSell: number;
  cashIn: number;
  cashOut: number;
  other: number;
  firstDate: string | null;
  lastDate: string | null;
};

export type ThsExportConfig = {
  directory: string;
  autoSync: boolean;
  lastFile: string;
  lastSyncedAt: string;
};

export type ThsExportParseResult = {
  sheets: string[];
  positionCount: number;
  positions: { code: string; name: string; quantity: number; cost: number }[];
  marketValue: number;
  tradeStats: ThsExportStats;
  cashInTotal: number;
  cashOutTotal: number;
};

export type ThsExportImportResult = {
  account: { id: number; name: string };
  file: string;
  positionCount: number;
  positionsApplied: number;
  rebuilt: string[];
  rebuildErrors: string[];
  tradeStats: ThsExportStats;
  cashInTotal: number;
  cashOutTotal: number;
  marketValue: number;
  cashImport: {
    adjusted?: boolean;
    direction?: string;
    amount?: number;
    currentCash?: number;
    skipped?: boolean;
    note?: string;
    error?: string;
  };
  totalCash: number | null;
  detected?: boolean;
  changed?: boolean;
  message?: string;
  lastFile?: string;
  filePath?: string;
  lastSyncedAt?: string;
};

const EXPORT_TIMEOUT = 120000;

export const thsExportApi = {
  parse: (file: File) => {
    const form = new FormData();
    form.append('file', file);
    return apiClient
      .post<ThsExportParseResult>('/api/v1/ths/export-parse', form, { timeout: EXPORT_TIMEOUT })
      .then((r) => toCamelCase<ThsExportParseResult>(r.data));
  },
  import: (file: File) => {
    const form = new FormData();
    form.append('file', file);
    return apiClient
      .post<ThsExportImportResult>('/api/v1/ths/export-import', form, { timeout: EXPORT_TIMEOUT })
      .then((r) => toCamelCase<ThsExportImportResult>(r.data));
  },
  getConfig: () =>
    apiClient.get<ThsExportConfig>('/api/v1/ths/export-config').then((r) => toCamelCase<ThsExportConfig>(r.data)),
  saveConfig: (directory?: string, autoSync?: boolean) => {
    const params = new URLSearchParams();
    if (directory !== undefined) params.set('directory', directory);
    if (autoSync !== undefined) params.set('auto_sync', String(autoSync));
    return apiClient
      .post<ThsExportConfig>(`/api/v1/ths/export-config?${params.toString()}`, null)
      .then((r) => toCamelCase<ThsExportConfig>(r.data));
  },
  detect: () =>
    apiClient
      .post<ThsExportImportResult>('/api/v1/ths/export-detect', null, { timeout: EXPORT_TIMEOUT })
      .then((r) => toCamelCase<ThsExportImportResult>(r.data)),
};
