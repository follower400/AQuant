# 项目名称：AI辅助量化决策系统

## 0. 当前进度（每完成一个 Phase 更新一次）

> 最近更新：2026-08-25（P4 策略重构完成：①PE/PB 分位可选化 ②推荐池持久化与熊市减仓建议 ③熊市切换金融股简化筛选 ④AI 解读层临时停用（只输出纯筛选报告）；全量 273 个单元测试通过；Ubuntu Crontab 部署待实施）

| 阶段 | 任务 | 状态 |
| :--- | :--- | :--- |
| 前置 | 需求分析与策略 PRD 定稿（v2.0，含参数管理约定） | ✅ 已完成 |
| 前置 | 工程结构搭建与分层架构规划 | ✅ 已完成 |
| 前置 | 阿里云百炼 API 连通性验证 | ✅ 已完成 |
| 前置 | 文档配套：requirements.txt / .gitignore 完善 | ✅ 已完成 |
| Phase 0 | `settings.yaml` 参数总表（PRD 全部阈值提取） | ✅ 已完成 |
| Phase 0 | `config.py` 加载与校验逻辑（类型/范围/交叉校验） | ✅ 已完成 |
| Phase 0 | SQLite 本地缓存模块（AKShare 降级读取） | ✅ 已完成 |
| Phase 1 | 数据层 `src/data_layer.py`（AKShare 封装 + SQLite 缓存，沪深300/个股） | ✅ 已完成 |
| Phase 1 | 因子库 `src/indicators.py`（MA/MACD/RSI/年化波动率/回撤，Decimal 精度） | ✅ 已完成 |
| Phase 1 | Pytest 单元测试（tests/，180 个用例全部通过，含真实数据链路验证） | ✅ 已完成 |
| Phase 2 | 策略信号逻辑（market_regime / stock_screener / position_sizing） | ✅ 已完成 |
| Phase 2 | 估值历史分位（valuation_percentile + AKShare 备用数据源 + 脏数据净化） | ✅ 已完成 |
| Phase 2 | 冒烟测试（P1+P2 主链路 smoke 标记，pytest.ini 默认跳过） | ✅ 已完成 |
| Phase 3 | AI 解读层（ai_layer.py：Prompt 去幻觉 + 分层代理池 + Pydantic-AI 校验 + 配置开关 + 数据健康检查） | ✅ 已完成 |
| Phase 3 | AI 层单元测试（test_config_ai.py + test_ai_layer.py，41 个用例） | ✅ 已完成 |
| Phase 4 | 通知层 `src/notifier.py`（PushPlus 推送 + 控制台降级 + 三种消息格式化） | ✅ 已完成 |
| Phase 4 | 主入口 `main.py`（L0→L5 全链路编排 + --dry-run / --stock-pool） | ✅ 已完成 |
| Phase 4 | AI 层增强（分批调用 + 指标快照回填 + PE 降级窗口） | ✅ 已完成 |
| Phase 4 | 股票池快照降级链（tools/fetch_pool_snapshot.py + src/pool_snapshot.py） | ✅ 已完成 |
| Phase 4 | 单元测试（test_notifier.py + test_main.py + test_pool_snapshot.py，全量 233 用例通过） | ✅ 已完成 |
| Phase 4 | P4 策略重构（2026-08）：PE/PB 分位可选化 + 推荐池持久化（熊市减仓建议）+ 熊市切换金融股简化筛选 + AI 解读层临时停用，全量 273 用例通过 | ✅ 已完成 |
| Phase 4 | 快照抓取工具数据源三级降级链（申万主源 → Tushare 备源 → 东财兜底），缺省白名单纳入银行/非银金融 | ✅ 已完成 |
| Phase 4 | Ubuntu Crontab 部署（推 GitHub → VM 拉取 → crontab） | ⏳ 未开始 |
| 待办 | 金融股选股补齐（股息率/ROE/负债率，需先接财务数据源；PB 分位简化版已实现）；AI 解读层恢复评估 | ⏳ 未开始 |
| Phase 5 | 本地回测闭环（vectorbt 调参验证） | ⏳ 未开始 |

