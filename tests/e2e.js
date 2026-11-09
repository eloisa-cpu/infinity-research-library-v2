// End-to-end tests for the Drive-backed library.
//   node tests/e2e.js            (needs python3 with requests/numpy/scipy/scikit-learn/cryptography, pdftotext, Playwright)
// Builds a test archive laid out like the Google Drive archive (tests/make_fixture.py), serves it through a mock of the
// Drive API (tests/mock_drive.py), runs the real pipeline (tools/drive_sync.py + tools/build_index.py), serves the built
// site and drives it in Chromium with a stubbed Google sign-in. Nothing touches the real Google Drive.
const { chromium } = require("playwright");
const { execFileSync, spawn } = require("child_process");
const fs = require("fs"), os = require("os"), path = require("path"), crypto = require("crypto");

const REPO = path.join(__dirname, "..");
const TMP = process.env.TEST_TMP || fs.mkdtempSync(path.join(os.tmpdir(), "irl-e2e-"));
const DRIVE_PORT = 8767, SITE_PORT = 8768;
const KEY = "test-access-key-0123456789";
let failures = 0, passes = 0;
function check(cond, msg) { if (cond) { passes++; console.log("  ok  " + msg); } else { failures++; console.log("  FAIL " + msg); } }
const py = (args, env = {}) => execFileSync("python3", args, { cwd: REPO, env: { ...process.env, ...env }, stdio: ["ignore", "pipe", "pipe"] }).toString();
const fid = rel => "id" + crypto.createHash("sha1").update(rel).digest("hex").slice(0, 24);
const procs = [];
function serve(args) { const p = spawn("python3", args, { cwd: REPO, stdio: "ignore" }); procs.push(p); return p; }
const sleep = ms => new Promise(r => setTimeout(r, ms));

const ARCHIVE = path.join(TMP, "archive"), CACHE = path.join(TMP, "cache"), SITE = path.join(TMP, "site");
const ENV = { DRIVE_API_BASE: `http://127.0.0.1:${DRIVE_PORT}`, DRIVE_ACCESS_TOKEN: "good-token", DRIVE_ROOT_FOLDER_ID: "ROOT", CACHE_DIR: CACHE };
function buildSite() {
  const out = py(["tools/drive_sync.py"], ENV) + py(["tools/build_index.py", "--cache", CACHE, "--out", SITE, "--blogs", path.join(TMP, "blogs-test.enc")]);
  fs.writeFileSync(path.join(SITE, "config.json"), JSON.stringify({ googleClientId: "test.apps.googleusercontent.com", rootFolderId: "ROOT", keyFileName: "Library access key.txt", driveApiBase: ENV.DRIVE_API_BASE, dataBase: "data/" }));
  return out;
}

async function newPage(browser, token = "good-token") {
  const ctx = await browser.newContext({ acceptDownloads: true });
  await ctx.route("https://accounts.google.com/gsi/client", r => r.fulfill({ contentType: "text/javascript",
    body: `window.google={accounts:{oauth2:{initTokenClient:o=>({requestAccessToken:()=>setTimeout(()=>o.callback({access_token:${JSON.stringify(token)},expires_in:3600}),10)})}}};` }));
  const page = await ctx.newPage();
  page.on("pageerror", e => { failures++; console.log("  FAIL page error: " + e.message); });
  return { ctx, page };
}
async function signIn(page, hash = "") {
  await page.goto(`http://localhost:${SITE_PORT}/${hash}`);
  await page.waitForSelector("#signin");
  await page.click("#signin");
  await page.waitForFunction(() => typeof S !== "undefined" && S.loaded, null, { timeout: 60000 });
}
async function searchAndWait(page, hash) {
  await page.evaluate(h => { location.hash = h; }, hash);
  await page.waitForFunction(() => ![...KW.values(), ...SEM.results.values()].some(r => r.pending), null, { timeout: 60000 });
  await page.waitForTimeout(80);
}
const resultTitles = page => page.$$eval("#results article.result h3 a", as => as.map(a => a.textContent));
const resultSources = page => page.$$eval("#results article.result", els => els.map(e => e.dataset.src || ""));

