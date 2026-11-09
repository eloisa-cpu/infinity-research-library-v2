#!/usr/bin/env python3
"""Create a test archive laid out exactly like the Google Drive research archive (for tests/e2e.js).

  <out>/Library access key.txt
  <out>/Collection Logs and Records/Zip inventory.csv          (must be ignored)
  <out>/<University>/<Name (C#####)>/Publication NNN - Title.md            Top 50 format
  <out>/<University>/<Name (C#####)>/_Possible misattribution (review)/... flagged
  <out>/<University>/<Name (A5…)>/Publication NNN - Title.md               Run2/Run3 format
  <out>/<University>/<Name (A5…)>/Publication NNN - Title.pdf              PDF only

--real DIR additionally copies a real archive tree (same layout) into the fixture, for scale testing.
"""
import argparse, os, shutil

ap = argparse.ArgumentParser()
ap.add_argument("out")
ap.add_argument("--real", help="real archive folder to merge in")
ap.add_argument("--key", default="test-access-key-0123456789")
args = ap.parse_args()
out = args.out
if os.path.isdir(out):
    shutil.rmtree(out)
os.makedirs(out)
open(os.path.join(out, "Library access key.txt"), "w").write(args.key + "\n")
os.makedirs(os.path.join(out, "Collection Logs and Records"))
open(os.path.join(out, "Collection Logs and Records", "Zip inventory.csv"), "w").write("Zip file,Status\nx.zip,ok\n")
os.makedirs(os.path.join(out, "_pipeline")); open(os.path.join(out, "_pipeline", "build.py"), "w").write("print(1)\n")

TOPICS = {
    "kernels": "We present a GPU kernel for attention that fuses softmax and matrix multiplication in shared memory, using CUDA warp-level primitives and tensor cores to reach high throughput on large language model inference.",
    "compiler": "This paper describes an optimizing tensor compiler that lowers deep learning graphs through MLIR dialects, applies loop tiling and vectorization, and generates code for CPUs and accelerators.",
    "biology": "We profile the gut microbiome of patients in a randomized placebo-controlled trial and measure changes in body mass index, social communication and fine motor function after probiotic supplementation.",
    "physics": "We report measurements of proton-proton collisions at the Large Hadron Collider, including precision luminosity calibration and evidence for a rare Higgs boson decay channel.",
    "economics": "Using administrative data on household income and urban features we estimate the effect of nighttime lights on poverty, depression and obesity across regions.",
}


def top50(univ, name, cid, n, title, topic, status="complete", sub="", attribution="Consistent - lists the university"):
    d = os.path.join(out, univ, f"{name} ({cid})", sub)
    os.makedirs(d, exist_ok=True)
    body = (f"**{title}**\n\n#### Abstract\n\n{TOPICS[topic]}\n\n## 1 Introduction\n\n{TOPICS[topic]} " * 3) if status != "unavailable" else \
        "**Unavailable.** The complete text could not be retrieved from any open source. No summary or abstract has been substituted. Source link: https://example.org/paywalled"
    md = f"""# {title}

## Archive metadata

- **Candidate:** {name} ({cid})
- **University:** {univ}
- **Publication title:** {title}
- **Authors:** {name}, Jane Coauthor
- **Publication date:** 2023-05-0{n % 9 + 1}
- **Journal / conference:** Proceedings of Test Systems
- **Work type:** article
- **Original publication URL:** https://example.org/{cid}/{n}
- **DOI:** https://doi.org/10.9999/{cid.lower()}.{n}
- **OpenAlex work:** https://openalex.org/W{n}000
- **Full-text source used:** {"None (full text not accessible)" if status == "unavailable" else "https://arxiv.org/html/0000." + str(n)}
- **Conversion:** {"-" if status == "unavailable" else "arXiv HTML (LaTeXML) -> Markdown"}
- **Archive status:** {status}
- **Attribution (source spreadsheet):** {attribution}

---

## Full publication text

> Extracted verbatim from the source listed above.

{body}
"""
    open(os.path.join(d, f"Publication {n:03d} - {title}.md"), "w").write(md)


