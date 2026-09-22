// Browser recording end to end, the way a person does it — clicks, not API.
//
//   NODE_PATH=<dir with playwright> node browser_record.js <app url> <password> <call page url> <mic.wav> <screens dir>
//
// Chrome's fake devices stand in for the hardware: the microphone plays
// <mic.wav> (what "you" say), and a second tab titled "Call" plays the other
// side; getDisplayMedia auto-picks that tab with its audio.
const { chromium } = require("playwright");

const [APP, PASSWORD, CALL, MIC, SHOTS] = process.argv.slice(2);
const PREFIX = process.env.SHOT_PREFIX || "notetaker-";
const shot = (page, name) => page.screenshot({ path: `${SHOTS}/${PREFIX}${name}.png` });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch({
    headless: !process.env.REAL_CAPTURE,
    executablePath: process.env.CHROME || undefined,
    args: [
      // REAL_CAPTURE: a visible browser capturing the real "Call" tab (headless
      // Chrome has no tab capture); the microphone is denied so nothing in the
      // room is recorded, and the speakers stay silent.
      ...(process.env.REAL_CAPTURE ? [process.env.MUTE === "0" ? "--x" : "--mute-audio"]
        : ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", `--use-file-for-fake-audio-capture=${MIC}`]),
      "--auto-select-tab-capture-source-by-title=Call",
      "--autoplay-policy=no-user-gesture-required",
      "--enable-features=AutoplayIgnoreWebAudio",
    ],
  });
  const ctx = await browser.newContext({ viewport: { width: 1360, height: 860 }, permissions: process.env.REAL_CAPTURE ? [] : ["microphone"] });
  const call = await ctx.newPage();
  await call.goto(CALL);
  const page = await ctx.newPage();
  page.on("console", (m) => console.log("console:", m.type(), m.text()));
  page.on("pageerror", (e) => console.log("pageerror:", e.message));

  await page.goto(APP);
  await shot(page, "01-login");
  await page.fill('input[name="password"]', PASSWORD);
  await page.click("button.btn.primary");
  await page.waitForSelector("#btn-record");
  await sleep(800);
  await shot(page, "02-home");

  await page.click("#btn-record");
  await page.fill('#dlg-record input[name="title"]', "");
  await page.check('#dlg-record input[name="consent"]');
  await page.selectOption("#rec-template", "standup");
  await shot(page, "03-record-dialog");
  await page.click('#dlg-record button[value="ok"]');
  // the call starts talking as the recording starts
  await call.evaluate(() => document.getElementById("a").play());
  await page.waitForSelector("#rec-stop", { timeout: 20000 });
  await sleep(12000);
  await shot(page, "04-recording");
  await sleep(50000);
  await page.click("#rec-stop");
  await page.waitForSelector(".progress, .tabs", { timeout: 60000 });
  await sleep(1500);
  await shot(page, "05-processing");
  await page.waitForSelector(".tabs", { timeout: 300000 });
  await sleep(1000);
  await shot(page, "06-notes");
  await page.click('.tabs [data-tab="transcript"]');
  await sleep(600);
  await shot(page, "07-transcript");
  await page.click('.tabs [data-tab="ask"]');
  await page.fill("#ask-q", "Write a short follow-up message to Sarah with what we agreed.");
  await page.click("#ask-form button");
  await page.waitForSelector(".msg.ai:not(.pending)", { timeout: 120000 });
  await sleep(500);
  await shot(page, "08-ask");
  const url = page.url();
  const mid = url.split("/m/")[1];
  const m = await page.evaluate(async (id) => (await fetch(`/api/meetings/${id}`)).json(), mid);
  console.log("RESULT", JSON.stringify({ id: m.id, status: m.status, duration: m.duration, tracks: m.tracks,
    usage: m.usage, speakers: m.segments.map((s) => s.speaker), lines: m.segments.length, title: m.title }));
  for (const s of m.segments) console.log(`  [${Math.floor(s.start)}s] ${s.speaker}: ${s.text}`);

  await page.click("#btn-settings");
  await sleep(500);
  await shot(page, "09-settings");
  await page.click("#btn-test");
  await page.waitForSelector("#test-out p:not(.muted)", { timeout: 60000 });
  await sleep(500);
  await shot(page, "10-settings-test");
  await browser.close();
})().catch((e) => { console.error("FAILED", e); process.exit(1); });
