"""
Unit tests for pdf2epub.py

Run with:  pytest test_pdf2epub.py -v
"""

import pytest
from pdf2epub import (
    is_page_number,
    title_from_filename,
    classify_block,
    is_footnote_block,
    is_footnote_continuation,
    parse_footnote_block,
    spans_to_html,
    html_escape,
    reconstruct_lines,
    normalize_for_search,
    word_overlap_score,
    looks_like_title_fragment,
    JUNK_TITLES,
)
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers for building fake PyMuPDF-style data structures
# ---------------------------------------------------------------------------

def make_span(text, size=12, flags=0, font="Helvetica"):
    """Build a minimal span dict that mirrors what PyMuPDF returns."""
    return {"text": text, "size": size, "flags": flags, "font": font}


def make_block(spans_per_line, size=12, flags=0, font="Helvetica", bbox=(0, 0, 400, 20)):
    """
    Build a block dict.

    spans_per_line: list of lists of strings, one inner list per line.
      e.g. [["Hello ", "world"], ["Next line"]]
    Each string becomes one span. All spans share the given size/flags/font
    unless you pass a list of span dicts directly.
    """
    lines = []
    for line_texts in spans_per_line:
        spans = []
        for item in line_texts:
            if isinstance(item, dict):
                spans.append(item)
            else:
                spans.append(make_span(item, size=size, flags=flags, font=font))
        lines.append({"spans": spans})
    return {"lines": lines, "bbox": bbox}


def make_fn_block(text, size=9, bbox=(0, 850, 400, 870), page_height=1000):
    """Convenience: a block that looks like a footnote at the bottom of the page."""
    return make_block([[text]], size=size, bbox=bbox)


# ---------------------------------------------------------------------------
# is_page_number
# ---------------------------------------------------------------------------

class TestIsPageNumber:
    def test_plain_integer(self):
        assert is_page_number("42")

    def test_plain_integer_with_whitespace(self):
        assert is_page_number("  42  ")

    def test_dashes_around_number(self):
        assert is_page_number("- 42 -")
        assert is_page_number("— 42 —")
        assert is_page_number("– 42 –")

    def test_roman_numerals_lowercase(self):
        assert is_page_number("xii")
        assert is_page_number("iv")
        assert is_page_number("xc")

    def test_roman_numerals_uppercase(self):
        assert is_page_number("XIV")
        assert is_page_number("II")

    def test_page_word(self):
        assert is_page_number("Page 42")
        assert is_page_number("page 42")
        assert is_page_number("Page 42 of 100")

    def test_normal_sentence_not_page_number(self):
        assert not is_page_number("Hello world")

    def test_empty_string(self):
        assert not is_page_number("")

    def test_number_within_sentence(self):
        assert not is_page_number("The 42 foxes")

    def test_decimal_not_page_number(self):
        assert not is_page_number("3.14")

    def test_single_letter_not_page_number(self):
        # A lone letter that happens to be a valid roman numeral char
        assert not is_page_number("a")


# ---------------------------------------------------------------------------
# title_from_filename
# ---------------------------------------------------------------------------

class TestTitleFromFilename:
    def test_simple_hyphenated(self):
        assert title_from_filename(Path("moby-dick.pdf")) == "Moby Dick"

    def test_underscores(self):
        assert title_from_filename(Path("great_expectations.pdf")) == "Great Expectations"

    def test_strips_final_copy(self):
        result = title_from_filename(Path("my-book_copy.pdf"))
        assert "copy" not in result.lower()

    def test_strips_final_version(self):
        result = title_from_filename(Path("my-book_v2.pdf"))
        assert "v2" not in result.lower()

    def test_strips_final_draft(self):
        result = title_from_filename(Path("my-book_draft.pdf"))
        assert "draft" not in result.lower()

    def test_strips_final_number(self):
        result = title_from_filename(Path("book-2.pdf"))
        # Trailing number stripped
        assert result == "Book"

    def test_title_case(self):
        result = title_from_filename(Path("war-and-peace.pdf"))
        assert result == "War And Peace"

    def test_no_extension_noise(self):
        # Should not include ".pdf"
        result = title_from_filename(Path("some-book.pdf"))
        assert ".pdf" not in result


