# pdf2epub

Convert PDFs into clean, reflowable EPUBs ready for e-readers like the Kindle Paperwhite.

Standard PDF-to-EPUB converters often produce static image pages — you can't resize the text or change fonts. `pdf2epub` fixes this by extracting real text, reconstructing paragraphs, detecting chapters and headings, and outputting a properly structured EPUB with a clickable table of contents.

Scanned (image-based) PDFs are handled automatically via OCR.

## Features

- **Reflowable text** — resize fonts and change reading settings on your Kindle
- **Auto OCR** — detects scanned PDFs and runs OCR automatically before converting
- **Smart metadata detection** — finds the title and author from the title page using font size analysis, falls back to PDF metadata and filename
- **Chapter & heading detection** — identifies chapters and section headings by font size and style; generates a clickable table of contents
- **Cover image** — renders the detected title page as the cover
- **Page number stripping** — removes standalone page numbers from the output
- **Paragraph reconstruction** — rejoins lines that were broken mid-sentence by PDF formatting

## Installation

Install Python dependencies:

```bash
pip install pymupdf ebooklib
```

Install `ocrmypdf` for scanned PDF support (Fedora/RHEL):

```bash
sudo dnf install ocrmypdf
```

On Debian/Ubuntu:

```bash
sudo apt install ocrmypdf
```

## Usage

```bash
python3 pdf2epub.py input.pdf
```

Output is saved as `input.epub` in the same directory.

**Options:**

```
python3 pdf2epub.py input.pdf [options]

positional arguments:
  input                 Input PDF file

options:
  -o, --output PATH     Output EPUB path (default: same name as input)
  --title TEXT          Override detected title
  --author TEXT         Override detected author
```

**Examples:**

```bash
# Basic conversion
python3 pdf2epub.py my-book.pdf

# Specify output path
python3 pdf2epub.py my-book.pdf -o ~/Books/my-book.epub

# Override metadata
python3 pdf2epub.py my-book.pdf --title "My Book" --author "Jane Doe"
```

## How It Works

### Text extraction
Uses [PyMuPDF](https://pymupdf.readthedocs.io/) to extract text blocks directly from the PDF. Each block maps to a paragraph. Lines broken mid-sentence by PDF formatting are rejoined using punctuation and capitalization heuristics.

### OCR
If the PDF has fewer than 100 characters per page on average (i.e. it's a scanned image), [ocrmypdf](https://ocrmypdf.readthedocs.io/) is invoked automatically to add a text layer before extraction.

### Metadata detection
The script scans the first 10 pages and scores each one for title page likelihood based on:
- Presence of large font text (>16pt)
- Low block count (sparse layout)
- Low total word count
- No long paragraphs

The page with the highest score is used as the title page. The largest font text becomes the title, and the next largest becomes the author. Falls back to PDF metadata, then the filename.

### Heading detection
The most common font size across the document (by character count) is treated as the body size. Blocks are classified as:
- `h1` — font ≥ 140% of body size, ≤ 12 words → chapter title
- `h2` — font ≥ 115% of body size, or all spans bold, ≤ 12 words → section heading
- `p` — everything else

Each `h1` boundary creates a new chapter file in the EPUB, which populates the table of contents.

## Caveats

- OCR quality depends on scan quality. Clean 300dpi scans work well; low-resolution or skewed scans may have errors.
- Heading detection is heuristic-based. Books that use the same font size for headings and body text (relying only on bold) may produce false positives or miss headings.
- Complex layouts (columns, callout boxes, footnotes) may not reconstruct perfectly.

## Dependencies

| Package | Purpose |
|---|---|
| [PyMuPDF](https://pymupdf.readthedocs.io/) | PDF parsing, text extraction, page rendering |
| [ebooklib](https://github.com/aerkalov/ebooklib) | EPUB generation |
| [ocrmypdf](https://ocrmypdf.readthedocs.io/) | OCR for scanned PDFs (optional, must be installed separately) |
