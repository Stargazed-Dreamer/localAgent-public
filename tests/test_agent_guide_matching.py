"""agent_guide 关键词匹配改进测试。

覆盖三类改进：
- A. web_archive keywords 扩展（爬文章/爬取/抓取网页/文章存档）
- B. 同义词组扩展（爬取↔保存）+ 助词过滤（一下/看看/试试）
- C. context 上下文增强（文件路径读取 + domain_hints 加分 + 二进制拒绝）

背景：用户问"爬一下文章"曾因 web_archive keywords 缺"爬"系动词、
prototype 的"试一下"被"保存一下"误匹配、context 文件信号未利用等原因，
导致 top-1 是 dev.prototype（score=5），web_archive 完全没进 top-5。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.agent_guide import (
    GUIDE_REGISTRY,
    _extract_context_signals,
    _read_context_file,
    _strip_aux,
    match_task_candidates,
)

# ========== A. keywords 扩展 ==========

class TestWebArchiveKeywords:
    """web_archive keywords 应覆盖爬取系口语动词。"""

    def test_keywords_include_crawl_verbs(self):
        """爬文章/爬取/抓取网页 等口语动词应进 keywords。"""
        kws = GUIDE_REGISTRY["adhoc.web_archive"]["keywords"]
        assert "爬文章" in kws
        assert "爬取" in kws
        assert "抓取网页" in kws

    def test_keywords_include_article_archive(self):
        """'文章存档' 用于盖过 wip_tracking 的'存档'误匹配。"""
        kws = GUIDE_REGISTRY["adhoc.web_archive"]["keywords"]
        assert "文章存档" in kws

    def test_domain_hints_declared(self):
        """web_archive 应声明 domain_hints 用于 context 加分。"""
        hints = GUIDE_REGISTRY["adhoc.web_archive"].get("domain_hints", {})
        assert "xiaoheihe.cn" in hints
        assert "mp.weixin.qq.com" in hints


class TestCrawlQueryMatching:
    """爬取系查询应该 strong_match 到 web_archive。"""

    @pytest.mark.parametrize("query", [
        "爬文章",
        "爬取",
        "爬取网页",
        "文章存档",
    ])
    def test_strong_match_web_archive(self, query):
        """这些查询应该 kw_exact 命中 web_archive 并 strong_match。"""
        r = match_task_candidates(query)
        assert r, f"无匹配: {query}"
        top = r[0]
        assert top["task_type"] == "adhoc.web_archive", (
            f"{query!r} 应匹配 web_archive，实际 top-1={top['task_type']}"
        )
        assert top["strong_match"], f"{query!r} 应 strong_match"

    def test_crawl_article_original_query(self):
        """用户原查询'爬一下文章'应该匹配 web_archive（曾误匹配 prototype）。"""
        r = match_task_candidates("爬一下文章")
        assert r, "无匹配"
        top = r[0]
        assert top["task_type"] == "adhoc.web_archive", (
            f"'爬一下文章' 应匹配 web_archive，实际 top-1={top['task_type']}"
        )

    def test_article_archive_overrides_wip_tracking(self):
        """'文章存档' 应匹配 web_archive 而非 wip_tracking（曾误匹配后者）。"""
        r = match_task_candidates("文章存档")
        top = r[0]
        assert top["task_type"] == "adhoc.web_archive"
        # wip_tracking 不应在 top-1
        assert top["task_type"] != "system.wip_tracking"


# ========== B. 同义词组 + 助词过滤 ==========

class TestAuxParticleStripping:
    """助词过滤：剥离'一下/看看/试试'等再算 bigram。"""

    @pytest.mark.parametrize("text,expected", [
        ("保存一下文章", "保存文章"),
        ("保存一下", "保存"),
        ("看看屏幕", "屏幕"),
        ("试试原型", "原型"),
        ("弄一下这个", "这个"),
    ])
    def test_strip_aux(self, text, expected):
        assert _strip_aux(text) == expected

    def test_strip_preserves_kw_exact(self):
        """助词过滤不影响 kw_exact（用原文 task_lower）。"""
        # '看一下录制' 是 recording.discover 的完整 keyword
        # 即使剥离'看一下'剩'录制'，kw_exact 仍用原文匹配
        r = match_task_candidates("看一下录制")
        top = r[0]
        assert top["task_type"].startswith("recording.")

    def test_save_yixia_not_match_prototype(self):
        """'保存一下' 不应该因'一下'误匹配 prototype 的'试一下'。"""
        r = match_task_candidates("保存一下文章")
        if r:
            top = r[0]
            assert top["task_type"] != "dev.prototype", (
                f"'保存一下文章' 不应匹配 prototype，实际 top-1={top['task_type']}"
            )


class TestSynonymGroupCrawlSave:
    """爬取↔保存 同义组应让'爬'系查询加分到含'保存'keyword 的 skill。"""

    def test_crawl_matches_save_keyword(self):
        """'抓取文章' 无 kw_exact 但应通过同义词组+bigram 命中 web_archive。"""
        r = match_task_candidates("抓取文章")
        assert r
        top = r[0]
        assert top["task_type"] == "adhoc.web_archive"


# ========== C. context 上下文增强 ==========

class TestReadContextFile:
    """_read_context_file 应只处理文本文件，二进制拒绝。"""

    def test_read_utf8_file(self, tmp_path):
        p = tmp_path / "test.txt"
        p.write_text("hello xiaoheihe.cn 世界", encoding="utf-8")
        text = _read_context_file(str(p))
        assert text is not None
        assert "xiaoheihe.cn" in text
        assert "世界" in text

    def test_read_ansi_gbk_file(self, tmp_path):
        """GBK/ANSI 编码文件应该能读。"""
        p = tmp_path / "test_gbk.txt"
        p.write_text("中文内容 xiaoheihe.cn", encoding="gbk")
        text = _read_context_file(str(p))
        assert text is not None
        assert "xiaoheihe.cn" in text

    def test_binary_file_rejected(self, tmp_path):
        """含 NUL 字节的二进制文件应返回 None。"""
        p = tmp_path / "test.bin"
        p.write_bytes(b"\x00\x01\x02\x03 binary\x00 data")
        assert _read_context_file(str(p)) is None

    def test_nonexistent_file_returns_none(self, tmp_path):
        assert _read_context_file(str(tmp_path / "nonexistent.txt")) is None

    def test_directory_returns_none(self, tmp_path):
        """目录不应被当作文件读取。"""
        assert _read_context_file(str(tmp_path)) is None


class TestExtractContextSignals:
    """_extract_context_signals 应提取 URL domain 和中文 bigrams。"""

    def test_extract_domains_from_text(self):
        sigs = _extract_context_signals("看 https://xiaoheihe.cn/article 和 http://mp.weixin.qq.com/s?xx")
        assert "xiaoheihe.cn" in sigs["domains"]
        assert "mp.weixin.qq.com" in sigs["domains"]

    def test_extract_domains_from_file(self, tmp_path):
        p = tmp_path / "tabs.json"
        p.write_text(json.dumps([
            {"url": "https://www.xiaoheihe.cn/app/bbs/link/abc"},
            {"url": "https://mp.weixin.qq.com/s?id=123"},
        ]), encoding="utf-8")
        sigs = _extract_context_signals(str(p))
        assert "www.xiaoheihe.cn" in sigs["domains"]
        assert "mp.weixin.qq.com" in sigs["domains"]

    def test_chinese_bigrams_only(self, tmp_path):
        """bigrams 应只含中文 bigrams，过滤英文/数字（避免 URL 参数名噪音）。"""
        # URL 含英文参数名 link/description/camp，这些不应进 bigrams
        p = tmp_path / "tabs.json"
        p.write_text(
            '{"url": "https://xiaoheihe.cn/link?description=hello&camp=x"}',
            encoding="utf-8",
        )
        sigs = _extract_context_signals(str(p))
        # 不应含纯英文 bigrams
        for bg in sigs["bigrams"]:
            assert all(ord(c) > 127 for c in bg), f"bigram {bg!r} 含非中文字符"

    def test_empty_context_returns_none(self):
        assert _extract_context_signals("") is None
        assert _extract_context_signals("   ") is None
        assert _extract_context_signals(None) is None


class TestContextBoost:
    """context 加分应让 web_archive 分数提升。"""

    def test_xiaoheihe_context_boosts_web_archive(self, tmp_path):
        """含 xiaoheihe.cn 的 context 应让 web_archive 加分。"""
        p = tmp_path / "tabs.json"
        p.write_text(json.dumps([
            {"url": "https://www.xiaoheihe.cn/app/bbs/link/abc"},
        ]), encoding="utf-8")

        # 无 context
        r_no_ctx = match_task_candidates("爬一下文章")
        score_no_ctx = r_no_ctx[0]["score"]

        # 有 context
        r_ctx = match_task_candidates("爬一下文章", context=str(p))
        score_ctx = r_ctx[0]["score"]

        assert r_ctx[0]["task_type"] == "adhoc.web_archive"
        assert score_ctx > score_no_ctx, (
            f"context 加分未生效：无 ctx score={score_no_ctx}，有 ctx score={score_ctx}"
        )
        # 应该有 domain_hint 的 matched reason
        matched_str = ",".join(r_ctx[0]["matched"])
        assert "domain_hint" in matched_str

    def test_binary_context_no_boost(self, tmp_path):
        """二进制文件 context 不应产生加分（_read_context_file 返回 None）。"""
        p = tmp_path / "tabs.bin"
        p.write_bytes(b"\x00\x01 binary xiaoheihe.cn \x00 data")
        sigs = _extract_context_signals(str(p))
        # 二进制文件读取应失败，domains 为空
        assert "xiaoheihe.cn" not in sigs["domains"]

    def test_no_domain_collision_with_english_keywords(self, tmp_path):
        """context 含 URL 参数名不应让英文 keywords 的 skill 误匹配。

        回归：曾因 context 的英文 bigrams（link/description/camp）跟
        dev.impeccable 的英文 keywords（craft/polish/critique）大量重叠，
        导致 impeccable 得 64 分误匹配。
        """
        p = tmp_path / "tabs.json"
        p.write_text(json.dumps([
            {"url": "https://xiaoheihe.cn/link?description=hello&session_id=xxx&camp=y"},
        ]), encoding="utf-8")
        r = match_task_candidates("爬一下文章", context=str(p))
        # top-1 应该是 web_archive 而非 impeccable
        assert r[0]["task_type"] == "adhoc.web_archive"
        # impeccable 不应该在 top-3
        top3 = [c["task_type"] for c in r[:3]]
        assert "dev.impeccable" not in top3, (
            f"impeccable 误匹配：top-3={top3}"
        )


# ========== 集成：原查询 + 真实 context ==========

class TestOriginalUserQuery:
    """验证用户原始查询（爬一下文章 + tabs json）能正确路由到 web_archive。

    这是改进的核心目标：用户原话'爬一下文章'曾让 agent_guide 失配，
    现在应该 strong_match 到 web_archive。
    """

    def test_original_query_no_context(self):
        """即使无 context，'爬一下文章' 也应匹配 web_archive。"""
        r = match_task_candidates("爬一下文章")
        assert r[0]["task_type"] == "adhoc.web_archive"

    def test_original_query_with_real_context(self):
        """用真实 tabs json 文件作为 context 应让 web_archive 高分命中。

        使用项目 temp/ 下的真实文件（如不存在则跳过）。
        """
        # 找一个真实的 tabs json 文件
        candidates = list(Path("temp").glob("tabs_current-window_all_*.json"))
        if not candidates:
            pytest.skip("无 tabs_current-window_all_*.json 测试文件")
        ctx_path = str(candidates[0])

        r = match_task_candidates("爬一下文章", context=ctx_path)
        top = r[0]
        assert top["task_type"] == "adhoc.web_archive"
        assert top["score"] >= 15, f"context 加分后分数应≥15，实际 {top['score']}"
        # 第二名应明显低于 top-1（避免歧义）
        if len(r) > 1:
            assert top["score"] - r[1]["score"] >= 5, (
                f"top-1 与 top-2 分差应≥5，实际 top1={top['score']} top2={r[1]['score']}"
            )


# ========== entry/method/support 分层修复（2026-08-05 关键词扫描修复） ==========

class TestEntryMethodSeparation:
    """dev 桶 entry/method/support 分层：entry 独占用户触发词。

    修复背景：dev.leader (method) 曾挊 5 个 entry 触发词，导致'帮我定目标'路由到 leader
    而非 goal_engineering。dev.grilling (support) 曾共享'找漏洞/打磨计划'导致平局。
    详见 docs/agent-guide-keywords.md 原则 2。
    """

    @pytest.mark.parametrize("query", [
        "帮我定目标",
        "定义目标",
        "目标工程",
        "goal engineering",
    ])
    def test_goal_engineering_entry_wins(self, query):
        """entry 触发词应路由到 goal_engineering 而非 leader (method)。"""
        r = match_task_candidates(query)
        assert r, f"无匹配: {query}"
        top = r[0]
        assert top["task_type"] == "dev.goal_engineering", (
            f"{query!r} 应匹配 entry (goal_engineering)，实际 top-1={top['task_type']}"
        )
        # leader 不应该反超 entry
        if len(r) > 1:
            assert r[1]["task_type"] != "dev.leader" or r[1]["score"] < top["score"], (
                f"{query!r}: leader (method) 不应反超 goal_engineering (entry)"
            )

    @pytest.mark.parametrize("query", [
        "找漏洞",
        "打磨计划",
        "拷问我",
    ])
    def test_grill_me_entry_wins(self, query):
        """grill_me (entry) 触发词不应被 grilling (support) 抢。"""
        r = match_task_candidates(query)
        assert r, f"无匹配: {query}"
        top = r[0]
        assert top["task_type"] == "dev.grill_me", (
            f"{query!r} 应匹配 entry (grill_me)，实际 top-1={top['task_type']}"
        )

    def test_leader_no_user_trigger_words(self):
        """leader (method) 不应含用户触发词。"""
        leader_kws = GUIDE_REGISTRY["dev.leader"]["keywords"]
        # 这些是 goal_engineering (entry) 的触发词，leader 不该有
        entry_triggers = {"帮我定目标", "定义目标", "让 agent 自己跑", "目标工程", "goal engineering"}
        shared = entry_triggers & set(leader_kws)
        assert not shared, f"leader (method) 不应含 entry 触发词: {shared}"

    def test_grilling_no_user_trigger_words(self):
        """grilling (support) 不应含用户触发词。"""
        grilling_kws = GUIDE_REGISTRY["dev.grilling"]["keywords"]
        # 这些是 grill_me (entry) 的触发词，grilling 不该有
        entry_triggers = {"找漏洞", "打磨计划", "拷问", "追问"}
        shared = entry_triggers & set(grilling_kws)
        assert not shared, f"grilling (support) 不应含 entry 触发词: {shared}"


class TestPrototypeNoGenericVerb:
    """prototype 不该用'试一下/看看效果'等泛动词作 keyword。

    修复背景：'试一下'曾 strong_match prototype，但用户说'试一下保存文章'会被抢。
    详见 docs/agent-guide-keywords.md 原则 4。
    """

    def test_shiyixia_not_strong_match_prototype(self):
        """'试一下' 不应该 strong_match prototype。"""
        r = match_task_candidates("试一下")
        if r:
            top = r[0]
            # 要么不匹配 prototype，要么匹配但 not strong_match
            if top["task_type"] == "dev.prototype":
                assert not top["strong_match"], (
                    "'试一下' 不应 strong_match prototype（泛动词误匹配）"
                )

    def test_prototype_keywords_no_aux_particles(self):
        """prototype keywords 不应含语气助词。"""
        kws = set(GUIDE_REGISTRY["dev.prototype"]["keywords"])
        aux_blacklist = {"试一下", "看一下", "看看效果", "弄一下", "搞一下"}
        bad = kws & aux_blacklist
        assert not bad, f"prototype keywords 含语气助词: {bad}"

    def test_shiji_fangan_matches_prototype(self):
        """'试几种方案' 应匹配 prototype（替代'试一下'的精确表达）。"""
        r = match_task_candidates("试几种方案")
        assert r
        assert r[0]["task_type"] == "dev.prototype"


class TestAntiHallucinationScoped:
    """anti_hallucination keywords 收紧为场景词，不再含纯动作词。

    修复背景：'改代码/修 bug/重构/加功能'等纯动作词几乎命中所有开发任务，
    跟 goal_engineering 冲突。修复后保留'修 bug'但加场景限定词。
    详见 docs/agent-guide-keywords.md 原则 1。
    """

    def test_xiu_bug_still_matches(self):
        """'修 bug' 应该匹配 anti_hallucination（用户明确要修 bug）。"""
        r = match_task_candidates("修 bug")
        assert r
        assert r[0]["task_type"] == "dev.anti_hallucination"

    def test_chonggou_not_match_anti(self):
        """'重构' 不应该匹配 anti_hallucination（应归 goal_engineering）。"""
        r = match_task_candidates("重构")
        assert r
        top = r[0]
        assert top["task_type"] != "dev.anti_hallucination", (
            f"'重构' 不应匹配 anti_hallucination，实际 top-1={top['task_type']}"
        )

    def test_jiagongneng_not_match_anti(self):
        """'加功能' 不应该匹配 anti_hallucination（应归 goal_engineering）。"""
        r = match_task_candidates("加功能")
        if r:
            top = r[0]
            assert top["task_type"] != "dev.anti_hallucination", (
                f"'加功能' 不应匹配 anti_hallucination，实际 top-1={top['task_type']}"
            )

    def test_why_series_matches_anti(self):
        """'为什么'系应该匹配 anti_hallucination（明确表达要理解原因）。"""
        for q in ["为什么不对", "为什么不显示", "为什么报错"]:
            r = match_task_candidates(q)
            assert r, f"无匹配: {q}"
            assert r[0]["task_type"] == "dev.anti_hallucination", (
                f"{q!r} 应匹配 anti_hallucination"
            )

    def test_understand_code_matches_anti(self):
        """'这段代码怎么工作' 应匹配 anti_hallucination。"""
        r = match_task_candidates("这段代码怎么工作")
        assert r
        assert r[0]["task_type"] == "dev.anti_hallucination"

    def test_anti_keywords_no_generic_action_verbs(self):
        """anti_hallucination keywords 不应含纯动作词。"""
        kws = set(GUIDE_REGISTRY["dev.anti_hallucination"]["keywords"])
        # 这些纯动作词已移除（归 goal_engineering）
        generic_verbs = {"改代码", "重构", "加功能", "重命名", "排查", "定位"}
        bad = kws & generic_verbs
        assert not bad, f"anti_hallucination keywords 仍含纯动作词: {bad}"


class TestSharedKeywordsReduction:
    """验证共享 keywords 减少（entry/method 分层修复后）。"""

    def test_leader_no_longer_shares_with_goal_engineering(self):
        """leader 和 goal_engineering 不应共享用户触发词。"""
        leader_kws = set(GUIDE_REGISTRY["dev.leader"]["keywords"])
        ge_kws = set(GUIDE_REGISTRY["dev.goal_engineering"]["keywords"])
        shared = leader_kws & ge_kws
        # 允许共享方法论标识词（如 leader），但不该共享用户触发词
        user_triggers = {"帮我定目标", "定义目标", "让 agent 自己跑", "目标工程", "goal engineering"}
        bad_shared = shared & user_triggers
        assert not bad_shared, (
            f"leader 和 goal_engineering 仍共享 entry 触发词: {bad_shared}"
        )

    def test_grilling_no_longer_shares_with_grill_me(self):
        """grilling 和 grill_me 不应共享用户触发词。"""
        grilling_kws = set(GUIDE_REGISTRY["dev.grilling"]["keywords"])
        gm_kws = set(GUIDE_REGISTRY["dev.grill_me"]["keywords"])
        shared = grilling_kws & gm_kws
        # 不该有任何共享词
        assert not shared, f"grilling 和 grill_me 仍共享 keywords: {shared}"