# ---------------------------------------------------------------------------
# classify_block
# ---------------------------------------------------------------------------

class TestClassifyBlock:
    """
    Body size = 12pt throughout.
    h1 threshold: >= 16.8pt  (12 * 1.4)
    h2 threshold: >= 13.8pt  (12 * 1.15), OR all spans bold
    """

    BODY = 12

    def _block(self, text, size, flags=0):
        words = text.split()
        return make_block([[text]], size=size, flags=flags)

    # --- h1 cases ---

    def test_large_font_short_text_is_h1(self):
        block = self._block("Chapter One", size=18)
        assert classify_block(block, self.BODY) == "h1"

    def test_exactly_at_h1_threshold_is_h1(self):
        block = self._block("Title", size=self.BODY * 1.4)
        assert classify_block(block, self.BODY) == "h1"

    # --- h2 cases ---

    def test_medium_font_short_text_is_h2(self):
        block = self._block("Section One", size=14)
        assert classify_block(block, self.BODY) == "h2"

    def test_exactly_at_h2_threshold_is_h2(self):
        block = self._block("A Subsection", size=self.BODY * 1.15)
        assert classify_block(block, self.BODY) == "h2"

    def test_bold_body_size_short_is_h2(self):
        # All spans bold at body size = h2 (intentional heading)
        block = self._block("Bold Heading", size=self.BODY, flags=16)
        assert classify_block(block, self.BODY) == "h2"

    def test_below_h2_threshold_not_bold_is_p(self):
        block = self._block("Small heading?", size=13)
        # 13 < 13.8 and not bold
        assert classify_block(block, self.BODY) == "p"

    # --- paragraph cases ---

    def test_body_size_not_bold_is_p(self):
        block = self._block("This is a normal paragraph sentence.", size=12)
        assert classify_block(block, self.BODY) == "p"

    def test_long_text_always_p_even_if_large_font(self):
        # > 12 words forces paragraph classification
        long_text = "one two three four five six seven eight nine ten eleven twelve thirteen"
        block = self._block(long_text, size=20)
        assert classify_block(block, self.BODY) == "p"

    def test_exactly_12_words_can_be_heading(self):
        text = "one two three four five six seven eight nine ten eleven twelve"
        block = self._block(text, size=18)
        assert classify_block(block, self.BODY) == "h1"

    def test_empty_block_is_p(self):
        block = make_block([[""]])
        assert classify_block(block, self.BODY) == "p"

    def test_no_spans_is_p(self):
        block = {"lines": [], "bbox": (0, 0, 400, 20)}
        assert classify_block(block, self.BODY) == "p"


# ---------------------------------------------------------------------------
# is_footnote_block
# ---------------------------------------------------------------------------

