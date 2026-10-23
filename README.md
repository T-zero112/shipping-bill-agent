# 航运账单 Agent

Python + LangChain 的证据驱动账单处理原型。输入文档，提取费用与业务事实，与旧账单匹配，输出 CREATE / UPDATE / NO_CHANGE / PENDING_REVIEW JSON。不会向真实财务系统记账。

## 本地演示界面

依照下方步骤安装依赖后，执行：

```powershell
.\.venv\Scripts\python.exe scripts/serve_demo.py
```

打开 http://127.0.0.1:8767 。填写实际记账主体，选择或拖入一个账单文件，开始提取。顶部配置后开始提取；左侧费用表格支持全部/待复核筛选，右侧通过原文证据、业务字段、原始 JSON 标签页查看详情和复核原因；支持下载结果、证据和运行报告 JSON。

“加载合成演示”不需要模型或 OCR，明确标记为回放，展示一条新增和一条缺少金额/币种的待复核候选。真实文件默认使用本地模型/解析和本地 OCR；原件、结果及日志保存在 outputs/web-demo，页面刷新后可从该目录取回，界面暂不提供历史列表/编辑/审批入账。

演示题目指定 OCR 时，请使用以下启动命令（不能只运行上面的本地模式命令）：

```powershell
.\.venv\Scripts\python.exe scripts/serve_demo.py --designated-ocr --cloudflared C:/tools/cloudflared.exe
```

请将路径替换为已安装的官方 cloudflared 可执行文件。也可设置 `BILLING_CLOUDFLARED` 环境变量或放入 PATH，再使用 `--designated-ocr`；本工作区还会查找 `outputs/remote-ocr-tunnel/cloudflared.exe`，但提交包不包含该程序。没有可用程序时启动即给出错误，不自动下载，也不静默退回本地处理。

重启后刷新网页，处理方式应出现可选的“指定 OCR + 临时 URL”。上传 PDF，选择该模式，勾选允许临时公网 URL，然后开始提取。它实际调用题目指定的 `/process` 接口，不是本地 OCR；目前前端临时 URL 模式仅支持 PDF。结果中的待复核状态须与接口调用是否成功分别判断。启动服务本身不会公开文件，隧道只在具体任务中创建，处理结束自动关闭。

默认不开放此模式。服务只绑定 127.0.0.1，单任务执行，有文件类型/30MB 限制、会话令牌和同源检查；它是本机演示工具，不是多人生产服务。停止前等待任务结束，再 Ctrl+C。

界面采用一屏工作台：整页不滚动，费用表、复核原因和详情在面板内滚动，下载按钮保持可见；运行报告使用独立弹窗。窄屏上下分区，长内容仍可在各自区域内查看。

界面验收见 WEB_DEMO_REPORT.md；无额外前端安装或构建步骤。

## 最短复现（Windows PowerShell）

