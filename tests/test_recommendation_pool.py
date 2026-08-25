"""推荐池持久化单元测试（src/recommendation_pool.py）—— 全部离线，使用临时文件

覆盖范围：空池/损坏文件兜底、保存与读取往返、合并入池（新股/重复股计数）、
自定义路径注入。
"""

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from src.recommendation_pool import (
    load_recommendation_pool,
    save_recommendation_pool,
    update_recommendation_pool,
)


@dataclass
class FakeCandidate:
    """stock_screener.StockCandidate 的替身（仅含入池用到的字段）"""
    code: str
    industry: str
    price: Optional[Decimal] = Decimal("12.34")


# ---------------------------------------------------------------------------
# 读取：兜底路径
# ---------------------------------------------------------------------------
class TestLoadPool:
    def test_missing_file_returns_empty(self, tmp_path):
        """文件不存在时返回空字典（永不抛错）"""
        assert load_recommendation_pool(tmp_path / "not_exist.json") == {}

    def test_corrupted_file_returns_empty(self, tmp_path):
        """文件内容非法 JSON 时返回空字典"""
        path = tmp_path / "recommendation_pool.json"
        path.write_text("这不是 JSON {", encoding="utf-8")
        assert load_recommendation_pool(path) == {}

    def test_missing_pool_key_returns_empty(self, tmp_path):
        """payload 缺 pool 键时返回空字典"""
        path = tmp_path / "recommendation_pool.json"
        path.write_text(json.dumps({"updated_at": "x"}), encoding="utf-8")
        assert load_recommendation_pool(path) == {}

    def test_non_dict_entry_filtered(self, tmp_path):
        """记录值非 dict 的条目应被过滤（防御手工篡改）"""
        path = tmp_path / "recommendation_pool.json"
        payload = {"pool": {"000063": {"industry": "通信"},
                            "300750": "非法值"}}
        path.write_text(json.dumps(payload), encoding="utf-8")
        pool = load_recommendation_pool(path)
        assert list(pool.keys()) == ["000063"]


# ---------------------------------------------------------------------------
# 保存与读取往返
# ---------------------------------------------------------------------------
class TestSaveLoadRoundTrip:
    def test_roundtrip(self, tmp_path):
        """保存后读取应还原池内容，且带元数据"""
        path = tmp_path / "recommendation_pool.json"
        save_recommendation_pool({"000063": {"industry": "通信"}}, path)
        assert load_recommendation_pool(path) == {"000063": {"industry": "通信"}}
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["count"] == 1
        assert payload["updated_at"]      # ISO 时间元数据存在

    def test_parent_dir_created(self, tmp_path):
        """路径父目录不存在时自动创建"""
        path = tmp_path / "sub" / "dir" / "recommendation_pool.json"
        save_recommendation_pool({"000063": {}}, path)
        assert path.exists()


# ---------------------------------------------------------------------------
# 合并入池
# ---------------------------------------------------------------------------
class TestUpdatePool:
    def test_new_stock_inserted(self, tmp_path):
        """新股入池：完整字段写入，计数为 1"""
        path = tmp_path / "recommendation_pool.json"
        pool = update_recommendation_pool(
            [FakeCandidate("000063", "通信", Decimal("35.2"))], path)
        entry = pool["000063"]
        assert entry["industry"] == "通信"
        assert entry["price"] == "35.2"
        assert entry["recommend_count"] == 1
        assert entry["first_recommended_at"] == entry["last_recommended_at"]
        # 已落盘
        assert load_recommendation_pool(path) == pool

    def test_repeat_stock_count_incremented(self, tmp_path):
        """重复入池：计数 +1，首次时间保留，最近时间与价格刷新"""
        path = tmp_path / "recommendation_pool.json"
        update_recommendation_pool(
            [FakeCandidate("000063", "通信", Decimal("35.2"))], path)
        pool = update_recommendation_pool(
            [FakeCandidate("000063", "通信", Decimal("33.0"))], path)
        entry = pool["000063"]
        assert entry["recommend_count"] == 2
        assert entry["price"] == "33.0"
        assert entry["first_recommended_at"] <= entry["last_recommended_at"]

    def test_price_none_stored_as_empty(self, tmp_path):
        """价格为 None 时存空字符串（不崩溃）"""
        path = tmp_path / "recommendation_pool.json"
        pool = update_recommendation_pool(
            [FakeCandidate("000063", "通信", None)], path)
        assert pool["000063"]["price"] == ""

    def test_empty_candidates_no_file_written(self, tmp_path):
        """无候选时不落盘（推荐池保持不变）"""
        path = tmp_path / "recommendation_pool.json"
        pool = update_recommendation_pool([], path)
        assert pool == {}
        assert not path.exists()
