"""
fetch_pool_snapshot.py —— 行业成分股快照抓取工具（离线运行，建议每周一次）

背景：东财行业板块接口（akshare stock_board_industry_cons_em）反爬严重，
实盘流程中实时拉取经常全部失败。本工具在人工值守时抓取成分股并写入
静态快照 data/industry_pool_snapshot.json，main.py 运行时按
「静态快照 → 手工池 → 内置小池」降级链读取（见 src/pool_snapshot.py）。

运行方式：
    python tools/fetch_pool_snapshot.py                 # 按 settings.yaml 行业白名单抓取
    python tools/fetch_pool_snapshot.py --industries 电子,通信
    python tools/fetch_pool_snapshot.py --retries 5     # 每行业重试次数

安全约定：
- 全部行业均失败时不覆盖旧快照（保留上次成功结果），退出码 1；
- 部分行业失败仅跳过该行业并告警；
- 每次请求间隔 2 秒，降低触发反爬概率。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Dict, Optional

# 项目根目录与 src 目录注册到 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _PROJECT_ROOT / "src"
for _p in (str(_PROJECT_ROOT), str(_SRC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config
from pool_snapshot import save_snapshot

REQUEST_INTERVAL_SECONDS = 2.0   # 行业间请求间隔（反爬友好）


# ---------------------------------------------------------------------------
# 单行业抓取（带重试）
# ---------------------------------------------------------------------------
def fetch_industry_cons(industry: str, retries: int = 3,
                        delay: float = REQUEST_INTERVAL_SECONDS
                        ) -> Optional[Dict[str, str]]:
    """抓取单个行业的成分股：{code: industry}；全部重试失败返回 None"""
    try:
        import akshare as ak
    except ImportError:
        print("❌ AKShare 未安装，请先执行: pip install akshare")
        sys.exit(2)

    for attempt in range(1, retries + 1):
        try:
            cons = ak.stock_board_industry_cons_em(symbol=industry)
            if "代码" not in cons.columns:
                print(f"  ⚠️ 行业 {industry} 返回缺少「代码」列，跳过")
                return None
            pool = {str(code): industry for code in cons["代码"]}
            print(f"  ✅ 行业 {industry}: {len(pool)} 只"
                  f"（第 {attempt}/{retries} 次成功）")
            return pool
        except Exception as e:
            print(f"  ⚠️ 行业 {industry} 第 {attempt}/{retries} 次失败: {e}")
            if attempt < retries:
                time.sleep(delay)
    return None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="抓取行业成分股静态快照（供 main.py 降级链使用）")
    parser.add_argument("--industries", type=str, default=None,
                        help="逗号分隔的行业名（缺省读 settings.yaml 白名单）")
    parser.add_argument("--retries", type=int, default=3,
                        help="每个行业的重试次数（默认 3）")
    args = parser.parse_args()

    if args.industries:
        industries = [s.strip() for s in args.industries.split(",") if s.strip()]
    else:
        industries = list(config.get_settings().tech.industries)

    print("=" * 50)
    print(f"开始抓取行业成分股快照: {industries}")
    print("=" * 50)

    pool: Dict[str, str] = {}
    failed = []
    for i, industry in enumerate(industries):
        part = fetch_industry_cons(industry, retries=args.retries)
        if part:
            pool.update(part)
        else:
            failed.append(industry)
        if i < len(industries) - 1:
            time.sleep(REQUEST_INTERVAL_SECONDS)

    print("-" * 50)
    if not pool:
        print(f"❌ 全部 {len(industries)} 个行业抓取失败，保留旧快照不覆盖")
        sys.exit(1)

    path = save_snapshot(pool)
    print(f"✅ 快照已保存: {path}")
    print(f"   共 {len(pool)} 只股票，覆盖 {len(industries) - len(failed)}"
          f"/{len(industries)} 个行业")
    if failed:
        print(f"   ⚠️ 失败行业（快照中不含）: {', '.join(failed)}")


if __name__ == "__main__":
    main()
