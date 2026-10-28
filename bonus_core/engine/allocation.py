"""分配树的精确分配、锁定重分、保留到分（最大余数法）。

已确认的业务规则：
- 调整后总额不变；
- 被调整的节点锁定为调整值，同级未锁定节点按「原始权重」分剩余金额；
- 多次调整时，之前调整过的节点保持锁定。
"""
from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from bonus_core.engine.tree import Node

CENT = Decimal("0.01")


class AdjustConflict(ValueError):
    def __init__(self, node_id: str, message: str, max_allowed: Decimal | None = None):
        self.node_id = node_id
        self.max_allowed = max_allowed
        super().__init__(f"{node_id}: {message}")


def allocate(node: Node, amount: Decimal) -> None:
    """自上而下精确分配（不取整）。锁定值写在 node.lock 上。"""
    node.exact = amount
    if not node.children:
        return
    locked = [c for c in node.children if c.locked]
    free = [c for c in node.children if not c.locked]
    rest = amount - sum((c.lock for c in locked), Decimal(0))
    if rest < 0:
        raise AdjustConflict(node.id, f"已锁定的下级合计超过本节点金额 {amount}，剩余为 {rest}")
    if not free and rest != 0:
        raise AdjustConflict(node.id, "同级已全部锁定，差额无人吸收，请先撤销其中一个调整")
    wsum = sum((c.weight for c in free), Decimal(0))
    if free and wsum == 0:
        if rest != 0:
            raise AdjustConflict(node.id, "未锁定成员的权重之和为 0，无法分配剩余金额")
        for c in free:
            allocate(c, Decimal(0))
    for c in locked:
        allocate(c, c.lock)
    for c in free:
        if wsum:
            allocate(c, rest * c.weight / wsum)


def round_tree(node: Node, target: Decimal) -> None:
    """保留到分，并用最大余数法保证每层下级之和严格等于上级。"""
    node.final = target
    if not node.children:
        return
    locked = [c for c in node.children if c.locked]
    free = [c for c in node.children if not c.locked]
    for c in locked:
        round_tree(c, c.lock)  # 锁定值本身就是分的整数倍
    rest = target - sum((c.lock for c in locked), Decimal(0))
    total = sum((c.exact for c in free), Decimal(0))
    exact = [c.exact * rest / total if total else Decimal(0) for c in free]
    floor = [e.quantize(CENT, ROUND_DOWN) for e in exact]
    n = int((rest - sum(floor, Decimal(0))) / CENT)
    if free and n < 0:
        raise AdjustConflict(node.id, "取整后剩余金额为负")
    order = sorted(range(len(free)), key=lambda i: (exact[i] - floor[i], free[i].id), reverse=True)
    for i in order[:n]:
        floor[i] += CENT
    for c, f in zip(free, floor):
        round_tree(c, f)


def round_pool(pool: Decimal) -> Decimal:
    return pool.quantize(CENT, ROUND_HALF_UP)


def max_allowed(node: Node) -> Decimal:
    """在当前锁定状态下，该节点最多能调到多少（父节点精确金额 - 其他已锁定同级之和）。"""
    p = node.parent
    others = sum((c.lock for c in p.children if c.locked and c is not node), Decimal(0))
    return (p.exact - others).quantize(CENT, ROUND_DOWN)