(async () => {
  // ---------------------------------------------------------------- pipeline
  console.log("Pipeline (mock Drive API)");
  fs.writeFileSync(path.join(TMP, "key.txt"), KEY + "\n");
  py(["tools/encrypt_blogs.py", "--plain", process.env.BLOGS_PLAIN || path.join(__dirname, "fixtures", "blogs-sample.json"), "--keyfile", path.join(TMP, "key.txt"), "--out", path.join(TMP, "blogs-test.enc")]);
  py(["tests/make_fixture.py", ARCHIVE, "--key", KEY, ...(process.env.REAL_ARCHIVE ? ["--real", process.env.REAL_ARCHIVE] : [])]);
  serve(["tests/mock_drive.py", ARCHIVE, "--port", String(DRIVE_PORT), "--tokens", "good-token", "--page-size", "3"]);
  await sleep(800);
  let log = buildSite();
  check(/crawl: \d+ paper files/.test(log) && /0 failed/.test(log), "sync crawls the whole archive (paged listing) without failures");
  const papers = +log.match(/crawl: (\d+) paper files/)[1];
  check(papers >= 10, `${papers} paper files found (Markdown and PDF), logs/_pipeline/notes ignored`);
  log = py(["tools/drive_sync.py"], ENV);
  check(/to download: 0 new\/changed/.test(log), "second sync downloads nothing (incremental)");
  // privacy of the published files
  const files = fs.readdirSync(path.join(SITE, "data"));
  const leak = files.some(f => { const b = fs.readFileSync(path.join(SITE, "data", f)); return b.includes("Kernelton") || b.includes("Fused Attention") || b.includes("quinoxaline"); });
  check(!leak && files.every(f => f.endsWith(".enc")), `all ${files.length} published data files are encrypted (no titles, names or text in clear)`);
  check(!fs.readFileSync(path.join(SITE, "index.html"), "utf8").includes("Kernelton"), "site page contains no library data");
  // access check used before enabling the build (tools/verify_drive_access.py)
  const vOk = py(["tools/verify_drive_access.py"], ENV);
  check(/All checks passed/.test(vOk) && /access-key file can be read \(\d+ bytes; contents not shown\)/.test(vOk) && !vOk.includes(KEY), "access check passes and never prints the key");
  let vBad = "";
  try { py(["tools/verify_drive_access.py"], { ...ENV, DRIVE_ACCESS_TOKEN: "other-token" }); } catch (e) { vBad = e.stdout.toString(); }
  check(/FAIL/.test(vBad) && /share the University Publications folder/.test(vBad), "access check fails with a clear hint when the account cannot see the folder");
  let vNone = "";
  try { py(["tools/verify_drive_access.py"], { ...ENV, DRIVE_ACCESS_TOKEN: "", GOOGLE_APPLICATION_CREDENTIALS: "" }); } catch (e) { vNone = e.stdout.toString() + e.stderr.toString(); }
  check(/No Google credentials/.test(vNone), "without Workload Identity credentials the tools stop with a clear message");
  serve(["-m", "http.server", String(SITE_PORT), "--directory", SITE]);
  await sleep(800);

  const browser = await chromium.launch();
  // ---------------------------------------------------------------- access control
  console.log("Access");
  {
    const { page, ctx } = await newPage(browser);
    await page.goto(`http://localhost:${SITE_PORT}/`);
    await page.waitForSelector("#signin");
    check((await page.textContent("#app")).includes("The library is private"), "visitors see only a sign-in page");
    await ctx.close();
    const o = await newPage(browser, "other-token");
    await o.page.goto(`http://localhost:${SITE_PORT}/`);
    await o.page.click("#signin");
    await o.page.waitForSelector(".gate .error");
    check((await o.page.textContent(".gate")).includes("does not have access"), "a Google account without access to the Drive archive is refused");
    await o.ctx.close();
  }
  const { page } = await newPage(browser);
  await signIn(page);
  const info = await page.evaluate(() => ({ docs: S.docs.length, papers: S.docs.filter(d => d.source === "university").length, cands: S.cands.size }));
  check(info.papers === papers, `signed-in user gets the whole collection: ${info.papers} papers + ${info.docs - info.papers} blog documents, ${info.cands} researchers`);
  check(await page.evaluate(() => !!S.cands.get("top50-c90003")), "researcher folders without papers are listed too");

  // ---------------------------------------------------------------- tokenizer parity
  console.log("Index parity (browser vs Python)");
  const samples = ["Writing CUDA kernels for FlashAttention's backward pass", "Ökonomie: Städte & Häuser (2024)", "量子计算 and GPUs", "isis bus glasses analysis"];
  const pyTok = JSON.parse(py(["-c", "import sys,json;sys.path.insert(0,'tools');import libcore as L;s=json.loads(sys.argv[1]);print(json.dumps([[L.index_tokens(x) for x in s],[L.semantic_tokens(x) for x in s],[L.fnv1a(w) for x in s for w in L.index_tokens(x)]]))", JSON.stringify(samples)]));
  const jsTok = await page.evaluate(s => [s.map(indexTokens), s.map(semTokenize), s.flatMap(x => indexTokens(x).map(fnv1a))], samples);
  check(JSON.stringify(pyTok[0]) === JSON.stringify(jsTok[0]), "full-text tokenizer matches Python (Unicode words)");
  check(JSON.stringify(pyTok[1]) === JSON.stringify(jsTok[1]), "meaning tokenizer matches Python");
  check(JSON.stringify(pyTok[2]) === JSON.stringify(jsTok[2]), "shard hash matches Python");

  // ---------------------------------------------------------------- full-text search
  console.log("Full-text search");
  await searchAndWait(page, "#/?q=softmax&view=docs&mode=keyword");
  check((await resultTitles(page)).includes("Fused Attention Kernels on Tensor Cores"), "a word that appears only in a paper's body text is found");
  await searchAndWait(page, "#/?q=quinoxaline&view=docs&mode=keyword");
  check((await resultTitles(page)).join() === "How to be reasonable about ought", "text inside a PDF is searchable");
  await searchAndWait(page, "#/?q=" + encodeURIComponent('"luminosity calibration"') + "&view=docs&mode=keyword");
  check((await resultTitles(page)).includes("Precision Luminosity at the LHC"), "quoted words");
  await searchAndWait(page, "#/?q=" + encodeURIComponent("microbiome OR deontic") + "&view=docs&mode=keyword");
  const orT = await resultTitles(page);
  check(orT.includes("Probiotic Supplementation Trial") && orT.includes("How to be reasonable about ought"), "OR");
  await searchAndWait(page, "#/?q=" + encodeURIComponent("compiler -dataflow") + "&view=docs&mode=keyword&src=university");
  const negT = await resultTitles(page);
  check(negT.includes("A Tensor Compiler for Accelerators") && !negT.includes("Dataflow Compiler Passes"), "-exclude");
  await searchAndWait(page, "#/?q=" + encodeURIComponent("photoinject*") + "&view=docs&mode=keyword");
  check((await resultTitles(page)).includes("Thermal Emittance in Photoinjectors"), "prefix search (word*)");
  await searchAndWait(page, "#/?q=Kernelton&view=docs&mode=keyword&src=university");
  check((await resultTitles(page)).length === 4, "researcher name finds all their papers (incl. external-only and flagged)");

  // tabs, global search, meaning search
  console.log("Tabs and meaning search");
  await searchAndWait(page, "#/?q=GPU&view=docs&n=2000");
  const allSrc = new Set(await resultSources(page));
  check(allSrc.has("university") && allSrc.has("companies"), "global search returns papers and blog posts");
  for (const src of ["university", "companies"]) for (const mode of ["", "semantic"]) {
    await searchAndWait(page, `#/?q=GPU%20kernels&view=docs&n=2000&src=${src}${mode ? "&mode=" + mode : ""}`);
    const s = await resultSources(page);
    check(s.length > 0 && s.every(x => x === src), `tab "${src}" ${mode || "smart"} search stays within its source (${s.length})`);
  }
  await searchAndWait(page, "#/?q=" + encodeURIComponent("attention kernels shared memory") + "&view=docs&mode=semantic&src=university");
  check((await resultTitles(page))[0] === "Fused Attention Kernels on Tensor Cores", "meaning search ranks the closest paper first");
  await searchAndWait(page, "#/?q=" + encodeURIComponent("probiotic microbiome trial") + "&view=docs&src=university");
  check((await resultTitles(page))[0] === "Probiotic Supplementation Trial", "smart mode ranks the best paper first");
  if (process.env.REAL_ARCHIVE) {   // with a real corpus the meaning index also links different wordings
    await searchAndWait(page, "#/?q=" + encodeURIComponent("making neural networks run faster on graphics cards") + "&view=docs&mode=semantic&src=university");
    const t = (await resultTitles(page)).slice(0, 10).join(" | ");
    check(/GPU|kernel|accelerat|inference|parallel|tensor|serving|compil|device|hardware|neural/i.test(t), `meaning search with different wording finds related papers (${t.slice(0, 160)}…)`);
  }

  // ---------------------------------------------------------------- filters + Needs Review
  console.log("Filters and Needs Review");
  await searchAndWait(page, "#/?src=university&view=docs&attr=Needs%20Review&n=5000");
  const nr = await page.$$eval("#results article.result", els => els.map(e => [e.querySelector("h3 a").textContent, e.textContent.includes("Needs Review")]));
  const nrTitles = nr.map(x => x[0]);
  check(nr.every(x => x[1]) && nrTitles.includes("Probiotic Supplementation Trial") && nrTitles.includes("Thermal Emittance in Photoinjectors") && (process.env.REAL_ARCHIVE || nr.length === 2),
    "uncertain researcher matches are kept and flagged Needs Review (OpenAlex 'needs-review' + misattribution folder)");
  await searchAndWait(page, "#/?src=university&view=docs&fmt=PDF");
  check((await resultTitles(page)).join() === "How to be reasonable about ought", "file format filter");
  await searchAndWait(page, "#/?src=university&view=docs&uni=Harvard%20University");
  check((await resultTitles(page)).length === 2, "university filter (new Harvard folder)");
  await searchAndWait(page, "#/?src=university&view=docs&avail=External%20Link%20Only");
  check((await resultTitles(page)).join() === "Paywalled Systems Paper", "availability filter");
  for (const f of ["uni", "attr", "cand", "plat", "type", "fmt", "avail", "year", "group"]) check(await page.$(`#f-${f}`) !== null, `filter "${f}" present`);

  // ---------------------------------------------------------------- opening documents from Drive
  console.log("Opening documents");
  const ids = await page.evaluate(() => ({ md: S.docs.find(d => /Fused Attention/.test(d.title)).id, pdf: S.docs.find(d => d.fmt === "pdf").id,
    ext: S.docs.find(d => d.availability === "External Link Only" && d.drive).id, blog: S.docs.find(d => d.source === "companies" && d.availability === "Locally Archived").id }));
  const driveHits = [];
  page.on("request", r => { if (r.url().startsWith(ENV.DRIVE_API_BASE)) driveHits.push(r.url()); });
  await page.evaluate(id => { location.hash = "#/doc/" + encodeURIComponent(id) + "?q=softmax"; }, ids.md);
  await page.waitForSelector("article.doc");
  check((await page.textContent("article.doc")).includes("shared memory") && /\d+ match/.test(await page.textContent("#mcount")), "Markdown paper opened from Google Drive, search terms highlighted");
  check(driveHits.some(u => u.includes("alt=media")), "the paper text was fetched from Drive at open time");
  await page.evaluate(id => { location.hash = "#/doc/" + encodeURIComponent(id); }, ids.pdf);
  await page.waitForSelector("object.pdfview");
  check(true, "PDF paper shown inline from Drive");
  await page.evaluate(id => { location.hash = "#/doc/" + encodeURIComponent(id); }, ids.ext);
  await page.waitForSelector("#extnotice");
  check((await page.textContent("#extnotice")).includes("You are being redirected to the original source"), "paper without full text shows the external-source notice");
  await page.evaluate(id => { location.hash = "#/doc/" + encodeURIComponent(id); }, ids.blog);
  await page.waitForSelector("article.doc");
  check(!(await page.$("#extnotice")), "blog document opens from the encrypted site data");
  await page.evaluate(() => { location.hash = "#/candidate/uni-a5000000001"; });
  await page.waitForSelector(".infobox");
  check((await page.textContent("#app")).includes("Needs Review") && (await page.textContent(".infobox")).includes("OpenAlex"), "researcher page links OpenAlex and notes Needs Review papers");

  // ---------------------------------------------------------------- bookmarks, role tags, CSV
  console.log("Bookmarks, role tags, CSV");
  await searchAndWait(page, "#/?q=compiler&view=docs&src=university");
  const d1 = await page.$eval("#results article.result [data-save]", b => b.dataset.save);
  await page.click(`#results [data-save="${d1}"]`);
  check(await page.$eval(`#results [data-save="${d1}"]`, b => b.textContent.includes("Bookmarked")), "bookmark a paper");
  await page.click(`#results [data-tagbtn="${d1}"]`);
  await page.click('.tagpop [data-tptoggle="Research Scientist"]');
  await page.fill(".tagpop input", "Compiler Lead");
  await page.press(".tagpop input", "Enter");
  await page.keyboard.press("Escape");
  check(await page.$eval(`[data-rtags="${d1}"]`, e => e.textContent.includes("Research Scientist") && e.textContent.includes("Compiler Lead")), "default + custom role tags on a paper");
  await searchAndWait(page, "#/?q=compiler&src=university");
  const c1 = await page.$eval("#results article.person [data-save]", b => b.dataset.save);
  await page.click(`#results [data-tagbtn="${c1}"]`);
  await page.click('.tagpop [data-tptoggle="CTO"]');
  await page.keyboard.press("Escape");
  await page.reload();
  await page.waitForSelector("#signin, #q, .loading", { timeout: 30000 });
  await page.waitForFunction(() => typeof S !== "undefined" && S.loaded, null, { timeout: 60000 });
  check(true, "session survives a reload without signing in again");
  await page.evaluate(() => { location.hash = "#/saved"; });
  await page.waitForSelector(".tagfilter");
  const cName = await page.evaluate(k => S.cands.get(k.slice(2)).name, c1);
  check((await page.textContent("#app")).includes(cName), "shortlist persists");
  const [dl] = await Promise.all([page.waitForEvent("download"), page.click("#csv")]);
  check(fs.readFileSync(await dl.path(), "utf8").includes("CTO"), "candidate CSV includes role tags");
  await page.evaluate(() => { location.hash = "#/saved?tab=docs"; });
  await page.waitForFunction(() => location.hash.includes("tab=docs"));
  const [dl2] = await Promise.all([page.waitForEvent("download"), page.click("#csv")]);
  const csv2 = fs.readFileSync(await dl2.path(), "utf8");
  check(csv2.includes("Research Scientist; Compiler Lead") && csv2.includes("Original URL"), "bookmark CSV includes role tags and links");
  await page.evaluate(() => { location.hash = "#/saved?manage=1"; });
  await page.waitForSelector("#managetags[open]");
  await page.fill('[data-rename="Compiler Lead"]', "Compilers Lead");
  await page.click('[data-renamebtn="Compiler Lead"]');
  await page.waitForSelector('[data-rename="Compilers Lead"]');
  check(true, "tag renamed");

  // ---------------------------------------------------------------- team sync (mocked GitHub contents API)
  console.log("Team sync via GitHub (mocked API)");
  {
    const remote = { content: null, sha: null, puts: 0, conflicts: 1 };
    const mock = async route => {
      const req = route.request();
      if (req.method() === "GET") {
        if (!remote.content) return route.fulfill({ status: 404, body: "{}" });
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ sha: remote.sha, content: Buffer.from(remote.content).toString("base64") }) });
      }
      const body = JSON.parse(req.postData());
      if (remote.conflicts > 0 && remote.content) { remote.conflicts--; return route.fulfill({ status: 409, body: "{}" }); }
      if ((body.sha || null) !== remote.sha) return route.fulfill({ status: 409, body: "{}" });
      remote.content = Buffer.from(body.content, "base64").toString("utf8"); remote.sha = "sha" + (++remote.puts);
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ content: { sha: remote.sha } }) });
    };
    const cfg = { repo: "acme/team-data", branch: "main", path: "irl/saved.json", token: "test", name: "" };
    const A = await newPage(browser), B = await newPage(browser);
    for (const [who, x] of [["Alice", A], ["Bob", B]]) {
      await x.ctx.route("https://api.github.com/**", mock);
      await x.ctx.addInitScript(c => localStorage.setItem("irl-sync-v1", JSON.stringify(c)), { ...cfg, name: who });
      await signIn(x.page, "#/saved");
    }
    await A.page.waitForFunction(() => SYNC.status().status === "ok");
    await A.page.evaluate(() => { STORE.toggleTag("c:" + S.index.candidates[0].id, "CFO"); });
    await A.page.waitForTimeout(1600);
    await A.page.waitForFunction(() => SYNC.status().status === "ok");
    check(remote.content && remote.content.includes('"CFO"'), "a role tag is written to the shared file");
    await B.page.evaluate(() => { STORE.toggleTag("d:" + S.docs[3].id, "GTM"); });
    await B.page.waitForTimeout(1600);
    await B.page.waitForFunction(() => SYNC.status().status === "ok");
    check(await B.page.evaluate(() => STORE.tagsOf("c:" + S.index.candidates[0].id).includes("CFO")), "another team member sees it");
    check(remote.content.includes('"GTM"') && remote.content.includes('"CFO"'), "conflicting writes are merged");
    await A.ctx.close(); await B.ctx.close();
  }

  // ---------------------------------------------------------------- incremental indexing of new Drive files
  console.log("New files on Drive");
  const nd = path.join(ARCHIVE, "California Institute of Technology", "Fay Brewer (A5000000099)");
  fs.mkdirSync(nd, { recursive: true });
  fs.writeFileSync(path.join(nd, "Publication 001 - Zymurgy at Scale.md"), "# Zymurgy at Scale\n\n- **Title:** Zymurgy at Scale\n- **Authors:** Fay Brewer\n- **Date:** 2026-01-02\n- **Archive status:** Full text\n\n---\n\n# Zymurgy at Scale\n\n## Abstract\n\nZymurgy studies fermentation in brewing at industrial scale with careful yeast selection.\n");
  fs.rmSync(path.join(ARCHIVE, "Stanford University", "Ben Nolabs (C90002)", "Publication 007 - Dataflow Compiler Passes.md"));
  log = buildSite();
  check(/to download: 1 new\/changed/.test(log), "re-sync downloads only the new file");
  await page.goto(`http://localhost:${SITE_PORT}/#/?q=zymurgy&view=docs&mode=keyword`);
  await page.reload();   // a new visit after the index was rebuilt
  await page.waitForFunction(() => typeof S !== "undefined" && S.loaded, null, { timeout: 60000 });
  await searchAndWait(page, "#/?q=zymurgy&view=docs&mode=keyword");
  check((await resultTitles(page)).join() === "Zymurgy at Scale", "a paper added to Drive is searchable after the next index build");
  await searchAndWait(page, "#/?q=dataflow&view=docs&mode=keyword&src=university");
  check(!(await resultTitles(page)).includes("Dataflow Compiler Passes"), "a paper removed from Drive disappears from the index");

  await browser.close();
  procs.forEach(p => p.kill());
  console.log(`\n${passes} passed, ${failures} failed`);
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error(e); procs.forEach(p => p.kill()); process.exit(1); });
