// Type messages into the composer the way a person does, one key at a time.
import { chromium } from "playwright";

const URL = process.env.SLIDEQUOTE_URL ?? "http://127.0.0.1:4321/";
const MESSAGES = [
  "Ciao, mi servono 100 metri di cavo FROR 3x2.5 e 4 magnetotermici 16A, consegna a Rovereto",
  "Servono 2 plafoniere LED 36W e 1 rotolo di striscia LED 24V",
  "Mi servono 2 valvole a sfera 1/2 e 20 metri di multistrato 16, consegna a Pergine",
  "ok",
];

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
  console.log(`${label.slice(0, 48).padEnd(50)} ${hit ? "BROKEN" : "ok"}`);
  if (hit) console.log("   message:", await page.locator("pre").first().innerText());
  return hit > 0;
};

console.log(`against ${URL}\n`);
await page.goto(URL, { waitUntil: "networkidle" });
await broke("after load");

for (const message of MESSAGES) {
  const box = page.getByPlaceholder("Scrivi un messaggio");
  await box.click();
  // Character by character, including the accents and slashes a person types.
  await box.pressSequentially(message, { delay: 12 });
  if (await broke(`while typing: ${message}`)) process.exit(1);
  await page.keyboard.press("Enter");
  await page.waitForTimeout(message === "ok" ? 8000 : 16000);
  if (await broke(`after sending: ${message}`)) process.exit(1);
}

console.log("\nchat bubbles:", await page.locator(".bubble").count());
console.log("--- problems ---");
console.log(problems.length ? problems.join("\n\n") : "none");
await browser.close();
