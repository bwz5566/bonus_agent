"""奖金核算引擎。"""


def _safe_excel_strings() -> None:
    """写 Excel 时去掉 Excel 不允许的控制字符（PPT 表格里的软回车 \\x0b 等），不让整次运行因此崩溃。
    软回车、换页换成换行，其余直接去掉。"""
    try:
        from openpyxl.cell import cell as _cell
    except ImportError:
        return
    if getattr(_cell.Cell.check_string, "_safe", False):
        return
    orig = _cell.Cell.check_string
    bad = _cell.ILLEGAL_CHARACTERS_RE

    def check_string(self, value):
        if isinstance(value, str) and bad.search(value):
            value = bad.sub(lambda m: "\n" if m.group() in "\x0b\x0c" else "", value)
        return orig(self, value)
    check_string._safe = True
    _cell.Cell.check_string = check_string


_safe_excel_strings()
