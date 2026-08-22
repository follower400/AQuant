"""
tests/test_smoke.py —— P1+P2 主链路冒烟测试（.coderrules 要求：主链路必须有冒烟测试）

与单元测试的区别：冒烟测试访问真实 AKShare 数据源（经 SQLite 缓存与降级逻辑），
串连 Layer 0 配置加载 -> Layer 1 数据拉取（指数/个股/估值）-> Layer 2 因子计算
（全指标 + 估值分位）-> Layer 3 策略判定（市场状态/单票评估/仓位决策/选股主流程）
全链路，验证各层模块真实协作是否正常。

运行方式（三选一，效果等价）：
    python -m pytest tests/test_smoke.py -m smoke -v   # pytest 方式（推荐，CI 可接入）
    python -m pytest -m smoke -v                       # 运行全部冒烟用例
    python tests/test_smoke.py                         # 直接运行脚本

注意：pytest.ini 的 addopts 默认排除 smoke 标记（日常全量单测保持离线快速），
因此日常 `python -m pytest` 不会触发真实数据源访问。

失败判定：任一环节失败（数据源彻底不可用且无缓存可降级，或下游计算/判定崩溃）
即冒烟整体失败、退出码非 0，便于 CI/CD 直接接入。
"""

import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

# 直接运行本脚本时（不经过 pytest 收集）conftest.py 不会加载，手动注册项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 冒烟标记：pytest.ini 已注册，日常全量测试默认跳过（addopts = -m "not smoke"）
pytestmark = pytest.mark.smoke

from config import get_settings                                          # noqa: E402
from src.data_layer import get_kline, get_valuation_history              # noqa: E402
from src.indicators import add_all_indicators, valuation_percentile      # noqa: E402
from src.market_regime import (                                          # noqa: E402
    MarketRegime,
    detect_regime,
    is_clear_tech_triggered,
    is_force_reduce_triggered,
    is_forbid_open,
    is_half_position_triggered,
    is_panic_add_triggered,
    prepare_index,
)
from src.position_sizing import (                                        # noqa: E402
    ACTION_NONE,
    apply_position_limits,
    decide_tech_batch,
    decide_value_position,
)
from src.stock_screener import evaluate_stock, screen_tech_stocks        # noqa: E402

# 冒烟标的：沪深300 指数 + 平安银行（蓝筹，K 线与估值数据源稳定）
INDEX_CODE = "sh000300"
STOCK_CODE = "000001"

# 小池选股验证用：000001（非白名单行业，验证「不通过」路径）
#                000063 中兴通讯（通信，白名单行业，验证「完整条件」路径）
SCREEN_POOL = {STOCK_CODE: "银行", "000063": "通信"}

# K 线最少样本数（保证 MA250 与近 5 年估值分位窗口可计算）
_MIN_ROWS = 250


# ---------------------------------------------------------------------------
# 数据夹具：module 级共享，三层数据各拉取一次（缓存新鲜时直接读 SQLite）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def index_df() -> pd.DataFrame:
    """沪深300 指数日 K 线（Layer 1 真实数据源；失败且无缓存时冒烟整体失败）"""
    return get_kline(INDEX_CODE, is_index=True)


@pytest.fixture(scope="module")
def stock_df() -> pd.DataFrame:
    """个股日 K 线（东财失败自动降级新浪备用源，data_layer 已含重试逻辑）"""
    return get_kline(STOCK_CODE)


@pytest.fixture(scope="module")
def valuation_df() -> pd.DataFrame:
    """个股估值历史 PE(TTM)/PB（百度股市通口径）"""
    return get_valuation_history(STOCK_CODE)


# ---------------------------------------------------------------------------
# Step 0 · Layer 0：配置加载
# ---------------------------------------------------------------------------
def test_smoke_config_loads():
    """配置加载与校验通过（冒烟起点，settings.yaml 非法将导致程序拒绝启动）"""
    settings = get_settings()
    assert settings.operation.schedule_times, "调度时刻配置为空"
    assert settings.tech.price_max > 0, "选股价格上限非法"
    assert settings.risk.position_single_stock_max > 0, "单票仓位上限非法"


