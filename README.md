# Infinity Research Library

A private, searchable library of research papers and technical writing, for evaluating and shortlisting candidates.

* **Source of truth: Google Drive.** All papers (the Top 50 archive and the University Research Papers collection,
  about 15,700 papers, Markdown and PDF) stay in the *University Publications* folder on Google Drive, in their
  university and researcher folders. Nothing on Drive is moved, renamed or modified by this project.
* **Website and search: GitHub Pages.** The site, the search interface and a lightweight encrypted index are published
  by a GitHub Actions workflow. Full texts are **not** copied to GitHub. When someone opens a paper, the browser fetches
  that one file from Google Drive with the reader's own Google permissions.
* **Private.** Every data file on the site is encrypted. Readers sign in with Google; the site can only unlock the
  library for Google accounts that can open the *University Publications* folder on Drive.

## What the site does

* Full-collection search over the **complete text** of every paper (Markdown text and PDF text layers), blog post and
  research summary, plus titles, authors and researcher names.
  * **Smart** (default): exact words and documents with a related meaning, ranked together.
  * **Exact words**: all words must appear; `"quoted words"`, `A OR B`, `-exclude`, `word*` (prefix) work.
  * **Meaning**: finds related documents even when they use different words (latent semantic analysis trained on the
    whole collection).
* Tabs: **All sources**, **University Data**, **Personal Blogs from Priority Companies**. Each tab has its own search.
* Filters: source, university, researcher match (Needs Review), fit rating, research area, company, researcher /
  author, venue, content type, file format (Markdown / PDF), availability, year, source group.
* **Needs Review**: papers whose researcher match is uncertain are kept and flagged: OpenAlex matches marked
  "needs review" / "name pending", `UNK-` paper keys, and the Top 50 archive's *Possible misattribution* folders.
* Opening a paper: Markdown is rendered (headings, tables, math as written) with search terms highlighted; PDFs open
  inline. Papers whose full text is not archived show the external-source notice and link to the original.
* Researcher pages (with OpenAlex and Google Drive folder links), browse by university / source.
* **Saved**: shortlist researchers and candidates, bookmark papers and posts, role tags (CEO, COO, CTO, CFO, GTM,
  Research Scientist, Research Engineer, Social Media Writer, plus custom tags), notes, CSV export, optional team sync
  (see *Team sharing* on the Saved page).

## How it works

```
Google Drive  University Publications/                 (master archive, read-only for this project)
  <University>/<Researcher (C… or A5…)>/[subfolder/]Publication NNN - Title.md | .pdf
  Library access key.txt                              (unlocks the encrypted site; see below)
        │  GitHub Actions, every 6 hours / on demand
        ▼
tools/drive_sync.py   crawls the folder tree, downloads only new or changed files (Drive md5), extracts text
                      (pdftotext for PDFs), parses metadata. Cache kept between runs (actions/cache).
tools/build_index.py  builds, from the complete text:
                        manifest, researchers, per-paper metadata + short snippet (no full text)
                        full-text keyword index: 1,024 posting shards (a query downloads only its words' shards)
                        meaning index: word vectors (16 shards) + one vector per document
                      and encrypts every file (gzip + AES-256-GCM).
        ▼
GitHub Pages  index.html + config.json + data/*.enc
        ▼
Browser       sign in with Google → read "Library access key.txt" from Drive → decrypt the index → search;
              open a paper → fetch that file from Drive (alt=media) with the user's token.
```

* New papers added to Drive are picked up by the next scheduled run (or run *Build library* manually from the
  Actions tab). Removed papers disappear from the index.
* Blog posts are not in the Drive archive; their text is kept encrypted in `data/blogs.enc`.

## Deployment (one-time setup)

GitHub Actions signs in to Google with **Workload Identity Federation**: GitHub gives each workflow run a short-lived
identity token, Google exchanges it for temporary credentials of the read-only service account
`infinity-library-reader`, and nothing secret is stored anywhere (no service-account keys, so the
`iam.disableServiceAccountKeyCreation` policy is respected).

1. **Access key in Drive.** Upload `Library access key.txt` (provided separately; do not edit it) into the
   *University Publications* folder.
2. **Service account** `infinity-library-reader` with **Viewer** access to the *University Publications* folder (done).
3. **OAuth Web client** for the website's Google sign-in, its client ID in `config.json` (done). Its authorised
   JavaScript origin must be `https://eloisa-cpu.github.io`.
4. **Google Cloud project of the service account** (permissions needed are listed per step):
   1. Enable these APIs (*Service Usage Admin* or project *Editor/Owner*): IAM API, IAM Service Account Credentials
      API, Security Token Service API, Google Drive API.
   2. Create a workload identity pool `github-actions` with an OIDC provider `github`
      (*Workload Identity Pool Admin* or *Owner*):
      * issuer `https://token.actions.githubusercontent.com`, default audience
      * attribute mapping: `google.subject = assertion.sub`, `attribute.repository = assertion.repository`,
        `attribute.repository_owner = assertion.repository_owner`, `attribute.ref = assertion.ref`
      * attribute condition: `assertion.repository == 'OWNER/REPOSITORY'` (the repository that runs the workflow)
   3. On the service account, grant **Workload Identity User** to
      `principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-actions/attribute.repository/OWNER/REPOSITORY`
      (*Service Account Admin* on that service account, or *Owner*).
   If creating the provider fails with an "allowed identity providers" error, the organisation policy
   `iam.workloadIdentityPoolProviders` must allow `https://token.actions.githubusercontent.com` (an organisation
   policy administrator decides this).