## 1. 项目核心定位（最重要：划清边界）
- **定性**：这是一个“AI 智能投研助手”与“策略信号生成器”，**不涉及任何券商 API 对接与自动下单**。
- **决策权**：AI 仅提供“买入/卖出/观望”的建议评分和解读，最终下单动作由人工在手机/电脑端手动执行。
- **目标市场**：A股（沪深两市）。

## 2. 技术选型与角色分工（明确谁干什么）
| 功能模块 | 选用工具 | 理由与职责 |
| :--- | :--- | :--- |
| **历史回测与因子验证** | **聚宽（JoinQuant）** | 仅用于验证 PRD 中的选股条件是否在历史上有效。**职责**：跑通 MA60、RSI、PB分位等因子的回测逻辑，输出回测报告。 |
| **实时行情与数据源** | **AKShare**（本地 Python） | 盘中实时获取最新价格、成交量、估值分位。不用聚宽实盘接口（免会员费）。 |
| **AI 大模型底座** | **阿里云百炼（通义千问/Qwen）** | 利用免费额度。**职责**：读取筛选出的结构化数据，生成自然语言分析报告和风险提示。 |
| **任务调度与执行** | **VMware Ubuntu + Crontab** | 每日定时（如 9:40，14:40）唤醒脚本。 |
| **消息推送** | **PushPlus/Server酱** | 将 AI 分析结果推送到微信。 |
| **代码版本控制** | **Git + 本地仓库** | 遵循已建立的 Git 工作流。 |

## 3. 系统架构分层（严格遵循 .coderule 的解耦要求）
- **Layer 0 (配置层)**：`settings.yaml` 集中承载 PRD v2.0 的全部策略阈值；`config.py` 负责加载、类型转换与合法性校验，启动时校验失败立即报错退出，禁止使用未经验证的默认值。
- **Layer 1 (数据层)**：负责调用 AKShare，清洗数据，缓存历史 K 线至本地 SQLite；网络异常时自动降级读取缓存并告警。
- **Layer 2 (因子计算层)**：纯数学计算（MA、MACD、RSI、PB分位）。**此层严禁调用 AI**，必须用 Decimal 处理，参数一律从 Layer 0 读取。
- **Layer 3 (策略信号层)**：根据 PRD 中的“宏观判定”、“科技股买入规则”、“金融股逢低布局”编写硬编码逻辑（If-Else），所有阈值引用 Layer 0。
- **Layer 4 (AI 解读层)**：仅接收 Layer 3 筛选出的“候选池”，调用百炼 API 生成最终点评；输出必须通过 JSON Schema 校验，失败则降级发送纯量化信号。
- **Layer 5 (通知层)**：格式化 Markdown 消息并推送。

## 4. 任务拆解与 Milestone（分阶段执行）
- **Phase 0：配置系统与数据缓存（预计 1 天）**
  - 编写 `settings.yaml` 参数总表：提取 PRD v2.0 全部阈值（MA/MACD/RSI 周期、止损 -8%、分批仓位比例、连跌天数、累计跌幅阈值、仓位上限等）。
  - 实现 `config.py` 的加载与校验逻辑（必填项、类型、取值范围校验），配套 pytest 单元测试。
  - 搭建 SQLite 本地缓存模块，支持 AKShare 异常时的缓存降级读取。
- **Phase 1：数据基建与因子库（预计 2 天）**
  - 封装 `AKShare` 获取沪深 300 和行业 ETF 数据。
  - 编写 `indicators.py`，实现 MA、MACD、RSI、最大回撤、年化波动率、PB/PE 分位函数（参数从 Layer 0 读取）。
