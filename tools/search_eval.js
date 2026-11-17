#!/usr/bin/env node
// Measures search quality on the built library, using the site's own search code in a headless browser.
//
//   node tools/search_eval.js --site site --keyfile .cache/drive/keyfile.bin --topics eval/topics.json --out report.json
//        [--sample 150] [--minutes 30]
//
// Nothing here talks to Google: the built site is served locally and the access key is read from the build cache.
// Three kinds of measurement:
//   1. Known items: papers sampled from the library are searched for by their exact title, by a few distinctive words
//      from their opening text ("describe it"), and by title in the people view (is their researcher found?).
//   2. Topics (eval/topics.json): recruiter-style searches judged automatically: a document counts as relevant when its
//      text contains enough of the topic's evidence words. Reported as precision of the top 10, per search mode.
//   3. Library health: missing details, papers without searchable text, Needs Review, duplicate papers and researchers.
// The report contains paper titles and researcher names, so it is only ever published encrypted (tools/publish_eval.py).
const http = require("http"), fs = require("fs"), path = require("path");
const { chromium } = require("playwright");

const args = Object.fromEntries(process.argv.slice(2).reduce((a, x, i, all) => (x.startsWith("--") ? a.concat([[x.slice(2), all[i + 1]]]) : a), []));
const SITE = path.resolve(args.site || "site"), KEYFILE = args.keyfile, OUT = args.out || "eval-report.json";
const SAMPLE = +(args.sample || 150), BUDGET_MS = +(args.minutes || 30) * 60000, T0 = Date.now();
const topics = JSON.parse(fs.readFileSync(args.topics || path.join(__dirname, "..", "eval", "topics.json"), "utf8")).topics;
const log = m => console.log(`[${((Date.now() - T0) / 1000).toFixed(0).padStart(5)}s] ${m}`);
const outOfTime = () => Date.now() - T0 > BUDGET_MS;

const CONFIG = { googleClientId: "eval", rootFolderId: "EVAL", keyFileName: "Library access key.txt", keyFileId: "EVALKEY",
  driveApiBase: "https://drive.eval.invalid", dataBase: "data/" };
const TYPES = { ".html": "text/html", ".json": "application/json", ".mjs": "text/javascript", ".js": "text/javascript", ".enc": "application/octet-stream" };

function serve() {
  const srv = http.createServer((req, res) => {
    const u = decodeURIComponent(req.url.split("?")[0]);
    if (u === "/config.json") { res.writeHead(200, { "Content-Type": "application/json" }); return res.end(JSON.stringify(CONFIG)); }
    const f = path.join(SITE, u === "/" ? "index.html" : u);
    if (!f.startsWith(SITE) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) { res.writeHead(404); return res.end(); }
    res.writeHead(200, { "Content-Type": TYPES[path.extname(f)] || "application/octet-stream" });
    fs.createReadStream(f).pipe(res);
  });
  return new Promise(r => srv.listen(0, "127.0.0.1", () => r(srv)));
}

const median = a => { if (!a.length) return null; const s = [...a].sort((x, y) => x - y); return s[s.length >> 1]; };
const pct = (a, p) => { if (!a.length) return null; const s = [...a].sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; };
const round = (x, n = 3) => x === null || x === undefined ? null : Math.round(x * 10 ** n) / 10 ** n;
function rankStats(ranks, ks) {          // ranks: 1-based rank or 0 (not found)
  const n = ranks.length, o = { n };
  for (const k of ks) o["top" + k] = n ? round(ranks.filter(r => r && r <= k).length / n) : null;
  o.mrr = n ? round(ranks.reduce((s, r) => s + (r ? 1 / r : 0), 0) / n) : null;
  return o;
}

