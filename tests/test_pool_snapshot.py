"""股票池快照与降级链单元测试（src/pool_snapshot.py）—— 全部离线，临时目录隔离

覆盖范围：快照保存/读取往返、损坏文件兜底、空池拒绝、手工池读取、
快照陈旧告警、三级降级链（快照 → 手工池 → 内置小池）各级命中。
"""

import json
from datetime import datetime, timedelta

import src.pool_snapshot as ps


# ---------------------------------------------------------------------------
# 快照存取
# ---------------------------------------------------------------------------
class TestSnapshotRoundTrip:
    def test_save_and_load(self, tmp_path):
        """保存后读取应还原池内容与抓取时间"""
        path = tmp_path / "snap.json"
        pool = {"000063": "通信", "300750": "电力设备"}
        ps.save_snapshot(pool, path=path)
        loaded, fetched_at = ps.load_snapshot(path)
        assert loaded == pool
        assert fetched_at  # ISO 时间字符串非空
        # 抓取时间应为刚写入（1 分钟内）
        age = ps.snapshot_age_days(fetched_at)
        assert age == 0

    def test_load_missing_file(self, tmp_path):
        """文件不存在应返回 (None, '')，不抛异常"""
        pool, fetched_at = ps.load_snapshot(tmp_path / "不存在.json")
        assert pool is None and fetched_at == ""

    def test_load_corrupted_json(self, tmp_path):
        """损坏 JSON 应返回 (None, '')，不抛异常"""
        path = tmp_path / "bad.json"
        path.write_text("{非法内容", encoding="utf-8")
        pool, _ = ps.load_snapshot(path)
        assert pool is None

    def test_load_empty_pool_rejected(self, tmp_path):
        """快照中池为空时应视为不可用（触发降级）"""
        path = tmp_path / "empty.json"
        path.write_text(json.dumps({"fetched_at": "2026-08-23T09:00:00",
                                    "pool": {}}), encoding="utf-8")
        pool, _ = ps.load_snapshot(path)
        assert pool is None

    def test_code_keys_normalized_to_str(self, tmp_path):
        """JSON 中被写成数字的代码应规范化为字符串"""
        path = tmp_path / "num.json"
        path.write_text(json.dumps({"fetched_at": "2026-08-23T09:00:00",
                                    "pool": {666: "计算机"}}),
                        encoding="utf-8")
        pool, _ = ps.load_snapshot(path)
        assert pool == {"666": "计算机"}


# ---------------------------------------------------------------------------
# 手工池读取
# ---------------------------------------------------------------------------
class TestManualPool:
    def test_load_ok(self, tmp_path):
        path = tmp_path / "manual.json"
        path.write_text(json.dumps({"000100": "电子"}), encoding="utf-8")
        assert ps.load_manual_pool(path) == {"000100": "电子"}

    def test_load_missing(self, tmp_path):
        assert ps.load_manual_pool(tmp_path / "不存在.json") is None

    def test_load_corrupted(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("不是 JSON", encoding="utf-8")
        assert ps.load_manual_pool(path) is None

    def test_load_empty(self, tmp_path):
        path = tmp_path / "empty.json"
        path.write_text("{}", encoding="utf-8")
        assert ps.load_manual_pool(path) is None


# ---------------------------------------------------------------------------
# 陈旧告警
# ---------------------------------------------------------------------------
class TestStaleness:
    def test_age_days(self):
        old = (datetime.now() - timedelta(days=40)).isoformat(
            timespec="seconds")
        assert ps.snapshot_age_days(old) == 40

    def test_age_parse_failure(self):
        assert ps.snapshot_age_days("非法时间") is None
        assert ps.snapshot_age_days("") is None


# ---------------------------------------------------------------------------
# 三级降级链
# ---------------------------------------------------------------------------
class TestResolveChain:
    def _write_snapshot(self, tmp_path, pool, days_ago=0):
        path = tmp_path / "snap.json"
        fetched = (datetime.now() - timedelta(days=days_ago)).isoformat(
            timespec="seconds")
        path.write_text(json.dumps({"fetched_at": fetched, "pool": pool}),
                        encoding="utf-8")
        return path

    def _write_manual(self, tmp_path, pool):
        path = tmp_path / "manual.json"
        path.write_text(json.dumps(pool), encoding="utf-8")
        return path

    def test_snapshot_first(self, tmp_path):
        """快照存在时优先使用快照"""
        snap = self._write_snapshot(tmp_path, {"000063": "通信"})
        manual = self._write_manual(tmp_path, {"000100": "电子"})
        pool, source = ps.resolve_stock_pool(snap, manual)
        assert pool == {"000063": "通信"}
        assert "快照" in source

    def test_manual_when_snapshot_missing(self, tmp_path):
        """快照缺失时降级到手工池"""
        manual = self._write_manual(tmp_path, {"000100": "电子"})
        pool, source = ps.resolve_stock_pool(tmp_path / "不存在.json", manual)
        assert pool == {"000100": "电子"}
        assert "手工池" in source

    def test_small_pool_when_both_missing(self, tmp_path):
        """快照与手工池均缺失时降级到内置小池"""
        pool, source = ps.resolve_stock_pool(tmp_path / "a.json",
                                             tmp_path / "b.json")
        assert pool == ps.FALLBACK_SMALL_POOL
        assert "内置小池" in source

    def test_stale_snapshot_still_used_with_warning(self, tmp_path, capsys):
        """陈旧快照（>30天）仍被使用，但打印刷新提示"""
        snap = self._write_snapshot(tmp_path, {"000063": "通信"},
                                    days_ago=40)
        pool, source = ps.resolve_stock_pool(snap, tmp_path / "m.json")
        assert pool == {"000063": "通信"}
        out = capsys.readouterr().out
        assert "未更新" in out

    def test_corrupted_snapshot_falls_to_manual(self, tmp_path):
        """快照损坏时应继续降级到手工池而非崩溃"""
        snap = tmp_path / "bad.json"
        snap.write_text("{坏数据", encoding="utf-8")
        manual = self._write_manual(tmp_path, {"000100": "电子"})
        pool, source = ps.resolve_stock_pool(snap, manual)
        assert pool == {"000100": "电子"}
        assert "手工池" in source
