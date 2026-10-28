"""分配树的精确分配、锁定重分、保留到分（最大余数法）。

已确认的业务规则：
- 调整后总额不变；
- 被调整的节点锁定为调整值，同级未锁定节点按「原始权重」分剩余金额；
- 多次调整时，之前调整过的节点保持锁定；
- 取整用最大余数法：先向下取整到分，剩余的分按余数从大到小每人补 1 分，余数相同按编号升序。
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


def allocate(node: Node, target: Decimal, exact: Decimal | None = None) -> None:
    """自上而下逐层分配到分。target 是本节点已取整到分的金额（奖金池先四舍五入到分），锁定值写在 node.lock 上。

    每层：已锁定的下级取锁定值，剩余金额按未锁定下级的权重占比算出理论值（exact），
    向下取整到分，剩余的分按余数从大到小每人补 1 分（余数相同按编号升序），保证下级之和严格等于上级。
    锁定是否超额按本节点取整后的金额判断，与显示给用户的金额一致。
    """
    node.final = target
    node.exact = target if exact is None else exact
    if not node.children:
        return
    locked = [c for c in node.children if c.locked]
    free = [c for c in node.children if not c.locked]
    rest = target - sum((c.lock for c in locked), Decimal(0))
    if rest < 0:
        raise AdjustConflict(node.id, f"已锁定的下级合计超过本节点金额 {target}，剩余为 {rest}")
    if not free and rest != 0:
        raise AdjustConflict(node.id, "同级已全部锁定，差额无人吸收，请先撤销其中一个调整")
    wsum = sum((c.weight for c in free), Decimal(0))
    if free and wsum == 0 and rest != 0:
        raise AdjustConflict(node.id, "未锁定成员的权重之和为 0，无法分配剩余金额")
    exacts = [rest * c.weight / wsum if wsum else Decimal(0) for c in free]
    floor = [e.quantize(CENT, ROUND_DOWN) for e in exacts]
    n = int((rest - sum(floor, Decimal(0))) / CENT)
    order = sorted(range(len(free)), key=lambda i: (-(exacts[i] - floor[i]), free[i].id))
    for i in order[:n]:
        floor[i] += CENT
    for c in locked:
        allocate(c, c.lock)  # 锁定值本身就是分的整数倍
    for c, f, e in zip(free, floor, exacts):
        allocate(c, f, e)


def round_pool(pool: Decimal) -> Decimal:
    return pool.quantize(CENT, ROUND_HALF_UP)


def max_allowed(node: Node) -> Decimal:
    """在当前锁定状态下，该节点最多能调到多少（父节点金额 - 其他已锁定同级之和）。"""
    p = node.parent
    others = sum((c.lock for c in p.children if c.locked and c is not node), Decimal(0))
    return p.final - others