def run2(univ, name, aid, n, title, topic, review=False):
    d = os.path.join(out, univ, f"{name} ({aid})")
    os.makedirs(d, exist_ok=True)
    md = f"""# {title}

- **Candidate:** {name}
- **University:** {univ}
- **Title:** {title}
- **Authors:** A. Author; {name}; B. Author
- **Date:** 2021-06-11
- **Venue:** Journal of Testing
- **Original URL:** https://doi.org/10.1007/test-{n}
- **DOI:** 10.1007/test-{n}
- **Full-text source:** arXiv HTML / ar5iv / Europe PMC full text
- **Conversion method:** HTML/XML full text converted to Markdown
- **Archive status:** Full text
- **OpenAlex work:** W31723{n}  |  **Paper key:** {"UNK" if review else "HAR"}-W31723{n}
- **Archived:** 2026-11-08 (University Research Papers, open-access copies only)

---

# {title}

Paper Key: HAR-W31723{n}
Researcher: {"name pending (see mapping), " + univ + " (needs-review OpenAlex match)" if review else name}

## Abstract

{TOPICS[topic]}

### Introduction

{TOPICS[topic]} {TOPICS[topic]}

| Model | Speed-up |
|---|---|
| A | 1.5x |
"""
    open(os.path.join(d, f"Publication {n:03d} - {title}.md"), "w").write(md)


def pdf(univ, name, aid, n, title, lines):
    d = os.path.join(out, univ, f"{name} ({aid})")
    os.makedirs(d, exist_ok=True)
    content = "BT /F1 12 Tf 72 720 Td " + " ".join(f"({l}) Tj 0 -16 Td" for l in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    pdfb, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(pdfb)); pdfb += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(pdfb)
    pdfb += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    pdfb += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    open(os.path.join(d, f"Publication {n:03d} - {title}.pdf"), "wb").write(pdfb)


MIT, STAN, HARV, CAL = "Massachusetts Institute of Technology", "Stanford University", "Harvard University", "California Institute of Technology"
top50(MIT, "Ada Kernelton", "C90001", 1, "Fused Attention Kernels on Tensor Cores", "kernels")
top50(MIT, "Ada Kernelton", "C90001", 2, "A Tensor Compiler for Accelerators", "compiler")
top50(MIT, "Ada Kernelton", "C90001", 3, "Paywalled Systems Paper", "kernels", status="unavailable")
top50(MIT, "Ada Kernelton", "C90001", 4, "Thermal Emittance in Photoinjectors", "physics", sub="_Possible misattribution (review)",
      attribution="Possible misattribution - field 'Physics' outside person's fields")
top50(STAN, "Ben Nolabs", "C90002", 1, "Scheduling Inference Requests", "kernels")
os.makedirs(os.path.join(out, STAN, "Cara Nopapers (C90003)"))                  # researcher with no publications
run2(HARV, "Siyu Testliu", "A5000000001", 2, "Probiotic Supplementation Trial", "biology", review=True)
run2(HARV, "Dan Collider", "A5000000002", 1, "Precision Luminosity at the LHC", "physics")
run2(CAL, "Eve Econ", "A5000000003", 1, "Nighttime Lights and Household Poverty", "economics")
run2(STAN, "Ben Nolabs", "C90002", 7, "Dataflow Compiler Passes", "compiler")
pdf(CAL, "Eve Econ", "A5000000003", 2, "How to be reasonable about ought", ["Deontic modals and the semantics of ought", "This quinoxaline paper is only available as a PDF."])
open(os.path.join(out, CAL, "Eve Econ (A5000000003)", "notes.txt"), "w").write("not a publication\n")

if args.real:
    for univ in os.listdir(args.real):
        src = os.path.join(args.real, univ)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(out, univ), dirs_exist_ok=True)
print("fixture written to", out)