- **Phase 2：策略逻辑工程化（预计 3 天）** — 实际耗时约 1.5 天
  - 实现 `market_regime.py`（判定景气/低落/减仓触发）。
  - 实现 `stock_screener.py`（按 1-20 元、行业白名单、回撤、波动率区间初筛）。
  - 实现 `position_sizing.py`（计算 30%/30%/40% 触发条件与仓位上限校验）。
  - 新增 `valuation_percentile`（估值分位函数）及百度股市通接口适配。
  - data_layer.py 个股 K 线新增新浪备用源（东财源反爬风险自动降级）。
  - P2 冒烟测试发现真实前复权脏数据导致除零崩溃，已在 data_layer 层实施双端净化（写侧过滤 + 读侧兜底），indicators.py 增加非正价防御逻辑。
  - 编写 pytest.ini 注册冒烟标记，日常回归保持离线快速运行。
- **Phase 3：AI 提示词工程（预计 1 天）** — 实际耗时约 0.5 天
  - 设计结构化 Prompt（去幻觉：仅基于传入的结构化量化指标，禁止引入实时市场情绪）。
  - 实现分层代理池：主力 qwen3.8-max → deepseek-v4-pro-0813 → kimi-k3 → 无 AI 降级。
  - 集成 pydantic-ai v2（output_type=StockAnalysis 自动 JSON 提取 + Schema 校验）。
  - 批量调用：5 只股票一次 API 调用（节省 ~47% Token 成本）。
  - settings.yaml 新增 ai 节（enable_ai_analysis 开关 + 模型池 + 超时参数）。
  - config.py 新增 _STR 校验类型、enable_ai_analysis 属性。
  - 数据健康检查函数（check_data_health）：空 DataFrame 提前拦截，防止下游崩溃。
  - 全部失败时降级为纯量化信号（Markdown 格式），不阻塞主流程。
- **Phase 4：端到端联调（预计 1 天）** — Windows 侧实际耗时约 1.5 天（含多轮缺陷修复）
  - 实现 `src/notifier.py`（Layer 5：PushPlus 推送 + Token 缺失/推送失败全兜底降级控制台）。
  - 实现 `main.py` 主入口（L0→L5 编排、--dry-run / --stock-pool 参数、异常告警推送）。
  - 修复 AI 连通问题（.env 缺 https 前缀、qwen 思考模式与 tool_choice 冲突）。
  - AI 分批调用（max_batch_size=5 + 超时 60s，12 只股票 3 分钟 → 33 秒）。
  - PE 分位降级窗口（5→3→2 年）与缺失原因日志。
  - 指标快照回填（StockCandidate 携带 MA/RSI/MACD 等 10 字段供 AI 引用）。
  - 股票池三级降级链：静态快照 → 手工池 → 内置小池（东财成分股接口反爬应对）。
  - 单元测试 233 个全部通过；端到端验证 12 只手工池全流程成功。
  - 待办：推送 GitHub 后在 Ubuntu VM 配置 crontab（9:40 / 14:40）。
- **Phase 4+：策略重构（P4 联调后，2026-08）** — 背景：熊市触发科技股清仓后初筛 0 只通过，且 AI 层仍将全量股票送入分析浪费 token；用户决策转向"纯筛选 + 推荐池"。
  - ① 放宽筛选标准：PE 分位改为可选条件（`tech.pe_percentile_optional`，估值缺失时跳过），避免熊市初筛全军覆没。
  - ② 推荐池机制：`src/recommendation_pool.py` + `data/recommendation_pool.json`，非熊市时初筛通过的股票合并入池；熊市（科技股清仓触发）时从池中取股票生成减仓/清仓建议随报告推送。
  - ③ 熊市切换金融股筛选（简化版）：`evaluate_financial_stock` / `screen_finance_stocks` 仅校验 PB 近 5 年分位 ≤ 30%（含可选模式），行业白名单 `value.financial_industries`（银行/非银金融）；快照抓取工具缺省纳入金融行业并改为申万一级接口主源（东财被反爬封锁）。
  - ④ AI 层处置：Layer 4 临时停用（main.py 注释，Layer 5 改用 `format_screening_report` 纯筛选报告）；`analyze()` 补 passed 过滤与 0 通过短路，恢复时不再全量调用。
  - 新增测试：test_recommendation_pool.py + test_fetch_pool_snapshot.py，全量 273 用例通过。
