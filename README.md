# 📈 AI 量化投研助手 —— 智能盯盘与信号生成系统

> **⚠️ 核心安全声明**：本项目严格遵循 **“只读不写”** 原则。系统仅负责数据筛选、指标计算与 AI 辅助解读，**不涉及任何券商 API 对接，不具备自动下单或资产划转功能**。所有交易决策的最终执行权完全保留给用户（人工手动下单）。

[![Python Version](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)

---

## 🎯 项目简介

本项目是为个人投资者设计的 A 股智能盯盘辅助系统。它解决了手动盯盘耗时、情绪干扰大的痛点，通过 **“量化硬性筛选 + AI 柔性解读”** 的双层架构，每日定时推送高价值的交易信号。

核心逻辑基于自研的 **“均衡混合灵活配置策略”**（详见 [策略文档](./STRATEGY_PRD.md)），涵盖：
- 🧠 **宏观景气度判定**（区分上行/低落/强制减仓三种市场状态 + 五项全局风控开关）
- ⚡ **科技股短线分批建仓**（精确控制 30%/30%/40% 的入场节奏）
- 🏦 **金融股与 ETF 长线底仓仓位决策**（基于 PB/PE 历史分位）；金融股选股已实现简化版（熊市切换，见下）
- 🛡️ **多维度卖出与全局硬性风控**（止盈止损、20日时间止损、系统性风险清仓）
- 🔄 **熊市防御闭环（P4 策略调整新增）**：科技股清仓触发（沪深300 跌破 MA120）时，切换金融股简化筛选（仅校验 PB 近 5 年分位 ≤ 30%），并从推荐池（历次初筛通过的股票持久化记录）取股票生成减仓/清仓建议

> **⚠️ 当前状态（P4 策略重构中）**：AI 解读层（Layer 4）临时停用，主流程只输出纯筛选报告与推荐池减仓建议；`analyze()` 已内置 passed 过滤与 0 通过短路，策略定型后恢复时不再全量调用浪费 token。

## ✨ 核心功能特性

- **分层解耦架构**：严格遵循数据层、因子层、策略层、AI层、通知层分离，易于维护和扩展。
- **高精度数值计算**：全面采用 `Decimal` 类型处理价格与仓位，规避浮点数精度丢失。
- **低成本 AI 集成**：对接阿里云百炼（通义千问），分批调用（每批 5 只）+ 分层代理池自动降级，日均成本控制在 0.1 元以内（策略重构期临时停用，见上方状态说明）。
- **多级降级永不空转**：AI 层模型池降级（主力→备用→无 AI 纯量化信号）；股票池三级降级链（静态快照 → 手工池 → 内置小池）；快照抓取三级数据源（申万 → Tushare → 东财）。
- **推荐池持久化**：初筛通过的股票自动合并入 `data/recommendation_pool.json`，熊市时据此生成减仓/清仓建议。
- **实时消息推送**：通过 PushPlus 将选股结果和操作建议直接推送到微信，推送失败自动降级控制台输出。
- **完备的测试与回测**：273 个 Pytest 离线单元测试全部通过；策略有效性使用聚宽（JoinQuant）进行历史数据验证。

## 🛠 技术栈与工具

| 层级 | 工具/库 | 用途 |
| :--- | :--- | :--- |
| **开发环境** | PyCharm | AI 辅助编程，管控代码生成质量 |
| **数据源** | AKShare（东方财富 + 新浪备用源自动降级） | 获取实时行情、财务数据、估值分位 |
| **指标计算** | Pandas, NumPy | 向量化计算 MA、RSI、MACD 等因子 |
| **大模型** | 阿里云百炼 (通义千问 Qwen) | 将枯燥的指标转化为自然语言分析报告 |
| **任务调度** | Ubuntu (VMware) + Crontab | 每日定时（如 9:40、14:40）唤醒主程序 |
| **AI 结构化输出** | pydantic-ai v2（Agent + output_type Schema 校验） | 大模型输出 JSON 自动提取与校验 |
| **回测验证** | 聚宽 (JoinQuant) 免费版 | 验证策略逻辑在历史 3-5 年数据中的有效性 |
| **消息推送** | PushPlus / Server酱 | 微信实时接收选股推送 |

## 📂 项目目录结构规划

```
AQuant/
├── .env                           # 敏感环境变量（API Key，严禁提交至 Git）
├── .gitignore                     # Git 忽略规则（data/ 下快照与手工池 JSON 例外，随 git 分发）
├── pytest.ini                     # pytest 配置（注册 smoke/network 标记，日常默认跳过）
├── README.md                      # 项目总览（本文件）
├── requirements.txt               # Python 依赖包列表
├── settings.yaml                  # 策略参数总表（Layer 0：总资金、止盈止损、AI 配置等）
├── config.py                      # Layer 0：配置加载与类型/范围/交叉校验（启动即校验）
├── main.py                        # 主入口：L0→L5 全链路编排（--dry-run / --stock-pool）
├── STRATEGY_PRD.md                # 策略详细需求文档（已定稿）
├── PROJECT_PLAN.md                # 项目工程实施计划（任务拆解蓝图 + 错误总结）
│
├── data/                          # 本地数据（SQLite 缓存被忽略；快照与手工池 JSON 随 git 分发）
│   ├── industry_pool_snapshot.json  # 行业成分股静态快照（离线抓取，含科技+金融行业）
│   ├── manual_pool.json           # 手工交易池（人工维护，格式 {code: industry}）
│   └── recommendation_pool.json   # 推荐池（运行时生成，不随 git 分发，新克隆从空池开始）
│
├── src/                           # 核心源代码（严格遵循分层架构）
│   ├── data_layer.py              # L1 数据层：AKShare 封装 + SQLite 缓存 + 非正价净化
│   ├── indicators.py              # L2 因子层：MA/MACD/RSI/年化波动率/回撤（Decimal）
│   ├── market_regime.py           # L3 策略层：宏观景气判定 + 五项全局风控开关
│   ├── stock_screener.py          # L3 策略层：科技股初筛 + 金融股简化筛选（熊市切换）
│   ├── position_sizing.py         # L3 策略层：分批建仓 + 金融/ETF 底仓决策 + 三层上限校验
│   ├── pool_snapshot.py           # L3 辅助：股票池三级降级链（快照 → 手工池 → 内置小池）
│   ├── recommendation_pool.py     # L3 辅助：推荐池存取（不含策略判断，由 main 编排）
│   ├── ai_layer.py                # L4 AI 层：分批调用 + 分层代理池 + Pydantic-AI 校验（临时停用）
│   └── notifier.py                # L5 通知层：PushPlus 推送 + 控制台降级 + 报告格式化
│
├── tools/                         # 离线运维工具（人工值守运行，不进主流程）
│   └── fetch_pool_snapshot.py     # 行业成分股快照抓取（申万→Tushare→东财，缺省含金融行业）
│
└── tests/                         # 单元测试与冒烟测试目录（273 个离线用例）
    ├── conftest.py                # pytest 共享配置（sys.path 注册）
    ├── test_data_layer.py         # 数据层缓存读写闭环 + 新鲜度 + Schema
    ├── test_indicators.py         # 因子计算层精度验证（含脏数据防御测试）
    ├── test_market_regime.py      # 宏观景气判定 + 风控五开关
    ├── test_stock_screener.py     # 科技股选股单票评估（白名单/回撤/涨停/指标快照）
    ├── test_position_sizing.py    # 仓位决策 + 上限校验（三批触发/暂停/加速）
    ├── test_config_ai.py          # AI 相关配置校验（模型池/超时/开关）
    ├── test_ai_layer.py           # AI 层：Prompt 组装/分批合并/降级路径（离线 mock）
    ├── test_notifier.py           # 通知层：推送成功/失败降级/报告格式化（离线 mock）
    ├── test_main.py               # 主入口：参数解析/全链路编排/熊市切换分支（离线 mock）
    ├── test_pool_snapshot.py      # 股票池快照存取与三级降级链（临时目录隔离）
    ├── test_recommendation_pool.py  # 推荐池存取合并与熊市减仓建议（临时目录隔离）
    ├── test_fetch_pool_snapshot.py  # 快照抓取工具三级数据源降级链（离线 mock）
    └── test_smoke.py              # P1+P2 主链路冒烟测试（访问真实 AKShare 数据源）
```

## 🚀 快速开始 (Quick Start)

### 1. 环境准备
- 确保已安装 Python 3.9+ 以及 VMware Ubuntu（用于定时部署）。
- 克隆本仓库至本地或虚拟机。

### 2. 安装依赖
```bash
pip install -r requirements.txt
```

### 3. 配置环境变量
复制根目录下的 `.env.example` 并重命名为 `.env`，填入以下关键信息：
```ini
# 阿里云百炼 API（通义千问）
DASHSCOPE_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx

# 微信推送服务（PushPlus）
PUSHPLUS_TOKEN=your_pushplus_token_here

# 模拟总资产（用于计算模拟仓位）
SIMULATED_TOTAL_CAPITAL=1000000
```

### 4. 运行本地测试与主程序
```bash
# 运行所有离线单元测试（默认跳过冒烟/网络测试，保持回归快速）
python -m pytest tests/ -q

# 显式运行冒烟测试（串连 P1+P2 主链路，访问真实 AKShare 数据源）
python -m pytest -m smoke -v

# 手动运行一次主程序（完整链路：数据 → 初筛 → 筛选报告推送；策略重构期 AI 解读临时停用，熊市自动切换金融股筛选+推荐池减仓建议）
python main.py

# 仅控制台查看报告不推送（冒烟验证用）
python main.py --dry-run

# 使用手工交易池（格式：代码:行业,代码:行业）
python main.py --stock-pool "000063:通信,300750:电力设备"
```
若配置正确，你绑定的微信账号将在 1 分钟内收到当日的筛选推送报告。
（东财成分股接口反爬严重，自动池采用离线快照：`python tools/fetch_pool_snapshot.py`，建议每周人工刷新一次；快照缺失时自动降级手工池/内置小池。注意：新克隆的仓库若快照尚未生成，运行时会直接降级到手工池，属预期行为；熊市切换金融股筛选依赖快照中含银行/非银金融成分股，抓取工具缺省白名单已含金融行业。）

## 📊 策略回测说明

本项目 **不包含内置回测引擎**。我们强烈建议在将策略逻辑编码到本地之前，先在聚宽平台进行充分的离线验证。

- **验证方法**：将 `src/` 策略层模块（market_regime / stock_screener）中的硬编码条件复制到聚宽回测脚本中，利用聚宽 `run_backtest()` 函数查看历史收益曲线和最大回撤。
- **历史表现目标**：年化收益 ≥ 12%，波动率 ≤ 18%，最大回撤 < 25%。

## 📝 开发规范 (必读)

本项目严格遵循根目录下 `.coderule` 文件的要求：
1. **严禁使用 Float**：金额、价格必须使用 `Decimal`。
2. **测试驱动**：新增因子必须附带单元测试；主链路必须有冒烟测试（`pytest -m smoke`）。
3. **禁止占位符**：不允许出现 `pass` 或 `# TODO`，代码必须立即生效。
4. **安全底线**：禁止硬编码 API Key，一律通过 `.env` 读取。
5. **报错标注**：每次修正 bug 必须在代码处标注修正记录，注明问题、影响面和改进措施。

## 🗺️ 项目路线图 (Roadmap)

- [x] 需求分析与策略 PRD 定稿
- [x] 项目工程结构搭建
- [x] 阿里云百炼 API 连通性验证
- [x] 数据层与因子层代码编写（Phase 1）
- [x] 策略信号逻辑工程化（market_regime / stock_screener / position_sizing）（Phase 2）
- [x] 估值历史分位与冒烟测试（P2 配套）
- [x] AI 提示词工程（Phase 3：Prompt 去幻觉 + 分层代理池 + Pydantic-AI 校验）
- [x] Windows 侧端到端联调（Phase 4：主入口 + 通知层 + AI 分批 + 股票池快照降级链，233 个单元测试通过）
- [x] P4 策略重构（2026-08：PE/PB 分位可选化 + 推荐池持久化 + 熊市切换金融股简化筛选 + AI 解读层临时停用，273 个单元测试通过）
- [ ] Ubuntu 服务器定时任务部署（Phase 4 收尾：推 GitHub → VM 拉取 → crontab 9:40/14:40）
- [ ] 金融股选股补齐（股息率/ROE/负债率，需先接财务数据源；PB 分位简化版已实现）与 AI 解读层恢复评估
- [ ] 本地回测闭环（Phase 5：vectorbt 调参验证）

## ⚠️ 免责声明 (Disclaimer)

本系统生成的任何信号、分析报告及代码仅供**学习和研究参考**，不构成任何投资建议或荐股行为。证券市场存在较大风险，过往业绩不代表未来表现。使用者须自行承担因依赖本系统输出结果进行投资而产生的全部损失，项目作者与贡献者不承担任何法律责任。