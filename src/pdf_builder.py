"""Renders a list of Markdown sections into one styled, right-to-left
Hebrew PDF with a handwritten look (Playpen Sans Hebrew).

Uses fpdf2 + uharfbuzz for real Hebrew text shaping and bidi reordering
(mixed Hebrew/English lines render correctly). Pure pip install, no GTK/
Pango system dependency, so it stays easy to set up on Windows.

This intentionally does NOT go through an HTML layer: fpdf2's write_html()
does not document reliable RTL support, so headings/paragraphs/bullets are
parsed directly from the Markdown and drawn with fpdf2 primitives, using
its built-in `markdown=True` cell option to bold `**text**` spans.
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import List, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from fpdf import FPDF
from fpdf.enums import MethodReturnValue
from PIL import Image

FONT_FAMILY = "PlaypenHebrew"
FONT_PATH = Path(__file__).parent.parent / "assets" / "fonts" / "PlaypenSansHebrew.ttf"

INK_COLOR = (43, 38, 38)          # body text - warm near-black
HEADING_COLOR = (172, 74, 39)     # burnt-orange marker color for headings
RULE_COLOR = (222, 210, 195)      # faint notebook rule lines
CHAPTER_LABEL_COLOR = (166, 146, 132)  # muted warm gray for the running chapter header
PAGE_BG = (255, 253, 247)         # warm off-white "paper"
BACK_COVER_BG = (249, 238, 187)   # slightly yellow notebook-cover tone
BACK_COVER_RULE_COLOR = (214, 195, 140)  # rule lines a touch darker, to show against the yellow


class NotebookPDF(FPDF):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.page_bg = PAGE_BG
        self.rule_color = RULE_COLOR
        self.page_dirty = False  # has anything been written on the current page yet?
        # Name of the chapter (unit) the current page belongs to, drawn small
        # at the top of every page. None on the covers, which have no chapter.
        self.chapter_label = None

    def add_page(self, *args, **kwargs) -> None:
        super().add_page(*args, **kwargs)
        self.page_dirty = False

    def header(self) -> None:
        self.set_fill_color(*self.page_bg)
        self.rect(0, 0, self.w, self.h, style="F")
        # Faint horizontal ruled lines, like notebook paper.
        self.set_draw_color(*self.rule_color)
        self.set_line_width(0.2)
        y = 25
        while y < self.h - 15:
            self.line(12, y, self.w - 12, y)
            y += 8

        # Running chapter name, above the first ruled line. add_page() saves
        # and restores font/colors around this call, so changing them here
        # can't leak into the body text that follows.
        if self.chapter_label:
            self.set_font(FONT_FAMILY, size=8)
            self.set_text_color(*CHAPTER_LABEL_COLOR)
            self.set_xy(self.l_margin, 13)
            self.cell(w=self.epw, h=5, text=self.chapter_label, align="R")
            self.set_draw_color(*self.rule_color)
            self.set_line_width(0.2)
            self.line(self.l_margin, 19.5, self.w - self.r_margin, 19.5)

        # Writing the label above moved the cursor; put it back where the
        # page's body content is expected to start. Without this, anything
        # drawn right after an automatic page break (a forced subject
        # heading, a wrapped paragraph) lands on top of the running header.
        self.set_xy(self.l_margin, self.t_margin)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font(FONT_FAMILY, size=9)
        self.set_text_color(*RULE_COLOR)
        self.cell(0, 8, str(self.page_no()), align="C")


def _register_font(pdf: FPDF) -> None:
    pdf.add_font(FONT_FAMILY, style="", fname=str(FONT_PATH), variations={"wght": 420})
    pdf.add_font(FONT_FAMILY, style="B", fname=str(FONT_PATH), variations={"wght": 750})


_BOLD_TOKEN_RE = re.compile(r"(\*\*.+?\*\*)", re.S)


def _split_bold_runs(text: str):
    """Splits into (is_bold, content) runs. A leading colon on a regular
    run right after a bold run is merged into the bold run (see
    _write_richtext for why)."""
    tokens = [t for t in _BOLD_TOKEN_RE.split(text) if t]
    runs = []
    for tok in tokens:
        m = re.match(r"^\*\*(.+)\*\*$", tok, re.S)
        runs.append((True, m.group(1)) if m else (False, tok))

    for i in range(len(runs) - 1):
        is_bold, content = runs[i]
        nxt_bold, nxt_content = runs[i + 1]
        if is_bold and not nxt_bold and nxt_content.startswith(":"):
            runs[i] = (True, content + ":")
            runs[i + 1] = (False, nxt_content[1:])
    return runs


_HEBREW_RE = re.compile(r"[֐-׿]")
_LATIN_TAIL_RE = re.compile(r"[A-Za-z][^֐-׿]*$")


def _tokenize_words(text: str, prefix: str = ""):
    """Splits into a flat list of (word, is_bold) tokens in logical
    (reading) order, from **bold**-marked runs.

    Placing words right-to-left one at a time is only correct for Hebrew,
    where each word really is its own RTL unit. Doing that to a run of
    consecutive non-Hebrew words (an English term, or a whole embedded
    phrase like "(Out of Vocabulary - OOV)") reverses their order relative
    to each other. So contiguous non-Hebrew words within a run are grouped
    into one atomic chunk instead of being split individually - a single
    call/unit lets the shaping engine keep its internal left-to-right
    order intact. Hebrew words are still split individually, since that's
    what allows the line to wrap at the right spots."""
    tokens = []
    runs = _split_bold_runs(text) or [(False, "")]
    is_bold0, content0 = runs[0]
    runs[0] = (is_bold0, prefix + content0)
    for is_bold, content in runs:
        words = content.split()
        i = 0
        while i < len(words):
            if _HEBREW_RE.search(words[i]):
                # A Hebrew prefix glued to English ("ו-Language Models") keeps
                # the English words after it in the same chunk; split apart,
                # they would be placed right-to-left and swap places.
                j = i + 1
                if _LATIN_TAIL_RE.search(words[i]):
                    while j < len(words) and not _HEBREW_RE.search(words[j]):
                        j += 1
                tokens.append((" ".join(words[i:j]), is_bold))
                i = j
            else:
                j = i
                while j < len(words) and not _HEBREW_RE.search(words[j]):
                    j += 1
                tokens.append((" ".join(words[i:j]), is_bold))
                i = j
    return tokens


def _write_richtext(pdf: FPDF, text: str, size: float, prefix: str = "",
                    link: Optional[int] = None) -> None:
    """Renders one right-aligned RTL paragraph with correct bold/regular
    styling and correct word-wrapping, laying out words manually instead
    of going through fpdf2's multi_cell/write.

    Two separate fpdf2 bugs forced this: (1) multi_cell's markdown=True
    bold parsing runs independently on each bidi-reordered fragment, so a
    "**" marker that lands in its own neutral bidi fragment (which happens
    whenever a bold run sits right at an English/Hebrew script boundary,
    e.g. "**Smoothing**: ...טכניקה") gets detached from the word it should
    wrap, misapplying bold to the wrong stretch of text. (2) write(), which
    sidesteps that by using single-styled calls, does not right-align
    wrapped lines at all - every wrapped line starts flush at the left
    margin, breaking RTL paragraphs that span more than one line.
    Manually measuring word widths, greedily wrapping them into lines, and
    placing each word's cell() explicitly from the right margin leftward
    avoids both bugs at once.

    `link`, when given, is an fpdf2 internal-link id: every laid-out line
    gets a clickable rectangle covering exactly the text it drew.
    """
    h = size * 0.6

    def word_width(word: str, bold: bool) -> float:
        pdf.set_font(FONT_FAMILY, style="B" if bold else "", size=size)
        return pdf.get_string_width(word)

    right_edge = pdf.w - pdf.r_margin
    max_w = right_edge - pdf.l_margin

    def fit_pieces(chunk: str, bold: bool) -> List[str]:
        """A non-Hebrew chunk wider than a whole line (a long English
        quote) is cut at spaces into pieces that each fit, so it wraps
        instead of running off the page."""
        pieces, cur = [], ""
        for w in chunk.split(" "):
            cand = f"{cur} {w}" if cur else w
            if cur and word_width(cand, bold) > max_w:
                pieces.append(cur)
                cur = w
            else:
                cur = cand
        return pieces + [cur]

    # ponytail: a single space-less word wider than the line (a long URL) still overflows.
    words = [(piece, b) for w, b in _tokenize_words(text, prefix=prefix)
             for piece in (fit_pieces(w, b) if word_width(w, b) > max_w else [w])]
    space_w = word_width(" ", False)

    lines, cur_line, cur_w = [], [], 0.0
    for word, is_bold in words:
        ww = word_width(word, is_bold)
        extra = ww if not cur_line else space_w + ww
        if cur_line and cur_w + extra > max_w:
            lines.append(cur_line)
            cur_line, cur_w = [], 0.0
            extra = ww
        cur_line.append((word, is_bold, ww))
        cur_w += extra
    if cur_line:
        lines.append(cur_line)

    for line in lines:
        y = pdf.get_y()
        if y + h > pdf.page_break_trigger:
            pdf.add_page()
            y = pdf.get_y()
        x = right_edge
        for word, is_bold, ww in line:
            x -= ww
            pdf.set_xy(x, y)
            pdf.set_font(FONT_FAMILY, style="B" if is_bold else "", size=size)
            pdf.cell(w=ww, h=h, text=word)
            x -= space_w
        if link is not None:
            # x sits one space to the left of the last word drawn, so the
            # text actually spans [x + space_w, right_edge].
            line_left = x + space_w
            pdf.link(line_left, y, right_edge - line_left, h, link)
        pdf.set_xy(pdf.l_margin, y + h)


_MATH_FONT_SIZE = 15  # pt - the visual scale rendered formulas come out at
_MATH_DPI = 300        # raster quality only; physical size is derived from this

# Common LaTeX the model writes that mathtext doesn't know, rewritten to an
# equivalent it does. \big( etc. only size a delimiter, so dropping the
# command keeps the delimiter itself.
# ponytail: \begin{cases} has no mathtext equivalent and still falls back to text.
_MATHTEXT_FIXES = [
    (re.compile(r"\\[Bb]igg?[lr]?(?![a-zA-Z])"), ""),
    (re.compile(r"\\iff(?![a-zA-Z])"), r"\\Leftrightarrow"),
    (re.compile(r"\\implies(?![a-zA-Z])"), r"\\Rightarrow"),
    (re.compile(r"\\x(right|left)arrow\{((?:[^{}]|\{[^{}]*\})*)\}"), r"\\overset{\2}{\\long\1arrow}"),
]


def _to_mathtext(latex_body: str) -> str:
    for pattern, repl in _MATHTEXT_FIXES:
        latex_body = pattern.sub(repl, latex_body)
    return latex_body


def _render_math_image(latex_body: str) -> Optional[io.BytesIO]:
    """Renders a LaTeX math expression via matplotlib's "mathtext" parser -
    a real subset of LaTeX math syntax (\\frac, \\sum, \\sqrt, sub/superscripts,
    Greek letters, \\left(...\\right), etc.) that needs no system LaTeX
    install - to an in-memory transparent PNG. Returns None if the
    expression fails to parse, so the caller can fall back to plain text
    instead of crashing the whole build over one malformed formula."""
    try:
        r, g, b = INK_COLOR
        color = f"#{r:02x}{g:02x}{b:02x}"
        fig = plt.figure(figsize=(0.1, 0.1))
        fig.patch.set_alpha(0.0)
        fig.text(0.5, 0.5, f"${_to_mathtext(latex_body)}$",fontsize=_MATH_FONT_SIZE, ha="center", va="center", color=color)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=_MATH_DPI, transparent=True, bbox_inches="tight", pad_inches=0.08)
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception:
        plt.close("all")
        return None


def _write_formula_text_fallback(pdf: FPDF, text: str, size: float = 12.0) -> None:
    """Old plain-text-in-a-box rendering, used only if a formula doesn't
    parse as mathtext (e.g. the model slipped back into plain notation
    instead of LaTeX). Standalone formulas are pure LTR content; rendering
    them under the document's global RTL paragraph direction is what caused
    garbled ordering (e.g. a leading Hebrew-adjacent period jumping to the
    front), so shaping is switched to LTR just for this one line."""
    pdf.set_font(FONT_FAMILY, size=size)
    pdf.set_text_shaping(use_shaping_engine=True, direction="ltr", script="latn", language="en")

    h = size * 0.6
    pad_x, pad_y = 6.0, 3.0
    box_w = min(pdf.epw, pdf.get_string_width(text) + 2 * pad_x + 4)
    box_x = pdf.l_margin + (pdf.epw - box_w) / 2

    height_used = pdf.multi_cell(
        w=box_w - 2 * pad_x, h=h, text=text, markdown=True, align="C",
        dry_run=True, output=MethodReturnValue.HEIGHT,
    )
    box_y = pdf.get_y()

    pdf.set_draw_color(*HEADING_COLOR)
    pdf.set_line_width(0.4)
    pdf.rect(box_x, box_y, box_w, height_used + 2 * pad_y)

    pdf.set_xy(box_x + pad_x, box_y + pad_y)
    pdf.set_text_color(*INK_COLOR)
    pdf.multi_cell(w=box_w - 2 * pad_x, h=h, text=text, markdown=True, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(box_y + height_used + 2 * pad_y)

    pdf.set_text_shaping(use_shaping_engine=True, direction="rtl", script="hebr", language="he")


def _write_formula(pdf: FPDF, text: str, top_gap: float = 4.0) -> None:
    """Draws a standalone formula as real typeset math (rendered via
    matplotlib's mathtext, see _render_math_image), boxed like a callout
    equation in a textbook. Falls back to the old plain-text box if the
    text doesn't parse as math."""
    pdf.ln(top_gap)

    img_buf = _render_math_image(text)
    if img_buf is None:
        _write_formula_text_fallback(pdf, text)
        pdf.page_dirty = True
        return

    img = Image.open(img_buf)
    px_w, px_h = img.size
    img_w_mm = px_w / _MATH_DPI * 25.4
    img_h_mm = px_h / _MATH_DPI * 25.4

    max_w = pdf.epw * 0.85
    if img_w_mm > max_w:
        scale = max_w / img_w_mm
        img_w_mm *= scale
        img_h_mm *= scale

    pad_x, pad_y = 6.0, 4.0
    box_w = img_w_mm + 2 * pad_x
    box_h = img_h_mm + 2 * pad_y
    box_x = pdf.l_margin + (pdf.epw - box_w) / 2

    if pdf.get_y() + box_h > pdf.page_break_trigger:
        pdf.add_page()
    box_y = pdf.get_y()

    pdf.set_draw_color(*HEADING_COLOR)
    pdf.set_line_width(0.4)
    pdf.rect(box_x, box_y, box_w, box_h)

    img_buf.seek(0)
    pdf.image(img_buf, x=box_x + pad_x, y=box_y + pad_y, w=img_w_mm, h=img_h_mm)
    pdf.set_y(box_y + box_h)
    pdf.page_dirty = True


def _write_image(pdf: FPDF, path: str, top_gap: float = 4.0) -> None:
    """Draws a slide figure centered, at most 80% of the text width and never
    taller than a page. Skipped silently if the image file is gone (e.g. the
    cache folder was deleted) or can't be decoded."""
    try:
        with Image.open(path) as img:
            px_w, px_h = img.size
    except Exception:
        return

    w = pdf.epw * 0.8
    h = w * px_h / px_w
    max_h = pdf.page_break_trigger - pdf.t_margin - top_gap
    if h > max_h:
        w, h = w * max_h / h, max_h

    pdf.ln(top_gap)
    if pdf.get_y() + h > pdf.page_break_trigger:
        pdf.add_page()
    y = pdf.get_y()
    pdf.image(path, x=pdf.l_margin + (pdf.epw - w) / 2, y=y, w=w, h=h)
    pdf.set_y(y + h)
    pdf.page_dirty = True


def _write_paragraph(pdf: FPDF, text: str, size: float = 11.5, top_gap: float = 2.0) -> None:
    pdf.set_text_color(*INK_COLOR)
    pdf.ln(top_gap)
    _write_richtext(pdf, text, size=size)
    pdf.page_dirty = True


def _write_toc_item(pdf: FPDF, number: int, title: str, link: Optional[int],
                    size: float = 12.0, top_gap: float = 2.6) -> None:
    """One clickable table-of-contents line on the cover page. Drawn in the
    heading color so it reads as a link, and jumping to `link`'s chapter."""
    pdf.set_text_color(*HEADING_COLOR)
    pdf.ln(top_gap)
    pdf.set_right_margin(pdf.r_margin + 6)
    _write_richtext(pdf, f"{number}. {title}", size=size, link=link)
    pdf.set_right_margin(pdf.r_margin - 6)
    pdf.set_text_color(*INK_COLOR)
    pdf.page_dirty = True


def _write_list_item(pdf: FPDF, text: str, size: float = 11.5, top_gap: float = 2.2) -> None:
    pdf.set_text_color(*INK_COLOR)
    pdf.ln(top_gap)
    # Bullet goes FIRST in the logical string: with RTL paragraph direction
    # the first logical character renders at the visual right (start) side,
    # which is where a bullet belongs for right-aligned Hebrew text.
    pdf.set_right_margin(pdf.r_margin + 6)
    _write_richtext(pdf, text, size=size, prefix="• ")
    pdf.set_right_margin(pdf.r_margin - 6)
    pdf.page_dirty = True


# Headings at these levels mark the start of a new "subject" - each one
# starts on a fresh page (unless it's the very first thing on an
# already-blank page, to avoid a pointless blank page before it).
_SUBJECT_HEADING_LEVELS = {3}
# The glossary and the past-exam question both close out a unit, and each
# gets its own page like any other subject.
_SUBJECT_HEADING_TEXT = {"מושגי מפתח", "שאלה ממבחנים קודמים"}


def _write_heading(pdf: FPDF, text: str, level: int) -> None:
    is_subject_heading = level in _SUBJECT_HEADING_LEVELS or text.strip() in _SUBJECT_HEADING_TEXT
    if is_subject_heading and pdf.page_dirty:
        pdf.add_page()

    sizes = {1: 24, 2: 17, 3: 13.5}
    gaps = {1: 4, 2: 8, 3: 6}
    pdf.ln(0 if is_subject_heading and not pdf.page_dirty else gaps[level])
    pdf.set_font(FONT_FAMILY, style="B", size=sizes[level])
    pdf.set_text_color(*HEADING_COLOR)
    pdf.multi_cell(w=pdf.epw, h=sizes[level] * 0.7, text=text, align="C" if level == 1 else "R", new_x="LMARGIN", new_y="NEXT")
    if level <= 2:
        pdf.set_draw_color(*HEADING_COLOR)
        pdf.set_line_width(0.5)
        y = pdf.get_y() + 1
        pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
        pdf.ln(3)
    pdf.page_dirty = True


_LIST_RE = re.compile(r"^\s*(?:[-*]|\d+[\.\)])\s+(.*)$")
_HEADING_RE = re.compile(r"^(#{1,3})(?:\s+(.*))?$")
_FORMULA_RE = re.compile(r"^@@F@@\s*(.*)$")
# Slide figure, already resolved to a local file by the pipeline: "@@IMG@@ C:\...\deck_p7.jpg".
_IMG_RE = re.compile(r"^@@IMG@@\s*(.+)$")
# Cover-page table-of-contents entry: "@@TOC@@ 3|Corpus Linguistics".
_TOC_RE = re.compile(r"^@@TOC@@\s*(\d+)\|(.*)$")
# A chapter's numbered topic list ("3. title" under "## נושאים:") and the
# "### 3. title" heading each item jumps to.
_NUMBERED_RE = re.compile(r"^(\d+)[\.\)]\s+(.*)$")
_TOPICS_HEADING = "נושאים"


def _render_section(pdf: FPDF, section_md: str,
                    chapter_links: Optional[List[int]] = None) -> None:
    # Topic links are seeded at page 1 and retargeted when their heading is
    # drawn, the same way chapter links are (see build_pdf).
    topic_links, in_topics = {}, False
    for raw_line in section_md.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        toc_match = _TOC_RE.match(line.strip())
        if toc_match:
            number = int(toc_match.group(1))
            link = None
            if chapter_links and 1 <= number <= len(chapter_links):
                link = chapter_links[number - 1]
            _write_toc_item(pdf, number, toc_match.group(2).strip(), link)
            continue

        formula_match = _FORMULA_RE.match(line.strip())
        if formula_match:
            _write_formula(pdf, formula_match.group(1).strip())
            continue

        img_match = _IMG_RE.match(line.strip())
        if img_match:
            _write_image(pdf, img_match.group(1).strip())
            continue

        heading_match = _HEADING_RE.match(line)
        if heading_match:
            # An empty "### " the model sometimes leaves behind would draw a blank subject page.
            title = (heading_match.group(2) or "").strip()
            if title:
                level = len(heading_match.group(1))
                in_topics = level == 2 and title.rstrip(":").strip() == _TOPICS_HEADING
                _write_heading(pdf, title, level)
                numbered = _NUMBERED_RE.match(title)
                if level == 3 and numbered and numbered.group(1) in topic_links:
                    pdf.set_link(topic_links.pop(numbered.group(1)), page=pdf.page_no(), y=0)
            continue

        topic_match = _NUMBERED_RE.match(line.strip()) if in_topics else None
        if topic_match:
            number = topic_match.group(1)
            topic_links[number] = pdf.add_link(page=1)
            _write_toc_item(pdf, int(number), topic_match.group(2).strip(), topic_links[number])
            continue

        list_match = _LIST_RE.match(line)
        if list_match:
            _write_list_item(pdf, list_match.group(1).strip())
            continue

        _write_paragraph(pdf, line.strip())


def _add_back_cover(pdf: FPDF, course_name: str = None) -> None:
    pdf.page_bg = BACK_COVER_BG
    pdf.rule_color = BACK_COVER_RULE_COLOR
    pdf.chapter_label = None  # the back cover belongs to no chapter
    pdf.add_page()

    pdf.set_y(pdf.h / 2 - 20)
    pdf.set_font(FONT_FAMILY, style="B", size=22)
    pdf.set_text_color(*HEADING_COLOR)
    pdf.multi_cell(w=pdf.epw, h=14, text="בהצלחה במבחן!", align="C")

    if course_name:
        pdf.ln(4)
        pdf.set_font(FONT_FAMILY, size=12)
        pdf.set_text_color(*INK_COLOR)
        pdf.multi_cell(w=pdf.epw, h=8, text=course_name, align="C")


def build_pdf(sections: List[str], output_path: Path, course_name: str = None,
              chapter_titles: Optional[List[str]] = None) -> Path:
    """sections[0] is the cover page, the rest are one per unit. Each
    section starts on a new page. A yellow, notebook-ruled back cover is
    appended after the last section.

    `chapter_titles` names the unit behind each section after the cover
    (so sections[i] is chapter_titles[i - 1]). It drives both the running
    chapter header on every page and the cover's clickable contents list.
    """
    if not FONT_PATH.exists():
        raise FileNotFoundError(
            f"Font not found at {FONT_PATH}. Expected assets/fonts/PlaypenSansHebrew.ttf."
        )

    pdf = NotebookPDF(orientation="P", format="A4")
    pdf.set_margins(left=20, top=25, right=20)
    pdf.set_auto_page_break(auto=True, margin=18)
    _register_font(pdf)
    pdf.set_text_shaping(use_shaping_engine=True, direction="rtl", script="hebr", language="he")

    # Link ids have to exist before the cover is drawn, but a chapter's real
    # target page is only known once that chapter starts. Seed them pointing
    # at page 1 (fpdf2 refuses to attach a link with no page assigned) and
    # retarget each below - set_link mutates the destination in place, so the
    # annotations already written on the cover follow along.
    titles = chapter_titles or []
    chapter_links = [pdf.add_link(page=1) for _ in titles]

    for idx, section_md in enumerate(sections):
        chapter_idx = idx - 1  # sections[0] is the cover, which has no chapter
        pdf.chapter_label = titles[chapter_idx] if 0 <= chapter_idx < len(titles) else None
        pdf.add_page()
        pdf.set_y(25)
        if 0 <= chapter_idx < len(chapter_links):
            pdf.set_link(chapter_links[chapter_idx], page=pdf.page_no(), y=0)
        _render_section(pdf, section_md, chapter_links=chapter_links if idx == 0 else None)

    _add_back_cover(pdf, course_name=course_name)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(output_path))
    return output_path