- **Phase 5：本地回测闭环（预计 2 天，可与 Phase 4 并行）**
  - 引入 vectorbt 做轻量本地回测，实现 settings.yaml 调参后快速验证；聚宽回测仅作最终有效性确认。

## 5. 关键数据结构定义（统一标准）
- **Settings 对象**：由 `config.py` 从 settings.yaml 加载生成，包含全部策略阈值字段，全项目唯一配置入口。
- **Stock 对象**：包含 `code`, `name`, `price`, `ma5`, `ma10`, `ma20`, `rsi`, `pe_percentile`, `pb_percentile` 等字段。
- **Signal 对象**：包含 `action`（BUY/SELL/HOLD）、`target_price`、`confidence_score`（1-5星）、`reason`。

## 6. 测试策略（遵守 TDD）
- 针对 `ma_cross_detection()`、`max_drawdown()` 编写 `pytest` 单元测试。
- 针对 `config.py` 编写配置校验测试（缺字段、类型错误、越界值均应抛错）。
- 使用聚宽免费版跑出近 3 年的历史回测曲线，作为策略有效性的唯一依据。

## 7. 风险预案
- **数据源失效**：若 AKShare 宕机，程序自动重试 3 次；仍失败则降级读取 SQLite 缓存数据并发送告警微信，缓存过期超过阈值则静默退出。
- **AI 解析失败**：若百炼 API 超时或返回内容未通过 JSON Schema 校验，直接发送“纯量化信号”的文本，不阻塞主流程。
- **配置校验失败**：settings.yaml 缺失或校验不通过时，程序启动即报错退出并推送告警，严禁携带默认参数运行（防止静默产生错误交易信号）。

## 8. 错误总结（P1 + P2 测试阶段发现的问题与修复记录）

以下所有修正均已在代码中按 `.coderrules` 要求标注了「修正记录」注释，注明修正问题、影响面及可能影响的模块。

### 8.1 Phase 1：数据基建与因子层

| # | 问题描述 | 根因 | 修复方案 | 影响文件 |
|---|---------|------|---------|--------|
| 1 | `config.py` 运行时 KeyError 抛错 | `settings.yaml` 新增 `valuation_history_period` 字段后 `config.py` 未同步处理，启动校验失败 | 在 `_SECTION_FIELDS` 注册新字段并完善类型定义 | config.py, settings.yaml |
| 2 | pytest 无法收集测试用例 | conftest.py 未将项目根目录加入 sys.path，导致 src/ 下模块不可导入 | 按 .coderrules 模板更新 conftest.py | tests/conftest.py |
| 3 | test_normalize_kline_by_ak 异常路径断言过严 | 东财源拉取失败时抛出 RuntimeError，非 DataFetchError | 修正断言为预期 RuntimeError 信息 | tests/test_data_layer.py |
| 4 | _normalize_valuation 列类型断言错误 | merge 后 pe/pb 列类型为 str，Decimal 仅在 _read 阶段还原 | 修正断言验证类型转换逻辑 | tests/test_data_layer.py |
| 5 | valuation_percentile 首次测试 1 failed | get_valuation_history 调用时缺少 period 参数，返回值无期约过滤 | 按 PRD 约定补齐 period 并完善测试用例 | tests/test_data_layer.py |

### 8.2 Phase 2：策略信号层与冒烟测试

