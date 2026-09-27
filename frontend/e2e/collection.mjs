// The collection panel, in a real browser.
//
// This project has twice shipped a panel that built cleanly, typechecked, and
// rendered nothing: an Astro slot that never hydrated, and a Pydantic model
// whose field types existed only under TYPE_CHECKING. Neither was caught by a
// test that did not open a browser. So this opens one, runs the ordinary
// sequence, and asserts the panel is actually on the page with numbers in it.
import { chromium } from "playwright";

const URL = process.env.SLIDEQUOTE_URL ?? "http://127.0.0.1:8080/";

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
await page.waitForTimeout(20000);
if (await broke("request -> collection panel")) process.exit(1);

const lanes = await page.locator(".lane").count();
const states = await page.locator(".states .state-chip").count();
console.log("   supplier lanes:", lanes);
console.log("   state chips:", states);
if (lanes === 0) {
  problems.push("the collection panel rendered no supplier lanes");
}

// Open a lane and check the timeline is populated rather than an empty shell.
if (lanes > 0) {
  await page.locator(".lane-head").first().click();
  await page.waitForTimeout(400);
  const entries = await page.locator(".timeline li").count();
  console.log("   timeline entries in first lane:", entries);
  if (entries === 0) problems.push("a supplier lane opened with an empty timeline");

  // Human takeover is the one destructive control on this screen; check it is
  // reachable and that a blank reason cannot trigger it.
  const takeover = page.locator(".takeover button").first();
  if (await takeover.count()) {
    console.log("   takeover disabled with no reason:", await takeover.isDisabled());
    await page.locator(".takeover input").first().fill("il fornitore vuole una persona");
    await takeover.click();
    await page.waitForTimeout(1500);
    if (await broke("human takeover")) process.exit(1);
    console.log("   lanes closed after takeover:",
      await page.locator(".lane.discarded").count());
  } else {
    console.log("   takeover: no open lane left to take over (round finished)");
  }
}

// The comparison table is the panel's whole point: if extraction produced
// nothing, the operator has lanes and no numbers, which looks like it works.
const rows = await page.locator(".comparison tbody tr").count();
const confidences = await page.locator(".conf").allInnerTexts();
console.log("   comparison rows:", rows);
console.log("   confidences shown:", confidences.join(" "));
if (rows === 0) problems.push("the comparison table rendered no quotes");
if (confidences.length === 0) problems.push("no confidence was shown for any quote");

// Operator override. The one control on this screen an automated run can always
// reach, and the one whose failure would be silent: a correction that does not
// round-trip leaves the operator looking at the model's number believing it is
// theirs.
if (rows > 0) {
  const before = await page.locator(".comparison tbody tr td").nth(1).innerText();
  await page.locator(".comparison .link").first().click();
  await page.waitForTimeout(300);
  await page.locator(".editor input").first().fill("199,90");
  await page.locator(".editor button").first().click();
  await page.waitForTimeout(1500);
  if (await broke("operator override")) process.exit(1);
  const after = await page.locator(".comparison tbody tr td").nth(1).innerText();
  console.log("   total before/after override:", JSON.stringify(before), "->",
    JSON.stringify(after));
  if (!after.includes("199,90")) {
    problems.push(`the override did not reach the table (still ${after})`);
  }
}

const spend = await page.locator(".spend").innerText().catch(() => "(absent)");
console.log("   spend line:", JSON.stringify(spend.slice(0, 80)));

console.log("\n--- problems ---");
console.log(problems.length ? problems.join("\n\n") : "none");
await browser.close();
if (problems.length) process.exit(1);