class TestIsFootnoteBlock:
    """page_height=1000, body_size=12. Bottom 20% starts at y=800."""

    PAGE_H = 1000
    BODY = 12

    def _block(self, text, y_top, size=9):
        bbox = (0, y_top, 400, y_top + 20)
        return make_block([[text]], size=size, bbox=bbox)

    def test_footnote_at_bottom(self):
        block = self._block("1. This is a footnote text.", y_top=850)
        assert is_footnote_block(block, self.PAGE_H, self.BODY)

    def test_asterisk_marker(self):
        block = self._block("*. A footnote with an asterisk.", y_top=860)
        assert is_footnote_block(block, self.PAGE_H, self.BODY)

    def test_dagger_marker(self):
        block = self._block("†. Dagger footnote.", y_top=870)
        assert is_footnote_block(block, self.PAGE_H, self.BODY)

    def test_not_footnote_too_high_on_page(self):
        # y=500 is middle of page, not bottom 20%
        block = self._block("1. This is not a footnote.", y_top=500)
        assert not is_footnote_block(block, self.PAGE_H, self.BODY)

    def test_not_footnote_font_too_large(self):
        # Font >= 88% of body size means it's body text, not a footnote
        block = self._block("1. Large font footnote?", y_top=850, size=11)
        assert not is_footnote_block(block, self.PAGE_H, self.BODY)

    def test_not_footnote_no_marker(self):
        # Bottom of page, small font, but no numeric/symbol marker
        block = self._block("Continued from previous page.", y_top=850)
        assert not is_footnote_block(block, self.PAGE_H, self.BODY)

    def test_not_footnote_empty_block(self):
        block = self._block("", y_top=850)
        assert not is_footnote_block(block, self.PAGE_H, self.BODY)

    def test_exactly_at_80_percent_boundary(self):
        # y_top = 800 is exactly at the boundary (not below it)
        block = self._block("1. Right at boundary.", y_top=800)
        # bbox[1] (800) is NOT < page_height * 0.80 (800), so it qualifies
        assert is_footnote_block(block, self.PAGE_H, self.BODY)

    def test_one_pixel_above_boundary(self):
        block = self._block("1. Just above boundary.", y_top=799)
        # bbox[1] (799) IS < 800, so it does NOT qualify as footnote
        assert not is_footnote_block(block, self.PAGE_H, self.BODY)


# ---------------------------------------------------------------------------
# is_footnote_continuation
# ---------------------------------------------------------------------------

class TestIsFootnoteContinuation:
    PAGE_H = 1000
    BODY = 12

    def _block(self, text, y_top, size=9):
        bbox = (0, y_top, 400, y_top + 20)
        return make_block([[text]], size=size, bbox=bbox)

    def test_continuation_at_bottom_small_font(self):
        block = self._block("continued text without a marker", y_top=870)
        assert is_footnote_continuation(block, self.PAGE_H, self.BODY)

    def test_not_continuation_in_middle_of_page(self):
        block = self._block("body text", y_top=400)
        assert not is_footnote_continuation(block, self.PAGE_H, self.BODY)

    def test_not_continuation_large_font_at_bottom(self):
        block = self._block("large text at bottom", y_top=870, size=12)
        assert not is_footnote_continuation(block, self.PAGE_H, self.BODY)

    def test_continuation_even_with_marker_text(self):
        # A block at bottom with small font qualifies even if it looks like a marker
        # (the caller decides whether it's a new footnote or continuation)
        block = self._block("1. Could be either", y_top=870)
        assert is_footnote_continuation(block, self.PAGE_H, self.BODY)


# ---------------------------------------------------------------------------
# parse_footnote_block
# ---------------------------------------------------------------------------

class TestParseFootnoteBlock:

    def _block(self, text):
        return make_block([[text]])

    def test_numeric_period_marker(self):
        result = parse_footnote_block(self._block("1. Watson and Crick, 1953."))
        assert result == ("1", "Watson and Crick, 1953.")

    def test_numeric_paren_marker(self):
        result = parse_footnote_block(self._block("2) Second footnote here."))
        assert result == ("2", "Second footnote here.")

    def test_asterisk_marker(self):
        result = parse_footnote_block(self._block("*. An asterisk footnote."))
        assert result == ("*", "An asterisk footnote.")

    def test_dagger_marker(self):
        result = parse_footnote_block(self._block("†. A dagger footnote."))
        assert result == ("†", "A dagger footnote.")

    def test_no_marker_returns_none(self):
        result = parse_footnote_block(self._block("Just some text without a marker."))
        assert result is None

    def test_empty_text_returns_none(self):
        result = parse_footnote_block(self._block(""))
        assert result is None

    def test_multiword_text_preserved(self):
        marker, text = parse_footnote_block(
            self._block("3. See Alberts et al., Molecular Biology of the Cell, 6th ed., 2014.")
        )
        assert marker == "3"
        assert "Alberts" in text

    def test_marker_only_no_text_returns_none(self):
        # "1. " with nothing after — regex requires at least one char after marker
        result = parse_footnote_block(self._block("1. "))
        assert result is None