| # | 问题描述 | 根因 | 修复方案 | 影响文件 |
|---|---------|------|---------|--------|
| 6 | `market_regime.py` ModuleNotFoundError（indicators 引用失败） | sys.path 注册模板仅加项目根目录，src 同目录模块互引失败 | 补充注册 src 目录自身（含修正记录） | market_regime.py, stock_screener.py, position_sizing.py |
| 7 | annualized_volatility DivisionByZero 崩溃 | 真实前复权数据（000001 新浪源）早期存在非正收盘价（-3.08、0 值），收益率计算除零 | data_layer 写侧+读侧双端过滤非正价，indicators 对非正价格跳过收益统计 | data_layer.py, indicators.py |
| 8 | 指数 K 线 amount 列 None 断言失败 | 新浪指数接口无成交额字段，amount 全为 None，冒烟断言过严 | 放宽断言（None 或 Decimal 合法），遵循 .coderrules 缺列边缘情况 | tests/test_smoke.py |
| 9 | evaluate_stock 返回 price=None | data_layer 脏数据传播至 indicators → volatility 崩溃导致全票筛选失败 | 见第 7 项净化修复，clean cache 后冒烟 10 passed | — |
| 10 | _pct(None) TypeError 崩溃 | valuation_percentile 分位缺失传 None 给 Decimal 乘算 | _pct 签名改为 Optional[Decimal]，None 显示 "-" | position_sizing.py |
| 11 | 二笔仓位判定构造数据失败 | hist 放大不够导致 MACD 红柱放大条件不满足，mock 数据不合理 | 迭代脚本寻找合适构造值（close=17.8）使条件全部成立 | tests/test_position_sizing.py |
| 12 | 三笔仓位判定 close 未严格大于 prev_high | mock 数据 close 恰好等于 12.9 = prev_high，未满足 > 条件 | 微调构造值（close=12.95）确保突破条件成立 | tests/test_position_sizing.py |

### 8.3 Phase 3：AI 解读层

| # | 问题描述 | 根因 | 修复方案 | 影响文件 |
|---|---------|------|---------|--------|
| 13 | pydantic-ai v2 API 变更：`result_type` 已改为 `output_type` | pydantic-ai 从 v1 升级到 v2，Agent 参数名变更 | 使用 `output_type=StockAnalysis` 替代 `result_type` | ai_layer.py |
| 14 | pydantic-ai v2 `OpenAIModel` 已改为 `OpenAIChatModel` | 模块重命名，旧导入路径失效 | 使用 `from pydantic_ai.models.openai import OpenAIChatModel` | ai_layer.py |
| 15 | OpenAIChatModel 不接受 `base_url` 参数 | v2 改为通过 `OpenAIProvider(base_url=...)` 传入 | 显式构建 `OpenAIProvider` 后传入 `provider` 参数 | ai_layer.py |
| 16 | test_all_candidates_have_error 断言文本不匹配 | 健康检查先于 Prompt 构建拦截，降级信号含「数据源异常」而非「无有效候选股票」 | 修正断言为 `数据源异常` | tests/test_ai_layer.py |

### 8.4 Phase 4：端到端联调与缺陷修复

