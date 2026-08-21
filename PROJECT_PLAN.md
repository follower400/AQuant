# 项目名称：AI辅助量化决策系统

## 0. 当前进度（每完成一个 Phase 更新一次）

> 最近更新：2026-08-21（完成 Phase 1 数据基建与因子库，进入 Phase 2）

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
| Phase 1 | Pytest 单元测试（tests/，41 个用例全部通过，含真实数据链路验证） | ✅ 已完成 |
| Phase 2 | 策略信号逻辑（market_regime / stock_screener / position_sizing） | ⏳ 未开始（下一步） |
| Phase 3 | AI 提示词工程与 JSON Schema 校验 | ⏳ 未开始 |
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
- **Phase 2：策略逻辑工程化（预计 3 天）**
  - 实现 `market_regime.py`（判定景气/低落/减仓触发）。
  - 实现 `stock_screener.py`（按 1-20 元、行业白名单、回撤、波动率区间初筛）。
  - 实现 `position_sizing.py`（计算 30%/30%/40% 触发条件与仓位上限校验）。
- **Phase 3：AI 提示词工程（预计 1 天）**
  - 设计结构化 Prompt（要求 AI 严格按 JSON 格式返回评级和理由，便于解析）。
  - 实现输出 JSON Schema 校验与失败降级（发送纯量化信号，不阻塞主流程）。
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