#!/usr/bin/env python3
"""pdf2epub - Convert PDF to clean, reflowable EPUB."""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import fitz  # PyMuPDF
from ebooklib import epub


FOOTNOTE_MARKER_RE = re.compile(r'^(\d+|[*†‡§¶])[.\)]\s+(.+)$', re.DOTALL)

PAGE_NUMBER_PATTERNS = [
    re.compile(r'^\d+$'),                          # Plain number: 42
    re.compile(r'^[-–—]\s*\d+\s*[-–—]$'),         # Dashes: - 42 -
    re.compile(r'^[ivxlcdmIVXLCDM]+$'),            # Roman numerals: xii
    re.compile(r'^page\s+\d+(\s+of\s+\d+)?$', re.IGNORECASE),  # Page 42 / Page 42 of 100
]

# PDF metadata titles that are clearly wrong (Word artifact, section name, etc.)
JUNK_TITLES = {
    "preface", "introduction", "contents", "table of contents",
    "foreword", "prologue", "epilogue", "index", "appendix",
    "chapter 1", "microsoft word", "untitled", "unknown", "document",
}


def is_page_number(text):
    t = text.strip()
    return any(p.fullmatch(t) for p in PAGE_NUMBER_PATTERNS)


def title_from_filename(path):
    """Derive a human-readable title from the filename as a last resort."""
    name = path.stem
    # Strip common trailing noise: _copy, _v2, _final, _draft, trailing numbers
    name = re.sub(r'[_\-](copy|final|v\d+|draft|\d+)$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'[_\-]+', ' ', name)
    return name.strip().title()


def score_title_page(page):
    """
    Return a score indicating how likely this page is a title page.
    Higher is better. Returns (score, spans) so we don't parse twice.
    """
    spans = []
    block_count = 0

    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        block_count += 1
        for line in block["lines"]:
            for span in line["spans"]:
                text = span["text"].strip()
                if text:
                    spans.append((span["size"], text))

    if not spans:
        return -1, []

    max_size = max(s for s, _ in spans)
    total_words = sum(len(t.split()) for _, t in spans)
    longest_block_words = max(
        (len(" ".join(s["text"] for ln in b["lines"] for s in ln["spans"]).split())
         for b in page.get_text("dict")["blocks"] if b["type"] == 0),
        default=0,
    )

    score = 0

    # Large font strongly suggests a title page
    if max_size >= 20:
        score += 40
    elif max_size >= 16:
        score += 20

    # Few blocks = sparse layout = title page
    if block_count <= 3:
        score += 30
    elif block_count <= 6:
        score += 15

    # Low word count
    if total_words <= 30:
        score += 20
    elif total_words <= 60:
        score += 10

    # No long paragraphs (body text has 50+ word blocks)
    if longest_block_words <= 20:
        score += 10

    return score, spans


def find_title_page(doc, max_pages=10):
    """Return the page index most likely to be the title page."""
    best_idx, best_score, best_spans = 0, -1, []

    for i in range(min(max_pages, len(doc))):
        score, spans = score_title_page(doc[i])
        if score > best_score:
            best_score, best_idx, best_spans = score, i, spans

    return best_idx, best_spans


def metadata_from_title_page(doc):
    """Detect title and author from the most likely title page."""
    page_idx, spans = find_title_page(doc)

    if not spans:
        return "", "", 0

    max_size = max(s for s, _ in spans)

    # Only proceed if there's actually a large-font title
    if max_size < 16:
        return "", "", page_idx

    title_parts = [t for size, t in spans if abs(size - max_size) < 1]
    candidate_title = re.sub(r'\s+', ' ', " ".join(title_parts)).strip()

    if len(candidate_title) < 3 or candidate_title.lower() in JUNK_TITLES:
        return "", "", page_idx

    # Author: next largest distinct font size that looks like a name
    author = ""
    sizes = sorted({s for s, _ in spans}, reverse=True)
    for size in sizes[1:]:
        author_parts = [t for s, t in spans if abs(s - size) < 1]
        candidate_author = re.sub(r'^[Bb]y\s+', '', " ".join(author_parts)).strip()
        words = candidate_author.split()
        if 1 <= len(words) <= 6 and any(w[0].isupper() for w in words if w):
            author = candidate_author
            break

    return candidate_title, author, page_idx


def looks_like_title_fragment(author_candidate):
    """
    Return True if the 'author' looks like it might actually be part of the
    title (e.g. 'DICK' when title is 'MOBY') — single word, no spaces.
    """
    words = author_candidate.strip().split()
    return len(words) == 1


def normalize_for_search(text):
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def word_overlap_score(candidate, reference):
    """Fraction of candidate words found in reference words."""
    c_words = set(normalize_for_search(candidate).split())
    r_words = set(normalize_for_search(reference).split())
    if not c_words:
        return 0.0
    return len(c_words & r_words) / len(c_words)


def _ol_search(query, timeout=8):
    """Execute a single Open Library search and return docs list."""
    params = urllib.parse.urlencode({
        "q": query,
        "limit": 3,
        "fields": "title,author_name",
    })
    url = f"https://openlibrary.org/search.json?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "pdf2epub/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read()).get("docs", [])


