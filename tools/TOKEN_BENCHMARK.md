# 三渠道 Token 对比试验

比较 `cline`、`opencode`、`commandcode` 三个 Key 在 `deepseek-v4.1-flash`、`deepseek-v4-pro` 上的质量、速度和用量。默认连接本机 `http://127.0.0.1:8080/v1`，直接请求本机服务，禁用环境中的 HTTP 代理及重定向。

**当前只准备脚本和只读核对。必须等用户明确说“开始测试”后，才能使用 `--run` 发起真实请求。** 普通预览不访问 Docker、数据库或 HTTP 接口；只有显式使用 `--check-system-keys` 时，才只读核对本机容器与数据库中的 Key 元数据，不调用模型。

## 使用系统已有 Key

需要 Python 3.10+，Python 部分仅使用标准库；读取本机系统 Key 另需 Docker 命令及访问本机服务、PostgreSQL 容器的权限。在仓库根目录先离线预览：

```bash
python3 tools/token_benchmark.py --system-keys --base-url http://127.0.0.1:8080/v1
```

默认 3 轮，每轮 8 题，全部 6 个渠道/模型组合各答一次，共 **144 次推理请求**。每个组合得到 24 次样本。每次请求统一 `max_tokens=4096`，所有请求的上限合计 **589,824**；这是输出预算参数之和，不是预计消耗，也不包括输入 Token，上游是否将推理 Token 计入此限制取决于接口实现。

直接使用系统里已经配置的三个 Key 及其当前分组，**不需要创建 `.env`，不需要复制或粘贴完整 Key**。脚本不会创建 Key、重新分组或修改系统配置。实际 API 请求仍以这三个普通 Key 鉴权，不使用管理员身份。

已通过本机数据库只读查询核对以下三条配置（未调用模型）：

| Key 名称 | 当前分组 | 掩码 |
| --- | --- | --- |
| Cline | Test | `sk-fcf...cafd` |
| commandcode | cc | `sk-c58...bbeb` |
| OpenCode | OpenCode | `sk-d01...6c97` |

开始测试之前，可用以下命令只读核对名称、分组及掩码：

```bash
python3 tools/token_benchmark.py --check-system-keys --base-url http://127.0.0.1:8080/v1
```

该模式核实服务容器发布了配置的本机 8080 端口，数据库容器与服务位于同一 Docker 网络，并严格匹配截图中的三个 Key 的名称和固定前后缀；还检查 Key、用户、分组状态及重名歧义。不匹配就停止，不任选其他 Key。默认容器名为 `sub2api` 和 `sub2api-postgres`，部署名称不同时可用 `--service-container`、`--database-container` 显式指定。

`--check-system-keys` 仅显示脱敏元数据，不读取完整 Key；`--system-keys` 只有同时指定 `--run` 后才为调用读取完整 Key，完整值保留在内存，不写入结果文件或日志。两种系统模式均不能与 `--env-file` 混用，`--check-system-keys` 也不能与 `--run` 混用。普通 `--system-keys` 预览不会访问 Docker、数据库或 HTTP。

以下自动检查和预览全部离线，可在开始真实测试之前执行：

```bash
python3 -m unittest discover -s tools -p 'test_token_benchmark*.py' -v
python3 tools/token_benchmark.py --system-keys --base-url http://127.0.0.1:8080/v1 --rounds 3
```

## 可选：无法访问本机 Docker 时手动提供 Key

当前本机部署优先使用上面的系统读取方式。只有运行环境无法访问本机 Docker 时，才改用本地环境文件；截图中的 Key 不完整，不要把完整 Key 发到聊天或写进脚本：

```bash
cp tools/token_benchmark.env.example tools/token_benchmark.env
chmod 600 tools/token_benchmark.env
```

编辑 `tools/token_benchmark.env`：

```dotenv
BENCH_BASE_URL=http://127.0.0.1:8080/v1
CLINE_API_KEY=填入完整Key
OPENCODE_API_KEY=填入完整Key
COMMANDCODE_API_KEY=填入完整Key
```

文件按字面读取，不执行 shell 内容；已存在的环境变量优先于文件。同名环境变量若曾指向域名，可用 `--base-url http://127.0.0.1:8080/v1` 明确覆盖。三个 Key 必须不同。`.env` 文件和 `output/` 均被 Git 忽略；结果目录权限设为 `700`，程序不打印 Key，并清理错误及响应中回显的完整 Key。

## 试验逻辑

每轮根据固定种子 `20261109` 产生新数据与试题标记；同一轮同一题的系统提示词、用户提示词和请求参数在六个组合之间完全相同。请求串行执行，轮换组合顺序，并在隔轮反转顺序，以分散先后次序的影响；默认间隔 0.5 秒，`temperature=0`，SSE 流式返回，用量通过 `stream_options.include_usage=true` 请求。固定种子复现的是题目和顺序，模型输出仍可能变化。

脚本沿用各渠道默认的 thinking/reasoning 设置，没有统一 `reasoning_effort` 等推理参数；`manifest.json` 将其记录为 `channel_default_uncontrolled`。因此试验衡量当前配置下各渠道的整体表现，推理模式差异也可能影响质量、速度及 Token 数，不能当作严格控制底层模型与推理模式的实验。统一 4,096 的输出上限为推理留出一定空间，覆盖题库模块自带的各题默认值；若仍发生截断，应先查看原因，经用户同意开始后，可用 `--max-tokens 8192` 对六个组合一起重测，避免仅给某个渠道增加预算。