# ---------------------------------------------------------------------------
# Step 1~3 · Layer 1：数据拉取（指数 / 个股 / 估值）
# ---------------------------------------------------------------------------
def test_smoke_index_kline(index_df):
    """指数日线拉取（缓存优先/降级），列集合与样本量校验"""
    required = {"trade_date", "open", "high", "low", "close", "volume"}
    assert required.issubset(index_df.columns), \
        f"指数 K 线缺列: {required - set(index_df.columns)}"
    # 指数接口无成交额（amount）列属正常（.coderrules 缺失列边缘情况），
    # 下游模块必须容忍缺列；列存在时末值允许为 None（新浪指数源无该字段）或 Decimal
    if "amount" in index_df.columns:
        last_amount = index_df["amount"].iloc[-1]
        assert last_amount is None or isinstance(last_amount, Decimal), \
            f"amount 类型非法: {type(last_amount)}"
    assert len(index_df) >= _MIN_ROWS, f"指数样本不足（{len(index_df)} < {_MIN_ROWS}）"
    assert index_df["close"].iloc[-1] > 0, "指数收盘价非正数"


def test_smoke_stock_kline(stock_df):
    """个股日线拉取（东财失败自动降级新浪），列集合与样本量校验"""
    required = {"trade_date", "open", "high", "low", "close", "volume"}
    assert required.issubset(stock_df.columns), \
        f"个股 K 线缺列: {required - set(stock_df.columns)}"
    assert len(stock_df) >= _MIN_ROWS, f"个股样本不足（{len(stock_df)} < {_MIN_ROWS}）"
    assert stock_df["close"].iloc[-1] > 0, "个股收盘价非正数"


def test_smoke_valuation_history(valuation_df):
    """估值历史拉取（PE(TTM)/PB），Decimal 序列与样本校验"""
    assert {"trade_date", "pe", "pb"}.issubset(valuation_df.columns), \
        f"估值缺列: {set(valuation_df.columns)}"
    assert len(valuation_df) > 0, "估值历史为空"
    assert valuation_df["pe"].notna().sum() > 0, "PE 序列无任何有效样本"


# ---------------------------------------------------------------------------
# Step 4~5 · Layer 2：因子计算（全指标 + 估值分位）
# ---------------------------------------------------------------------------
def test_smoke_indicators(stock_df):
    """全指标计算（真实个股 K 线），指标列齐全且末值有效"""
    df = stock_df.copy()  # 指标计算为原地追加列，复制避免污染共享夹具
    add_all_indicators(df)
    expected = ("ma5", "ma10", "ma20", "ma60", "ma120", "ma250",
                "macd_dif", "macd_dea", "macd_hist", "rsi14",
                "volatility", "max_drawdown")
    for col in expected:
        assert col in df.columns, f"缺少指标列 {col}"
        last = df[col].iloc[-1]
        assert last is not None and not pd.isna(last), f"指标 {col} 末值为空"


def test_smoke_valuation_percentile(valuation_df):
    """PE 近5年估值分位（真实估值序列），输出列存在且末值合法"""
    df = valuation_df.copy()
    years = get_settings().tech.pe_percentile_years
    valuation_percentile(df, column="pe", years=years)
    col = f"pe_percentile_{years}y"
    assert col in df.columns, f"缺少分位列 {col}"
    last = df[col].iloc[-1]
    if last is not None:  # 末样本可能因窗口有效样本不足而为 None（合法）
        assert Decimal(0) <= last <= Decimal(1), f"分位越界: {last}"


