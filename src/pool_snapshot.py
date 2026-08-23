"""
pool_snapshot.py —— 行业成分股池快照与混合降级链（Layer 3 数据辅助）

职责边界（遵循 .coderule）：
- 仅负责股票池的静态存取与降级选择，严禁包含任何策略判断；
- 快照由 tools/fetch_pool_snapshot.py 离线生成（东财行业板块接口反爬严重，
  实盘运行时不再实时拉取，改读静态快照）；
- 文件读写全部 try-except 兜底，任何文件损坏/缺失都降级到下一级，严禁崩溃。

三级降级链（resolve_stock_pool）：
  1. 静态快照  data/industry_pool_snapshot.json（快照抓取程序生成）
  2. 手工池    data/manual_pool.json（人工维护，格式 {code: industry}）
  3. 内置小池  兜底常量（保证程序永不空转）

用法示例：
    from pool_snapshot import resolve_stock_pool
    pool, source = resolve_stock_pool()   # source 为 "静态快照" / "手工池" / "内置小池"
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
SNAPSHOT_PATH = _PROJECT_ROOT / "data" / "industry_pool_snapshot.json"
MANUAL_POOL_PATH = _PROJECT_ROOT / "data" / "manual_pool.json"

# 快照超过该天数未更新时打印陈旧告警（仍然使用，仅提示重新抓取）
STALE_DAYS = 30

# 最终兜底小池（冒烟测试同款，保证降级链永不返回空池）
FALLBACK_SMALL_POOL: Dict[str, str] = {"000063": "通信", "300750": "电力设备"}


# ---------------------------------------------------------------------------
# 快照存取
# ---------------------------------------------------------------------------
def save_snapshot(pool: Dict[str, str], path: Optional[Path] = None,
                  source: str = "akshare-em") -> Path:
    """保存成分股快照（覆盖写入，UTF-8）

    快照结构：{"fetched_at": ISO 时间, "source": 来源, "count": 数量,
              "pool": {code: industry}}
    :return: 实际写入的文件路径
    """
    path = path or SNAPSHOT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "count": len(pool),
        "pool": pool,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def load_snapshot(path: Optional[Path] = None) -> Tuple[Optional[Dict[str, str]], str]:
    """读取成分股快照

    :return: (pool, fetched_at)；文件不存在/损坏/池为空时返回 (None, "")
    """
    path = path or SNAPSHOT_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        pool = payload.get("pool")
        if not isinstance(pool, dict) or not pool:
            return None, ""
        # 规范化：键值一律转字符串（防御 JSON 中被写成数字的代码）
        pool = {str(k): str(v) for k, v in pool.items()}
        return pool, str(payload.get("fetched_at", ""))
    except (OSError, ValueError):
        return None, ""


def snapshot_age_days(fetched_at: str) -> Optional[int]:
    """快照距今天数；解析失败返回 None"""
    try:
        dt = datetime.fromisoformat(fetched_at)
    except (ValueError, TypeError):
        return None
    return (datetime.now() - dt).days


# ---------------------------------------------------------------------------
# 手工池读取
# ---------------------------------------------------------------------------
def load_manual_pool(path: Optional[Path] = None) -> Optional[Dict[str, str]]:
    """读取人工维护的手工池文件（纯 {code: industry} JSON）

    :return: 池字典；文件不存在/损坏/为空时返回 None
    """
    path = path or MANUAL_POOL_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            pool = json.load(f)
        if not isinstance(pool, dict) or not pool:
            return None
        return {str(k): str(v) for k, v in pool.items()}
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 三级降级链
# ---------------------------------------------------------------------------
def resolve_stock_pool(
    snapshot_path: Optional[Path] = None,
    manual_path: Optional[Path] = None,
) -> Tuple[Dict[str, str], str]:
    """按降级链选择股票池：静态快照 → 手工池 → 内置小池

    :return: (pool, source)，source 为中文来源名（供日志展示）
    """
    pool, fetched_at = load_snapshot(snapshot_path)
    if pool is not None:
        age = snapshot_age_days(fetched_at)
        if age is not None and age > STALE_DAYS:
            print(f"[pool_snapshot] ⚠️ 快照已 {age} 天未更新"
                  f"（抓取于 {fetched_at}），建议运行 "
                  f"tools/fetch_pool_snapshot.py 刷新")
        return pool, f"静态快照（{len(pool)} 只，抓取于 {fetched_at or '未知时间'}）"

    pool = load_manual_pool(manual_path)
    if pool is not None:
        return pool, f"手工池（{len(pool)} 只）"

    return dict(FALLBACK_SMALL_POOL), "内置小池（快照与手工池均不可用）"
