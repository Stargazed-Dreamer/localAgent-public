"""tier 归一化哨兵回归测试（2026-09-01 入站网关 T6 发现的既有 bug 修复）

背景：无 tier/无 use_case 的 pool.call()/pool.stream() 会把 resolved_range=(0,0)
传入 _acquire()，后者再次调用 _normalize_tier_range()。修复前 _normalize_tier(0)
被 clamp 成 1，导致 (0,0) → (1,1)：所有无 tier 请求被硬过滤到仅含 tier-1 模型的
key（入站网关流式全失败、非流式静默换模型）。0 是"未指定"哨兵，必须保持 0。
"""

from server.llm_pool.key_store import _normalize_tier, _normalize_tier_range


class TestNormalizeTierSentinel:
    def test_int_zero_is_unspecified(self):
        assert _normalize_tier(0) == 0

    def test_str_zero_is_unspecified(self):
        assert _normalize_tier("0") == 0

    def test_none_is_unspecified(self):
        assert _normalize_tier(None) == 0


class TestNormalizeTierClamp:
    def test_valid_int_passthrough(self):
        assert _normalize_tier(3) == 3

    def test_above_range_clamps_to_5(self):
        assert _normalize_tier(7) == 5

    def test_negative_clamps_to_1(self):
        assert _normalize_tier(-1) == 1

    def test_legacy_names(self):
        assert _normalize_tier("cheap") == 2
        assert _normalize_tier("default") == 3
        assert _normalize_tier("powerful") == 5


class TestNormalizeTierRangeSentinel:
    def test_zero_tuple_stays_unspecified(self):
        # 核心回归点：(0,0) 不得被误归一成 (1,1)
        assert _normalize_tier_range((0, 0)) == (0, 0)

    def test_none_stays_unspecified(self):
        assert _normalize_tier_range(None) == (0, 0)

    def test_zero_int_stays_unspecified(self):
        assert _normalize_tier_range(0) == (0, 0)


class TestNormalizeTierRangeNormal:
    def test_full_range(self):
        assert _normalize_tier_range((1, 5)) == (1, 5)

    def test_exact_int(self):
        assert _normalize_tier_range(3) == (3, 3)

    def test_single_element_tuple(self):
        assert _normalize_tier_range([4]) == (4, 4)

    def test_mixed_zero_and_value_keeps_value(self):
        assert _normalize_tier_range((0, 3)) == (3, 3)