def query_open_library(title_candidate, author_candidate="", alt_title=""):
    """
    Search Open Library for canonical title and author.
    Tries multiple query strategies and returns the first confident match.
    Returns (title, author, source) or ("", "", "") if no confident match.
    """
    # Build a prioritised list of queries to try
    queries = []

    # If author looks like a title fragment, fold it in
    if author_candidate and looks_like_title_fragment(author_candidate):
        queries.append(f"{title_candidate} {author_candidate}")

    queries.append(title_candidate)

    # Also try the alternate title (e.g. PDF metadata) if it's more informative
    if alt_title and alt_title.lower() != title_candidate.lower():
        queries.append(alt_title)

    for query in queries:
        if not query.strip():
            continue
        try:
            docs = _ol_search(query)
        except Exception:
            continue

        for doc in docs:
            ol_title = doc.get("title", "")
            ol_authors = doc.get("author_name", [])
            ol_author = ol_authors[0] if ol_authors else ""

            if word_overlap_score(query, ol_title) >= 0.5:
                return ol_title, ol_author, "Open Library"

    return "", "", ""


def get_metadata(doc, path, title_override=None, author_override=None, offline=False):
    # 1. Find the title page and extract title/author from it
    title, author, title_page_idx = metadata_from_title_page(doc)

    # 2. Fill gaps from PDF metadata, rejecting known junk values
    if not title:
        meta_title = doc.metadata.get("title", "").strip()
        if meta_title and meta_title.lower() not in JUNK_TITLES:
            title = meta_title

    if not author:
        meta_author = doc.metadata.get("author", "").strip()
        if meta_author and meta_author.lower() not in ("", "unknown"):
            author = meta_author

    # 3. Fall back to filename for title if still empty
    if not title:
        title = title_from_filename(path)

    # 4. Verify and improve with Open Library
    # Pass pdf_meta_title as an alt query in case title page detection got a fragment
    pdf_meta_title = doc.metadata.get("title", "").strip().rstrip(";:,").strip()
    metadata_source = "local detection"
    if not offline and title:
        ol_title, ol_author, ol_source = query_open_library(title, author, alt_title=pdf_meta_title)
        if ol_title:
            title = ol_title
            if ol_author:
                author = ol_author
            metadata_source = ol_source

    # 5. Apply user overrides last
    if title_override:
        title = title_override
        metadata_source = "override"
    if author_override:
        author = author_override
        metadata_source = "override"

    return title, author, title_page_idx, metadata_source


def detect_body_font_size(doc):
    """Find the most common font size by character count — that's body text."""
    size_chars = {}
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    size = round(span["size"])
                    size_chars[size] = size_chars.get(size, 0) + len(span["text"])
    return max(size_chars, key=size_chars.get) if size_chars else 12


def classify_block(block_dict, body_size):
    """
    Return 'h1', 'h2', or 'p' for a block.

    Heading rules:
    - Must be short (<=12 words) — long blocks are always body text
    - h1: font notably larger than body (>=140% of body size)
    - h2: font moderately larger (>=115%) OR every span in the block is bold
          (whole-block bold = intentional heading, not inline emphasis)
    """
    spans = [s for line in block_dict["lines"] for s in line["spans"]]
    if not spans:
        return "p"

    text = " ".join(s["text"] for s in spans).strip()
    word_count = len(text.split())

    if word_count == 0 or word_count > 12:
        return "p"

    max_size = max(s["size"] for s in spans)
    all_bold = all(s["flags"] & 16 for s in spans if s["text"].strip())

    if max_size >= body_size * 1.4:
        return "h1"
    if max_size >= body_size * 1.15 or all_bold:
        return "h2"

    return "p"