# ---------------------------------------------------------------------------
# html_escape
# ---------------------------------------------------------------------------

class TestHtmlEscape:
    def test_ampersand(self):
        assert html_escape("a & b") == "a &amp; b"

    def test_less_than(self):
        assert html_escape("a < b") == "a &lt; b"

    def test_greater_than(self):
        assert html_escape("a > b") == "a &gt; b"

    def test_double_quote(self):
        assert html_escape('say "hi"') == "say &quot;hi&quot;"

    def test_no_special_chars(self):
        assert html_escape("hello world") == "hello world"

    def test_multiple_escapes_in_one_string(self):
        assert html_escape('<a href="x&y">') == "&lt;a href=&quot;x&amp;y&quot;&gt;"


# ---------------------------------------------------------------------------
# reconstruct_lines
# ---------------------------------------------------------------------------

class TestReconstructLines:
    def test_single_line_unchanged(self):
        assert reconstruct_lines("Hello world") == "Hello world"

    def test_hyphenated_wrap_joined(self):
        text = "extraordi-\nnary"
        assert reconstruct_lines(text) == "extraordinary"

    def test_mid_sentence_lowercase_joined(self):
        text = "The quick brown\nfox jumps"
        result = reconstruct_lines(text)
        assert result == "The quick brown fox jumps"

    def test_new_sentence_joined_with_space(self):
        text = "End of sentence.\nNew sentence begins."
        result = reconstruct_lines(text)
        assert "End of sentence." in result
        assert "New sentence begins." in result

    def test_blank_lines_skipped(self):
        text = "Line one\n\nLine two"
        result = reconstruct_lines(text)
        assert result == "Line one Line two"

    def test_empty_string(self):
        assert reconstruct_lines("") == ""


# ---------------------------------------------------------------------------
# normalize_for_search
# ---------------------------------------------------------------------------

class TestNormalizeForSearch:
    def test_lowercases(self):
        assert normalize_for_search("Hello World") == "hello world"

    def test_strips_punctuation(self):
        result = normalize_for_search("Moby-Dick: or, the Whale")
        assert "-" not in result
        assert ":" not in result
        assert "," not in result

    def test_collapses_whitespace(self):
        result = normalize_for_search("  too   many   spaces  ")
        assert result == "too many spaces"

    def test_empty_string(self):
        assert normalize_for_search("") == ""


# ---------------------------------------------------------------------------
# word_overlap_score
# ---------------------------------------------------------------------------

class TestWordOverlapScore:
    def test_perfect_match(self):
        assert word_overlap_score("moby dick", "moby dick") == 1.0

    def test_no_overlap(self):
        assert word_overlap_score("moby dick", "war peace") == 0.0

    def test_partial_overlap(self):
        score = word_overlap_score("moby dick", "moby whale")
        assert score == 0.5

    def test_candidate_is_subset_of_reference(self):
        # All candidate words are in the reference
        score = word_overlap_score("moby dick", "the great moby dick story")
        assert score == 1.0

    def test_empty_candidate(self):
        assert word_overlap_score("", "moby dick") == 0.0

    def test_case_insensitive(self):
        assert word_overlap_score("Moby Dick", "moby dick") == 1.0


# ---------------------------------------------------------------------------
# looks_like_title_fragment
# ---------------------------------------------------------------------------

class TestLooksTitleFragment:
    def test_single_word_is_fragment(self):
        assert looks_like_title_fragment("DICK")

    def test_two_words_not_fragment(self):
        assert not looks_like_title_fragment("Herman Melville")

    def test_single_word_with_leading_space(self):
        assert looks_like_title_fragment("  MOBY  ")

    def test_empty_string(self):
        # No words = empty split = len 0, not 1
        assert not looks_like_title_fragment("")