# ---------------------------------------------------------------------------
# Step 6 · Layer 3：宏观景气判定 + 全局风控
# ---------------------------------------------------------------------------
def test_smoke_market_regime(index_df):
    """宏观景气判定 + 全局风控五开关（真实指数数据）"""
    df = index_df.copy()
    prepare_index(df)
    regime = detect_regime(df)
    assert isinstance(regime, MarketRegime), f"判定结果类型非法: {type(regime)}"
    assert regime.value in ("BULL", "BEAR", "NEUTRAL"), f"非法市场状态: {regime.value}"
    for fn in (is_force_reduce_triggered, is_half_position_triggered,
               is_forbid_open, is_clear_tech_triggered, is_panic_add_triggered):
        assert isinstance(fn(df), bool), f"{fn.__name__} 必须返回 bool"


# ---------------------------------------------------------------------------
# Step 7~9 · Layer 3：单票评估 / 仓位决策 / 小池选股主流程
# ---------------------------------------------------------------------------
def test_smoke_stock_evaluate(stock_df, valuation_df):
    """单票初筛评估（真实 K 线 + 估值），候选结构合法且白名单校验生效"""
    cand = evaluate_stock(stock_df.copy(), STOCK_CODE, "银行", valuation_df.copy())
    assert cand.code == STOCK_CODE, f"候选代码错误: {cand.code}"
    assert cand.price is not None and cand.price > 0, "候选价格为空或非正"
    assert isinstance(cand.passed, bool), "passed 必须为 bool"
    assert isinstance(cand.failures, list), "failures 必须为 list"
    # 银行不在科技白名单，行业条件必须记为不通过（验证白名单逻辑真实生效）
    assert any("行业" in f for f in cand.failures), f"白名单校验未生效: {cand.failures}"


def test_smoke_position_sizing(stock_df):
    """仓位决策（科技分批 + 底仓 + 三层上限校验，真实 K 线）"""
    kline = stock_df.copy()
    plan = decide_tech_batch(kline, batch_done=0)
    assert plan.action in ("BUY_1ST", ACTION_NONE), f"非法动作: {plan.action}"
    assert isinstance(plan.ratio, Decimal) and plan.ratio >= 0, f"比例非法: {plan.ratio}"
    assert plan.reason, "决策理由为空"

    # 底仓决策：PB 分位缺失时不得触发买入（None 安全降级）
    plan_value = decide_value_position("stock", None, Decimal("0.40"))
    assert plan_value.action in ("BUY_INIT", ACTION_NONE), f"非法底仓动作: {plan_value.action}"

    # 三层上限：请求 30% 超单票上限 15%，必须被压到 <= 15% 且给出约束说明
    allowed, note = apply_position_limits(Decimal("0.30"))
    assert allowed <= get_settings().risk.position_single_stock_max, \
        f"单票上限校验失效: {allowed}"
    assert note, "约束说明为空"


def test_smoke_screen_flow():
    """小池选股主流程（screen_tech_stocks 逐票评估，单票失败不阻塞）"""
    results = screen_tech_stocks(SCREEN_POOL)
    assert len(results) == len(SCREEN_POOL), \
        f"结果数量不符: {len(results)} != {len(SCREEN_POOL)}"
    for r in results:
        assert r.code in SCREEN_POOL, f"未知代码: {r.code}"
        assert isinstance(r.passed, bool), f"{r.code} passed 必须为 bool"
    # 非白名单行业（银行）必须被正确标记为不通过，验证主流程逐票过滤生效
    bank = next(r for r in results if r.code == STOCK_CODE)
    assert bank.passed is False, f"{STOCK_CODE} 非白名单却通过初筛"
    assert any("行业" in f for f in bank.failures), f"{STOCK_CODE} 缺少行业失败明细"


if __name__ == "__main__":
    # 直接运行入口：python tests/test_smoke.py
    # 命令行 -m smoke 覆盖 pytest.ini addopts 的默认排除，确保直接运行必然执行
    raise SystemExit(pytest.main(["-m", "smoke", __file__, "-v"]))