(async () => {
  const srv = await serve();
  const base = `http://127.0.0.1:${srv.address().port}/`;
  const browser = await chromium.launch();
  const ctx = await browser.newContext();
  await ctx.route("https://accounts.google.com/gsi/client", r => r.fulfill({ contentType: "text/javascript",
    body: `window.google={accounts:{oauth2:{initTokenClient:o=>({requestAccessToken:()=>setTimeout(()=>o.callback({access_token:"eval",expires_in:36000}),5)})}}};` }));
  const keyBytes = fs.readFileSync(KEYFILE);
  await ctx.route(CONFIG.driveApiBase + "/**", r => r.request().url().includes("/files/EVALKEY")
    ? r.fulfill({ status: 200, body: keyBytes, headers: { "Access-Control-Allow-Origin": "*" } })
    : r.fulfill({ status: 404, body: "{}", headers: { "Access-Control-Allow-Origin": "*" } }));
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.goto(base);
  await page.click("#signin");
  await page.waitForFunction(() => typeof S !== "undefined" && (S.loaded || S.loadError), null, { timeout: 600000 });
  const loadMs = Date.now() - T0;
  await page.evaluate(() => { window.hydratePassages = () => {}; });     // passages read papers from Drive: not measured here
  log(`library loaded in ${(loadMs / 1000).toFixed(1)}s`);

  // one search, exactly as a user would run it; returns what is on screen
  const search = p => page.evaluate(async p => {
    const t0 = performance.now();
    history.replaceState(null, "", "#/?" + new URLSearchParams(p));
    route();
    const tick = () => new Promise(r => setTimeout(r, 5));
    while ([...KW.values(), ...SEM.results.values()].some(r => r.pending)) await tick();
    await tick();
    const ms = performance.now() - t0;
    const id = (a, re) => decodeURIComponent(((a.getAttribute("href") || "").match(re) || [])[1] || "");
    return { ms,
      docs: [...document.querySelectorAll("#results article.result:not(.person) h3 > a:first-child")].map(a => id(a, /^#\/doc\/([^?]+)/)),
      people: [...document.querySelectorAll("#results article.person a.pname")].map(a => id(a, /^#\/candidate\/([^?]+)/)) };
  }, p);

  const report = { generated: new Date().toISOString(), loadSeconds: round(loadMs / 1000, 1), sample: SAMPLE, partial: false };
  report.library = await page.evaluate(() => ({ built: S.index.built, docs: S.docs.length, candidates: S.cands.size, aiIndex: !!S.index.embed }));
  // with the AI meaning index, meaning-based tests also run with the older word-association engine, for comparison
  const AI = report.library.aiIndex;
  const MODES = ["keyword", "hybrid", "semantic", ...(AI ? ["hybrid_lsa", "semantic_lsa"] : [])];
  const modeParams = m => ({ keyword: { mode: "keyword" }, hybrid: {}, semantic: { mode: "semantic" }, hybrid_lsa: { engine: "lsa" }, semantic_lsa: { mode: "semantic", engine: "lsa" } }[m]);
  const MODE_NAME = { keyword: "Exact words", hybrid: "Smart", semantic: "Meaning", hybrid_lsa: "Smart (old meaning engine)", semantic_lsa: "Meaning (old engine)" };
  const speed = Object.fromEntries(MODES.map(m => [m, []]));

  // ------------------------------------------------------------ 1. known items
  const items = await page.evaluate(n => {
    const h = s => { let x = 0x811c9dc5; for (const c of s) { x ^= c.charCodeAt(0); x = Math.imul(x, 0x01000193) >>> 0; } return x; };
    const pool = S.docs.filter(d => d.source === "university" && !isExternal(d) && (d.title || "").split(/\s+/).length >= 4 && (d.snippet || "").length > 150);
    pool.sort((a, b) => h(a.id) - h(b.id));
    const words = s => (s.toLowerCase().match(/[a-z][a-z-]{5,}[a-z]/g) || []);
    return pool.slice(0, n).map(d => {
      const tw = new Set(words(d.title)), seen = new Set();
      const desc = words(d.snippet).filter(w => !tw.has(w) && !STOP.has(w) && !seen.has(w) && seen.add(w));
      desc.sort((a, b) => b.length - a.length || (a < b ? -1 : 1));
      return { id: d.id, title: d.title, cand: d.candidate, name: d.name, describe: desc.slice(0, 6).join(" ") };
    });
  }, SAMPLE);
  log(`known-item test: ${items.length} papers`);
  const ki = { exact: [], describe: Object.fromEntries(MODES.map(m => [m, []])), researcher: [] }, misses = [];
  const rankOf = (list, id) => { const i = list.indexOf(id); return i < 0 ? 0 : i + 1; };
  for (const it of items) {
    if (outOfTime()) { report.partial = true; break; }
    let r = await search({ q: it.title, view: "docs", mode: "keyword" });
    speed.keyword.push(r.ms);
    const er = rankOf(r.docs, it.id); ki.exact.push(er);
    if (er !== 1) misses.push({ test: "Exact title", mode: "Exact words", q: it.title, id: it.id, title: it.title, rank: er });
    if (it.describe.split(" ").length >= 4) for (const mode of MODES) {
      r = await search({ q: it.describe, view: "docs", ...modeParams(mode) });
      speed[mode].push(r.ms);
      const dr = rankOf(r.docs, it.id); ki.describe[mode].push(dr);
      if ((!dr || dr > 10) && !mode.endsWith("_lsa")) misses.push({ test: "Describe it", mode: MODE_NAME[mode], q: it.describe, id: it.id, title: it.title, rank: dr });
    }
    r = await search({ q: it.title });
    speed.hybrid.push(r.ms);
    const pr = rankOf(r.people, it.cand); ki.researcher.push(pr);
    if (!pr || pr > 5) misses.push({ test: "Find the researcher", mode: "Smart", q: it.title, id: it.id, title: it.title, name: it.name, rank: pr });
  }
  report.knownItem = {
    exactTitle: rankStats(ki.exact, [1, 10]),
    describe: Object.fromEntries(Object.entries(ki.describe).map(([m, v]) => [m, rankStats(v, [1, 10])])),
    researcher: rankStats(ki.researcher, [1, 5]),
  };
  report.misses = misses.slice(0, 150);
  log(`known-item test done: exact title top-1 ${report.knownItem.exactTitle.top1}, describe (Smart) top-10 ${report.knownItem.describe.hybrid.top10}`);

  // ------------------------------------------------------------ 2. topics (automatic relevance from evidence words)
  report.topics = [];
  for (const t of topics) {
    if (outOfTime()) { report.partial = true; break; }
    const rel = await page.evaluate(async t => {
      const hits = new Map();
      for (const e of t.evidence) {
        const g = parseQuery(e);
        if (!g.length) continue;
        const r = await keywordSearch(g);
        for (const i of r.scores.keys()) hits.set(i, (hits.get(i) || 0) + 1);
      }
      const docs = [...hits].filter(([, n]) => n >= (t.minHits || 2)).map(([i]) => S.docs[i]?.id).filter(Boolean);
      return { docs, people: [...new Set(docs.map(id => S.byId.get(id).candidate))] };
    }, t);
    const relDocs = new Set(rel.docs), relPeople = new Set(rel.people);
    const row = { q: t.q, paraphrase: !!t.paraphrase, relevantDocs: relDocs.size, relevantPeople: relPeople.size, modes: {} };
    if (relDocs.size) {
      for (const mode of MODES) {
        const r = await search({ q: t.q, view: "docs", ...modeParams(mode) });
        speed[mode].push(r.ms);
        const top = r.docs.slice(0, 10);
        row.modes[mode] = { returned: r.docs.length, p10: round(top.filter(id => relDocs.has(id)).length / Math.min(10, relDocs.size)),
          top: top.map(id => [id, relDocs.has(id)]) };
      }
      const r = await search({ q: t.q });
      const topP = r.people.slice(0, 10);
      row.people = { returned: r.people.length, p10: round(topP.filter(id => relPeople.has(id)).length / Math.min(10, relPeople.size)) };
    }
    report.topics.push(row);
  }
  const judged = report.topics.filter(t => t.relevantDocs);
  const avg = (f, set = judged) => { const v = set.map(f).filter(x => x !== null && x !== undefined); return v.length ? round(v.reduce((a, b) => a + b, 0) / v.length) : null; };
  const para = judged.filter(t => t.paraphrase);
  report.topicSummary = { topics: report.topics.length, judged: judged.length, judgedParaphrase: para.length,
    p10: { ...Object.fromEntries(MODES.map(m => [m, avg(t => t.modes[m]?.p10)])), people: avg(t => t.people?.p10) },
    p10Paraphrase: Object.fromEntries(MODES.map(m => [m, avg(t => t.modes[m]?.p10, para)])),
    noResults: { hybrid: judged.filter(t => !t.modes.hybrid?.returned).length, keyword: judged.filter(t => !t.modes.keyword?.returned).length } };
  log(`topic test done: ${judged.length} of ${report.topics.length} topics have relevant papers; Smart precision ${report.topicSummary.p10.hybrid}`);

  // ------------------------------------------------------------ 3. library health
  report.health = await page.evaluate(() => {
    const papers = S.docs.filter(d => d.source === "university");
    const full = papers.filter(d => !isExternal(d));
    const norm = s => (s || "").toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, " ").trim();
    const byTitle = new Map();
    for (const d of papers) { const k = norm(d.title); if (k.split(" ").length < 3) continue; if (!byTitle.has(k)) byTitle.set(k, []); byTitle.get(k).push(d); }
    const sameRes = [], shared = [];
    for (const ds of byTitle.values()) {
      if (ds.length < 2) continue;
      const byCand = new Map(); for (const d of ds) byCand.set(d.candidate, (byCand.get(d.candidate) || []).concat(d));
      for (const g of byCand.values()) if (g.length > 1) sameRes.push(g.map(d => ({ id: d.id, title: d.title, name: d.name, fmt: d.fmt })));
      if (byCand.size > 1) shared.push({ title: ds[0].title, researchers: [...byCand.keys()].map(c => S.cands.get(c)?.name || c) });
    }
    const byName = new Map();
    for (const c of S.cands.values()) { if (c.source !== "university") continue; const k = norm(c.name).split(" ").filter(Boolean); if (k.length < 2) continue;
      const key = k[0] + " " + k[k.length - 1]; if (!byName.has(key)) byName.set(key, []); byName.get(key).push(c); }
    const dupPeople = [...byName.values()].filter(g => g.length > 1).map(g => g.map(c => ({ id: c.id, name: c.name, university: c.university, match: c.match || "", docs: c.docs.length })));
    const cands = [...S.cands.values()].filter(c => c.source === "university");
    return {
      papers: papers.length, fullText: full.length, externalOnly: papers.length - full.length,
      pdf: papers.filter(d => d.fmt === "pdf").length,
      noSearchableText: full.filter(d => (d.len || 0) < 30).length,
      noSearchableTextPdf: full.filter(d => (d.len || 0) < 30 && d.fmt === "pdf").length,
      missingYear: papers.filter(d => !d.year).length, missingAuthors: papers.filter(d => !d.authors).length,
      missingVenue: papers.filter(d => !d.platform || d.platform === "Not recorded").length,
      needsReview: papers.filter(d => d.attribution === "Needs Review").length,
      researchers: cands.length, researchersNoPapers: cands.filter(c => !c.docs.length).length,
      researchersByMatch: cands.reduce((m, c) => (m[c.match || "Other"] = (m[c.match || "Other"] || 0) + 1, m), {}),
      duplicatePapersSameResearcher: sameRes.length, duplicatePaperExamples: sameRes.slice(0, 40),
      papersSharedByResearchers: shared.length, sharedExamples: shared.slice(0, 20),
      possibleDuplicateResearchers: dupPeople.length, duplicateResearcherExamples: dupPeople.slice(0, 60),
    };
  });

  report.speed = Object.fromEntries(Object.entries(speed).map(([m, v]) => [m, { n: v.length, medianMs: round(median(v), 0), p90Ms: round(pct(v, 0.9), 0), maxMs: round(v.length ? Math.max(...v) : null, 0) }]));
  report.meaningModel = await page.evaluate(() => ({ status: EMB.status, error: EMB.error }));
  report.pageErrors = errors.slice(0, 20);
  report.seconds = round((Date.now() - T0) / 1000, 0);
  fs.writeFileSync(OUT, JSON.stringify(report));
  log(`report written to ${OUT}${report.partial ? " (time limit reached: partial)" : ""}`);
  await browser.close(); srv.close();
  if (errors.length) console.error("page errors (recorded in the report):", errors.slice(0, 5));
})().catch(e => { console.error(e); process.exit(1); });