| # | 问题描述 | 根因 | 修复方案 | 影响文件 |
|---|---------|------|---------|--------|
| 17 | AI 模型调用 404 | `.env` 中 DASHSCOPE_BASE_URL 缺 `https://` 前缀 | 补全前缀并改用标准百炼端点 | .env |
| 18 | qwen3.8-max 返回 400 | 思考模型在 thinking mode 下不支持 pydantic-ai 依赖的 tool_choice=required | model_settings 增加 extra_body enable_thinking=False | ai_layer.py |
| 19 | 12 只股票单次 AI 调用反复超时（3 分钟） | 单次调用 Prompt 过长 + 超时仅 15s | 分批调用（max_batch_size=5）+ 超时 60s + 结果合并，降至 33 秒 | ai_layer.py, settings.yaml, config.py |
| 20 | PE 分位大量缺失 | 百度股市通估值历史对部分股票覆盖不足 5 年 | 降级窗口 5→3→2 年逐级尝试 + 缺失原因日志 | stock_screener.py |
| 21 | AI 误报「均线/RSI/MACD 指标缺失」 | 指标在初筛算后未保留，Prompt 组装字典未回填，字段全部输出 N/A | StockCandidate 新增 10 个指标快照字段并在 stock_dict 逐字段回填 | stock_screener.py, ai_layer.py |
| 22 | dry-run 打印报告时 UnicodeEncodeError 崩溃 | Windows GBK 终端无法编码 emoji（📊/❌） | 启动时 reconfigure stdout/stderr errors=replace（保留终端原编码避免中文乱码） | main.py |
| 23 | notifier 响应解析异常漏接 | requests 的 resp.json() 失败抛继承 ValueError 的 JSONDecodeError，仅捕 json.JSONDecodeError 会漏 | 改按 ValueError 兜底降级控制台 | notifier.py |
| 24 | 东财行业成分股接口全部被反爬封锁（15/15 失败） | 东财 push2 接口对本机 IP 级断连 | 离线快照抓取工具 + 「静态快照 → 手工池 → 内置小池」三级降级链 | tools/fetch_pool_snapshot.py, src/pool_snapshot.py, main.py |
| 25 | 初筛 0 只通过时 AI 层仍将全量股票送入分析（token 浪费） | analyze() 构建 candidates_data 时未按 passed 过滤，且 0 通过时无短路 | 加 passed 过滤 + 0 通过短路（0 次 API 调用） | ai_layer.py |
| 26 | 熊市初筛几乎全军覆没 | 估值数据缺失直接判不通过（百度接口对中小盘股无覆盖）；深跌条件与趋势确认条件在熊市内在矛盾 | PE 分位改可选模式（有数据才校验，日志留痕） | settings.yaml, config.py, stock_screener.py |
| 27 | settings.yaml 新增字段不生效 | config.py 只解析 `_RULES` 中注册的字段，未注册字段静默丢弃 | 新字段同步注册到 `_RULES`（pe_percentile_optional/pb_percentile_optional/financial_industries） | config.py |
| 28 | 快照抓取工具东财单源双端被封锁，快照永远无法生成 | 原仅东财单源 | 三级数据源降级链：申万一级接口主源 → Tushare 可选备源 → 东财兜底 | tools/fetch_pool_snapshot.py |

### 8.5 经验教训与改进
1. **AKShare 接口稳定性**：东方财富接口反爬策略升级频繁，已实现自动降级到新浪备用源，但仍需持续关注接口可用性变化。
2. **前复权数据质量**：新浪源早期前复权数据存在历史性的负价/零价，这是除权算法的数学溢出效应，必须在因子层做防御性过滤。
3. **Mock 数据设计原则**：自动化测试用例应经过手工演算验证（如二笔、三笔条件的 mock 数据），否则会掩盖真实路径 bug。
4. **PowerShell 兼容性**：复杂参数的 python -c 命令在 PowerShell 中存在引号转义问题，已改用临时脚本文件方案（用完即删）。
5. **AI 报「数据缺失」先查组装链路**：指标明明算好了却没传给 Prompt，会导致 AI 如实转述 N/A 并误导排查方向；数据类应保留指标快照并在组装层逐字段回填。
6. **终端编码必须处理**：凡输出含 emoji 的脚本，在 Windows GBK 终端与 Linux crontab C 编码下都会触发 UnicodeEncodeError，需提前 reconfigure（errors=replace）。
7. **反爬严重的第三方接口用离线快照**：实时拉取不可靠时，「人工值守抓取快照 + 静态降级链」比无限重试更稳定（如东财成分股）。
8. **配置新字段必须在 config._RULES 注册**：load_settings 只解析 `_RULES` 中声明的字段，新增 settings.yaml 字段不注册会静默不生效（必填字段缺失则启动报错），改动需同步两处并补配置校验测试。
9. **筛选条件要与市场状态自洽**：熊市下"超跌反弹"与"趋势确认"类条件内在矛盾，通过率趋零是策略必然而非 bug；缺失数据类条件应改"有数据才校验"的可选模式，而非一刀切判不通过。