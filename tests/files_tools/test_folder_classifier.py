"""test_folder_classifier.py — 文件夹分类协议单元测试（ticket 06 Seam 1）

用 importlib.util 动态加载 tools/file_classifier/predictor.py，
mock llm_caller，覆盖 8 个 slice：
  1. 调用次数计数（1→15，达上限停止）
  2. token 预算监控（累计达 180k 强制停止扩展）
  3. 无状态重调（每次调用不含过去上下文，但含调用次数）
  4. need_more 逻辑（LLM 请求文件夹，补全后重调）
  5. 累计文件数上限（1500）
  6. 单次请求文件夹数上限（10）
  7. 截断提示（文件名超 150 字符）
  8. 降级（LLM 不可用→规则匹配；达上限→标记未知）
"""

import importlib.util
import os

# 动态加载 predictor 模块
_PREDICTOR_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "tools", "file_classifier", "predictor.py",
)
spec = importlib.util.spec_from_file_location("predictor_under_test", _PREDICTOR_PATH)
predictor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(predictor)


# ── 辅助：构造 mock llm_caller ──

def _make_response(classification="文档", confidence=0.8, need_more=None, reason="ok"):
    """构造 LLM JSON 响应字符串"""
    import json
    return json.dumps({
        "classification": classification,
        "confidence": confidence,
        "need_more": need_more or [],
        "reason": reason,
    }, ensure_ascii=False)


class _CallRecorder:
    """记录每次 llm_caller 调用的 prompt，可按序返回不同响应"""

    def __init__(self, responses):
        # responses: list[str|None]，按顺序返回；None 表示 LLM 不可用
        self.responses = list(responses)
        self.prompts = []
        self.call_count = 0

    def __call__(self, prompt, model=None):
        self.prompts.append(prompt)
        self.call_count += 1
        if self.call_count > len(self.responses):
            return None
        return self.responses[self.call_count - 1]


# ── slice 1: 调用次数计数（1→15，达上限停止）──

def test_call_count_reaches_max_stops(tmp_path):
    """LLM 一直返回 need_more，达 FOLDER_MAX_CALLS 次后停止并标记未知"""
    (tmp_path / "a.txt").write_text("a")
    # 一直返回 need_more，迫使循环达上限
    recorder = _CallRecorder([_make_response(need_more=["sub1"], confidence=0.5)
                              for _ in range(predictor.FOLDER_MAX_CALLS + 5)])
    result = predictor.classify_folder(str(tmp_path),
                                       [{"name": "文档", "extensions": ["txt"]}],
                                       llm_caller=recorder)
    assert result["calls_made"] == predictor.FOLDER_MAX_CALLS
    assert result["degraded"] is True
    assert result["degrade_reason"] == "max_calls_exceeded"
    assert result["classification"] == "未知"
    # 调用次数不应超过上限
    assert recorder.call_count <= predictor.FOLDER_MAX_CALLS


def test_call_count_increments_on_need_more(tmp_path):
    """need_more 触发重调时 call_count 递增"""
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b")
    # 第 1 次返回 need_more，第 2 次返回最终分类
    recorder = _CallRecorder([
        _make_response(classification="", confidence=0.3, need_more=["sub"], reason="need info"),
        _make_response(classification="文档", confidence=0.9, need_more=[], reason="ok"),
    ])
    result = predictor.classify_folder(str(tmp_path),
                                       [{"name": "文档", "extensions": ["txt"]}],
                                       llm_caller=recorder)
    assert result["calls_made"] == 2
    assert result["classification"] == "文档"
    assert result["degraded"] is False


# ── slice 2: token 预算监控（累计达 180k 强制停止扩展）──

def test_token_budget_exceeded_stops_expansion(tmp_path):
    """累计 prompt 字符达 FOLDER_TOKEN_BUDGET 时强制停止并返回降级结果"""
    (tmp_path / "a.txt").write_text("a")
    # 构造一个超长 prompt 的场景：让 llm_caller 返回 need_more
    # 但通过 monkey-patch build_folder_prompt 让 prompt 非常长
    long_str = "x" * 200_000

    def fake_build(folder_name, folder_info, categories, call_count, need_more_paths):
        return long_str  # 单次 prompt 就达 200k 字符

    original_build = predictor.build_folder_prompt
    predictor.build_folder_prompt = fake_build
    try:
        recorder = _CallRecorder([_make_response(need_more=["sub"], confidence=0.5)])
        result = predictor.classify_folder(str(tmp_path),
                                           [{"name": "文档", "extensions": ["txt"]}],
                                           llm_caller=recorder)
        # 累计字符超过 180k，应触发 token_budget_exceeded
        assert result["degraded"] is True
        assert result["degrade_reason"] == "token_budget_exceeded"
    finally:
        predictor.build_folder_prompt = original_build


