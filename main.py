"""
main.py —— AQuant 主入口：L0→L1→L2→L3→L4→L5 全链路编排

职责边界（遵循 .coderule 与 PROJECT_PLAN.md）：
- 本模块仅负责流程编排与异常兜底，严禁包含任何策略判断或因子计算逻辑；
- 各层模块保持独立解耦，main.py 作为胶水层串联；
- 全部异常必须 try-except 兜底，推送告警通知后安全退出（严禁静默崩溃）；
- 支持 --dry-run 模式（仅打印不推送，用于本地调试）。

运行方式：
    python main.py                  # 正式运行（拉取数据 → 分析 → 推送微信）
    python main.py --dry-run        # 调试模式（仅打印到控制台，不推送）
    python main.py --stock-pool 000063:通信,300750:电力设备  # 手工指定股票池

Ubuntu Crontab 配置（Phase 4 部署阶段）：
    40 9 * * 1-5  cd /path/to/AQuant && /usr/bin/python3 main.py >> /var/log/aquant.log 2>&1
    40 14 * * 1-5 cd /path/to/AQuant && /usr/bin/python3 main.py >> /var/log/aquant.log 2>&1
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# 项目根目录注册到 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 控制台编码兼容（P4 修正）：Windows GBK 终端 / Linux crontab C 编码下，
# print 消息中的 emoji（📊/❌/⚠️）会触发 UnicodeEncodeError 导致流程崩溃；
# 保留终端原有编码（避免中文乱码），仅将 errors 改为 replace 兜底不可编码字符。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding=_stream.encoding, errors="replace")
    except Exception:
        pass  # 非标准流（如被重定向到不支持 reconfigure 的对象）时保持原样

import config
from src.data_layer import DataFetchError, get_kline, close as close_db
from src.market_regime import (
    MarketRegime,
    detect_regime,
    is_clear_tech_triggered,
    is_force_reduce_triggered,
    is_forbid_open,
    is_half_position_triggered,
    is_panic_add_triggered,
)
from src.stock_screener import StockCandidate, screen_tech_stocks
from src.pool_snapshot import resolve_stock_pool
from src.ai_layer import analyze, check_data_health
from src.notifier import (
    send_notification,
    format_ai_report,
    format_degraded_report,
    format_error_alert,
)


# ---------------------------------------------------------------------------
# 日志工具
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    """带时间戳的日志输出（统一格式 [HH:MM:SS] 消息）"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


