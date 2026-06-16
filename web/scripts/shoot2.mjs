import { chromium } from "playwright";
const BASE = "http://localhost:5174";
const OUT = "/tmp/shots";
const browser = await chromium.launch();
const page = await browser
  .newContext({ viewport: { width: 1480, height: 1100 }, deviceScaleFactor: 2, colorScheme: "dark" })
  .then((c) => c.newPage());

// Settings — Connection (default)
await page.goto(`${BASE}/settings`, { waitUntil: "networkidle" });
await page.waitForTimeout(1200);
await page.screenshot({ path: `${OUT}/settings_connection.png` });

// Settings — Safety tab
await page.getByRole("tab", { name: "Safety" }).click();
await page.waitForTimeout(700);
await page.screenshot({ path: `${OUT}/settings_safety.png` });

// Settings — Bot tab
await page.getByRole("tab", { name: "Bot" }).click();
await page.waitForTimeout(700);
await page.screenshot({ path: `${OUT}/settings_bot.png` });

await browser.close();
console.log("done");
