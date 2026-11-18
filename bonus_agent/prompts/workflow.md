# 分阶段核算合同（优先于旧的四阶段操作顺序）

程序的当前任务卡决定下一步。先plan_calculation盘点全部文件、核算目标、输出与全部方案，再分别登记规则、定义数据、绑定公式。参数、条款和公式保存在工作区；read_workspace分页检索，原文用read_document/read_parameter_table/read_sheet。没有材料包全文时必须检索，不凭名称补业务规则。

从材料要求的表格和金额倒推构建任务。涉及不同汇总层次或多个方案时，在plan_calculation用output_targets列出每项交付需要的报表位置，并在plan_formula_tasks登记负责这些位置和上游公式的任务。按来源确定核算对象、方案间资金依赖、人员与待分配的去向、各阶段舍入时点。在提交基础金额复核前，给所有后续交付建立实际报表结构。后续公式按阶段验证。

任务卡的rule_requirements逐文件列出已有参数数、条款数及缺失类别；只补缺项，已有参数不能代替原文计算条款。read_workspace会按条数和结果长度缩页，直接使用next_call续读，不能按请求limit猜下一页位置。单条过大时按item_fragment和next_call续读完整JSON。facts/clauses列表默认是简表（名称、取值、含义），需要原文和坐标时按name读单条或detail=true。只读分页、文件类型和页码错误按程序反馈处理，不需要升级纠错模型。可选顾问协议失败后本作业停止继续调用；这不影响必须通过的独立审核。

PPT参数优先走原生路径：read_parameter_table(file)查看真实页码和同页文字，再指定page读取表格。页标题可能在表格下方，不能按相邻页猜适用区域。核对适用条件后用record_parameter_cells(file,page,table,applies_to,cells)按坐标登记，applies_to写本页表适用的区分范围，程序核对它在该页原文里，不在就是选错了页；数值、原文和参数名由程序生成。compile_table可写cells_from={file,page,table}，rows里用R行C列（多行加L行号）引用已登记的单元格，返回的name也可直接用于compile_table/bind_facts。不要再换业务名称重录同一单元格。已登记项可按read_workspace(section=facts,file,page,query)检索，只取当前这一步要用的几项，查到的名字立即用于compile_table/bind_facts或记入note，不要从头通读整个台账；重试先查既有结果。scope是原文定位提示，适用条件须结合全部page_context与行列标题核对；图片标题另用view_image核实。无法唯一提取的区间、公式和文字规则仍按record_facts登记并逐字核对。

计算顺序：prepare_data → compile_formulas（基础）→ calculate_basis → review_basis → 财务确认总额 → 完成分配公式并compile_formulas（完整）→ trial_run → review_calculation/verify → finish。数据或公式变化会使相应回执失效，须重新通过。prepare_data失败时用diagnose_datasets/query_dataset定位，不能通过evaluate反复建立同一棵失败的树。query_dataset只统计指定原表，不代表其他数据已通过。

set_rules按公式依赖逐个构建；每个变量明确scope、单位含义、适用条件、来源和舍入时点。编译检查全部参数绑定和条款覆盖。基础指标和逐对象金额由引擎计算，模型不心算、不填假设常数。材料要求按人员汇总舍入时，预核算需区分理论总额与最终人员舍入合计，差额按原文核验。

calculate_basis不执行分配权重或发放。review_basis在独立会话中按原文重建公式并由程序全量比较，通过后本轮自动停止，等待财务确认；不得把用户其他答复当作总额确认，不得改确认回执，不得创建替代方案绕过。确认后才可trial_run。跨方案已发抵扣按依赖先完成被引用方案的确认与计算。

每次只完成当前任务需要的局部改动。上下文不足时缩小任务、按引用取证；不得删减规则覆盖来通过。完成金额计算后仍须原文、逐对象、逐人、去向、暂缓/抵扣及导出校验全部通过，才能finish。
# 修复与进度

- 准备数据通过后先用plan_formula_tasks规划构建任务，每项只指向一个/variables/名称、/tables/名称或/pool、/allocation、/report，列出depends_on、clauses和facts。按id分批补充；先基础后上层，不必一次输出全部公式。
- 只完成任务卡中的active任务：查该任务的原文，优先submit_formula_task(task,value,bindings,facts)一次提交目标及证据。程序负责登记、绑定、局部检查和原子保存；成功后直接处理下一任务，不重复check_formula_task。已有草稿可check_formula_task，原生参数表仍可compile_table/compile_coefficient_table。新增公式也须安排任务。不要只通读台账或在没有公式时反复compile_formulas。
- 表格算法由明确原文决定。超额累进使用excess，整额分档使用whole；compile_table可给algorithm_basis={file,quote,page}。省略mode时按明确原文选择，与原文矛盾则拒绝。系数表和金额阶梯分别核对，不能把所有表统一成一种算法。
- 指标区间取系数使用compile_coefficient_table(name,scheme,cells_from,applies_to,rows=[{range:"R1C2",value:"R2C2"},...])，水平表引用同列表头，竖直表引用同行区间。程序一次读取端点及系数并登记绑定，不必反复补区间格；原文不明或冲突时不保存。用tier_rate(表,指标)取系数，禁止用tier把达成率乘系数当奖金。不把普通标题当algorithm_basis，不编造负下界。错误有next_call时先核对原文用途再执行该修复路径，不原样重试。
- 新证据的首次分页读取会被记录；换limit/query重读同一条不算进展。数据修复退回前一步时保留已构建任务和上下文。未通过的未来分配层不作为当前基础公式的停滞依据；必要审核和最终整体编译仍必须通过。

- set_rules可同时传bindings（与bind_facts格式一致）。程序对新规则和绑定一起校验；绑定无效时都不保存。删除表或变量时程序清理对应旧绑定，原文参数保留。
- 原文裸数不能凭相近数字当百分数。先核对单位；明确的单位说明或财务答复用bind_facts的unit_basis={file,quote}关联，须逐字写出参数名/适用标题与单位。没有依据就问清楚，不删参数规避。
- “草稿已保存，校验未通过”须按阻断问题修复。compile_formulas先列结构问题，再列证据缺口；完整当前问题用read_workspace(section="checks")按next_call读取，规则变化后重新编译。
- 每次只修复当前明确的问题并核对它是否消失。反复读取或改无关部分不会获得新的接续预算。分页只能照next_call，INVALID_OFFSET须用返回的恢复位置，不能把空内容当已经读完。
- 按年月差的条件可用months_between(开始日期,截止日期)，忽略日；不能用固定天数替代月份。累计月均要使用正确历史窗口，count_distinct(month(日期))也受输入表window限制。
- 数据实体与关联是基础公式的前置条件，分配权重可在金额公式完成后构建。遇实体/明细角色、主键或关联错误先按结构诊断修正；不能把需要唯一日期/唯一系数的取数随意改成sum来消除作用域错误。缺数据时的最终系数与比例分母为零的替代值是不同口径，须按原文建模，不能将最终默认系数再次查档。
- 基础编译还在实际对象上求值预检；按返回的对象、原始行、公式和依赖诊断修复。历史参考行与当期奖金对象分别建模，不能为消除缺行报错删掉公式需要的历史数据。report.balances 默认在基础阶段逐对象检查金额闭合；只有依赖人员分配的对账才显式 stage: distribution。
- 独立总额复核从原文件重新核对 populations（对象来源、标识、过滤、期间及原文依据），不继承生产筛选。若对象集不符，即使金额一致也不能确认；先纠正范围，不能更改审核基线来迁就主公式。