def is_footnote_block(block, page_height, body_size):
    """
    Return True if this block looks like a footnote:
    - Located in the bottom 20% of the page
    - Smaller font than body text
    - Text starts with a numbered or symbolic marker (e.g. '1. ', '* ')
    """
    if block["bbox"][1] < page_height * 0.80:
        return False
    spans = [s for line in block["lines"] for s in line["spans"]]
    if not spans:
        return False
    avg_size = sum(s["size"] for s in spans) / len(spans)
    if avg_size >= body_size * 0.88:
        return False
    text = " ".join(s["text"] for s in spans).strip()
    return bool(FOOTNOTE_MARKER_RE.match(text))


def is_footnote_continuation(block, page_height, body_size):
    """
    Return True if this block is a continuation of a footnote (bottom of page,
    small font, but no leading marker).
    """
    if block["bbox"][1] < page_height * 0.80:
        return False
    spans = [s for line in block["lines"] for s in line["spans"]]
    if not spans:
        return False
    avg_size = sum(s["size"] for s in spans) / len(spans)
    return avg_size < body_size * 0.88


def parse_footnote_block(block):
    """
    Extract (marker, text) from a footnote block.
    Returns None if the block doesn't match the expected pattern.
    """
    text = " ".join(
        s["text"] for line in block["lines"] for s in line["spans"]
    ).strip()
    m = FOOTNOTE_MARKER_RE.match(text)
    if not m:
        return None
    return m.group(1), m.group(2).strip()


def get_pdf_outline(doc):
    """
    Return the PDF's built-in bookmark/outline as a list of
    {"level": int, "title": str, "page": int (0-indexed)} dicts.
    Returns empty list if the PDF has no outline.
    """
    return [
        {"level": level, "title": title.strip(), "page": max(0, page - 1)}
        for level, title, page in doc.get_toc()
        if title.strip()
    ]


def extract_content(doc, skip_pages=None):
    """
    Extract structured content (headings + paragraphs) from the PDF.

    Chapter detection strategy:
    - If the PDF has a built-in outline (bookmarks), use it as the authoritative
      source for h1/h2 headings. Inject outline titles at their start pages and
      suppress duplicate font-detected headings on those pages.
    - If no outline exists, fall back to font-size heuristics for all headings.
    """
    skip_pages = skip_pages or set()
    body_size = detect_body_font_size(doc)
    outline = get_pdf_outline(doc)

    # Build page -> outline entry lookup (one entry per page, most prominent level)
    outline_by_page = {}
    for entry in outline:
        page = entry["page"]
        if page not in outline_by_page or entry["level"] < outline_by_page[page]["level"]:
            outline_by_page[page] = entry

    has_outline = bool(outline)
    content = []
    footnotes = []  # list of {"marker": str, "text": str}

    for page_num, page in enumerate(doc):
        if page_num in skip_pages:
            continue

        page_height = page.rect.height

        # --- First pass: split blocks into footnotes vs body ---
        fn_blocks = []
        body_blocks = []
        last_was_fn = False

        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            raw = " ".join(s["text"] for line in block["lines"] for s in line["spans"]).strip()
            if not raw or is_page_number(raw):
                continue
            if is_footnote_block(block, page_height, body_size):
                fn_blocks.append(block)
                last_was_fn = True
            elif last_was_fn and is_footnote_continuation(block, page_height, body_size):
                fn_blocks.append(block)
            else:
                body_blocks.append(block)
                last_was_fn = False

        # Parse footnotes and build marker set for this page
        page_fn_markers = set()
        for fn_block in fn_blocks:
            parsed = parse_footnote_block(fn_block)
            if parsed:
                marker, text = parsed
                footnotes.append({"marker": marker, "text": text})
                page_fn_markers.add(marker)
            elif footnotes:
                # Continuation: append to the last footnote's text
                extra = " ".join(
                    s["text"] for line in fn_block["lines"] for s in line["spans"]
                ).strip()
                if extra:
                    footnotes[-1]["text"] += " " + extra

        # --- Second pass: extract body content ---
        if page_num in outline_by_page:
            entry = outline_by_page[page_num]
            block_type = "h1" if entry["level"] == 1 else "h2"
            content.append({"type": block_type, "text": entry["title"]})

        for block in body_blocks:
            raw_text = " ".join(
                s["text"] for line in block["lines"] for s in line["spans"]
            ).strip()

            if not raw_text:
                continue

            block_type = classify_block(block, body_size)

            if has_outline:
                if block_type in ("h1", "h2") and page_num in outline_by_page:
                    ol_title = outline_by_page[page_num]["title"].lower()
                    if raw_text.lower() in ol_title or ol_title in raw_text.lower():
                        continue
                if block_type == "h1":
                    block_type = "h2"

            if block_type == "p":
                html = spans_to_html(block, body_size=body_size, fn_markers=page_fn_markers)
                if html:
                    content.append({"type": "p", "text": raw_text, "html": html})
            else:
                text = re.sub(r'\s+', ' ', raw_text)
                if text:
                    content.append({"type": block_type, "text": text})

    return content, has_outline, footnotes