# ---------------------------------------------------------------------------
# spans_to_html
# ---------------------------------------------------------------------------

class TestSpansToHtml:
    """
    All tests construct minimal block dicts and call spans_to_html directly.
    """

    def test_plain_text_no_formatting(self):
        block = make_block([["Hello world"]])
        result = spans_to_html(block)
        assert result == "Hello world"

    def test_bold_span(self):
        block = make_block([[make_span("important", flags=16)]])
        result = spans_to_html(block, body_size=12)
        assert "<strong>important</strong>" in result

    def test_italic_span(self):
        block = make_block([[make_span("emphasis", flags=2)]])
        result = spans_to_html(block, body_size=12)
        assert "<em>emphasis</em>" in result

    def test_bold_italic_span(self):
        # flags=18 = bold (16) + italic (2)
        block = make_block([[make_span("bolditalic", flags=18)]])
        result = spans_to_html(block, body_size=12)
        assert "<strong><em>bolditalic</em></strong>" in result

    def test_superscript_small_size(self):
        # Size 7 < 12 * 0.75 = 9 → superscript
        block = make_block([[make_span("2", size=7)]])
        result = spans_to_html(block, body_size=12)
        assert "<sup>2</sup>" in result

    def test_superscript_flag(self):
        # flags & 1 = superscript flag
        block = make_block([[make_span("3", size=12, flags=1)]])
        result = spans_to_html(block, body_size=12)
        assert "<sup>3</sup>" in result

    def test_footnote_marker_becomes_link(self):
        block = make_block([[
            make_span("text before "),
            make_span("1", size=7),
        ]])
        result = spans_to_html(block, body_size=12, fn_markers={"1"})
        assert 'href="#fn-1"' in result
        assert 'epub:type="noteref"' in result
        assert "<sup>1</sup>" in result

    def test_superscript_not_in_fn_markers_is_plain_sup(self):
        block = make_block([[make_span("5", size=7)]])
        result = spans_to_html(block, body_size=12, fn_markers={"1", "2"})
        assert "<sup>5</sup>" in result
        assert "href" not in result

    def test_adjacent_same_format_spans_merged(self):
        # Two plain spans should merge into one
        block = make_block([[
            make_span("Hello "),
            make_span("world"),
        ]])
        result = spans_to_html(block, body_size=12)
        assert result == "Hello world"

    def test_superscript_not_merged_with_body(self):
        # A superscript span next to a body span must NOT merge
        # (they'd lose the super flag)
        block = make_block([[
            make_span("text", size=12),
            make_span("1", size=7),
            make_span(" more text", size=12),
        ]])
        result = spans_to_html(block, body_size=12, fn_markers={"1"})
        assert 'href="#fn-1"' in result  # link survived, not merged away

    def test_html_special_chars_escaped(self):
        block = make_block([["a < b & c > d"]])
        result = spans_to_html(block)
        assert "&lt;" in result
        assert "&amp;" in result
        assert "&gt;" in result

    def test_hyphenated_line_break_joined(self):
        block = make_block([
            [make_span("extraordi-")],
            [make_span("nary")],
        ])
        result = spans_to_html(block, body_size=12)
        assert "extraordinary" in result

    def test_soft_line_break_gets_space(self):
        block = make_block([
            [make_span("the quick")],
            [make_span("brown fox")],
        ])
        result = spans_to_html(block, body_size=12)
        assert "the quick brown fox" in result

    def test_empty_block_returns_empty(self):
        block = make_block([[""]])
        result = spans_to_html(block, body_size=12)
        assert result == ""

    def test_bold_font_name_detected(self):
        # Bold detected via font name rather than flags
        block = make_block([[make_span("bold by font", font="Helvetica-Bold")]])
        result = spans_to_html(block, body_size=12)
        assert "<strong>" in result

    def test_italic_font_name_detected(self):
        block = make_block([[make_span("italic by font", font="Times-Italic")]])
        result = spans_to_html(block, body_size=12)
        assert "<em>" in result