# ── slice 3: 无状态重调（每次调用不含过去上下文，但含调用次数）──

def test_stateless_recall_has_call_count(tmp_path):
    """每次 prompt 包含调用次数，但不包含前一次的响应内容"""
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b")
    secret_marker = "UNIQUE_PREV_RESPONSE_MARKER_42"
    # 第 1 次响应包含特殊标记，第 2 次响应直接给分类
    recorder = _CallRecorder([
        _make_response(classification="", confidence=0.3, need_more=["sub"],
                       reason=secret_marker),
        _make_response(classification="文档", confidence=0.9, need_more=[], reason="ok"),
    ])
    predictor.classify_folder(str(tmp_path),
                              [{"name": "文档", "extensions": ["txt"]}],
                              llm_caller=recorder)
    # 第 2 次 prompt 不应包含第 1 次的响应内容（无状态）
    assert len(recorder.prompts) == 2
    assert secret_marker not in recorder.prompts[1]
    # 第 2 次 prompt 应包含调用次数 2/15
    assert "2/15" in recorder.prompts[1]
    # 第 1 次 prompt 应包含调用次数 1/15
    assert "1/15" in recorder.prompts[0]


# ── slice 4: need_more 逻辑（LLM 请求文件夹，补全后重调）──

def test_need_more_triggers_subfolder_expansion(tmp_path):
    """LLM 请求 sub 文件夹，循环应补全该子文件夹信息后重新调用"""
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.jpg").write_text("b")
    (tmp_path / "sub" / "c.jpg").write_text("c")
    recorder = _CallRecorder([
        _make_response(classification="", confidence=0.3, need_more=["sub"], reason="need info"),
        _make_response(classification="图片", confidence=0.85, need_more=[], reason="ok"),
    ])
    result = predictor.classify_folder(str(tmp_path),
                                       [{"name": "图片", "extensions": ["jpg"]},
                                        {"name": "文档", "extensions": ["txt"]}],
                                       llm_caller=recorder)
    assert result["calls_made"] == 2
    assert result["classification"] == "图片"
    # 第 2 次 prompt 应包含补全提示（need_more hint）
    assert "sub" in recorder.prompts[1]
    # folder_info 应已合并子文件夹的扩展名分布（jpg: 2）
    assert result["folder_info"]["ext_distribution"].get("jpg", 0) == 2


def test_need_more_empty_returns_immediately(tmp_path):
    """need_more 为空时立即返回分类，不重调"""
    (tmp_path / "a.txt").write_text("a")
    recorder = _CallRecorder([
        _make_response(classification="文档", confidence=0.9, need_more=[], reason="ok"),
    ])
    result = predictor.classify_folder(str(tmp_path),
                                       [{"name": "文档", "extensions": ["txt"]}],
                                       llm_caller=recorder)
    assert result["calls_made"] == 1
    assert recorder.call_count == 1


# ── slice 5: 累计文件数上限（1500）──

def test_collect_folder_info_respects_max_files(tmp_path):
    """collect_folder_info 在 file_count 达 max_files 时停止并标记 truncated"""
    # 创建 20 个文件，max_files=10 测试截断
    for i in range(20):
        (tmp_path / f"f{i}.txt").write_text("x")
    result = predictor.collect_folder_info(str(tmp_path), max_files=10)
    assert result["file_count"] == 10
    assert result["truncated"] is True


def test_classify_folder_with_truncated_info_returns_result(tmp_path):
    """即使 folder_info 被截断，分类仍能正常进行"""
    (tmp_path / "a.txt").write_text("a")
    # 用较小的 max_files 触发截断（通过 monkey-patch）
    original_collect = predictor.collect_folder_info

    def fake_collect(folder_path, max_files=predictor.FOLDER_MAX_FILES,
                     sample_per_level=predictor.FOLDER_SAMPLE_PER_LEVEL):
        info = original_collect(folder_path, max_files=max_files,
                                sample_per_level=sample_per_level)
        info["truncated"] = True
        return info

    predictor.collect_folder_info = fake_collect
    try:
        recorder = _CallRecorder([
            _make_response(classification="文档", confidence=0.9, need_more=[], reason="ok"),
        ])
        result = predictor.classify_folder(str(tmp_path),
                                           [{"name": "文档", "extensions": ["txt"]}],
                                           llm_caller=recorder)
        assert result["classification"] == "文档"
        assert result["folder_info"]["truncated"] is True
    finally:
        predictor.collect_folder_info = original_collect