def reconstruct_lines(block_text):
    """Join wrapped lines within a block into a single clean paragraph."""
    lines = block_text.splitlines()
    result = ""

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if not result:
            result = line
        elif result.endswith("-"):
            # Hyphenated word wrap — join without space and drop the hyphen
            result = result[:-1] + line
        elif result[-1] not in ".!?:" and line[0].islower():
            # Mid-sentence line wrap — join with space
            result += " " + line
        else:
            # New sentence on next line — join with space
            result += " " + line

    return result.strip()


def html_escape(text):
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
    )


def render_cover(doc, page_idx=0):
    """Render a PDF page as a cover image (PNG bytes)."""
    page = doc[page_idx]
    mat = fitz.Matrix(2, 2)  # 2x scale for a sharp cover
    pix = page.get_pixmap(matrix=mat)
    return pix.tobytes("png")


def is_bold_span(span):
    return bool(span["flags"] & 16) or "bold" in span["font"].lower()

def is_italic_span(span):
    return bool(span["flags"] & 2) or "italic" in span["font"].lower() or "oblique" in span["font"].lower()


def spans_to_html(block_dict, body_size=None, fn_markers=None):
    """
    Convert a block's spans to an HTML string with inline bold/italic preserved.
    Handles line-break reconstruction (hyphen joins, mid-sentence continuations).
    Superscript spans matching footnote markers are rendered as endnote links.
    """
    lines = block_dict["lines"]

    # Flatten spans, tagging the last span of each non-final line as a line-end
    flat = []
    for i, line in enumerate(lines):
        spans = line["spans"]
        for j, span in enumerate(spans):
            size = span.get("size", 0)
            flags = span.get("flags", 0)
            super_ = (body_size and size < body_size * 0.75) or bool(flags & 1)
            flat.append({
                "text":    span["text"],
                "bold":    is_bold_span(span),
                "italic":  is_italic_span(span),
                "size":    size,
                "flags":   flags,
                "super":   super_,
                "line_end": j == len(spans) - 1 and i < len(lines) - 1,
            })

    # Reconstruct line breaks
    processed = []
    for span in flat:
        if span["line_end"]:
            stripped = span["text"].rstrip()
            if stripped.endswith("-"):
                # Hyphenated wrap — drop hyphen, join directly
                processed.append({**span, "text": stripped[:-1], "line_end": False})
            else:
                # Soft wrap — add a space
                processed.append({**span, "text": stripped + " ", "line_end": False})
        else:
            processed.append(span)

    # Merge adjacent spans with identical formatting
    merged = []
    for span in processed:
        text = span["text"]
        if not text:
            continue
        if merged and merged[-1]["bold"] == span["bold"] and merged[-1]["italic"] == span["italic"] and merged[-1]["super"] == span["super"]:
            merged[-1] = {**merged[-1], "text": merged[-1]["text"] + text}
        else:
            merged.append(dict(span))

    # Render to HTML
    parts = []
    for span in merged:
        t = html_escape(span["text"].strip()) if span["text"].strip() else html_escape(span["text"])
        raw = span["text"].strip()

        # Use pre-computed super field (re-computing from size/flags is wrong after merging)
        is_super = span.get("super", False)

        if is_super and fn_markers and raw in fn_markers:
            # Link to endnote anchor
            t = f'<a href="#fn-{html_escape(raw)}" epub:type="noteref"><sup>{html_escape(raw)}</sup></a>'
        elif is_super:
            t = f"<sup>{t}</sup>"
        elif span["bold"] and span["italic"]:
            t = f"<strong><em>{t}</em></strong>"
        elif span["bold"]:
            t = f"<strong>{t}</strong>"
        elif span["italic"]:
            t = f"<em>{t}</em>"
        parts.append(t)

    return "".join(parts).strip()


