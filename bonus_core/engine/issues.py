from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


@dataclass
class Issue:
    """校验或计算中发现的问题。blocking 会阻断流程，warning 允许财务确认后继续。"""
    severity: Literal["blocking", "warning"]
    code: str
    message: str
    where: str = ""

    @classmethod
    def block(cls, code, message, where=""):
        return cls("blocking", code, message, where)

    @classmethod
    def warn(cls, code, message, where=""):
        return cls("warning", code, message, where)

    def to_dict(self):
        return asdict(self)

    def __str__(self):
        tag = "阻断" if self.severity == "blocking" else "提示"
        return f"[{tag}] {self.code}: {self.message}" + (f" ({self.where})" if self.where else "")


class EngineError(Exception):
    def __init__(self, issues: list[Issue]):
        self.issues = issues
        super().__init__("\n".join(str(i) for i in issues))
