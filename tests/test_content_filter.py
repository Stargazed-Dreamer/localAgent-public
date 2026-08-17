"""测试 content_filter 模块（D4 正则匹配 + D5 预检缓存）

D4: "av" 等关键词改为 \b 单词边界匹配，"Avicii" 不再误命中
D5: 预检结果缓存 1 小时，同批标题不重复调 LLM
"""

from unittest.mock import patch

from server.activity_tracker.content_filter import (
    _local_keyword_check,
    _prefilter_cache_get,
    _prefilter_cache_put,
    clear_prefilter_cache,
    find_sensitive_titles,
)

# ==================== D4: 正则单词边界匹配 ====================

class TestLocalKeywordRegex:
    """D4: ASCII 关键词用 \b 单词边界匹配"""

    def test_avicii_not_matched(self):
        """Avicii 不被 "av" 误命中"""
        titles = ["Avicii - The Nights - YouTube"]
        result = _local_keyword_check(titles)
        assert result == set()

    def test_available_not_matched(self):
        """Available 不被 "av" 误命中"""
        titles = ["Available storage settings"]
        result = _local_keyword_check(titles)
        assert result == set()

    def test_standalone_av_matched(self):
        """独立 "AV" 或 "av" 仍被命中"""
        titles = ["AV player", "av converter"]
        result = _local_keyword_check(titles)
        assert len(result) == 2

    def test_porn_substring_not_matched(self):
        """"Pornhub" 作为完整词被命中，但 "pornography" 不被命中（单词边界）"""
        titles = ["Pornhub - Videos", "pornography documentary"]
        result = _local_keyword_check(titles)
        # "Pornhub" 命中，"pornography" 不命中（\bporn\b 不匹配 pornography）
        assert "Pornhub - Videos" in result
        assert "pornography documentary" not in result

    def test_chinese_keyword_substring(self):
        """中文关键词保持子串匹配"""
        titles = ["色情视频网站", "成人内容提醒"]
        result = _local_keyword_check(titles)
        assert len(result) == 2

    def test_chinese_not_affected_by_word_boundary(self):
        """中文关键词不受 \b 影响（\b 对中文无效）"""
        titles = ["无色情相关内容"]
        result = _local_keyword_check(titles)
        assert "无色情相关内容" in result  # 子串匹配命中

    def test_xxx_word_boundary(self):
        """XXX 作为独立词命中，但 XXXX 不命中"""
        titles = ["XXX movie", "XXXX rated"]
        result = _local_keyword_check(titles)
        assert "XXX movie" in result
        assert "XXXX rated" not in result

    def test_case_insensitive(self):
        """大小写不敏感"""
        titles = ["AV Player", "av player", "Av Player"]
        result = _local_keyword_check(titles)
        assert len(result) == 3

    def test_empty_titles(self):
        """空标题列表"""
        result = _local_keyword_check([])
        assert result == set()

    def test_empty_string_title(self):
        """空字符串标题被跳过"""
        result = _local_keyword_check(["", "normal title"])
        assert result == set()


# ==================== D5: 预检缓存 ====================

class TestPrefilterCache:
    """D5: 预检结果缓存 1 小时"""

    def setup_method(self):
        """每个测试前清空缓存"""
        clear_prefilter_cache()

    def test_cache_miss_returns_none(self):
        """未缓存的标题集 → 返回 None"""
        result = _prefilter_cache_get(["title1", "title2"])
        assert result is None

    def test_cache_put_then_get(self):
        """写入后查询命中"""
        titles = ["title1", "title2"]
        result_set = {"title1"}
        _prefilter_cache_put(titles, result_set)
        cached = _prefilter_cache_get(titles)
        assert cached is not None
        assert cached == result_set

    def test_cache_returns_copy(self):
        """缓存返回副本，修改不影响缓存内容"""
        titles = ["title1"]
        _prefilter_cache_put(titles, {"title1"})
        cached1 = _prefilter_cache_get(titles)
        cached1.add("injected")
        cached2 = _prefilter_cache_get(titles)
        assert "injected" not in cached2

    def test_cache_different_titles_different_entries(self):
        """不同标题集各自独立"""
        _prefilter_cache_put(["a"], set())
        _prefilter_cache_put(["b"], {"b"})
        assert _prefilter_cache_get(["a"]) == set()
        assert _prefilter_cache_get(["b"]) == {"b"}

    def test_cache_same_titles_different_order(self):
        """标题顺序不影响缓存命中（frozenset 无序）"""
        _prefilter_cache_put(["a", "b"], {"a"})
        cached = _prefilter_cache_get(["b", "a"])
        assert cached is not None
        assert cached == {"a"}

    def test_find_sensitive_titles_uses_cache(self):
        """find_sensitive_titles 第二次调用相同标题时命中缓存，不调 LLM"""
        titles = ["Normal Title 1", "Normal Title 2"]

        # mock _bisect 避免真实 LLM 调用
        call_count = 0

        from server.activity_tracker import content_filter as cf_mod

        def _mock_bisect(titles, sensitive, depth):
            nonlocal call_count
            call_count += 1
            # 不标记任何敏感

        with patch.object(cf_mod, "_bisect", _mock_bisect):
            # 第一次调用：未命中缓存，调用 _bisect
            result1 = find_sensitive_titles(titles)
            assert call_count == 1

            # 第二次调用：命中缓存，不调用 _bisect
            result2 = find_sensitive_titles(titles)
            assert call_count == 1  # 仍然是 1，没有增加

        assert result1 == result2

    def test_cache_expired(self):
        """缓存过期后不命中"""
        import time as _time
        titles = ["expired_title"]
        _prefilter_cache_put(titles, {"expired_title"})

        # 手动修改缓存时间为过去
        from server.activity_tracker.content_filter import _get_prefilter_cache_lock, _prefilter_cache
        key = frozenset(titles)
        with _get_prefilter_cache_lock():
            result, _ = _prefilter_cache[key]
            _prefilter_cache[key] = (result, _time.time() - 1)  # 已过期

        cached = _prefilter_cache_get(titles)
        assert cached is None  # 过期后返回 None