5. **GitHub repository** (*Settings → Secrets and variables → Actions → Variables*; these are not secrets):
   * `GCP_WORKLOAD_IDENTITY_PROVIDER` = `projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-actions/providers/github`
   * `GCP_SERVICE_ACCOUNT` = the service account's e-mail (`infinity-library-reader@PROJECT_ID.iam.gserviceaccount.com`)
   * optional `DRIVE_ROOT_FOLDER_ID` (defaults to the University Publications folder)
6. **Verify**: *Actions → Verify Drive access → Run workflow*. It signs in, confirms the token is read-only, lists the
   archive, finds the access-key file (without showing it) and downloads one paper. It builds nothing.
7. **Enable the build** only after the check passes: add the variable `LIBRARY_BUILD_ENABLED` = `true`, set
   *Settings → Pages → Source: GitHub Actions*, then *Actions → Build library → Run workflow*. The first run downloads
   the whole archive (roughly 30–90 minutes); later runs, every 6 hours, only fetch changes.
8. **Remove old public data.** Earlier versions of this repository published blog texts, ratings and Top 50 papers in
   plain text, and they remain in the git history. Use a new repository (or delete and recreate this one) with only
   the current code. A private repository can serve Pages on GitHub Pro / Team / Enterprise; with the encrypted design
   a public repository is also safe.

Site URL: `https://eloisa-cpu.github.io/<repository>/`

## Meaning search (AI model)

Smart and Meaning searches use an open-source text-embedding model, **bge-small-en-v1.5** (BAAI, MIT licence), to find
papers about the same idea even when they use different words.

- **Documents**: on the build server, each paper's title and abstract (or opening text) is turned into a vector. Vectors
  are cached between builds, so only new or changed papers are processed (first build: roughly 20–30 extra minutes).
- **Searches**: turned into a vector in each person's browser, with the same model, served from this site
  (`models/`, `vendor/transformers/`). Nothing is sent to any outside service. The model and its runtime (about 60 MB)
  download once per browser and are cached.
- **Safety**: the build downloads the model from Hugging Face and the browser runtime (transformers.js) from npm, each
  pinned to an exact version and checked against SHA-256 fingerprints recorded in `tools/models.py`; a mismatch is refused.
  The model file is ONNX (numbers only, no code).
- **Fallback**: if the model cannot load (very old browser) or the build could not prepare it, meaning search uses the
  older word-association (LSA) index and says so. The Search quality report compares both engines at every build.
- To build without the model: set the repository variable or environment `LIBRARY_EMBEDDINGS=0`.

## Search quality and library health

Each build runs the site's own search on the real library (`tools/search_eval.js`, headless browser) and publishes an
encrypted report, visible to signed-in users at **Search quality** (link on the search page, `#/quality`):

- **Known papers**: papers sampled from the library are searched by exact title, by a few distinctive words from their
  opening text ("describe it", in each search mode), and by title in the people view (is their researcher found?).
- **Topics**: recruiter-style searches from `eval/topics.json`, judged automatically (a paper is relevant when its text
  contains enough of the topic's evidence words). Edit that file to add your team's real searches; no code changes needed.
- **Speed**, and **library health**: missing years/authors/venues, papers without searchable text, Needs Review,
  duplicate papers under one researcher, possible duplicate researchers.
- A history of headline numbers across builds, to see whether changes help.

Only headline numbers (no titles or names) go to the public Actions run summary. The measurement never blocks publishing.

## Managing access

* **Grant**: share the *University Publications* Drive folder with the person (Viewer is enough).
* **Revoke**: unshare the folder. They can no longer open papers or the key file. Because they may have seen the key
  before, also **rotate the key**: replace the contents of `Library access key.txt` with new random text, then
  re-encrypt the blog file (`python3 tools/encrypt_blogs.py --rekey --old-keyfile old.txt --keyfile new.txt`), commit
  `data/blogs.enc` and run the workflow. Until the next build, the site cannot be unlocked by anyone.

## Limits

* Quoted words must all appear in the paper, but not necessarily next to each other (the index stores words, not
  positions). Opening the paper highlights the exact matches.
* Result lists show the paper's abstract or opening as the snippet; the matching passage is highlighted when the paper
  is opened.
* Meaning search uses one vector per document (LSA trained on the collection), not a large neural model.
* The site's HTML/JS shell is public (it contains no library data); all data files are encrypted.

## Development and tests

```bash
pip install requests google-auth numpy scipy scikit-learn cryptography   # plus pdftotext (poppler-utils)
```

```bash
node tests/e2e.js
```

The test suite builds a test archive in the same layout as Drive (`tests/make_fixture.py`), serves it through a mock of
the Drive API (`tests/mock_drive.py`), runs the real pipeline and drives the site in Chromium with a stubbed Google
sign-in. It covers access control, encryption of every published file, full-text and PDF search, query syntax, tabs,
meaning search, filters, Needs Review flags, opening Markdown/PDF/external papers from Drive, bookmarks, role tags,
CSV, team sync, and incremental indexing of files added to or removed from Drive.
`REAL_ARCHIVE=<folder>` adds a real archive copy for a scale run.
