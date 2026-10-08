// 盲操作测试：不注入 JS/evaluate，只用坐标点击，验证能否绕过反自动化
import { createRequire } from 'module';
const require = createRequire('E:/daily_stock_analysis-main/apps/dsa-web/package.json');
const { chromium } = require('playwright');

const browser = await chromium.launch({
  channel: 'chrome',
  headless: false,
  args: ['--disable-blink-features=AutomationControlled', '--no-sandbox'],
});
const ctx = await browser.newContext({
  viewport: { width: 1280, height: 960 },
  locale: 'zh-CN',
});
const page = await ctx.newPage();
try {
  await page.goto('https://tzzb.10jqka.com.cn/pc/index.html', { timeout: 30000, waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(5000);
  await page.screenshot({ path: 'E:/daily_stock_analysis-main/tools/ths_probe_shot1.png' });
  // 盲点右上角登录/注册（坐标 950,30 视口）
  await page.mouse.click(950, 30);
  await page.waitForTimeout(4000);
  await page.screenshot({ path: 'E:/daily_stock_analysis-main/tools/ths_probe_shot2.png' });
  console.log('URL after click:', page.url());
} catch (e) {
  console.log('ERROR:', e.message.slice(0, 300));
}
await browser.close();