已验证 Python 3.12 x64 / Windows；其他操作系统尚未完整复现。在解压后的项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe scripts/check_environment.py --ocr-smoke
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/run_offline_smoke.py
```

无需激活虚拟环境。锁文件记录验证过的版本；requirements.txt 是兼容范围，不保证将来解析出的所有组合都已测试。可改用 `pip install -r requirements.txt`，但应重新运行测试。下载 Python 包需要网络；可以复用 pip wheel 缓存。

离线演示由合成文本、显式候选回放和合成旧账单生成四种操作；不是模型提取准确率验证。结果在 outputs/offline-smoke，各运行同时输出 result.json、run_report.json、evidence.json 和结果 schema。

## 可选外部依赖

| 功能 | 需要 | 缺失时 |
|---|---|---|
| 离线测试、回放 | 上述 Python 依赖 | 安装后即可运行 |
| 本地图片 / 扫描 PDF OCR | RapidOCR/ONNX 和 PDFium，随 Python 依赖安装 | 明确报告失败，不能声称无费用 |
| DOC 转 DOCX、生成 XLS 测试夹具 | 已安装 LibreOffice；可设 BILLING_SOFFICE 为 soffice.exe 路径 | 明确提示，不自动安装 |
| 本地模型提取 | Ollama 服务、qwen3:8b 模型 | 请求失败被记录，不自动使用云模型 |
| 指定 OCR + 自动公网 URL | 本地 cloudflared 便携程序、联网、指定服务可用 | 前置校验/调用失败，自动清理临时进程 |
| 云模型（可选） | 显式环境变量和自有凭据 | 默认不启用，不读取其他项目配置 |

.env.example 是示例，CLI 不自动读取 .env。不要将密钥或真实文档放入公开提交包。没有绑定到本项目的 API 密钥、模型权重、LibreOffice、cloudflared 二进制或安装器。

## 本地模型与多格式输入

先按 Ollama 官方方式安装服务并获取 qwen3:8b；模型需要单独下载，可能有显存/内存要求。启动 Ollama 后执行：

```powershell
.\.venv\Scripts\python.exe scripts/check_environment.py --check-ollama --ocr-smoke
.\.venv\Scripts\python.exe scripts/create_financial_demo.py
.\.venv\Scripts\python.exe scripts/run_local.py --input outputs/financial/synthetic/invoice.txt --entity 'Beta LLC' --extractor llm --pdf-mode local --image-mode local --output outputs/model-demo
```

`run_local.py` 只用本机环回地址的 Ollama，不加载云端凭据。服务默认 http://127.0.0.1:11434，模型默认 qwen3:8b；temperature=0、seed=42、8192 context、4096 output、think=false。`--extractor llm` 通过 LangChain Runnable 调用模型；确定性列/角色/校验规则控制费用归属和修改边界。

普通目录/EML 使用同一入口；EML 递归解析正文及附件，按实际文件格式路由。默认 PDF 和图片走指定 OCR，需明确提供 URL 或响应缓存；仅本地运行时显式加 `--pdf-mode local --image-mode local`。本地解析/本地 OCR 不等于指定接口验收。

`--existing old-bills.json` 接收标准结果结构的数组，建议每条有唯一 id。`--entity` 必填，必须是实际记账主体；不猜缩写别名。`--entity-aliases aliases.json` 接收人工确认的主体到别名映射。

## 指定 OCR 与临时 URL（显式公开文件）

使用官方 cloudflare/cloudflared 发布的对应平台便携程序，校验发布摘要后放在本机路径。它不随包分发，不安装系统服务。测试时仅所选 PDF 通过随机路径公开；没有目录列表，不暴露模型或其他本机服务。URL 依赖本机进程/网络，运行结束自动关闭，无生产 SLA。

```powershell
.\.venv\Scripts\python.exe -m billing_agent --input 'C:/path/to/authorized-bill.pdf' --entity 'Your Company' --pdf-mode ocr --extractor remote --temporary-url-file 'C:/path/to/authorized-bill.pdf' --cloudflared 'C:/tools/cloudflared.exe' --output outputs/remote-run
```

`--temporary-url-file` 按内容摘要建立公开白名单，可重复；附件不会全部自动发布。调用前多次下载校验文件字节，之后调用指定接口并复验。超时/异常也清理。自动 URL 当前仅支持 PDF。

也支持 `--url-map urls.json`（SHA256 → 可下载 URL）或 `--ocr-cache cache.json`（SHA256 → 原始 OCR 响应）；缓存优先。`--extractor remote` 只提取远端适配证据；混合格式用 `--extractor llm`，适配费用不再重复送模型。

当前远端适配只对单页 PDF + 单条目做原件精确补证：同名金额集合对应或聚合和唯一一致时恢复原始物理行；币种和单号需要原文明确证据。多页 page_index 语义不明、不唯一拆分或字段冲突，保留待复核。运输角色、单据角色与财务方向分别校验，不能把 shipper/consignee 当作收付款双方。

## 输出与验收

- result.json 是候选业务输出；run_report.json 给出运行状态、错误和计数。
- PENDING_REVIEW 或空数组不代表自动成功或无账单；退出码 0 不代表允许入账。失败/未解决远端 OCR/输出不合法返回 2。
- 保留原始证据、来源位置、金额/名称原文、角色证明、字段补丁、匹配理由和原始 OCR 响应审计。
- 仅明示 amount / currency / payment_term 修改受支持；其他字段修改未通用实现，不扩大授权。未请求字段保持原样。
- `--state-db outputs/demo.sqlite` 是可选本地事务模拟账本，支持幂等与回放，绝非生产系统。
- `--extraction-cache outputs/model-cache` 启用模型分块成功缓存；限次重试，截断拆块，保留失败块，缓存命中不算新模型调用。

功能覆盖、证据层级和边界见 REQUIREMENTS_COVERAGE.md；环境实测见 REPRODUCTION_REPORT.md；设计见 ARCHITECTURE.md。

## Harness 命令

```powershell
.\.venv\Scripts\python.exe scripts/run_status_regression.py
.\.venv\Scripts\python.exe scripts/run_recovery_faults.py
.\.venv\Scripts\python.exe scripts/run_business_regression.py
# 以下需要本地模型；格式回归还需要 LibreOffice
.\.venv\Scripts\python.exe scripts/run_format_regression.py
.\.venv\Scripts\python.exe scripts/run_complex_regression.py
.\.venv\Scripts\python.exe scripts/run_linking_regression.py
.\.venv\Scripts\python.exe scripts/run_credit_regression.py
.\.venv\Scripts\python.exe scripts/run_amendment_regression.py
```

测试数据均为合成夹具或动态生成，不含公司原始 PDF、邮件、图片和账本。题目真实样本及其响应日志不随包分发。完整真实样本历史验证结果只作分项证据，不冒充全量财务验收。

## 验收第九条：五点以上补充思考

正式回答见 [业务与技术补充思考](DESIGN_REFLECTIONS.md)，共八点：交易角色与 AR/AP、字段修改授权、费用去重上下文、OCR 校验、Credit Note 原单与冲销边界、证据追溯、Harness 恢复与验证、分层验收。该文区分设计建议和已验证实现，不将建议表述为生产能力。

## 指定 OCR 图片验证（CLI）

图片可使用 CLI 的 `--image-mode remote --extractor remote`，同时传入 `--temporary-url-file 图片路径 --cloudflared 程序路径 --input 图片路径 --entity 当前主体 --output 输出目录`。临时托管按文件签名支持 PNG/JPEG/TIFF 和 PDF，逐文件摘要授权、下载字节校验、限次重试与退出清理与 PDF 相同。前端的临时 URL 入口仍仅支持 PDF，图片验证通过 CLI 运行；接口是否能识别某张图片须以实际响应为准。十项验收记录见 ACCEPTANCE_RECORD.md。
