import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const BASE = process.env.SHOT_BASE ?? "http://localhost:5174";
const OUT = "/tmp/shots";
mkdirSync(OUT, { recursive: true });

const routes = [
  ["command", "/"],
  ["portfolio", "/portfolio"],
  ["trades", "/trades"],
  ["research", "/research"],
  ["strategies", "/strategies"],
  ["learning", "/learning"],
  ["settings", "/settings"],
  ["audit", "/audit"],
];

const browser = await chromium.launch();
const ctx = await browser.newContext({
  viewport: { width: 1480, height: 940 },
  deviceScaleFactor: 2,
  colorScheme: "dark",
});
const page = await ctx.newPage();

for (const [name, path] of routes) {
  await page.goto(`${BASE}${path}`, { waitUntil: "networkidle", timeout: 30000 }).catch(() => {});
  await page.waitForTimeout(1800); // fonts, charts, first poll
  await page.screenshot({ path: `${OUT}/${name}.png` });
  console.log(`shot ${name}`);
}

await browser.close();
console.log("done");
