"""挂接建议：按编号类列比对参考表和主表，给出对得上的组合、同名列、跨期间提示。"""
from bonus_agent.skills.link_suggest import kind, suggest
from bonus_core.data.table_detect import SheetTable


def _st(sheet, headers, rows):
    return SheetTable("f.xlsx", sheet, 1, headers, [(i + 2, list(r)) for i, r in enumerate(rows)], [], 1.0, [], [], [], [])


SALES = _st("销售单", ["日期", "客户编码", "客户简称", "产品型号", "销售金额", "客户简称(AE列)"], [
    ["202609", "C1", "集团A", "M1", 100, "A"],
    ["202609", "C1", "集团A", "M2", 100, "A"],
    ["202609", "C2", "集团A", "M1", 100, "B"],
    ["202609", "C3", "丙", "M3", 100, "丙"],
])


def test_kind():
    assert kind("客户简称(AE列)") == "客户" and kind("产品型号") == "型号" and kind("日期") == "月份"
    assert kind("销售金额") == "" and kind("产品系列") == ""


def test_project_level_prefers_matching_same_name_column_and_filters_months():
    ref = _st("名单", ["日期", "客户编码", "客户简称2", "产品型号", "销售负责人"], [
        ["202608", "C1", "A", "M1", "张三"],
        ["202609", "C1", "A", "M1", "张三"],
        ["202609", "C1", "A", "M2", "李四"],
        ["202609", "C2", "B", "M1", "王五"],
    ])
    text = suggest(SALES, ref)
    assert "跨期间" in text
    assert "客户简称(AE列)" in text
    first = [line for line in text.splitlines() if line.startswith("  · ") and "↔" in line][0]
    assert "客户简称(AE列)" in first and "产品型号" in first and "3/3" in first and "项目" in first


def test_customer_level_table():
    ref = _st("库存", ["客户简称", "金额"], [["A", 1], ["丙", 2], ["别家", 3]])
    text = suggest(SALES, ref)
    assert "「客户」这一层" in text and "别家" in text
