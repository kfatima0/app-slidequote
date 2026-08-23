// Drive the demo through the sequences a person actually performs, and report
// anything the page complains about. Run with the backend and dev server up.
import { chromium } from "playwright";

const browser = await chromium.launch();
const page = await browser.newPage();

const problems = [];
page.on("console", (m) => {
  if (m.type() === "error") problems.push(`console.error: ${m.text()}`);
});
page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}\n${e.stack}`));
page.on("requestfailed", (r) =>
  problems.push(`requestfailed: ${r.url()} ${r.failure()?.errorText}`),
);

const broke = async (label) => {
  const hit = await page.locator("text=Qualcosa si è rotto").count();
  console.log(`${label.padEnd(34)} ${hit ? "BROKEN" : "ok"}`);
  if (hit) {
    console.log("   message:", await page.locator("pre").first().innerText());
  }
  return hit > 0;
};

await page.goto("http://127.0.0.1:4321/", { waitUntil: "networkidle" });
await broke("after load");

await page.getByRole("button", { name: "Richiesta completa" }).click();
await page.waitForTimeout(18000);
if (await broke("after first request")) process.exit(1);

// Open the evidence for the phone supplier, then close it.
const evidence = page.getByRole("button", { name: "trascrizione" }).first();
if (await evidence.count()) {
  await evidence.click();
  await page.waitForTimeout(800);
  if (await broke("after opening a transcript")) process.exit(1);
  await page.keyboard.press("Escape");
}

// Confirm, which drives placement.
await page.getByPlaceholder("Scrivi un messaggio").fill("ok");
await page.keyboard.press("Enter");
await page.waitForTimeout(8000);
if (await broke("after confirming")) process.exit(1);

// A second request on top of the first.
await page.getByRole("button", { name: "Richiesta completa" }).click();
await page.waitForTimeout(15000);
if (await broke("after a second request")) process.exit(1);

// Reset, then use it again.
await page.getByRole("button", { name: "Azzera demo" }).click();
await page.waitForTimeout(3000);
if (await broke("after reset")) process.exit(1);

await page.getByRole("button", { name: "Senza quantità" }).click();
await page.waitForTimeout(6000);
if (await broke("after a request post-reset")) process.exit(1);

// Every pipeline row, in whatever state it is in. Clicking one re-reads the
// order, and an order parked at "Ricevuto" has no items and no quotes yet,
// which is the emptiest shape the detail view ever has to render.
const rows = page.locator(".order-row");
for (let i = 0; i < (await rows.count()); i += 1) {
  const state = (await rows.nth(i).locator(".status-chip").innerText()).trim();
  await rows.nth(i).click();
  await page.waitForTimeout(1000);
  if (await broke(`after clicking a '${state}' row`)) process.exit(1);
}

console.log("\nsupplier rows:", await page.locator(".supplier").count());
console.log("chat bubbles:", await page.locator(".bubble").count());
console.log("\n--- problems ---");
console.log(problems.length ? problems.join("\n\n") : "none");

await browser.close();
