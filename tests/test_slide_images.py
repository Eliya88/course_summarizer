import os
import sys
from pathlib import Path

from fpdf import FPDF
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import pdf_builder, pdf_utils, pipeline


def make_deck(tmp_path):
    """2-page PDF: page 1 has only a tiny logo, page 2 has the logo plus a big figure."""
    logo, figure = tmp_path / "logo.png", tmp_path / "figure.png"
    Image.new("RGB", (8, 8), "red").save(logo)
    Image.frombytes("RGB", (300, 200), os.urandom(300 * 200 * 3)).save(figure)  # noise -> doesn't compress
    pdf = FPDF()
    for images in ([logo], [logo, figure]):
        pdf.add_page()
        for i, img in enumerate(images):
            pdf.image(str(img), x=10, y=10 + 60 * i, w=50)
    out = tmp_path / "deck.pdf"
    pdf.output(str(out))
    return out


def test_extract_main_image_skips_logos(tmp_path):
    deck = make_deck(tmp_path)
    img = pdf_utils.extract_main_image(deck, 2, tmp_path / "imgs")
    assert img is not None and img.stat().st_size >= pdf_utils.MIN_IMAGE_BYTES
    assert Image.open(img).size == (300, 200)
    assert pdf_utils.extract_main_image(deck, 1, tmp_path / "imgs") is None
    assert pdf_utils.extract_main_image(deck, 9, tmp_path / "imgs") is None
    assert pdf_utils.extract_main_image(deck, 0, tmp_path / "imgs") is None


def test_resolve_images_rewrites_valid_markers_and_drops_the_rest(tmp_path):
    deck = make_deck(tmp_path)
    body = "before\n@@IMG@@ 1 2\nmiddle\n@@IMG@@ 1 1\n@@IMG@@ 2 2\nafter"
    out = pipeline._resolve_images(body, [deck], tmp_path / "imgs")
    lines = out.splitlines()
    img_lines = [line for line in lines if line.startswith("@@IMG@@")]
    assert len(img_lines) == 1 and Path(img_lines[0].split(" ", 1)[1]).exists()
    assert [line for line in lines if line and not line.startswith("@@IMG@@")] == ["before", "middle", "after"]


def test_build_pdf_draws_images_and_skips_bad_ones(tmp_path):
    img = pdf_utils.extract_main_image(make_deck(tmp_path), 2, tmp_path / "imgs")
    section = f"# Unit\n### \n### 1. Topic\ntext\n@@IMG@@ {img}\n@@IMG@@ {tmp_path / 'gone.png'}\nmore"
    out = pdf_builder.build_pdf(["# Cover", section], tmp_path / "out.pdf", chapter_titles=["Unit"])
    assert out.exists()


def test_empty_heading_is_skipped(monkeypatch):
    headings = []
    monkeypatch.setattr(pdf_builder, "_write_heading", lambda pdf, text, level: headings.append(text))
    monkeypatch.setattr(pdf_builder, "_write_paragraph", lambda pdf, text: headings.append("para:" + text))
    pdf_builder._render_section(None, "### \n###\n### 2. Real")
    assert headings == ["2. Real"]


def test_deep_heading_is_a_sub_heading_not_text(monkeypatch):
    seen = []
    monkeypatch.setattr(pdf_builder, "_write_heading", lambda pdf, text, level: seen.append((text, level)))
    monkeypatch.setattr(pdf_builder, "_write_paragraph", lambda pdf, text: seen.append("para:" + text))
    pdf_builder._render_section(None, "#### Word2Vec\n##### CBOW")
    assert seen == [("Word2Vec", 4), ("CBOW", 4)]


def test_unsupported_latex_is_rewritten_for_mathtext():
    import matplotlib
    from matplotlib.mathtext import MathTextParser
    matplotlib.use("Agg")
    parser = MathTextParser("path")
    for body in [
        r"P(x_t \mid x_1, \ldots, x_{t-1}) = \operatorname{softmax}\big(f_\theta(x_1, \ldots, x_{t-1})\big)",
        r"t_1 =^{\varepsilon} t_2 \iff |t_2 - t_1| \leq \varepsilon",
        r"f(x) \xrightarrow{\text{Fourier Transform}} F(\omega)",
        r"\Bigl( a \Bigr) \implies b",
    ]:
        parser.parse(f"${pdf_builder._to_mathtext(body)}$")  # raises if still unsupported
    assert pdf_builder._to_mathtext(r"\bigcup_i A_i") == r"\bigcup_i A_i"  # real commands untouched