HEADING_STYLE = "text-align:center;font-weight:bold;margin:1.5em 0 0.5em 0;"
PARA_STYLE = "text-indent:1.5em;margin:0 0 0.6em 0;"

def render_block(item):
    if item["type"] == "h1":
        return f'<h1 style="{HEADING_STYLE}font-size:1.4em;">{html_escape(item["text"])}</h1>'
    if item["type"] == "h2":
        return f'<h2 style="{HEADING_STYLE}font-size:1.15em;">{html_escape(item["text"])}</h2>'
    # Paragraphs: use pre-rendered HTML (preserves bold/italic) if available
    body = item.get("html") or html_escape(item["text"])
    return f'<p style="{PARA_STYLE}">{body}</p>'


def split_into_chapters(content, book_title):
    """
    Split content blocks into chapters at every h1 boundary.
    Returns a list of {"title": str, "blocks": [...]} dicts.
    Everything before the first h1 becomes a front-matter chapter.
    """
    chapters = []
    current_title = book_title
    current_blocks = []

    for item in content:
        if item["type"] == "h1":
            if current_blocks:
                chapters.append({"title": current_title, "blocks": current_blocks})
            current_title = item["text"]
            current_blocks = [item]  # keep the heading inside its chapter
        else:
            current_blocks.append(item)

    if current_blocks:
        chapters.append({"title": current_title, "blocks": current_blocks})

    return chapters


def make_chapter_html(chapter_title, blocks, book_title):
    body_html = "\n".join(render_block(b) for b in blocks)
    return (f"""<?xml version='1.0' encoding='utf-8'?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>{html_escape(chapter_title)}</title>
  <style>
    body {{
      font-family: serif;
      line-height: 1.6;
      margin: 1.5em;
    }}
  </style>
</head>
<body>
{body_html}
</body>
</html>""").encode("utf-8")


def make_endnotes_html(footnotes):
    items = []
    for fn in footnotes:
        m = html_escape(fn["marker"])
        t = html_escape(fn["text"])
        items.append(
            f'<p id="fn-{m}" style="margin:0 0 0.8em 0;">'
            f'<sup><a href="#fnref-{m}">{m}</a></sup> {t}</p>'
        )
    body = "\n".join(items)
    return (f"""<?xml version='1.0' encoding='utf-8'?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>Notes</title>
  <style>body {{ font-family: serif; line-height: 1.6; margin: 1.5em; }}</style>
</head>
<body>
<h1 style="text-align:center;font-weight:bold;margin:1.5em 0 0.5em 0;">Notes</h1>
{body}
</body>
</html>""").encode("utf-8")


def build_epub(content, title, author, cover_png, output_path, footnotes=None):
    book = epub.EpubBook()
    book.set_title(title or "Untitled")
    book.set_language("en")
    if author:
        book.add_author(author)

    book.set_cover("cover.png", cover_png)

    chapters = split_into_chapters(content, title or "Content")
    epub_chapters = []

    for i, chap in enumerate(chapters):
        epub_chap = epub.EpubHtml(
            title=chap["title"],
            file_name=f"chapter_{i:03d}.xhtml",
            lang="en",
        )
        epub_chap.content = make_chapter_html(chap["title"], chap["blocks"], title)
        book.add_item(epub_chap)
        epub_chapters.append(epub_chap)

    toc_items = [epub.Link(c.file_name, c.title, c.file_name) for c in epub_chapters]

    if footnotes:
        notes_chap = epub.EpubHtml(title="Notes", file_name="endnotes.xhtml", lang="en")
        notes_chap.content = make_endnotes_html(footnotes)
        book.add_item(notes_chap)
        epub_chapters.append(notes_chap)
        toc_items.append(epub.Link("endnotes.xhtml", "Notes", "endnotes"))

    book.toc = toc_items
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["cover", "nav"] + epub_chapters

    epub.write_epub(str(output_path), book)


def is_text_based(doc, sample_pages=5, min_chars_per_page=100):
    """Return True if the PDF has meaningful embedded text."""
    pages_to_check = min(sample_pages, len(doc))
    total_chars = sum(len(doc[i].get_text()) for i in range(pages_to_check))
    return (total_chars / pages_to_check) >= min_chars_per_page