# ── slice 6: 单次请求文件夹数上限（10）──

def test_need_more_capped_at_max(tmp_path):
    """LLM 返回超过 FOLDER_MAX_NEED_MORE 个 need_more 路径时，应被截断到上限"""
    (tmp_path / "a.txt").write_text("a")
    # 创建 15 个子文件夹（LLM 请求 15 个，但只应处理前 10 个）
    for i in range(15):
        sub = tmp_path / f"sub{i}"
        sub.mkdir()
        (sub / "f.txt").write_text("x")
    many_paths = [f"sub{i}" for i in range(15)]
    recorder = _CallRecorder([
        _make_response(classification="", confidence=0.3, need_more=many_paths, reason="need all"),
        _make_response(classification="文档", confidence=0.9, need_more=[], reason="ok"),
    ])
    result = predictor.classify_folder(str(tmp_path),
                                       [{"name": "文档", "extensions": ["txt"]}],
                                       llm_caller=recorder)
    assert result["calls_made"] == 2
    # parse_folder_response 已截断 need_more 到 10
    # 第 2 次 prompt 应包含补全提示（最多 10 个子路径）
    assert "sub0" in recorder.prompts[1]


def test_parse_folder_response_caps_need_more():
    """parse_folder_response 直接截断 need_more 到 FOLDER_MAX_NEED_MORE"""
    import json
    paths = [f"p{i}" for i in range(20)]
    content = json.dumps({
        "classification": "x", "confidence": 0.5,
        "need_more": paths, "reason": "y",
    })
    parsed = predictor.parse_folder_response(content)
    assert len(parsed["need_more"]) == predictor.FOLDER_MAX_NEED_MORE


# ── slice 7: 截断提示（文件名超 150 字符）──

def test_truncate_filename_short_unchanged():
    """短文件名不变"""
    assert predictor.truncate_filename("a.txt") == "a.txt"


def test_truncate_filename_exact_limit_unchanged():
    """恰好 150 字符不变"""
    name = "a" * predictor.FOLDER_MAX_FILENAME_LEN
    assert predictor.truncate_filename(name) == name


def test_truncate_filename_over_limit_adds_marker():
    """超 150 字符截断并加 "…(截断)" """
    name = "a" * (predictor.FOLDER_MAX_FILENAME_LEN + 50)
    result = predictor.truncate_filename(name)
    assert result.endswith("…(截断)")
    assert len(result) == predictor.FOLDER_MAX_FILENAME_LEN + len("…(截断)")


def test_collect_folder_info_truncates_long_filenames(tmp_path):
    """collect_folder_info 对超长文件名采样应用截断"""
    long_name = "x" * 200 + ".txt"
    (tmp_path / long_name).write_text("a")
    info = predictor.collect_folder_info(str(tmp_path))
    # samples 中的文件名应被截断
    sample = info["samples"][0]
    assert "…(截断)" in sample
    assert len(sample) <= predictor.FOLDER_MAX_FILENAME_LEN + len("…(截断)") + len(".txt")


# ── slice 8: 降级（LLM 不可用→规则匹配；达上限→标记未知）──

def test_llm_unavailable_degrades_to_rule_based(tmp_path):
    """llm_caller 返回 None 时降级为规则匹配"""
    (tmp_path / "a.jpg").write_text("a")
    (tmp_path / "b.jpg").write_text("b")
    (tmp_path / "c.txt").write_text("c")
    recorder = _CallRecorder([None])  # LLM 不可用
    result = predictor.classify_folder(str(tmp_path),
                                       [{"name": "图片", "extensions": ["jpg"]},
                                        {"name": "文档", "extensions": ["txt"]}],
                                       llm_caller=recorder)
    assert result["degraded"] is True
    assert result["degrade_reason"] == "llm_unavailable"
    # 规则匹配：jpg 出现 2 次，应归入"图片"
    assert result["classification"] == "图片"
    assert result["confidence"] > 0


def test_rule_based_folder_predict_with_matching_ext():
    """规则匹配：能匹配到分类时给出合理置信度"""
    info = {
        "folder_name": "test",
        "file_count": 5,
        "ext_distribution": {"jpg": 3, "txt": 2},
        "samples": [],
        "truncated": False,
        "error": None,
    }
    categories = [{"name": "图片", "extensions": ["jpg"]},
                  {"name": "文档", "extensions": ["txt"]}]
    result = predictor._rule_based_folder_predict(info, categories)
    assert result["classification"] == "图片"
    assert result["confidence"] > 0.3