| 题目 ID | 检查内容 |
| --- | --- |
| `arithmetic` | 分步金额计算、取整和门槛规则 |
| `constraints` | 六人排序的唯一解约束推理 |
| `extraction` | 订单筛选、金额转换、空值与资料内干扰指令 |
| `instructions` | 多步字符串筛选、顺序和大小写规则 |
| `multilingual` | 德语、西班牙语、日语中的事实、否定与改期 |
| `code_reasoning` | 静态理解 Python 缺陷及选择修复 |
| `long_context` | 数千字资料中的有效记录和版本检索 |
| `sustained_output` | 完整输出 80 项结构化数据并逐项检查 |

质量由本地确定性答案评分，不调用裁判模型，不执行模型生成的代码。最终答案必须是单个严格 JSON 对象；对象键顺序和空白不限，数组顺序、字段类型和值必须符合要求。JSON 格式错误、重复键、非有限数值记零分；合法 JSON 按正确叶子字段比例给部分分，缺字段、多字段、类型或数组长度不符时再乘 0.9。完整通过要求值与结构都正确。调用失败或达到输出上限 `finish_reason=length` 记零分，并单独记录失败、截断和成功响应得分。

这 8 类小题只反映本套试验内的正确性与稳定性，不能代表通用智能、完整编程能力或复杂真实项目表现。每个组合默认只有 24 个样本，p95 仅作描述，不宜据此给出统计显著性结论。

## 速度、Token 与费用如何统计

- **速度**：记录首个推理或正文片段的等待时间、首个正文片段的等待时间、推理首片段时间、总耗时；汇总首片段、正文首片段和总耗时的 p50/p95。SSE 片段可能包含多个 Token，首片段时间是可观测的 TTFT 近似值。
- **吞吐**：主要看“返回的输出 Token ÷ 总耗时”，包括等待时间；另保存“输出 Token ÷ 首末生成片段时间差”的近似生成速度。片段缓冲、推理 Token 口径及网络传输会影响后者。正文字符/秒也会保存，但不等同于 Token/秒。
- **Token**：保留原始 `usage`，提取输入、输出、总量、缓存命中和推理 Token，并标明多少条样本有计数。上游未返回的字段标为未知；不能把缺失当作零，也不使用字符数伪装为精确 Token 数。
- **费用**：对每个 Key 的本机 `/v1/usage` 做开始和结束快照，结束后默认最多等待 10 秒让账单入库，计算 `cost`（标准费用）和 `actual_cost`（实际扣费）的差额，也保留模型分项。不是按官网单价估算。只有实验期间该 Key 无其他调用、账单入库且请求数对齐时，才可将差额归因于本试验；计数缺失、失败或无法对账时保留“未对账/未知”。请求数吻合也不能排除并发调用与延迟入账刚好抵消。

报告分别展示六个组合，并按相同模型、轮次与题目将 `opencode`、`commandcode` 与 `cline` 配对，给出质量差、耗时比和输出 Token 比。速度汇总基于成功响应，用量保留失败请求已返回的计数；失败率及截断率需与速度一起看。

每轮新标记减少重复题目造成的缓存影响，但无法强制清除上游缓存。六个组合收到相同题目，仍可能共享缓存；不同 Key 也可能走到不同上游账号或队列。请结合缓存字段及服务端路由记录解释结果。走 localhost 仅减少客户端到网关的外部网络路径，网关到模型上游的延迟仍在总耗时内。

## 模型名称核查

真实执行时先用三个 Key 分别读取 `/v1/models`，只有两个**精确模型 ID** 都存在才发送推理请求。`deepseek-v4.1-flash` 不会自动替换为 `deepseek-v4-flash`；`deepseek-v4-pro` 也不做静默替换。若接口列出不同别名，先核实再编辑 `tools/token_benchmark.models.example.json` 的本地副本，并用 `--model-map 文件路径` 显式指定各渠道的请求 ID。一个渠道的两个模型不能映射到同一个 ID。

脚本保留目标模型名、实际请求的模型 ID 和响应中的 `model`。网关可能改写响应模型名；名称一致并不能证明底层模型身份，需要服务端证据核对。

## 等用户说“开始测试”后才执行

以下命令会产生真实模型调用及计费，当前不要执行：

```bash
python3 tools/token_benchmark.py --system-keys --base-url http://127.0.0.1:8080/v1 --rounds 3 --run
```

需要更多轮次时改为 `--rounds 5`：240 次推理请求，默认 `max_tokens` 合计 983,040。可用 `--cases arithmetic,constraints` 只选指定题目；用 `--seed` 固定另一套数据；用 `--max-tokens` 统一调整各题输出预算；用 `--timeout` 调整连接空闲超时及流式总时限（默认 180 秒）；用 `--billing-wait` 延长结算等待。脚本不自动重试，避免额外花费及样本偏差；遇到 401/402/403 或连续六次失败时停止。Ctrl+C 会保留已完成结果并尝试结算。

默认输出到 `output/token-benchmark/上海时间戳/`。也可用 `--output` 指定一个尚不存在的目录，避免覆盖历史结果：

- `report.md`：可读的结果与限制说明。
- `summary.json`：整体、分题、配对统计和账单差额。
- `results.csv`：便于用表格工具查看的逐次主要指标。
- `results.jsonl`：每次调用的正文、推理、耗时、评分、原始用量和错误。
- `manifest.json`：种子、参数、题目、标准答案及执行顺序；标准答案不会发送给模型。
- `models.json`、`billing_before.json`、`billing_after.json`：模型列表与用量快照；预检失败另有 `preflight_error.json`。

结果中包含完整回复和试题，请按本地实验资料保管。脚本、离线测试和题库就绪不代表已经取得任何真实渠道测试结果。
