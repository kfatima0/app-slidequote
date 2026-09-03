// Type something the catalog does not contain, and check a reply comes back.
import { chromium } from "playwright";
const URL = process.env.YQUO_URL ?? "http://127.0.0.1:8080/";
const browser = await chromium.launch();
const page = await browser.newPage();
const problems = [];
page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
page.on("console", (m) => { if (m.type() === "error") problems.push(`console: ${m.text()}`); });

await page.goto(URL, { waitUntil: "networkidle" });
const box = page.getByPlaceholder("Scrivi un messaggio");
await box.click();
await box.pressSequentially("mi servono 3 flangiatori quantici", { delay: 15 });
await page.keyboard.press("Enter");
await page.waitForTimeout(9000);

const broke = await page.locator("text=Qualcosa si è rotto").count();
console.log("broken:", broke > 0);
console.log("bubbles:");
for (const b of await page.locator(".bubble").allInnerTexts()) {
  console.log("   " + b.replace(/\n/g, " | ").slice(0, 110));
}
console.log("problems:", problems.length ? problems.join("; ") : "none");
await browser.close();