def test_rule_based_folder_predict_no_match():
    """规则匹配：无扩展名匹配到分类时返回"未知" """
    info = {
        "folder_name": "test",
        "file_count": 3,
        "ext_distribution": {"xyz": 3},
        "samples": [],
        "truncated": False,
        "error": None,
    }
    categories = [{"name": "图片", "extensions": ["jpg"]}]
    result = predictor._rule_based_folder_predict(info, categories)
    assert result["classification"] == "未知"
    assert result["confidence"] <= 0.3


# ── 补充：parse_folder_response 边界 ──

def test_parse_folder_response_empty():
    """空内容返回无效结果"""
    parsed = predictor.parse_folder_response("")
    assert parsed["valid"] is False
    assert parsed["classification"] == ""


def test_parse_folder_response_markdown_block():
    """支持 markdown 代码块包裹的 JSON"""
    import json
    data = {"classification": "文档", "confidence": 0.9, "need_more": [], "reason": "ok"}
    content = f"```json\n{json.dumps(data, ensure_ascii=False)}\n```"
    parsed = predictor.parse_folder_response(content)
    assert parsed["valid"] is True
    assert parsed["classification"] == "文档"
    assert parsed["confidence"] == 0.9


def test_parse_folder_response_invalid_json():
    """无效 JSON 返回无效结果"""
    parsed = predictor.parse_folder_response("not a json")
    assert parsed["valid"] is False


def test_parse_folder_response_confidence_clamped():
    """置信度被规范化到 [0, 1]"""
    import json
    content = json.dumps({"classification": "x", "confidence": 1.5, "need_more": [], "reason": ""})
    parsed = predictor.parse_folder_response(content)
    assert parsed["confidence"] == 1.0

    content = json.dumps({"classification": "x", "confidence": -0.5, "need_more": [], "reason": ""})
    parsed = predictor.parse_folder_response(content)
    assert parsed["confidence"] == 0.0


# ── 补充：collect_folder_info 基础场景 ──

def test_collect_folder_info_empty_dir(tmp_path):
    """空文件夹返回零计数"""
    info = predictor.collect_folder_info(str(tmp_path))
    assert info["file_count"] == 0
    assert info["samples"] == []
    assert info["ext_distribution"] == {}
    assert info["truncated"] is False
    assert info["error"] is None


def test_collect_folder_info_nonexistent_path():
    """不存在的路径返回 error"""
    info = predictor.collect_folder_info("Z:/nonexistent/path/xyz")
    assert info["error"] is not None
    assert info["file_count"] == 0


def test_collect_folder_info_with_nested_files(tmp_path):
    """嵌套文件夹的文件被正确统计"""
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.jpg").write_text("b")
    (tmp_path / "sub" / "c.jpg").write_text("c")
    info = predictor.collect_folder_info(str(tmp_path))
    assert info["file_count"] == 3
    assert info["ext_distribution"].get("txt") == 1
    assert info["ext_distribution"].get("jpg") == 2
    # samples 应包含带相对路径的文件名
    samples_joined = " ".join(info["samples"])
    assert "a.txt" in samples_joined
    assert "b.jpg" in samples_joined


# ── 补充：build_folder_prompt 基础 ──

def test_build_folder_prompt_includes_call_count():
    """prompt 中包含调用次数计数"""
    info = {
        "folder_name": "test",
        "file_count": 5,
        "ext_distribution": {"txt": 5},
        "samples": ["a.txt", "b.txt"],
        "truncated": False,
        "error": None,
    }
    prompt = predictor.build_folder_prompt("test", info,
                                           [{"name": "文档", "extensions": ["txt"]}],
                                           call_count=3)
    assert "3/15" in prompt
    assert "文档" in prompt
    assert "test" in prompt


def test_build_folder_prompt_includes_need_more_hint():
    """need_more_paths 非空时 prompt 包含补全提示"""
    info = {
        "folder_name": "test",
        "file_count": 1,
        "ext_distribution": {"txt": 1},
        "samples": ["a.txt"],
        "truncated": False,
        "error": None,
    }
    prompt = predictor.build_folder_prompt("test", info,
                                           [{"name": "文档", "extensions": ["txt"]}],
                                           call_count=2,
                                           need_more_paths=["sub1", "sub2"])
    assert "sub1" in prompt
    assert "sub2" in prompt
    assert "补全" in prompt