def test_long_english_quote_wraps_inside_the_margins():
    pdf = pdf_builder.NotebookPDF()
    pdf_builder._register_font(pdf)
    pdf.add_page()
    cells = []
    pdf.cell = lambda w, h, text: cells.append((pdf.get_x(), w))
    quote = '"Write an essay about how aluminium cans are used in food storage. Include keywords waste, material and meal."'
    pdf_builder._write_richtext(pdf, "דוגמה: " + quote, size=11.5, prefix="• ")
    assert len(cells) > 3  # the quote was split across lines
    assert all(x >= pdf.l_margin - 0.01 and x + w <= pdf.w - pdf.r_margin + 0.01 for x, w in cells)


def test_english_after_a_hebrew_prefix_stays_in_one_chunk():
    words = [w for w, _ in pdf_builder._tokenize_words("Tokens ו-Language Models של **ה-BPE** מודל")]
    assert words == ["Tokens", "ו-Language Models", "של", "ה-BPE", "מודל"]


def test_chapter_topic_list_links_to_each_topic(tmp_path):
    pypdf = __import__("pypdf")
    section = "# Unit\n\n## נושאים:\n\n1. First\n2. Second\n\n### 1. First\n\nbody\n\n### 2. Second\n\nbody"
    out = pdf_builder.build_pdf(["# Cover", section], tmp_path / "out.pdf", chapter_titles=["Unit"])
    reader = pypdf.PdfReader(out)
    page_of = {p.indirect_reference.idnum: i for i, p in enumerate(reader.pages)}
    # Page 2 is the chapter page with the topic list; topics start pages 3 and 4.
    targets = [page_of[a.get_object()["/Dest"][0].idnum] for a in reader.pages[1]["/Annots"]]
    assert targets == [2, 3]


def test_inline_latex_becomes_plain_text():
    plain = lambda s: pdf_builder._INLINE_MATH_RE.sub(lambda m: pdf_builder._plain_math(m[1]), s)
    assert plain("טמפרטורה $T > 0$ בחזקה $1/T$") == "טמפרטורה T > 0 בחזקה 1/T"
    assert plain(r"רצף $x_1, \ldots, x_{t-1}$ ו-$\frac{1}{|V|}$") == "רצף x_1, …, x_(t-1) ו-1/(|V|)"
    assert plain(r"$P_{\mathrm{LM}}(W)$ עם $\lambda$") == "P_(LM)(W) עם λ"
    code = "'for', '$', '1' ... for $1 billion"
    assert plain(code) == code


def test_latin_and_math_runs_are_drawn_left_to_right():
    p = pdf_builder._bidi_pieces
    assert p("T → ∞") == [("T → ∞", True)]
    assert p("0 < k < 1") == [("0 < k < 1", True)]
    assert p("T = 1.5 ו-T = 2") == [("T = 1.5", True), (" ו-", False), ("T = 2", True)]
    assert p("מ-1.") == [("מ-", False), ("1", True), (".", False)]
    assert p("Models:") == [("Models", True), (":", False)]  # the colon stays on the RTL side
    assert p("שלום,") == [("שלום", False), (",", False)]
    assert pdf_builder._tokenize_words("**T → ∞**: טקסט", prefix="• ")[:2] == [("•", False), ("T → ∞:", True)]


def test_cases_formula_becomes_one_formula_per_case():
    rows = pdf_builder._split_cases(r"f(x) = \begin{cases} 1 & \text{if } x > 0 \\ 0 & \text{otherwise} \end{cases}")
    assert rows == [r"f(x) = 1 \quad \text{if } x > 0", r"f(x) = 0 \quad \text{otherwise}"]
    assert all(pdf_builder._render_math_image(r) for r in rows)
    assert pdf_builder._split_cases("a + b") == ["a + b"]