# ---------------------------------------------------------------------------
# 股票池解析（命令行参数）
# ---------------------------------------------------------------------------
def _parse_stock_pool(pool_str: str) -> Dict[str, str]:
    """解析命令行股票池参数：格式 'code1:industry1,code2:industry2'

    :param pool_str: 如 "000063:通信,300750:电力设备"
    :return: {code: industry} 字典
    """
    pool: Dict[str, str] = {}
    for item in pool_str.split(","):
        item = item.strip()
        if ":" not in item:
            print(f"[main] 跳过非法格式: {item}（应为 code:industry）")
            continue
        code, industry = item.split(":", 1)
        pool[code.strip()] = industry.strip()
    return pool


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(dry_run: bool = False, stock_pool_str: Optional[str] = None) -> None:
    """主流程编排：L0→L1→L2→L3→L4→L5

    :param dry_run: True=仅打印不推送（本地调试）
    :param stock_pool_str: 手工指定股票池（格式 "code:industry,..."），
                           缺省自动拉取行业成分股
    """
    _log("=" * 50)
    _log("AQuant 量化系统启动")
    _log(f"模式: {'调试（仅打印）' if dry_run else '正式（推送微信）'}")
    _log("=" * 50)

    # -----------------------------------------------------------------------
    # Layer 0：配置加载
    # -----------------------------------------------------------------------
    _log("[Layer 0] 加载配置...")
    settings = config.get_settings()
    _log(f"[Layer 0] 模拟总资产: {settings.simulated_capital} 元")
    _log(f"[Layer 0] AI 分析: {'开启' if settings.enable_ai_analysis else '关闭'} | "
         f"主力模型: {settings.ai.primary_model}")

    # -----------------------------------------------------------------------
    # Layer 1：数据拉取（指数 K 线）
    # -----------------------------------------------------------------------
    _log("[Layer 1] 拉取沪深300 指数 K 线...")
    try:
        index_df = get_kline("sh000300", is_index=True)
        _log(f"[Layer 1] 指数 K 线: {len(index_df)} 条记录, "
             f"最新收盘 {index_df['close'].iloc[-1]}")
    except DataFetchError as e:
        error_msg = f"指数 K 线拉取失败: {e}"
        _log(f"[Layer 1] ❌ {error_msg}")
        _send_alert(f"数据源异常: {error_msg}", dry_run)
        return
    except Exception as e:
        error_msg = f"指数数据异常: {e}\n{traceback.format_exc()}"
        _log(f"[Layer 1] ❌ {error_msg}")
        _send_alert(error_msg, dry_run)
        return

    # -----------------------------------------------------------------------
    # Layer 3（宏观判定）：市场状态 + 风控五开关
    # -----------------------------------------------------------------------
    _log("[Layer 3] 宏观景气判定...")
    regime = detect_regime(index_df)
    _log(f"[Layer 3] 市场状态: {regime.value}")

    risk_flags = {
        "强制减仓": is_force_reduce_triggered(index_df),
        "科技股减半": is_half_position_triggered(index_df),
        "禁止开仓": is_forbid_open(index_df),
        "科技股清仓": is_clear_tech_triggered(index_df),
        "恐慌加仓": is_panic_add_triggered(index_df),
    }
    triggered = [name for name, on in risk_flags.items() if on]
    _log(f"[Layer 3] 风控信号: {', '.join(triggered) if triggered else '全部未触发'}")

    # -----------------------------------------------------------------------
    # Layer 3（选股）：科技股初筛
    # -----------------------------------------------------------------------
    _log("[Layer 3] 科技股初筛...")
    stock_pool: Optional[Dict[str, str]] = None
    if stock_pool_str:
        stock_pool = _parse_stock_pool(stock_pool_str)
        _log(f"[Layer 3] 手工股票池: {stock_pool}")
    else:
        # 三级降级链（P4 新增）：静态快照 → 手工池 → 内置小池。
        # 东财实时成分股接口反爬严重，改由 tools/fetch_pool_snapshot.py
        # 离线抓取快照，实盘运行时不再实时拉取。
        stock_pool, pool_source = resolve_stock_pool()
        _log(f"[Layer 3] 成分股池来源: {pool_source}")

    candidates = screen_tech_stocks(stock_pool)
    passed = [c for c in candidates if c.passed]
    error_count = sum(1 for c in candidates if c.error is not None)
    _log(f"[Layer 3] 初筛结果: {len(candidates)} 只评估, "
         f"{len(passed)} 只通过, {error_count} 只数据异常")

    # -----------------------------------------------------------------------
    # 数据健康检查（P3 新增：提前拦截空数据 / 全失败）
    # -----------------------------------------------------------------------
    health_issues = check_data_health(index_df, candidates)
    if health_issues:
        _log(f"[健康检查] 数据异常: {health_issues}")

    # -----------------------------------------------------------------------
    # Layer 4：AI 解读
    # -----------------------------------------------------------------------
    _log("[Layer 4] AI 解读...")
    analysis_result = analyze(
        candidates=candidates,
        regime=regime.value,
        index_df=index_df,
        risk_flags=risk_flags,
    )

    if analysis_result.used_ai:
        _log(f"[Layer 4] AI 分析成功（模型: {analysis_result.used_model}）")
        if analysis_result.analysis:
            recs = analysis_result.analysis.stock_recommendations
            _log(f"[Layer 4] 推荐: {len(recs)} 只股票")
            for r in recs:
                _log(f"  - {r.code}: {r.action} (信心 {r.confidence}/5)")
    else:
        _log(f"[Layer 4] AI 降级: {analysis_result.errors}")

    # -----------------------------------------------------------------------
    # Layer 5：格式化 + 推送
    # -----------------------------------------------------------------------
    _log("[Layer 5] 格式化通知消息...")
    if analysis_result.used_ai:
        title = f"📊 AQuant AI 分析 ({regime.value})"
        content = format_ai_report(analysis_result)
    else:
        title = f"📊 AQuant 纯量化信号 ({regime.value})"
        content = format_degraded_report(analysis_result)

    # 追加风控信号到消息末尾
    if triggered:
        content += f"\n\n---\n**⚠️ 风控触发**: {', '.join(triggered)}"

    if dry_run:
        _log("[Layer 5] 调试模式，仅打印不推送:")
        print(content)
    else:
        _log("[Layer 5] 推送微信通知...")
        ok = send_notification(title, content)
        if ok:
            _log("[Layer 5] ✅ 推送成功")
        else:
            _log("[Layer 5] ⚠️ 推送失败（已降级到控制台）")

    # -----------------------------------------------------------------------
    # 收尾
    # -----------------------------------------------------------------------
    _log("=" * 50)
    _log("AQuant 运行结束")
    _log("=" * 50)

    # 关闭 SQLite 连接
    close_db()


def _send_alert(error_msg: str, dry_run: bool) -> None:
    """发送异常告警通知（PushPlus 或控制台降级）"""
    content = format_error_alert(error_msg)
    if dry_run:
        _log("[告警] 调试模式，仅打印:")
        print(content)
    else:
        send_notification("⚠️ AQuant 运行告警", content)


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def main() -> None:
    """命令行入口：解析参数 → 调用 run() → 异常兜底"""
    parser = argparse.ArgumentParser(
        description="AQuant 量化系统主入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python main.py                          # 正式运行\n"
               "  python main.py --dry-run                # 调试模式\n"
               "  python main.py --stock-pool 000063:通信  # 手工股票池",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="调试模式：仅打印到控制台，不推送微信",
    )
    parser.add_argument(
        "--stock-pool", type=str, default=None,
        help="手工指定股票池（格式: code1:industry1,code2:industry2）",
    )
    args = parser.parse_args()

    try:
        run(dry_run=args.dry_run, stock_pool_str=args.stock_pool)
    except config.ConfigError as e:
        # 配置错误：直接打印并退出（不推送，因为可能是密钥缺失）
        _log(f"❌ 配置错误: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        _log("用户中断（Ctrl+C）")
        sys.exit(0)
    except Exception as e:
        # 未预期异常：打印堆栈 + 尝试推送告警
        error_msg = f"{e}\n{traceback.format_exc()}"
        _log(f"❌ 未预期异常: {error_msg}")
        try:
            _send_alert(error_msg, dry_run=args.dry_run)
        except Exception:
            pass  # 推送本身也失败时静默（避免二次崩溃）
        sys.exit(1)


if __name__ == "__main__":
    main()
