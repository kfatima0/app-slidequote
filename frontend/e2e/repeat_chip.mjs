// Click a suggestion chip again while a quote is already awaiting confirmation,
// instead of replying "ok". Reported as breaking the interface.
import { chromium } from "playwright";

const URL = process.env.YQUO_URL ?? "http://127.0.0.1:8080/";

const browser = await chromium.launch();
const page = await browser.newPage();

const problems = [];
page.on("console", (m) => {
  if (m.type() === "error") problems.push(`console.error: ${m.text()}`);
});
page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}\n${e.stack}`));
page.on("response", (r) => {
  if (r.status() >= 400) problems.push(`HTTP ${r.status()} ${r.url()}`);
});

const broke = async (label) => {
  const hit = await page.locator("text=Qualcosa si è rotto").count();
  console.log(`${label.padEnd(46)} ${hit ? "BROKEN" : "ok"}`);
  if (hit) console.log("   message:", await page.locator("pre").first().innerText());
  return hit > 0;
};

console.log(`against ${URL}\n`);
await page.goto(URL, { waitUntil: "networkidle" });

await page.getByRole("button", { name: "Richiesta completa" }).click();
await page.waitForTimeout(18000);
if (await broke("first request -> awaiting confirmation")) process.exit(1);
console.log("   supplier rows:", await page.locator(".supplier").count());

// The reported action: the same chip again, rather than answering.
await page.getByRole("button", { name: "Richiesta completa" }).click();
await page.waitForTimeout(12000);
if (await broke("same chip again while awaiting")) process.exit(1);
console.log("   supplier rows:", await page.locator(".supplier").count());
console.log("   chat bubbles:", await page.locator(".bubble").count());

// And a different chip, which is the same shape of mistake.
await page.getByRole("button", { name: "Urgente" }).click();
await page.waitForTimeout(12000);
if (await broke("a different chip while awaiting")) process.exit(1);

const bodies = await page.locator(".bubble").allInnerTexts();
console.log("\nlast reply:", JSON.stringify(bodies.at(-1)?.slice(0, 90)));
console.log("\n--- problems ---");
console.log(problems.length ? problems.join("\n\n") : "none");
await browser.close();
