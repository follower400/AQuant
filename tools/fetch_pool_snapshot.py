"""
fetch_pool_snapshot.py —— 行业成分股快照抓取工具（离线运行，建议每周一次）

背景：东财行业板块接口（akshare stock_board_industry_cons_em）反爬严重
（Windows/Ubuntu 双端均被封锁），实盘流程中实时拉取经常全部失败。
本工具在人工值守时抓取成分股并写入静态快照 data/industry_pool_snapshot.json，
main.py 运行时按「静态快照 → 手工池 → 内置小池」降级链读取
（见 src/pool_snapshot.py）。

数据源三级降级链（P4 新增）：
  1. 申万官网/乐咕（akshare sw 接口）—— 默认主源：sw_index_first_info
     动态映射行业名 → 申万一级代码（31 行业，含股息率等附加数据），
     再逐行业 index_component_sw 取成分股；不经过东财，无强反爬。
  2. Tushare index_member_all —— 可选备源：仅当 .env 配置 TUSHARE_TOKEN
     时启用（该接口需 2000 积分）；通过 index_classify 动态映射
     行业名 → 申万一级代码。
  3. 东财 stock_board_industry_cons_em —— 最后兜底：反爬严重，
     大概率失败，仅保留以防前两个源同时不可用。

修正记录（P4）：原默认主源为 baostock query_stock_industry，实测发现
其返回的是证监会行业分类（文档示例仍为 2018 年申万数据，已过时），
电子/计算机/通信被合并为 C39 一个大类无法拆分，与白名单粒度不匹配，
已从降级链移除。

运行方式：
    python tools/fetch_pool_snapshot.py                 # 按 settings.yaml 行业白名单抓取
    python tools/fetch_pool_snapshot.py --industries 电子,通信
    python tools/fetch_pool_snapshot.py --retries 5     # 每行业重试次数（仅东财源使用）

安全约定：
- 全部行业均失败时不覆盖旧快照（保留上次成功结果），退出码 1；
- 部分行业失败仅跳过该行业并告警；
- 每个数据源独立 try-except，任一源崩溃不影响后续源接管。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# 项目根目录与 src 目录注册到 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _PROJECT_ROOT / "src"
for _p in (str(_PROJECT_ROOT), str(_SRC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config
from pool_snapshot import save_snapshot

# 控制台编码兼容（P4 修正）：Windows GBK 终端/管道重定向下，print 中的 emoji
# （✅/⚠️）会触发 UnicodeEncodeError 导致流程崩溃；保留原编码仅替换不可编码字符。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding=_stream.encoding, errors="replace")
    except Exception:
        pass  # 非标准流（重定向对象不支持 reconfigure）时保持原样

REQUEST_INTERVAL_SECONDS = 2.0   # 东财源行业间请求间隔（反爬友好）


# ---------------------------------------------------------------------------
# 数据源 1：申万一级行业成分（akshare sw 接口，默认主源）
# ---------------------------------------------------------------------------
def fetch_pool_swindex(industries: List[str]) -> Optional[Dict[str, str]]:
    """申万一级行业成分股拉取：先 sw_index_first_info 动态映射行业名 →
    行业代码（避免硬编码），再逐行业 index_component_sw 取成分

    返回 {code: industry}；未安装/失败/白名单零覆盖时返回 None
    （返回 None 才会触发降级到下一个数据源；空结果不算成功，防误写空快照）。
    """
    try:
        import akshare as ak
    except ImportError:
        print("⚠️ [申万] AKShare 未安装（pip install akshare），跳过该数据源")
        return None

    try:
        info = ak.sw_index_first_info()
        name_to_code = dict(zip(info["行业名称"], info["行业代码"]))
        pool: Dict[str, str] = {}
        for industry in industries:
            sw_code = name_to_code.get(industry)
            if not sw_code:
                print(f"  ⚠️ [申万] 行业 {industry} 无申万一级行业代码，跳过")
                continue
            members = ak.index_component_sw(symbol=sw_code.split(".")[0])
            codes = [str(c) for c in members["证券代码"]]
            pool.update({c: industry for c in codes})
            print(f"  ✅ [申万] 行业 {industry}: {len(codes)} 只")
    except Exception as e:
        print(f"⚠️ [申万] 拉取失败（网络异常或接口变更）: {e}")
        return None

    if not pool:
        print(f"⚠️ [申万] 白名单行业 {industries} 无任何成分股，判定该源不可用")
        return None
    _print_pool_summary("[申万]", pool, industries)
    return pool


# ---------------------------------------------------------------------------
# 数据源 2：Tushare（可选备源，需 TUSHARE_TOKEN 且积分 >= 2000）
# ---------------------------------------------------------------------------
def fetch_pool_tushare(industries: List[str],
                       token: Optional[str]) -> Optional[Dict[str, str]]:
    """Tushare 申万行业成分拉取：先 index_classify 动态映射行业代码，
    再逐行业 index_member_all（is_new=Y 仅取最新成分）

    返回 {code: industry}；未配置 Token/未安装/失败时返回 None 触发降级。
    """
    if not token:
        print("ℹ️ [Tushare] 未配置 TUSHARE_TOKEN，跳过该数据源")
        return None
    try:
        import tushare as ts
    except ImportError:
        print("⚠️ [Tushare] 未安装（pip install tushare），跳过该数据源")
        return None

    try:
        pro = ts.pro_api(token)
        # 行业名 → 申万一级行业代码（SW2021 分类），动态查询避免硬编码
        classify = pro.index_classify(src="SW2021", level="L1")
        name_to_code = dict(zip(classify["l1_name"], classify["l1_code"]))

        pool: Dict[str, str] = {}
        for industry in industries:
            l1_code = name_to_code.get(industry)
            if not l1_code:
                print(f"  ⚠️ [Tushare] 行业 {industry} 无申万一级代码映射，跳过")
                continue
            members = pro.index_member_all(l1_code=l1_code, is_new="Y")
            codes = [c.split(".")[0] for c in members["ts_code"]]
            pool.update({c: industry for c in codes})
            print(f"  ✅ [Tushare] 行业 {industry}: {len(codes)} 只")
            time.sleep(0.5)  # 频率友好（积分用户限频约 120 次/分钟）
    except Exception as e:
        print(f"⚠️ [Tushare] 拉取失败（可能积分不足或网络异常）: {e}")
        return None

    if not pool:
        print(f"⚠️ [Tushare] 白名单行业 {industries} 无任何成分股，判定该源不可用")
        return None
    _print_pool_summary("[Tushare]", pool, industries)
    return pool


# ---------------------------------------------------------------------------
# 数据源 3：东财（原方案，最后兜底，反爬严重大概率失败）
# ---------------------------------------------------------------------------
def fetch_pool_eastmoney(industries: List[str], retries: int = 3,
                         delay: float = REQUEST_INTERVAL_SECONDS
                         ) -> Optional[Dict[str, str]]:
    """东财逐行业 HTTP 抓取（带重试）；全部失败返回 None"""
    try:
        import akshare as ak
    except ImportError:
        print("⚠️ [东财] AKShare 未安装，跳过该数据源")
        return None

    pool: Dict[str, str] = {}
    for i, industry in enumerate(industries):
        part = _fetch_industry_cons_em(ak, industry, retries=retries, delay=delay)
        if part:
            pool.update(part)
        if i < len(industries) - 1:
            time.sleep(delay)

    if not pool:
        print("⚠️ [东财] 全部行业抓取失败")
        return None
    _print_pool_summary("[东财]", pool, industries)
    return pool


def _fetch_industry_cons_em(ak, industry: str, retries: int, delay: float
                            ) -> Optional[Dict[str, str]]:
    """东财单行业抓取（带重试）：{code: industry}；全部重试失败返回 None"""
    for attempt in range(1, retries + 1):
        try:
            cons = ak.stock_board_industry_cons_em(symbol=industry)
            if "代码" not in cons.columns:
                print(f"  ⚠️ [东财] 行业 {industry} 返回缺少「代码」列，跳过")
                return None
            part = {str(code): industry for code in cons["代码"]}
            print(f"  ✅ [东财] 行业 {industry}: {len(part)} 只"
                  f"（第 {attempt}/{retries} 次成功）")
            return part
        except Exception as e:
            print(f"  ⚠️ [东财] 行业 {industry} 第 {attempt}/{retries} 次失败: {e}")
            if attempt < retries:
                time.sleep(delay)
    return None


# ---------------------------------------------------------------------------
# 汇总打印与主流程
# ---------------------------------------------------------------------------
def _print_pool_summary(source: str, pool: Dict[str, str],
                        industries: List[str]) -> None:
    """按行业汇总打印抓取结果，便于人工核对覆盖度"""
    covered = sorted({ind for ind in pool.values()})
    missing = [ind for ind in industries if ind not in covered]
    print(f"✅ {source} 抓取成功: 共 {len(pool)} 只，"
          f"覆盖 {len(covered)}/{len(industries)} 个行业")
    if missing:
        print(f"   ⚠️ 未覆盖行业: {', '.join(missing)}")


def fetch_pool_with_fallback(industries: List[str], retries: int,
                             tushare_token: Optional[str]
                             ) -> Tuple[Optional[Dict[str, str]], str]:
    """三级数据源降级链：申万 → Tushare → 东财

    返回 (pool, 数据源名称)；全部失败返回 (None, "无可用数据源")。
    修正记录（P4）：原仅东财单源，Windows/Ubuntu 双端被反爬封锁导致快照
    永远无法生成；现引入申万官网/乐咕主源 + 可选 Tushare 备源。
    """
    sources: List[Tuple[str, Callable[[], Optional[Dict[str, str]]]]] = [
        ("申万一级（akshare sw 接口）", lambda: fetch_pool_swindex(industries)),
        ("Tushare（申万一级）", lambda: fetch_pool_tushare(industries, tushare_token)),
        ("东财（行业板块）", lambda: fetch_pool_eastmoney(industries, retries=retries)),
    ]
    for name, fetch in sources:
        print("-" * 50)
        print(f"尝试数据源: {name}")
        try:
            pool = fetch()
        except Exception as e:
            # 单源内部未预期的异常也须兜住，保证降级链继续
            print(f"⚠️ 数据源 {name} 异常: {e}")
            pool = None
        if pool:
            return pool, name
    return None, "无可用数据源"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="抓取行业成分股静态快照（供 main.py 降级链使用）")
    parser.add_argument("--industries", type=str, default=None,
                        help="逗号分隔的行业名（缺省读 settings.yaml 白名单）")
    parser.add_argument("--retries", type=int, default=3,
                        help="东财源每个行业的重试次数（默认 3）")
    args = parser.parse_args()

    if args.industries:
        industries = [s.strip() for s in args.industries.split(",") if s.strip()]
    else:
        # P4 策略调整：缺省白名单 = 科技行业 + 金融行业（熊市科技股清仓时
        # 切换金融股筛选，依赖快照中含银行/非银金融成分股）
        s = config.get_settings()
        industries = list(s.tech.industries) + list(s.value.financial_industries)

    print("=" * 50)
    print(f"开始抓取行业成分股快照: {industries}")
    print("=" * 50)

    token = config.get_settings().tushare_token
    pool, source = fetch_pool_with_fallback(industries, retries=args.retries,
                                            tushare_token=token)

    print("-" * 50)
    if not pool:
        print(f"❌ 三级数据源全部失败，保留旧快照不覆盖")
        sys.exit(1)

    path = save_snapshot(pool, source=source)
    print(f"✅ 快照已保存: {path}（数据源: {source}）")
    print(f"   共 {len(pool)} 只股票")


if __name__ == "__main__":
    main()
