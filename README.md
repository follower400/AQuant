# 📈 AI 量化投研助手 —— 智能盯盘与信号生成系统

> **⚠️ 核心安全声明**：本项目严格遵循 **“只读不写”** 原则。系统仅负责数据筛选、指标计算与 AI 辅助解读，**不涉及任何券商 API 对接，不具备自动下单或资产划转功能**。所有交易决策的最终执行权完全保留给用户（人工手动下单）。

[![Python Version](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)

---

## 🎯 项目简介

本项目是为个人投资者设计的 A 股智能盯盘辅助系统。它解决了手动盯盘耗时、情绪干扰大的痛点，通过 **“量化硬性筛选 + AI 柔性解读”** 的双层架构，每日定时推送高价值的交易信号。

核心逻辑基于自研的 **“均衡混合灵活配置策略”**（详见 [策略文档](./docs/strategy_prd.md)），涵盖：
- 🧠 **宏观景气度判定**（区分上行/低落/强制减仓三种市场状态）
- ⚡ **科技股短线分批建仓**（精确控制 30%/30%/40% 的入场节奏）
- 🏦 **金融股与 ETF 长线底仓布局**（基于 PB/PE 历史分位）
- 🛡️ **多维度卖出与全局硬性风控**（止盈止损、20日时间止损、系统性风险清仓）

## ✨ 核心功能特性

- **分层解耦架构**：严格遵循数据层、因子层、策略层、AI层、通知层分离，易于维护和扩展。
- **高精度数值计算**：全面采用 `Decimal` 类型处理价格与仓位，规避浮点数精度丢失。
- **低成本 AI 集成**：对接阿里云百炼（通义千问），仅对初筛后的少量标的调用，日均成本控制在 0.1 元以内。
- **实时消息推送**：通过 PushPlus 将选股结果和操作建议直接推送到微信，无需盯盘。
- **完备的测试与回测**：核心因子逻辑包含 Pytest 单元测试；策略有效性使用聚宽（JoinQuant）进行历史数据验证。

## 🛠 技术栈与工具

| 层级 | 工具/库 | 用途 |
| :--- | :--- | :--- |
| **开发环境** | VS Code + Continue/Cline | AI 辅助编程，管控代码生成质量 |
| **数据源** | AKShare | 获取实时行情、财务数据、估值分位 |
| **指标计算** | Pandas, NumPy | 向量化计算 MA、RSI、MACD 等因子 |
| **大模型** | 阿里云百炼 (通义千问 Qwen) | 将枯燥的指标转化为自然语言分析报告 |
| **任务调度** | Ubuntu (VMware) + Crontab | 每日定时（如 9:40、14:40）唤醒主程序 |
| **回测验证** | 聚宽 (JoinQuant) 免费版 | 验证策略逻辑在历史 3-5 年数据中的有效性 |
| **消息推送** | PushPlus / Server酱 | 微信实时接收选股推送 |

## 📂 项目目录结构规划

```
AI-Quant-Assistant/
├── .env                           # 敏感环境变量（API Key，严禁提交至 Git）
├── .gitignore                     # Git 忽略规则
├── README.md                      # 项目总览（本文件）
├── requirements.txt               # Python 依赖包列表
│
├── config/                        # 配置文件目录
│   └── settings.yaml              # 策略参数总表（如总资金、止盈止损阈值）
│
├── docs/                          # 文档目录
│   ├── strategy_prd.md            # 策略详细需求文档（已定稿）
│   └── project_plan.md            # 项目工程实施计划（任务拆解蓝图）
│
├── src/                           # 核心源代码
│   ├── data_layer/                # 数据获取层
│   │   └── data_fetcher.py        # 封装 AKShare，获取 K 线与估值
│   ├── factor_layer/              # 因子计算层
│   │   └── indicators.py          # MA、MACD、RSI、回撤、分位点计算（使用 Decimal）
│   ├── strategy_layer/            # 策略逻辑层
│   │   ├── market_regime.py       # 市场景气度判定
│   │   ├── tech_screener.py       # 科技股选股与分批建仓条件
│   │   └── etf_screener.py        # 金融股与 ETF 筛选逻辑
│   ├── ai_layer/                  # AI 交互层
│   │   └── ai_analyzer.py         # 调用百炼 API，生成结构化评价
│   ├── notify_layer/              # 通知层
│   │   └── wechat_pusher.py       # 封装 PushPlus 微信推送
│   └── main.py                    # 程序主入口（编排上述模块）
│
├── tests/                         # 单元测试目录
│   ├── test_indicators.py         # 针对核心因子的 Pytest 用例
│   └── test_strategy_logic.py     # 针对买卖信号的逻辑测试
│
└── joinquant/                     # 聚宽回测脚本（与本地代码解耦）
    └── backtest_research.ipynb    # 在聚宽研究环境运行的验证脚本
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

### 4. 运行本地测试
```bash
# 运行所有单元测试，确保核心因子计算准确
pytest tests/

# 手动运行一次主程序（不依赖定时任务）
python src/main.py
```
若配置正确，你绑定的微信账号将在 1 分钟内收到当日的选股推送报告。

## 📊 策略回测说明

本项目 **不包含内置回测引擎**。我们强烈建议在将策略逻辑编码到本地之前，先在聚宽平台进行充分的离线验证。

- **验证方法**：将 `src/strategy_layer/` 中的硬编码条件（如 MA5>MA20）复制到 `/joinquant/backtest_research.ipynb` 中，利用聚宽 `run_backtest()` 函数查看历史收益曲线和最大回撤。
- **历史表现目标**：年化收益 ≥ 12%，波动率 ≤ 18%，最大回撤 < 25%。

## 📝 开发规范 (必读)

本项目严格遵循根目录下 `.coderule` 文件的要求：
1. **严禁使用 Float**：金额、价格必须使用 `Decimal`。
2. **测试驱动**：新增因子必须附带单元测试。
3. **禁止占位符**：不允许出现 `pass` 或 `# TODO`，代码必须立即生效。
4. **安全底线**：禁止硬编码 API Key，一律通过 `.env` 读取。

## 🗺️ 项目路线图 (Roadmap)

- [x] 需求分析与策略 PRD 定稿
- [x] 项目工程结构搭建
- [x] 阿里云百炼 API 连通性验证
- [ ] 数据层与因子层代码编写 (Phase 1)
- [ ] 策略信号逻辑工程化 (Phase 2)
- [ ] AI 提示词工程与端到端联调 (Phase 3)
- [ ] Ubuntu 服务器定时任务部署 (Phase 4)

## ⚠️ 免责声明 (Disclaimer)

本系统生成的任何信号、分析报告及代码仅供**学习和研究参考**，不构成任何投资建议或荐股行为。证券市场存在较大风险，过往业绩不代表未来表现。使用者须自行承担因依赖本系统输出结果进行投资而产生的全部损失，项目作者与贡献者不承担任何法律责任。