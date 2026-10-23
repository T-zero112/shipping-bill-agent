# 架构与 Harness

入口：`python -m billing_agent`。流程：输入/现有账本 → 格式路由 → Evidence → 提取/远端适配 → 原文与业务校验 → 主体范围内分级匹配 → 显式补丁/贷项 → JSON 契约 → 可选 SQLite 模拟事务。

## 组件

- parsers/tabular/pdf_layout/party_layout：格式、物理行和角色证据。Evidence 包含来源、位置、父记录、坐标、结构元数据和摘要。
- local_ocr/hybrid：CPU OCR 与页面图片区域补取；相同位置的相同内容去重，不按费用名称跨位置去重；冲突保留原文复核。
- temporary_url/remote_ocr：显式授权 PDF 的临时公开路径和生命周期；远端原始响应不直接视为可入账事实，原件补证后形成候选。
- extraction/recovery：LangChain Runnable 调本地模型；受控分块、成功缓存、失败限次、截断拆块。结构化行由确定性路径处理，避免重复模型提取。云模型为可选路径，恢复覆盖不同。
- normalization/business/linking/financial：费用与金额同证据绑定，跨页须显式身份一致、无日期/角色冲突；原文证明每个字段；会计主体及方向独立判断。
- engine/amendments/credits：匹配分层、授权补丁、未改字段继承、贷项原单及金额上限、重复/冲突待复核。
- contracts/reporting/state：JSON 验证、运行阶段与复核分开；无效输出不当成结果。可选本地账本事务先验证、再提交，失败回滚，重复回放不重复提交。

## 为什么保留规则和证据

模型负责有限文档语义提取，不拥有公开文件、扩大更新字段、跨单据借证或自动记账的自由权限。所有金额/名称需要原文，AR/AP 需要完整交易方向。不能确认时输出 PENDING_REVIEW，而不是编造完整字段。

## Harness 分层

1. 单元测试：数值、角色、匹配、跨页、贷项、输出契约、表格和 OCR 适配、清理/失败行为。
2. 离线 CLI：合成证据回放覆盖四操作和九运行状态；属于业务控制验证，不能用于声称模型准确率。
3. 故障注入：超时与输出截断；实际写缓存、第二轮只重试失败块。
4. 合成格式/复杂文档 CLI：实际 LibreOffice、CPU OCR 与本地模型；结果与生成的已知费用比较。
5. 公司原样本本地回归与单份指定 OCR 联调：分别记录，不把缓存、OCR HTTP 成功、候选生成等同于全字段/自动入账通过。真实数据不进入公开提交包。

## 状态与审计

SUCCESS / PENDING_REVIEW / EMPTY_RESULT 与 PARSE_FAILED / EXTRACTION_FAILED / PARTIAL_FAILURE / UNRESOLVED / OUTPUT_INVALID 等区分。看 run_report.json 的状态、错误与 output_validation；退出码 0 仍可能是待复核。

金额用 Decimal 运算；对外 JSON 是数值或 null。每条候选保留费用、原文、角色、业务字段、匹配与变更理由；远端候选额外保留原始 OCR 响应。日志可能含真实业务数据，不随包分发。
