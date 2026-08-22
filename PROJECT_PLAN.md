# 项目名称：AI辅助量化决策系统

## 0. 当前进度（每完成一个 Phase 更新一次）

> 最近更新：2026-08-22（完成 Phase 3 AI 解读层：提示词工程 + 分层代理池 + Pydantic-AI 校验 + 配置开关 + 数据健康检查）

| 阶段 | 任务 | 状态 |
| :--- | :--- | :--- |
| 前置 | 需求分析与策略 PRD 定稿（v2.0，含参数管理约定） | ✅ 已完成 |
| 前置 | 工程结构搭建与分层架构规划 | ✅ 已完成 |
| 前置 | 阿里云百炼 API 连通性验证 | ✅ 已完成 |
| 前置 | 文档配套：requirements.txt / .gitignore 完善 | ✅ 已完成 |
| Phase 0 | `config/settings.yaml` 参数总表（PRD 全部阈值提取） | ✅ 已完成 |
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
| Phase 4 | 端到端联调（Ubuntu Crontab + 微信推送） | ⏳ 未开始 |
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
- **Layer 0 (配置层)**：`config/settings.yaml` 集中承载 PRD v2.0 的全部策略阈值；`config.py` 负责加载、类型转换与合法性校验，启动时校验失败立即报错退出，禁止使用未经验证的默认值。
- **Layer 1 (数据层)**：负责调用 AKShare，清洗数据，缓存历史 K 线至本地 SQLite；网络异常时自动降级读取缓存并告警。
- **Layer 2 (因子计算层)**：纯数学计算（MA、MACD、RSI、PB分位）。**此层严禁调用 AI**，必须用 Decimal 处理，参数一律从 Layer 0 读取。
- **Layer 3 (策略信号层)**：根据 PRD 中的“宏观判定”、“科技股买入规则”、“金融股逢低布局”编写硬编码逻辑（If-Else），所有阈值引用 Layer 0。
- **Layer 4 (AI 解读层)**：仅接收 Layer 3 筛选出的“候选池”，调用百炼 API 生成最终点评；输出必须通过 JSON Schema 校验，失败则降级发送纯量化信号。
- **Layer 5 (通知层)**：格式化 Markdown 消息并推送。

## 4. 任务拆解与 Milestone（分阶段执行）
- **Phase 0：配置系统与数据缓存（预计 1 天）**
  - 编写 `config/settings.yaml` 参数总表：提取 PRD v2.0 全部阈值（MA/MACD/RSI 周期、止损 -8%、分批仓位比例、连跌天数、累计跌幅阈值、仓位上限等）。
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
- **Phase 4：端到端联调（预计 1 天）**
  - 在 Ubuntu 上运行主程序，测试微信能否收到含 AI 评论的推送。
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

### 8.4 经验教训与改进
1. **AKShare 接口稳定性**：东方财富接口反爬策略升级频繁，已实现自动降级到新浪备用源，但仍需持续关注接口可用性变化。
2. **前复权数据质量**：新浪源早期前复权数据存在历史性的负价/零价，这是除权算法的数学溢出效应，必须在因子层做防御性过滤。
3. **Mock 数据设计原则**：自动化测试用例应经过手工演算验证（如二笔、三笔条件的 mock 数据），否则会掩盖真实路径 bug。
4. **PowerShell 兼容性**：复杂参数的 python -c 命令在 PowerShell 中存在引号转义问题，已改用临时脚本文件方案（用完即删）。