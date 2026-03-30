# Milestones

## In Progress

## Backlog

- [x] **Open Library API metadata lookup** — query openlibrary.org with the detected title candidate to get canonical title, author, and other metadata. Fall back to local detection if no confident match.
  - Tries multiple query strategies (title+author fragment, title alone, PDF metadata as alt)
  - `--offline` flag to skip the lookup entirely
- [ ] **PDF outline/bookmark-based chapter detection** — use `doc.get_toc()` to read the PDF's built-in bookmark tree for chapter titles and page numbers. Fall back to font heuristics only if no outline exists.
- [ ] **Inline bold/italic preservation** — detect bold/italic spans within body paragraphs and wrap them in `<strong>`/`<em>` rather than stripping formatting.
- [ ] **Footnote → endnote conversion** — detect floating footnote blocks at page bottoms, match them to their in-text references, and reformat as endnotes in the EPUB.
- [ ] **Multi-language OCR** — auto-detect document language and pass it to Tesseract for more accurate OCR on non-English scans.
- [ ] **Batch mode** — accept a directory or glob pattern and convert multiple PDFs in one run.

## Completed

- [x] **Core PDF → EPUB pipeline** — extract text blocks, reconstruct paragraphs, strip page numbers, output reflowable EPUB.
- [x] **Auto OCR fallback** — detect image-based PDFs and run ocrmypdf automatically before extraction.
- [x] **Title page detection** — score first 10 pages by font size, block count, and word count to find the title page; extract title and author from largest fonts.
- [x] **Metadata fallback chain** — title page → PDF metadata → filename.
- [x] **Cover image** — render detected title page at 2x resolution as EPUB cover.
- [x] **Heading detection** — classify blocks as h1/h2/p using font size relative to body, bold flag, and word count. Center and bold headings in output.
- [x] **Chapter splitting + TOC** — split EPUB at every h1 boundary into separate chapter files; generate clickable NCX/Nav table of contents.
- [x] **Kindle-safe inline styles** — apply formatting via inline styles so Kindle cannot override them.
