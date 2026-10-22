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
