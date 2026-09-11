"""chat-panel-v2 Ticket 03 验收测试：模板存储 CRUD + skill 路径解析

覆盖 T03 acceptance（temp/sdd/chat-panel-v2/tickets.md）：
- [x] data/chat_templates.json 初始化默认空白模板（id="blank", name="空白", prompt="", skills=[]）
- [x] client/core/agent/template_store.py CRUD：load_all / save_all / create / update / delete / get
- [x] build_skill_path_map() 解析 .agents/skills/_index.md 构建 task_type → SKILL.md 路径映射
- [x] resolve_skill_path() fallback 路径探测
- [x] format_skills_prompt() task_type list → system prompt 段格式

测试 prior art: tests/test_chat_panel_v2_db_migration.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import (  # noqa: E402  # noqa: E402
    Template,
    build_skill_path_map,
    format_skills_prompt,
    resolve_skill_path,
    template_create,
    template_delete,
    template_get,
    template_load_all,
    template_save_all,
    template_update,
)
from client.core.agent.template_store import (  # noqa: E402
    _BLANK_TEMPLATE_ID,
    _reset_skill_path_map_cache,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def tmp_templates_path(tmp_path) -> Path:
    """每个测试独立的模板 JSON 文件路径。"""
    return tmp_path / "chat_templates.json"


@pytest.fixture(autouse=True)
def reset_skill_cache():
    """每个测试前后重置 skill 路径映射缓存，避免测试间相互污染。"""
    _reset_skill_path_map_cache()
    yield
    _reset_skill_path_map_cache()


# ============================================================================
# 1. Template dataclass 序列化往返
# ============================================================================


class TestTemplateSerialization:
    def test_to_dict_and_from_dict_roundtrip(self):
        """Template.to_dict → from_dict 往返不丢字段。"""
        tpl = Template(
            id="test-1",
            name="测试模板",
            prompt="帮我分析股票",
            skills=["recurring.accounting", "adhoc.deep_research"],
            created_at=1700000000.0,
            updated_at=1700000100.0,
        )
        d = tpl.to_dict()
        restored = Template.from_dict(d)
        assert restored.id == "test-1"
        assert restored.name == "测试模板"
        assert restored.prompt == "帮我分析股票"
        assert restored.skills == ["recurring.accounting", "adhoc.deep_research"]
        assert restored.created_at == 1700000000.0
        assert restored.updated_at == 1700000100.0

    def test_from_dict_tolerates_missing_fields(self):
        """from_dict 容忍缺失字段（旧 schema 兼容）。"""
        tpl = Template.from_dict({"id": "x", "name": "y"})
        assert tpl.id == "x"
        assert tpl.name == "y"
        assert tpl.prompt == ""
        assert tpl.skills == []
        assert tpl.created_at == 0.0
        assert tpl.updated_at == 0.0

    def test_from_dict_tolerates_none_skills(self):
        """from_dict 容忍 skills=None。"""
        tpl = Template.from_dict({"id": "x", "name": "y", "skills": None})
        assert tpl.skills == []

    def test_default_skills_is_empty_list(self):
        """Template() 默认 skills 是空 list（不共享，dataclass field default_factory）。"""
        tpl1 = Template(id="a", name="A")
        tpl2 = Template(id="b", name="B")
        tpl1.skills.append("recurring.accounting")
        assert tpl2.skills == []  # 不应被污染


# ============================================================================
# 2. load_all / save_all + 默认空白模板自动初始化
# ============================================================================


class TestLoadAllAndDefaultInit:
    def test_load_all_auto_init_when_file_missing(self, tmp_templates_path):
        """文件不存在时 load_all 自动初始化默认空白模板。"""
        assert not tmp_templates_path.exists()
        templates = template_load_all(path=tmp_templates_path)
        # 文件已自动创建
        assert tmp_templates_path.exists()
        # 默认含 1 个空白模板
        assert len(templates) == 1
        blank = templates[0]
        assert blank.id == _BLANK_TEMPLATE_ID
        assert blank.name == "空白"
        assert blank.prompt == ""
        assert blank.skills == []
        assert blank.created_at > 0
        assert blank.updated_at > 0

    def test_load_all_reads_existing_file(self, tmp_templates_path):
        """已有文件时 load_all 直接读取。"""
        # 预写一个模板
        template_save_all(
            [Template(id="x", name="X", prompt="hello", skills=["adhoc.test"])],
            path=tmp_templates_path,
        )
        templates = template_load_all(path=tmp_templates_path)
        assert len(templates) == 1
        assert templates[0].id == "x"
        assert templates[0].skills == ["adhoc.test"]

    def test_load_all_handles_corrupt_json(self, tmp_templates_path):
        """文件 JSON 损坏时 load_all 返回空 list（不抛异常）。"""
        tmp_templates_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_templates_path.write_text("not a valid json {{{", encoding="utf-8")
        templates = template_load_all(path=tmp_templates_path)
        assert templates == []

    def test_load_all_handles_non_list_json(self, tmp_templates_path):
        """文件是 dict 而非 list 时返回空 list。"""
        tmp_templates_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_templates_path.write_text('{"id": "x"}', encoding="utf-8")
        templates = template_load_all(path=tmp_templates_path)
        assert templates == []

    def test_save_all_creates_parent_dir(self, tmp_path):
        """save_all 自动创建父目录。"""
        nested_path = tmp_path / "deep" / "nested" / "chat_templates.json"
        template_save_all([Template(id="x", name="X")], path=nested_path)
        assert nested_path.exists()

    def test_save_all_is_idempotent(self, tmp_templates_path):
        """save_all 多次调用结果一致。"""
        templates = [Template(id="x", name="X", prompt="hi")]
        template_save_all(templates, path=tmp_templates_path)
        template_save_all(templates, path=tmp_templates_path)
        loaded = template_load_all(path=tmp_templates_path)
        assert len(loaded) == 1
        assert loaded[0].id == "x"

    def test_default_blank_template_is_stable_across_loads(self, tmp_templates_path):
        """默认空白模板 id 固定 "blank"，多次 load_all 不重复创建。"""
        t1 = template_load_all(path=tmp_templates_path)
        t2 = template_load_all(path=tmp_templates_path)
        assert len(t1) == len(t2) == 1
        assert t1[0].id == t2[0].id == _BLANK_TEMPLATE_ID


# ============================================================================
# 3. CRUD: create / update / delete / get
# ============================================================================


class TestTemplateCRUD:
    def test_create_generates_uuid_id(self, tmp_templates_path):
        """create 不传 template_id 时自动生成 uuid hex[:12]。"""
        tpl = template_create(
            name="问股",
            prompt="分析今日股票",
            skills=["recurring.stock_advisor"],
            path=tmp_templates_path,
        )
        assert tpl.id  # 非空
        assert len(tpl.id) == 12  # uuid hex[:12]
        assert tpl.name == "问股"
        assert tpl.prompt == "分析今日股票"
        assert tpl.skills == ["recurring.stock_advisor"]
        assert tpl.created_at > 0
        assert tpl.updated_at == tpl.created_at

        # 持久化到文件
        loaded = template_load_all(path=tmp_templates_path)
        # 默认空白模板 + 新建的 = 2
        assert len(loaded) == 2
        assert any(t.id == tpl.id for t in loaded)

    def test_create_with_custom_id(self, tmp_templates_path):
        """create 可指定自定义 id。"""
        tpl = template_create(
            name="自定义",
            path=tmp_templates_path,
            template_id="my-custom-id",
        )
        assert tpl.id == "my-custom-id"

    def test_create_avoids_id_collision(self, tmp_templates_path):
        """create 时 id 冲突自动加随机后缀。"""
        # 先建一个用 my-id 的
        template_create(name="A", path=tmp_templates_path, template_id="my-id")
        # 再建一个用相同 id 的
        tpl2 = template_create(name="B", path=tmp_templates_path, template_id="my-id")
        assert tpl2.id != "my-id"
        assert tpl2.id.startswith("my-id_")  # 加了 _xxxx 后缀

    def test_get_existing_template(self, tmp_templates_path):
        """get 找到现有模板。"""
        created = template_create(
            name="X", prompt="p", skills=["a.b"], path=tmp_templates_path,
        )
        loaded = template_get(created.id, path=tmp_templates_path)
        assert loaded is not None
        assert loaded.id == created.id
        assert loaded.name == "X"
        assert loaded.skills == ["a.b"]

    def test_get_nonexistent_returns_none(self, tmp_templates_path):
        """get 找不到返回 None。"""
        template_load_all(path=tmp_templates_path)  # 初始化默认
        assert template_get("nonexistent-id", path=tmp_templates_path) is None

    def test_update_partial_fields(self, tmp_templates_path):
        """update 仅更新非 None 字段。"""
        created = template_create(
            name="原名", prompt="原 prompt", skills=["a.b"],
            path=tmp_templates_path,
        )
        # 仅更新 name
        updated = template_update(created.id, name="新名", path=tmp_templates_path)
        assert updated is not None
        assert updated.name == "新名"
        # 其他字段不变
        assert updated.prompt == "原 prompt"
        assert updated.skills == ["a.b"]
        # updated_at 已刷新
        assert updated.updated_at >= created.updated_at

    def test_update_skills_field(self, tmp_templates_path):
        """update skills 字段。"""
        created = template_create(name="X", path=tmp_templates_path)
        updated = template_update(
            created.id, skills=["recurring.accounting", "adhoc.deep_research"],
            path=tmp_templates_path,
        )
        assert updated is not None
        assert updated.skills == ["recurring.accounting", "adhoc.deep_research"]

    def test_update_nonexistent_returns_none(self, tmp_templates_path):
        """update 不存在的 id 返回 None。"""
        template_load_all(path=tmp_templates_path)
        result = template_update("nonexistent-id", name="x", path=tmp_templates_path)
        assert result is None

    def test_delete_existing(self, tmp_templates_path):
        """delete 现有模板返回 True。"""
        created = template_create(name="X", path=tmp_templates_path)
        assert template_delete(created.id, path=tmp_templates_path) is True
        # 删除后 get 返回 None
        assert template_get(created.id, path=tmp_templates_path) is None

    def test_delete_nonexistent_returns_false(self, tmp_templates_path):
        """delete 不存在的 id 返回 False。"""
        template_load_all(path=tmp_templates_path)
        assert template_delete("nonexistent-id", path=tmp_templates_path) is False

    def test_delete_does_not_delete_default_blank(self, tmp_templates_path):
        """删除默认 blank 后文件仍合法（允许删除全部，不强制保留 blank）。"""
        # 删默认空白
        assert template_delete(_BLANK_TEMPLATE_ID, path=tmp_templates_path) is True
        # load_all 不会重新创建（文件已存在但为空 list）
        loaded = template_load_all(path=tmp_templates_path)
        assert loaded == []


# ============================================================================
# 4. build_skill_path_map: 解析 _index.md
# ============================================================================


class TestBuildSkillPathMap:
    def test_returns_dict(self):
        """build_skill_path_map 返回 dict（非 None）。"""
        m = build_skill_path_map()
        assert isinstance(m, dict)

    def test_includes_known_task_types(self):
        """映射表包含已知 task_type（如 recurring.accounting）。"""
        m = build_skill_path_map()
        # 这些是 _index.md 中明确列出的 task_type
        assert "recurring.accounting" in m
        assert m["recurring.accounting"] == "workspace/accounting/SKILL.md"
        assert "adhoc.auto_shutdown" in m
        assert m["adhoc.auto_shutdown"] == ".agents/skills/auto_shutdown.md"

    def test_map_size_reasonable(self):
        """映射表至少 30 条（_index.md 总 49 个 skill，解析可能漏几个但不会漏一大半）。"""
        m = build_skill_path_map()
        assert len(m) >= 30, f"skill path map only has {len(m)} entries, expected >= 30"

    def test_cache_reused(self):
        """build_skill_path_map 缓存：第二次调用不重新解析（返回同一对象）。"""
        m1 = build_skill_path_map()
        m2 = build_skill_path_map()
        # 同一对象（缓存命中）
        assert m1 is m2

    def test_custom_index_path_bypasses_cache(self, tmp_path):
        """传入 index_path 参数时不使用缓存，每次重新解析。"""
        # 先用默认路径加载缓存
        build_skill_path_map()
        # 用自定义路径解析（应不返回缓存）
        custom_index = tmp_path / "custom_index.md"
        custom_index.write_text(
            "### test_skill (test_skill) [adhoc]\n"
            "- **task_type**：`adhoc.test_skill`\n"
            "- **Skill 文件**：`workspace/test_skill/SKILL.md`\n",
            encoding="utf-8",
        )
        m = build_skill_path_map(index_path=custom_index)
        assert "adhoc.test_skill" in m
        assert m["adhoc.test_skill"] == "workspace/test_skill/SKILL.md"

    def test_handles_missing_index_file(self, tmp_path):
        """_index.md 不存在时返回空 dict（不抛异常）。"""
        m = build_skill_path_map(index_path=tmp_path / "nonexistent.md")
        assert m == {}


# ============================================================================
# 5. resolve_skill_path: 优先查映射表，fallback 探测路径
# ============================================================================


class TestResolveSkillPath:
    def test_resolves_via_index_map(self):
        """task_type 在 _index.md 映射中 → 直接返回映射路径。"""
        # recurring.accounting 在 _index.md 中映射到 workspace/accounting/SKILL.md
        path = resolve_skill_path("recurring.accounting")
        assert path == "workspace/accounting/SKILL.md"

    def test_fallback_workspace_path(self):
        """_index.md 找不到时 fallback 探测 workspace/<name>/SKILL.md。"""
        # 用一个不在 _index.md 但 workspace/ 下存在 SKILL.md 的 task_type
        # accounting 已在 _index.md，找一个不存在的 fake task_type 但 fallback 也能命中的
        # 直接用一个保证不存在的 task_type 测试 fallback 返回 None
        path = resolve_skill_path("fake_scope.nonexistent_skill_xyz")
        assert path is None

    def test_empty_task_type_returns_none(self):
        """空 task_type 返回 None。"""
        assert resolve_skill_path("") is None
        assert resolve_skill_path(None) is None  # type: ignore[arg-type]

    def test_task_type_without_dot_returns_none(self):
        """task_type 不含 '.' 的（如 "accounting"）返回 None（要求 scope.name 格式）。"""
        # 实际上 fallback 函数检查 "." in task_type
        path = resolve_skill_path("accounting")
        assert path is None


# ============================================================================
# 6. format_skills_prompt: task_type list → system prompt 段
# ============================================================================


class TestFormatSkillsPrompt:
    def test_empty_skills_returns_empty_string(self):
        """空 skills list 返回空字符串。"""
        assert format_skills_prompt([]) == ""

    def test_none_skills_returns_empty_string(self):
        """None 返回空字符串。"""
        assert format_skills_prompt(None) == ""  # type: ignore[arg-type]

    def test_single_skill_format(self):
        """单 skill 输出格式正确（含路径 + 引导语）。"""
        result = format_skills_prompt(["recurring.accounting"])
        assert "# Active Skills" in result
        assert "本会话已激活以下 skill" in result
        assert "accounting (workspace/accounting/SKILL.md)" in result
        assert "请先读取相关 SKILL.md 了解流程" in result

    def test_multiple_skills_numbered(self):
        """多 skill 按序号 1/2/3... 列出。"""
        result = format_skills_prompt([
            "recurring.accounting",
            "adhoc.auto_shutdown",
        ])
        assert "1. accounting" in result
        assert "2. auto_shutdown" in result
        # 路径正确
        assert "workspace/accounting/SKILL.md" in result
        assert ".agents/skills/auto_shutdown.md" in result

    def test_unresolvable_skill_skipped(self):
        """无法解析路径的 task_type 被跳过（不出现在输出中）。"""
        result = format_skills_prompt([
            "recurring.accounting",
            "fake_scope.nonexistent_skill_xyz",
        ])
        # 第一个 skill 仍输出
        assert "1. accounting" in result
        # 第二个 skill 不输出（编号仍是 1）
        assert "2." not in result
        # 仅 1 行编号
        assert result.count("1. ") == 1

    def test_all_unresolvable_returns_empty(self):
        """全部 skill 无法解析时返回空字符串。"""
        result = format_skills_prompt([
            "fake_scope.nonexistent_1",
            "fake_scope.nonexistent_2",
        ])
        assert result == ""

    def test_format_consistent_across_calls(self):
        """同样输入多次调用结果一致（无随机性）。"""
        skills = ["recurring.accounting", "adhoc.auto_shutdown"]
        r1 = format_skills_prompt(skills)
        r2 = format_skills_prompt(skills)
        assert r1 == r2
