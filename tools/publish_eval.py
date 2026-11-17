#!/usr/bin/env python3
"""Publish the search-quality report made by tools/search_eval.js.

  python3 tools/publish_eval.py --report eval-report.json --site site --cache .cache/drive [--history eval-history.json]

* adds this run's headline numbers to a small history file (kept between runs) so the report can show trends
* writes the full report, encrypted like all library data, to <site>/data/quality.enc ("Search quality" page)
* writes headline numbers only (no titles or names) to the GitHub Actions run summary, which may be public
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from libcore import encrypt, key_from_keyfile  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--report", required=True)
ap.add_argument("--site", default="site")
ap.add_argument("--cache", default=os.environ.get("CACHE_DIR", ".cache/drive"))
ap.add_argument("--history", default="", help="history file (default: <cache>/../eval-history.json)")
args = ap.parse_args()

rep = json.load(open(args.report))
hist_path = args.history or os.path.join(os.path.dirname(os.path.abspath(args.cache)), "eval-history.json")
history = json.load(open(hist_path)) if os.path.exists(hist_path) else []
ki, ts, h = rep.get("knownItem", {}), rep.get("topicSummary", {}), rep.get("health", {})
entry = {
    "generated": rep.get("generated"), "built": rep.get("library", {}).get("built"), "docs": rep.get("library", {}).get("docs"),
    "exactTitleTop1": ki.get("exactTitle", {}).get("top1"),
    "describeTop10": {m: v.get("top10") for m, v in ki.get("describe", {}).items()},
    "researcherTop5": ki.get("researcher", {}).get("top5"),
    "topicP10": ts.get("p10", {}),
    "topicP10Paraphrase": ts.get("p10Paraphrase", {}),
    "meaningModel": rep.get("meaningModel", {}).get("status"),
    "medianMs": {m: v.get("medianMs") for m, v in rep.get("speed", {}).items()},
    "missingYear": h.get("missingYear"), "noSearchableText": h.get("noSearchableText"), "needsReview": h.get("needsReview"),
    "partial": rep.get("partial", False),
}
history = [x for x in history if x.get("generated") != entry["generated"]] + [entry]
history = history[-30:]
os.makedirs(os.path.dirname(hist_path), exist_ok=True)
json.dump(history, open(hist_path, "w"))
rep["history"] = history

key = key_from_keyfile(open(os.path.join(args.cache, "keyfile.bin"), "rb").read())
os.makedirs(os.path.join(args.site, "data"), exist_ok=True)
with open(os.path.join(args.site, "data", "quality.enc"), "wb") as fh:
    fh.write(encrypt(json.dumps(rep, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), key))


def pc(x):
    return "–" if x is None else f"{x * 100:.0f}%"


def share(n, of):
    return "–" if n is None or not of else f"{n:,} ({n / of * 100:.0f}%)"


lines = [
    "## Search quality and library health",
    "",
    "Headline numbers only; the full report (with titles and names) is on the library site under *Search quality*.",
    "",
    "| Test | Result |", "|---|---|",
    f"| Exact title search finds the paper first | {pc(entry['exactTitleTop1'])} |",
    f"| \"Describe it\" search finds the paper in the top 10 (Smart / Exact words / Meaning) | {pc(entry['describeTop10'].get('hybrid'))} / {pc(entry['describeTop10'].get('keyword'))} / {pc(entry['describeTop10'].get('semantic'))} |",
    f"| Title search finds the paper's researcher in the top 5 | {pc(entry['researcherTop5'])} |",
    f"| Topic searches: relevant share of the top 10 (Smart / Exact / Meaning / People) | {pc(ts.get('p10', {}).get('hybrid'))} / {pc(ts.get('p10', {}).get('keyword'))} / {pc(ts.get('p10', {}).get('semantic'))} / {pc(ts.get('p10', {}).get('people'))} |",
    *([f"| Meaning search, AI model vs old engine: \"describe it\" top 10 | {pc(entry['describeTop10'].get('semantic'))} vs {pc(entry['describeTop10'].get('semantic_lsa'))} |",
       f"| Meaning search, AI model vs old engine: topic precision (all / reworded topics) | {pc(ts.get('p10', {}).get('semantic'))} vs {pc(ts.get('p10', {}).get('semantic_lsa'))} / {pc(ts.get('p10Paraphrase', {}).get('semantic'))} vs {pc(ts.get('p10Paraphrase', {}).get('semantic_lsa'))} |"]
      if rep.get("library", {}).get("aiIndex") else []),
    f"| Median search time (Smart / Exact / Meaning) | {' / '.join(str(entry['medianMs'].get(m)) + ' ms' for m in ('hybrid', 'keyword', 'semantic'))} |",
    "",
    "| Library health | |", "|---|---|",
    f"| Papers | {h.get('papers', 0):,} |",
    f"| Full text in library | {share(h.get('fullText'), h.get('papers'))} |",
    f"| Full text but no searchable text (e.g. scanned PDFs) | {share(h.get('noSearchableText'), h.get('fullText'))} |",
    f"| Missing publication year | {share(h.get('missingYear'), h.get('papers'))} |",
    f"| Missing authors | {share(h.get('missingAuthors'), h.get('papers'))} |",
    f"| Needs Review | {share(h.get('needsReview'), h.get('papers'))} |",
    f"| Same paper twice under one researcher | {h.get('duplicatePapersSameResearcher', 0):,} |",
    f"| Possible duplicate researchers (same name) | {h.get('possibleDuplicateResearchers', 0):,} |",
]
if rep.get("partial"):
    lines.append("\n_The test stopped at its time limit; numbers cover the part that ran._")
summary = "\n".join(lines) + "\n"
if os.environ.get("GITHUB_STEP_SUMMARY"):
    open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(summary)
print(summary)