def ocr_pdf(input_path, language=None):
    """
    Run ocrmypdf on a scanned PDF and return a Path to the OCR'd temp file.
    Caller is responsible for deleting it when done.
    language: Tesseract language code(s), e.g. "eng", "fra", "eng+fra".
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)

    lang = language or "eng"
    print(f"  Scanned PDF detected — running OCR (language: {lang}, this may take a minute)...")
    result = subprocess.run(
        ["ocrmypdf", "--force-ocr", "--quiet", "--language", lang, str(input_path), str(tmp_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        print(f"  OCR failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    return tmp_path


def convert_one(input_path, output_path=None, title=None, author=None, offline=False, language=None):
    """Convert a single PDF to EPUB. Returns the output path on success."""
    input_path = Path(input_path)
    output_path = Path(output_path) if output_path else input_path.with_suffix(".epub")

    print(f"Reading {input_path.name}...")
    doc = fitz.open(str(input_path))
    ocr_tmp = None

    if not is_text_based(doc):
        ocr_tmp = ocr_pdf(input_path, language=language)
        doc.close()
        doc = fitz.open(str(ocr_tmp))

    print("Fetching metadata...")
    title_val, author_val, title_page_idx, meta_source = get_metadata(
        doc, input_path, title, author, offline=offline
    )
    print(f"  Title:  {title_val or '(not detected)'}")
    print(f"  Author: {author_val or '(not detected)'}")
    print(f"  Source: {meta_source}")
    print(f"  Title page detected at page {title_page_idx + 1}")

    print("Rendering cover...")
    cover_png = render_cover(doc, title_page_idx)

    print("Extracting and formatting text...")
    content, used_outline, footnotes = extract_content(doc, skip_pages={title_page_idx})
    headings = sum(1 for c in content if c["type"] != "p")
    chapters = sum(1 for c in content if c["type"] == "h1")
    chapter_source = "PDF outline" if used_outline else "font heuristics"
    print(f"  {len(content)} blocks ({chapters} chapters, {headings - chapters} subheadings, {len(content) - headings} paragraphs)")
    print(f"  Chapter detection: {chapter_source}")
    print(f"  Footnotes found: {len(footnotes)}")

    print(f"Writing {output_path.name}...")
    build_epub(content, title_val, author_val, cover_png, output_path, footnotes=footnotes or None)

    print(f"Done -> {output_path}")

    if ocr_tmp:
        ocr_tmp.unlink(missing_ok=True)

    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Convert a PDF to a clean, reflowable EPUB."
    )
    parser.add_argument("input", help="Input PDF file or directory (with --batch)")
    parser.add_argument("-o", "--output", help="Output EPUB path (single file) or directory (batch)")
    parser.add_argument("--title", help="Override detected title (single file only)")
    parser.add_argument("--author", help="Override detected author (single file only)")
    parser.add_argument("--offline", action="store_true", help="Skip Open Library metadata lookup")
    parser.add_argument("--language", default="eng",
                        help="Tesseract language code(s) for OCR, e.g. eng, fra, eng+fra (default: eng)")
    parser.add_argument("--batch", action="store_true",
                        help="Convert all PDFs in a directory; input must be a directory path")
    args = parser.parse_args()

    if args.batch:
        input_dir = Path(args.input)
        if not input_dir.is_dir():
            print(f"Error: '{input_dir}' is not a directory.", file=sys.stderr)
            sys.exit(1)
        pdfs = sorted(input_dir.glob("*.pdf"))
        if not pdfs:
            print(f"No PDF files found in '{input_dir}'.", file=sys.stderr)
            sys.exit(1)
        out_dir = Path(args.output) if args.output else input_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"Batch mode: {len(pdfs)} PDF(s) found in '{input_dir}'")
        failed = []
        for i, pdf in enumerate(pdfs, 1):
            print(f"\n[{i}/{len(pdfs)}] {pdf.name}")
            try:
                convert_one(pdf, output_path=out_dir / pdf.with_suffix(".epub").name,
                            offline=args.offline, language=args.language)
            except Exception as exc:
                print(f"  ERROR: {exc}", file=sys.stderr)
                failed.append(pdf.name)
        print(f"\nBatch complete. {len(pdfs) - len(failed)}/{len(pdfs)} succeeded.")
        if failed:
            print("Failed:", ", ".join(failed), file=sys.stderr)
            sys.exit(1)
    else:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"Error: '{input_path}' not found.", file=sys.stderr)
            sys.exit(1)
        convert_one(input_path, output_path=args.output,
                    title=args.title, author=args.author,
                    offline=args.offline, language=args.language)


if __name__ == "__main__":
    main()
