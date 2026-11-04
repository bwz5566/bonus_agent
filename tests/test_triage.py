"""材料预筛：多页规则文档先按页分类，介绍、装饰、空白页折叠，不进上下文；有表格和数字的页不折叠。"""
import json
from pathlib import Path

from bonus_agent.agent import Workspace
from bonus_agent.auto import build_inventory
from bonus_agent.llm import FakeLLM
from bonus_agent.parsing.docs import Block, ParsedDoc

DOC = "制度.pptx"


def _ws(blocks, triage=None):
    llm = FakeLLM(["x"])
    llm.triage = triage
    ws = Workspace(llm, build_inventory([]))
    ws.inv.docs.append(ParsedDoc(DOC, blocks))
    ws._file = lambda name: Path(name)          # 测试里没有真实文件
    return ws, llm


def _pages():
    head = "某公司 激励制度汇编"
    return [
        Block(DOC, 1, "text", text=f"{head}\n公司简介：成立于多年前，致力于成为行业领先企业"),
        Block(DOC, 2, "text", text=f"{head}\n目录\n一、总则\n二、核算办法"),
        Block(DOC, 3, "text", text=f"{head}\n提成 = 回款额 × 提成比例\n提成比例 3%"),
        Block(DOC, 4, "text", text=f"{head}\n发展历程：一路走来，感谢每一位伙伴"),
        Block(DOC, 5, "text", text=f"{head}\n愿景：成为受人尊敬的企业，回款超过 500 万的团队有表彰"),
        Block(DOC, 6, "text", text=f"{head}\n谢谢观看"),
    ]


def _answer(labels):
    return json.dumps({"pages": [{"page": p, "label": l, "reason": f"第{p}页是{l}"} for p, l in labels.items()]},
                      ensure_ascii=False)


def test_background_pages_are_folded_and_can_be_expanded():
    ws, llm = _ws(_pages(), _answer({1: "背景", 2: "装饰", 3: "规则", 4: "背景", 5: "背景", 6: "装饰"}))
    ws.triage_all()
    text = ws.read_document(DOC, 0, limit=10 ** 9)
    assert "提成比例 3%" in text
    assert "公司简介" not in text and "发展历程" not in text and "谢谢观看" not in text
    assert "第1–2页 已折叠" in text and "第4页 已折叠" in text and "folded=true" in text
    # 兜底：第 5 页被判成背景，但有“500 万”这样带单位的数字，照常给
    assert "回款超过 500 万" in text
    assert not ws.triage[DOC]["pages"]["5"]["fold"]
    # 展开
    full = ws.read_document(DOC, 0, limit=10 ** 9, folded=True)
    assert "公司简介" in full and "谢谢观看" in full
    report = "\n".join(ws.triage_report())
    assert "第1页" in report and "第6页" in report and "第3页" not in report


def test_pages_with_tables_are_never_folded():
    blocks = _pages() + [Block(DOC, 4, "table", text="级别 | 系数\nA | 1.2")]
    ws, _ = _ws(blocks, _answer({4: "背景"}))
    ws.triage_all()
    assert not ws.triage[DOC]["pages"]["4"]["fold"]
    assert "发展历程" in ws.read_document(DOC, 0, limit=10 ** 9)


def test_triage_failure_folds_nothing():
    ws, _ = _ws(_pages(), "这不是 JSON")
    ws.triage_all()
    text = ws.read_document(DOC, 0, limit=10 ** 9)
    assert "公司简介" in text and "已折叠" not in text


def test_short_documents_are_not_triaged_and_unchanged_docs_not_redone():
    ws, llm = _ws(_pages()[:3], _answer({1: "背景"}))
    n0 = llm.usage.calls
    ws.triage_all()
    assert llm.usage.calls == n0 and not ws.triage
    ws, llm = _ws(_pages(), _answer({1: "背景"}))
    ws.triage_all()
    n1 = llm.usage.calls
    ws.triage_all()                 # 内容没变：不再调用模型
    assert llm.usage.calls == n1


def test_repeated_page_header_is_shown_once():
    ws, _ = _ws(_pages())
    text = ws.read_document(DOC, 0, limit=10 ** 9)
    assert text.count("某公司 激励制度汇编") == 1
    assert text.count(f"—— {DOC} 第") == 6       # 每页仍标页码


def test_body_heading_repeated_mid_page_is_kept():
    """各页正文中间都有的小标题不是页眉，不去掉。"""
    blocks = [Block(DOC, p, "text", text=f"第{p}个奖项说明\n考核方式\n按季度考核\n其余说明\n结尾{p}") for p in range(1, 6)]
    ws, _ = _ws(blocks)
    assert ws.read_document(DOC, 0, limit=10 ** 9).count("考核方式") == 5


def test_empty_image_transcripts_are_collapsed():
    blocks = _pages() + [Block(DOC, 3, "image", image=b"\x89PNG-a", mime="image/png")]
    ws, llm = _ws(blocks)
    llm.images = "看不清"
    ws.transcribe_all()
    assert "[图片 #1：没有可用的文字内容]" in ws.read_document(DOC, 0, limit=10 ** 9)


def test_triage_survives_case_snapshot():
    from bonus_agent import case as cs
    ws, _ = _ws(_pages(), _answer({1: "背景"}))
    ws.triage_all()
    snap = json.loads(json.dumps(cs.snapshot(ws), ensure_ascii=False))
    ws2, llm2 = _ws(_pages())
    cs.restore(ws2, snap)
    assert ws2.triage[DOC]["pages"]["1"]["fold"]
    n0 = llm2.usage.calls
    ws2.triage_all()
    assert llm2.usage.calls == n0


def test_short_meaningful_image_text_is_kept():
    from bonus_agent.agent import _empty_transcript
    assert not _empty_transcript("按季度发放")
    assert not _empty_transcript("系数 1.2")
    assert _empty_transcript("图片为公司标志，没有与奖金相关的文字")
    assert _empty_transcript("看不清")


def test_independent_reread_still_sees_folded_pages():
    from bonus_agent import reread as rr
    ws, _ = _ws(_pages(), _answer({1: "背景"}))
    ws.triage_all()
    assert "已折叠" in ws.read_document(DOC, 0, limit=10 ** 9)
    assert "公司简介" in rr.material(ws)
