"""
recommendation_pool.py —— 推荐池持久化（P4 策略调整新增）

背景：策略从"AI 解读推荐"切换为"纯筛选 + 推荐池"后，需要一个持久化的
推荐池记录历次初筛通过的股票；熊市触发科技股清仓信号时，main.py 从池中
取出股票生成减仓/清仓建议（对应用户诉求"从建议买入变为建议减仓"）。

职责边界（遵循 .coderule）：
- 仅负责推荐池的存取与合并，严禁包含任何买卖策略判断
  （何时入池、何时提减仓建议由 main.py 编排层依据风控信号决定）；
- 文件读写全部 try-except 兜底，任何文件损坏/缺失都按空池处理，严禁崩溃。

文件结构 data/recommendation_pool.json：
  {"updated_at": ISO 时间, "count": 数量,
   "pool": {code: {"industry": 行业, "price": 最近入池价格(字符串),
                    "first_recommended_at": 首次入池时间,
                    "last_recommended_at": 最近入池时间,
                    "recommend_count": 累计入池次数}}}

用法示例：
    from recommendation_pool import load_recommendation_pool, update_recommendation_pool
    pool = load_recommendation_pool()                    # {} 表示空池
    pool = update_recommendation_pool(passed_candidates)  # 合并入池并落盘
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
POOL_PATH = _PROJECT_ROOT / "data" / "recommendation_pool.json"


# ---------------------------------------------------------------------------
# 推荐池存取
# ---------------------------------------------------------------------------
def load_recommendation_pool(path: Optional[Path] = None) -> Dict[str, dict]:
    """读取推荐池

    :return: {code: 入池记录}；文件不存在/损坏/为空时返回空字典（永不抛错）
    """
    import json

    path = path or POOL_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        pool = payload.get("pool")
        if not isinstance(pool, dict):
            return {}
        # 规范化：代码键一律转字符串，记录值必须是 dict（防御手工篡改）
        return {str(k): v for k, v in pool.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def save_recommendation_pool(pool: Dict[str, dict],
                             path: Optional[Path] = None) -> Path:
    """保存推荐池（覆盖写入，UTF-8）

    :return: 实际写入的文件路径
    """
    import json

    path = path or POOL_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(pool),
        "pool": pool,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def update_recommendation_pool(passed_candidates,
                               path: Optional[Path] = None) -> Dict[str, dict]:
    """将初筛通过的候选合并入推荐池并落盘

    已在池中的股票：刷新最近入池时间与价格、计数 +1；
    新股票：写入首次入池记录。

    :param passed_candidates: 初筛通过的 StockCandidate 列表（仅需
                              code/industry/price 属性）
    :return: 合并后的完整推荐池
    """
    pool = load_recommendation_pool(path)
    now = datetime.now().isoformat(timespec="seconds")
    for cand in passed_candidates:
        code = str(cand.code)
        price = str(cand.price) if cand.price is not None else ""
        entry = pool.get(code)
        if entry:
            entry["industry"] = cand.industry
            entry["price"] = price
            entry["last_recommended_at"] = now
            entry["recommend_count"] = int(entry.get("recommend_count", 1)) + 1
        else:
            pool[code] = {
                "industry": cand.industry,
                "price": price,
                "first_recommended_at": now,
                "last_recommended_at": now,
                "recommend_count": 1,
            }
    if pool:
        save_recommendation_pool(pool, path)
    return pool
