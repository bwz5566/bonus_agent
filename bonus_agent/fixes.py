"""阻断问题的改法：按问题类别给出正确写法，附在报错后面，避免模型看不懂报错、换着花样硬凑。"""
from __future__ import annotations

from bonus_core.dsl.functions import FUNCTIONS

BUILTIN = "引擎已自动保证、不用写 checks 的：主键重复、主键为空、编号引用不存在、上级金额为正但下级权重全为 0、各级金额之和等于上级（最大余数法）。"

FIXES = {
    "name_unknown": "名字只能是数据表字段、变量或参数表名；字段名用 define_dataset 定义的英文名，不是 Excel 列名；文字取值要加双引号，如 grade == \"优\"。",
    "fn_unknown": "可用的函数：" + "、".join(sorted(FUNCTIONS)) + "。",
    "fn_args": "参数个数不对：sum(字段, of: \"表名\", where: 条件)，ratio(分子, 分母, zero: 0)，if(条件, 是, 否)。",
    "fn_kwarg": "聚合函数只支持 of（表名）、where（筛选条件）；ratio 支持 zero（分母为 0 时的取值）。",
    "cycle": "变量之间互相引用了：把公共部分拆成一个新变量，让依赖只朝一个方向。",
    "scope_bad": "scope 只能写 global、allocation 里的层级名，或关联实体表名（如 人员、项目）。",
    "scope_var": "下层变量不能在上层直接引用：在上层直接对明细表聚合（如项目层 sum(hours, of: \"工时明细\", where: ...)）；"
                 "奖金池按项目各自计提时，pool 的 scope 写项目。",
    "scope_field": "字段属于下层实体表：在本层用聚合（sum / count / avg），或把变量的 scope 写成字段所在的层。",
    "scope_aux": "关联实体（如人员、项目）的字段：在本行带有它编号的层级对象上可以直接用；否则在 scope=该实体 的变量里用，或在带编号的表的聚合里用。",
    "agg_above": "聚合的表挂在上层：本层对象聚合不到。确认这张表的 link 写对了层，或把计算放到表所在的层。",
    "agg_unrelated": "这张表的行没有本对象的编号，聚合不到属于本对象的行：换一张带编号的表，或先在被引用的实体上定义变量。",
    "detail_outside_agg": "明细表的字段只能写在聚合里面：sum(字段, of: \"明细表\", where: ...)。",
    "global_in_level": "变量没写 scope 就是全公司合计：按对象计算的要写 scope（层级名或实体表名）。",
    "parent_key_missing": "上级编号不在本层实体表里：一个对象对应上一层多条记录（如一人参与多个项目）时，最末层用那张记录表"
                          "（如 参与分配，key=参与编号、parent_key=项目编号），按人汇总抵扣用 offsets 的 scope=人员表。",
    "level_no_parent": "非最上层的层级要写 parent_key（指向上一层主键的字段）。",
    "payout_scope": "发放条件写在最末层；跨记录按人汇总再抵扣预发的，用 offsets，scope 写人员表。",
    "payout_ratio": "发放条件二选一：pay_ratio 可发比例（如 if(warranty_done == \"是\", 1, 0.8)）或 hold_ratio 暂缓比例。",
    "hold_ratio_range": "比例要在 0~1 之间：80% 写 0.8。",
    "tier_order": "阶梯 steps 是 [[下限, 比例], ...]，下限严格递增，最后一档不写上限（不要用 null 或极大数占位）。",
    "tier_empty": "阶梯 steps 至少一档：[[0, 0.05], [100000, 0.08]]。",
    "key_dup": "同一编号出现多行：数据录错了就提问财务；如果一个编号本来就对应多行（如一人参与多个项目），这张表不是这个编号的实体表，"
               "改用每行唯一的编号作 key，或写成 link 明细表。",
    "key_empty": "主键为空的行：数据缺失要提问财务；合计行、说明行用 define_dataset 的 filter 排除。",
    "link_unmatched": "编号在上级表里找不到：漏录的要提问财务；应排除的记录用 filter 排除，不要改数据。",
    "link_bad": "link 写本表里指向上级（层级主键或关联实体主键）的编号字段。",
    "check_failed": "先用 explain 看没通过的对象；数据确实有误就提问财务。数据没错时检查校验写的层级（scope）和条件是否与原文一致"
                    "（如“正奖金包须有可分配权重”是项目层的事，不是每条记录各自判断）。" + BUILTIN,
    "eval_error": "看报错里的对象和算式：常见是空聚合（count(...) == 0 时怎么办要按原文写）、文字当成条件（写成 字段 == \"是\"）。",
    "rollup_key_missing": "按人汇总需要最末层每行都有人员编号，并且在人员表里找得到。",
    "patch_field": "数据修订：补充表先按原表的字段名定义；去重字段、编号字段要两张表都有。",
    "patch_append": "补充表和原表同号内容不同：问财务以哪个为准，不要自行取舍。",
    "patch_correct": "更正表对不上（旧值不符 / 找不到 / 重复 / 改成不同的值）：问财务，不要自行取舍。",
}


def hints(issues) -> str:
    """一组阻断问题对应的改法（同类只说一次）。"""
    seen, out = set(), []
    for i in issues:
        code = getattr(i, "code", "")
        if getattr(i, "severity", "blocking") == "blocking" and code in FIXES and code not in seen:
            seen.add(code)
            out.append(f"  · {code}：{FIXES[code]}")
    return ("\n改法：\n" + "\n".join(out)) if out else ""
