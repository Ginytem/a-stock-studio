// 最小可行性测试：Playwright 打开同花顺投资账本，检查是否被反自动化检测拦截
import { createRequire } from 'module';
const require = createRequire('E:/daily_stock_analysis-main/apps/dsa-web/package.json');
const { chromium } = require('playwright');

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

const browser = await chromium.launch({
  channel: 'chrome',
  headless: false,
  args: [
    '--disable-blink-features=AutomationControlled',
    '--no-sandbox',
  ],
});
const ctx = await browser.newContext({
  userAgent: UA,
  viewport: { width: 1280, height: 960 },
  locale: 'zh-CN',
});
await ctx.addInitScript(() => {
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
});
const page = await ctx.newPage();
page.on('console', m => { if (m.type() === 'error') console.log('[console.error]', m.text().slice(0, 150)); });
try {
  await page.goto('https://tzzb.10jqka.com.cn/pc/index.html', { timeout: 30000, waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  const url = page.url();
  const text = (await page.evaluate(() => document.body.innerText || '')).slice(0, 300);
  console.log('URL:', url);
  console.log('TEXT:', text.replace(/\n/g, ' | '));
  const hasLogin = /登录|注册/.test(text);
  console.log('hasLoginEntry:', hasLogin);
  await page.screenshot({ path: 'E:/daily_stock_analysis-main/tools/ths_probe_shot.png' });
} catch (e) {
  console.log('ERROR:', e.message.slice(0, 300));
}
await browser.close();
