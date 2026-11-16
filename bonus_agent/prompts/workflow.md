# 分阶段核算合同（优先于旧的四阶段操作顺序）

程序的当前任务卡决定下一步。先plan_calculation盘点全部文件、核算目标、输出与全部方案，再分别登记规则、定义数据、绑定公式。参数、条款和公式保存在工作区；read_workspace分页检索，原文用read_document/read_parameter_table/read_sheet。没有材料包全文时必须检索，不凭名称补业务规则。

计算顺序：prepare_data → compile_formulas → calculate_basis → review_basis → 财务确认总额 → trial_run → review_calculation/verify → finish。数据或公式变化会使相应回执失效，须重新通过。prepare_data失败时用diagnose_datasets/query_dataset定位，不能通过evaluate反复建立同一棵失败的树。query_dataset只统计指定原表，不代表其他数据已通过。

set_rules按公式依赖逐个构建；每个变量明确scope、单位含义、适用条件、来源和舍入时点。编译检查全部参数绑定和条款覆盖。基础指标和逐对象金额由引擎计算，模型不心算、不填假设常数。材料要求按人员汇总舍入时，预核算需区分理论总额与最终人员舍入合计，差额按原文核验。

calculate_basis不执行分配权重或发放。review_basis在独立会话中按原文重建公式并由程序全量比较，通过后本轮自动停止，等待财务确认；不得把用户其他答复当作总额确认，不得改确认回执，不得创建替代方案绕过。确认后才可trial_run。跨方案已发抵扣按依赖先完成被引用方案的确认与计算。

每次只完成当前任务需要的局部改动。上下文不足时缩小任务、按引用取证；不得删减规则覆盖来通过。完成金额计算后仍须原文、逐对象、逐人、去向、暂缓/抵扣及导出校验全部通过，才能finish。
