"""PDF 规则文档：文字和表格由程序读出；扫描页、截图页整页转成图片交给多模态模型。"""
from pathlib import Path

import pytest

from bonus_agent.auto import build_inventory
from bonus_agent.clauses import check_quote
from bonus_agent.facts import norm
from bonus_agent.parsing.docs import parse_any
from bonus_agent.web import safe_name

pytest.importorskip("pdfplumber")
pytest.importorskip("pypdfium2")
rl = pytest.importorskip("reportlab")


def make_pdf(path: Path, img: Path) -> Path:
    from PIL import Image as PI
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    st = ParagraphStyle("c", fontName="STSong-Light", fontSize=11)
    PI.new("RGB", (600, 400), "white").save(img)
    t = Table([["档位", "计提比例"], ["10万元以下", "5%"], ["10万元以上", "8%"]],
              style=TableStyle([("FONTNAME", (0, 0), (-1, -1), "STSong-Light"), ("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    SimpleDocTemplate(str(path), pagesize=A4).build(
        [Paragraph("交付奖金按贡献毛利超额累进计提。", st), t, PageBreak(), Image(str(img), 400, 300)])
    return path


def test_pdf_text_tables_and_scanned_pages(tmp_path):
    pdf = make_pdf(tmp_path / "规则.pdf", tmp_path / "scan.png")
    d = parse_any(pdf)
    kinds = [(b.page, b.kind) for b in d.blocks]
    assert kinds == [(1, "text"), (1, "table"), (2, "image")]
    assert "超额累进计提" in d.blocks[0].text
    assert "| 10万元以下 | 5% |" in d.blocks[1].text and "档位" not in d.blocks[0].text   # 表格内容不重复进正文
    assert d.blocks[2].mime == "image/png" and d.blocks[2].image[:4] == b"\x89PNG"
    # 原文摘录能在 PDF 读出的文字里逐字找到
    assert check_quote("规则.pdf", "交付奖金按贡献毛利超额累进计提。", {"规则.pdf": norm(d.full_text())}) is None
    inv = build_inventory([pdf])
    assert inv is not None
    assert safe_name("补充规则.PDF") == "补充规则.PDF"
