"""直连 OpenAI 兼容 API 批量总结社群帖子

复用 summarize_posts.py 的评分逻辑与 prompt，通过 server.llm_pool 统一管理多 key 并发池，
支持 per-key 并发限制、429 自动冷却切换、余额耗尽自动剔除，适合大规模消耗 token。

特性：
  - 扫描 workspace/community_review/社群/<板块>/post_d_*.json 全部单篇帖子
  - 通过 LLM 并发池（server.llm_pool）管理多 key，自动轮转 + 限流退避
  - 30 线程并发（可配置，池内部按 key 管理并发）
  - 每 20 篇保存断点，支持续跑
  - 多维评分 + 加权排序，输出 JSON + Markdown 报告

用法:
  uv run python tools/llm/summarize_posts_direct.py                          # 全量
  uv run python tools/llm/summarize_posts_direct.py --board 自主发展 --limit 50  # 测试50篇
  uv run python tools/llm/summarize_posts_direct.py --concurrency 30          # 指定并发
"""
import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# 让脚本能导入 server.llm_pool（脚本位于 tools/llm/，需向上两级到项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from server.llm_pool import (
    call_via_backend_full,
    check_backend_pool,
    get_backend_pool_status,
)

# ========== 配置 ==========

OUTPUT_BASE = Path(r"<project_root>\output\社群")
REPORT_DIR = Path(r"<project_root>\output\社群\reports")
SUMMARIES_DIR = Path(r"<project_root>\output\社群\summaries")  # 单篇总结文件目录

KEYS_FILE = PROJECT_ROOT / "temp" / "mimo_keys_tested.json"
SAVE_INTERVAL = 100

# ========== Prompt（与 server/agent.py 完全一致）==========

COMMUNITY_SUMMARIZE_PROMPT = """你是一个严苛、客观的知识筛选引擎。你的核心职责是帮用户过滤低价值内容，宁可误杀不可放过。
**绝对禁止为了"鼓励作者"或"照顾情绪"而给同情分。0分就是0分，不存在"至少给了3分"的底线。**
**如果某个维度完全不涉及，必须给0分，不要因为"文章存在"就给保底分。**

若有效文本不足100字，或完全无法提取核心观点，请直接输出"[无效文本-跳过]"，不要输出后续任何模板内容。

## 提取字段与格式
**使用下述Markdown模板，各字段间保留一个空行。**
---

## 标题：核心标题（去除编号及纯修饰符号，保留实质主题）
* 作者：[作者名]。**无法推断则跳过这一行**
* 体裁：教程/深度复盘/观点论述/盘点推荐/工具合集/叙事故事/随笔杂谈/其它
* 关键词：词1, 词2, ... （3-10个，优先名词、专有名词，覆盖核心主题与方法）
* 标签：从下方标签库中选3-5个，若无完全匹配的可自拟，但须保持大类宽泛
* 适合人群：从下方人群标签库中选1-3个，标注文章对哪类读者最有价值
### 核心摘要
[用200字左右概括全文核心内容与结论，非导语，须包含文章"解决了什么问题"或"提供了什么价值"]
### 内容大纲（当文章有明显逻辑结构时提取，**无结构则不提取，忽略这一部分**，最多2级，每级一行简述）
1. 一级标题
   1.1 二级标题
### 多维评分（每个维度给出0-10的整数，备注可为空或简述理由）
- 知识/技术深度：[0-10] 备注：[可选]
- 实用可操作性：[0-10] 备注：[可选]
- 思想启发性：[0-10] 备注：[可选]
- 信息稀缺度：[0-10] 备注：[可选]
- 结构清晰度：[0-10] 备注：[可选]
- 趣味/可读性：[0-10] 备注：[可选]

---

## 维度评分标准（逐条严格判定，0分是常态不是例外）

### 1. 知识/技术深度
*   **0分**：纯个人经历叙述、情绪宣泄、日常记录，不涉及任何专业知识或技术。**个人经历本身不是知识的凭据。**
*   *1-3分*：仅罗列事实或链接，无解释；纯消遣聊天。
*   *4-6分*：有基础概念解释，行业通识级科普。
*   *7-10分*：深入原理解析、源码级分析、数学推导、系统级关联，或对复杂问题给出超越浅层的主流观点。

### 2. 实用可操作性
*   **0分**：纯抽象议论、情绪发泄、个人状态描述，无任何可落地的方法。**"我失败了然后我反思了"不等于可操作。**
*   *1-3分*：仅提供了方向性建议（如"要多尝试"），无法直接执行。
*   *4-6分*：提供了宽泛思路、原则或检查清单，但缺少操作细节。
*   *7-10分*：提供逐步SOP、流程图、可运行代码、具体话术、精确参数。用户可模仿执行。

### 3. 思想启发性
*   **0分**：毫无新见解，正确但空洞的鸡汤，纯资讯，或仅是个人情绪宣泄。
*   *1-3分*：有观点但浅显，或只是复述已知道理。
*   *4-6分*：提供了一个可辩论的观点，或对常见事物进行了有趣的角度转换。
*   *7-10分*：提供颠覆性的认知框架、改变了读者对某个问题的底层理解，或具有极强的哲学/战略反思价值。

### 4. 信息稀缺度
*   **0分**：纯个人经历且未提炼出通用见解。**"我的面试经历"若只描述过程不提炼方法论，对他人不构成信息稀缺。** 百度首页即可获得的常识。
*   *1-3分*：需要一定搜索才能获得，但并非独家。个人经历+场景化复盘（如"陌生环境中利他性融入"这类从具体经历提炼的通用策略）可给2-3分。
*   *4-6分*：需要专门搜索、翻译外网或花钱购买的知识。个人经历+推广到普遍困境+给出可复用SOP可给4-6分。
*   *7-10分*：作者独家的成败复盘（须提炼出通用方法论）、付费社群/公司内网流出的一手数据、未公开的行业潜规则、极其冷门且验证过的技巧。

### 5. 结构清晰度
*   **0分**：意识流，思维跳跃，无逻辑线。**仅仅分了段不等于有结构。**
*   *1-3分*：有分段但逻辑混乱，从具体事件突然滑向抽象感悟，缺乏过渡。
*   *4-6分*：有基本分段和逻辑顺序，但缺乏总结或层级。
*   *7-10分*：标题层级分明、有总述有总结、图表编号、阅读负担低，适合快速定位信息。

### 6. 趣味/可读性
*   **0分**：干涩堆砌，或纯情绪宣泄无叙事。
*   *1-3分*：平实但无风格，或叙事混乱。
*   *4-6分*：通顺可读，但无特别之处。
*   *7-10分*：幽默、有强烈故事感、语言鲜活、有互动设计或令人印象深刻的情感张力。

---

## 个人视角内容判定铁律
1. **个人经历本身不是低分理由，关键看是否推广到普遍困境**：
   - 纯个人日志/状态/日常，未提炼出通用见解 → 知识深度=0，实用=0，信息稀缺=0
   - 个人经历+推广到普遍困境+给出可复用解法 → 各维度可正常给高分
   - **判定标准**：读者读完能否将作者的方法直接迁移到自己的处境？如果能，就不是"纯个人"。
2. **"深刻共鸣"陷阱**：即使读者可能产生共鸣，纯情感共鸣不等于知识价值。共鸣型文章的启发性最多给3分，除非它同时提供了认知框架。
3. **课后作业标记**：带【#课后作业】标签的文章，所有维度评分先按正常标准评，但最终会由脚本乘以0.5系数。你无需特殊处理，正常评分即可。
4. **外部链接**：如果文章主体是外部链接（腾讯文档、飞书、Notion等），请根据链接周围的描述文字评分，并在摘要中标注"[含外部链接-建议人工阅读]"。不要因为内容不完整就判为无效文本。

---

## 标签库（可参照或自拟同层级标签）
1. **软件工具**  - 音乐软件、电脑优化、清理工具、翻译器、截图工具、扫描软件、网盘服务
2. **游戏相关**  - 游戏攻略、MOD模组、游戏评测、模拟器、联机教程、游戏硬件配置
3. **硬件与DIY**  - 电脑装机指南、显示器/键盘/鼠标推荐、硬件知识、笔记本维护
4. **生活与健康**  - 健康锻炼、烹饪食谱、生理知识、心理健康、日常记录
5. **学习与教育**  - 大学生活指南、学术工具、考试技巧、教材资源、语言学习
6. **网络安全与隐私**  - 防骗指南、账号安全、隐私保护、网络限速破解
7. **金融理财**  - 理财产品、省钱技巧、信用卡攻略、副业赚钱
8. **影视与娱乐**  - 壁纸资源、动漫推荐、影视网站、直播素材
9. **科技与互联网**  - 网络技术、AI工具、云服务、开源项目、浏览器优化
10. **旅行与日常**  - 旅游攻略、礼物推荐、日常记录、城市探索
11. **法律与实用知识**  - 手机卡注销、简历制作、版权问题、租房攻略
12. **创意与设计**  - 像素画、字体管理、Live2D建模、视频剪辑
13. **个人成长与复盘**
14. **深度思考**
15. **就业与职场**
16. **人际与恋爱**

## 人群标签库（选择1-3个，标注文章对哪类读者最有价值）
### 按需求类型
- **要方法论**：需要可操作的SOP、框架、步骤指南
- **要认知突破**：需要新视角、颠覆性观点、底层理解重构
- **要情感共鸣**：需要"有人跟我一样"的认同感
- **要资源工具**：需要推荐、合集、工具链接
- **要行动激励**：需要案例驱动、打鸡血、推动执行
### 按阶段
- **迷茫无方向**：不知道自己要什么
- **求职焦虑**：面临就业压力
- **自我认知探索**：在了解自己
- **执行力困境**：拖延/完美主义/低效
### 特殊
- **所有人**：通用内容，不限人群
- **仅作者本人**：纯个人日记，对他人无价值

待分析内容：
"""

# ========== 评分解析（与 summarize_posts.py 一致）==========

SCORE_WEIGHTS = {
    "知识/技术深度": 1.5,
    "实用可操作性": 1.5,
    "思想启发性": 2.0,
    "信息稀缺度": 1.5,
    "结构清晰度": 0.5,
    "趣味/可读性": 1.0,
}

EXTERNAL_LINK_KEYWORDS = [
    "docs.qq.com", "docs.tencent", "飞书", "feishu.cn",
    "notion.so", "notion.site", "yuque.com", "语雀",
    "shimo.im", "石墨", "docin.com", "豆丁",
]


def parse_multi_scores(summary_text: str) -> dict:
    scores = {}
    for line in summary_text.split("\n"):
        for dim in SCORE_WEIGHTS:
            pattern = re.escape(dim) + r'[：:]\s*\[?(\d+)\]?'
            m = re.search(pattern, line)
            if m:
                scores[dim] = int(m.group(1))
    return scores


def calc_weighted_score(scores: dict) -> float:
    total = 0.0
    weight_sum = 0.0
    for dim, weight in SCORE_WEIGHTS.items():
        if dim in scores:
            total += scores[dim] * weight
            weight_sum += weight
    return round(total / weight_sum, 1) if weight_sum > 0 else 0


def parse_field(summary_text: str, field_name: str) -> str:
    for line in summary_text.split("\n"):
        if field_name in line:
            parts = line.split("：", 1)
            if len(parts) > 1:
                return parts[1].strip()
            parts = line.split(":", 1)
            if len(parts) > 1:
                return parts[1].strip()
    return ""


# ========== 核心调用 ==========

def summarize_one_with_retry(post: dict) -> dict:
    """总结单篇帖子，通过后端代理调用 LLM（后端统一管理重试/限流/key 切换）"""
    title = post.get("title", "")
    author = post.get("author", "")
    content = post.get("content", "")

    if len(content) > 4000:
        content = content[:4000]

    messages = [
        {"role": "system", "content": COMMUNITY_SUMMARIZE_PROMPT},
        {"role": "user", "content": f"标题：{title}\n作者：{author}\n\n{content}"},
    ]

    t0 = time.perf_counter()
    r = call_via_backend_full(messages, temperature=0.2, max_tokens=3000, timeout=90,
                              project="社群帖子总结", http_timeout=300)
    elapsed_ms = round((time.perf_counter() - t0) * 1000)

    if not r.get("ok"):
        return {
            "title": title,
            "author": author,
            "summary": f"[请求失败: {r.get('error', '?')}]",
            "scores": {},
            "weighted_score": 0,
            "has_external_link": any(kw in content for kw in EXTERNAL_LINK_KEYWORDS),
            "needs_manual_review": False,
            "is_homework": False,
            "endpoint": "none",
            "elapsed_ms": elapsed_ms,
            "usage": {},
        }

    summary_text = r["content"]
    scores = parse_multi_scores(summary_text)
    weighted = calc_weighted_score(scores)

    # 评分缺失且非无效文本 → 缩短内容重试一次
    if not scores and "[无效文本-跳过]" not in summary_text and len(content) > 1500:
        short_content = content[:1500]
        messages2 = [
            {"role": "system", "content": COMMUNITY_SUMMARIZE_PROMPT},
            {"role": "user", "content": f"标题：{title}\n作者：{author}\n\n{short_content}"},
        ]
        r2 = call_via_backend_full(messages2, temperature=0.2, max_tokens=3000, timeout=90,
                                   project="社群帖子总结", http_timeout=300)
        if r2.get("ok"):
            retry_summary = r2["content"]
            retry_scores = parse_multi_scores(retry_summary)
            if retry_scores:
                summary_text = retry_summary
                scores = retry_scores
                weighted = calc_weighted_score(scores)
                r = r2  # 用重试结果的 usage

    # 课后作业 0.5 系数
    tags_str = " ".join(post.get("tags", [])) if isinstance(post.get("tags"), list) else str(post.get("tags", ""))
    is_homework = "#课后作业" in tags_str or "#课后作业" in content[:200]
    if is_homework and weighted > 0:
        weighted = round(weighted * 0.5, 1)

    # 外部链接检测
    has_external_link = any(kw in content for kw in EXTERNAL_LINK_KEYWORDS)
    needs_manual_review = weighted == 0 and has_external_link
    if needs_manual_review:
        summary_text += "\n\n[含外部链接-建议人工阅读]"

    return {
        "title": title,
        "author": author,
        "summary": summary_text,
        "scores": scores,
        "weighted_score": weighted,
        "has_external_link": has_external_link,
        "needs_manual_review": needs_manual_review,
        "is_homework": is_homework,
        "endpoint": r.get("key_name", ""),
        "elapsed_ms": elapsed_ms,
        "usage": r.get("usage", {}),
    }


# ========== 数据加载 ==========

def load_posts(board: str, limit: int = None) -> list:
    """加载板块下所有 post_d_*.json，返回统一格式的帖子列表"""
    board_dir = OUTPUT_BASE / board
    post_files = sorted(board_dir.glob("post_d_*.json"))
    print(f"扫描 {board}: {len(post_files)} 个文件")

    posts = []
    skipped_empty = 0
    for f in post_files:
        try:
            d = json.load(open(f, encoding="utf-8"))
            content = d.get("content_text", "")
            if not content or len(content.strip()) < 20:
                # 内容太短，跳过（纯图片帖等）
                skipped_empty += 1
                continue

            tags = d.get("tags", [])
            if isinstance(tags, list):
                tags_str = " ".join(str(t) for t in tags)
            else:
                tags_str = str(tags)

            posts.append({
                "file": f.name,
                "file_stem": f.stem,  # 用于命名总结文件，如 post_d_xxx
                "feeds_id": d.get("feeds_id", ""),
                "title": d.get("title", ""),
                "author": d.get("author", ""),
                "time": d.get("time", ""),
                "ip": d.get("ip", ""),
                "content": content,
                "tags": tags_str,
                "likes_count": d.get("likes_count", 0),
                "comments_count": d.get("comments_count", 0),
                "is_selected": d.get("is_selected", False),
                "url": d.get("url", ""),
                "board": d.get("board", board),
            })
        except Exception as e:
            print(f"  [!] 读取失败 {f.name}: {e}")

    print(f"  有效帖子: {len(posts)}，跳过空内容: {skipped_empty}")
    if limit:
        posts = posts[:limit]
        print(f"  限制处理前 {limit} 篇")
    return posts


def now_iso() -> str:
    return datetime.now().isoformat()


def now_filename() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def save_checkpoint(checkpoint_file: Path, completed: dict):
    results = sorted(completed.values(), key=lambda x: x.get("index", 0))
    checkpoint = {
        "updated_at": now_iso(),
        "completed": len(results),
        "results": results,
    }
    checkpoint_file.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")


def save_summary_file(post: dict, result: dict) -> str:
    """将单篇总结保存为 markdown 文件，返回相对路径（相对于 SUMMARIES_DIR）"""
    board = post.get("board", "未知")
    file_stem = post.get("file_stem", "unknown")

    board_dir = SUMMARIES_DIR / board
    board_dir.mkdir(parents=True, exist_ok=True)

    md_file = board_dir / f"{file_stem}.md"

    # 构建带元信息的 markdown
    lines = [
        f"# {post.get('title', '无标题')}",
        "",
        "> **映射信息**",
        f"> - 原始文件: `{post.get('file', '')}`",
        f"> - feeds_id: `{post.get('feeds_id', '')}`",
        f"> - 板块: {board}",
        f"> - 作者: {post.get('author', '')}",
        f"> - 时间: {post.get('time', '')}",
        f"> - IP: {post.get('ip', '')}",
        f"> - 原文链接: {post.get('url', '')}",
        f"> - 内容长度: {len(post.get('content', ''))} 字",
        f"> - 点赞: {post.get('likes_count', 0)} | 评论: {post.get('comments_count', 0)}",
        f"> - 加权评分: **{result.get('weighted_score', 0)}**",
        f"> - 评分端点: {result.get('endpoint', '')}",
        "",
        "---",
        "",
        result.get("summary", "[无总结]"),
        "",
    ]
    md_file.write_text("\n".join(lines), encoding="utf-8")
    return f"{board}/{file_stem}.md"


# ========== 主流程 ==========

def main():
    parser = argparse.ArgumentParser(description="直连 API 批量总结社群帖子")
    parser.add_argument("--board", type=str, default="all",
                        help="板块名称：自主发展 / 自主规划 / all（默认全部）")
    parser.add_argument("--limit", type=int, default=None, help="每个板块只处理前N篇（测试用）")
    parser.add_argument("--concurrency", type=int, default=30, help="并发数（默认30，池内部按 key 管理并发）")
    parser.add_argument("--report-top", type=int, default=200, help="Markdown报告展示前N篇（默认200）")
    args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)

    # 加载帖子
    boards = ["自主发展", "自主规划"] if args.board == "all" else [args.board]
    all_posts = []
    for b in boards:
        posts = load_posts(b, args.limit)
        for p in posts:
            p["board"] = b
        all_posts.extend(posts)

    if not all_posts:
        print("没有可处理的帖子")
        return

    print(f"\n总计处理: {len(all_posts)} 篇，并发: {args.concurrency}")

    # 断点文件
    board_tag = args.board if args.board != "all" else "all"
    limit_tag = f"_lim{args.limit}" if args.limit else ""
    checkpoint_file = REPORT_DIR / f"summarize_direct_checkpoint_{board_tag}{limit_tag}.json"

    # 加载断点
    completed = {}
    if checkpoint_file.exists():
        try:
            checkpoint = json.load(open(checkpoint_file, encoding="utf-8"))
            for item in checkpoint.get("results", []):
                completed[item["index"]] = item
            print(f"发现断点：已处理 {len(completed)} 篇")
        except Exception:
            print("断点文件损坏，从头开始")

    # 待处理
    pending = [(i, p) for i, p in enumerate(all_posts) if i not in completed]
    if not pending:
        print("所有帖子已处理完毕，直接生成报告")
    else:
        print(f"待处理: {len(pending)} 篇")

        # 检查后端 LLM 池是否可用（后端统一管理并发池）
        if not check_backend_pool():
            print("后端 LLM 池不可用，退出。请先启动后端: start.bat")
            sys.exit(1)
        pool_status = get_backend_pool_status()
        if pool_status.get("initialized"):
            print(f"[backend] {pool_status.get('active_keys', 0)}/"
                  f"{pool_status.get('total_keys', 0)} keys, "
                  f"并发 {pool_status.get('current_active', 0)}/"
                  f"{pool_status.get('total_max_concurrency', 0)}")

        lock = threading.Lock()
        save_counter = 0
        total_tokens = 0
        t_start = time.perf_counter()

        def _process(idx, post):
            nonlocal save_counter, total_tokens
            result = summarize_one_with_retry(post)
            result["index"] = idx
            result["board"] = post.get("board", "")
            result["url"] = post.get("url", "")
            result["time"] = post.get("time", "")
            result["ip"] = post.get("ip", "")
            result["likes_count"] = post.get("likes_count", 0)
            result["comments_count"] = post.get("comments_count", 0)
            result["content_length"] = len(post.get("content", ""))
            result["original_file"] = post.get("file", "")
            result["file_stem"] = post.get("file_stem", "")
            result["feeds_id"] = post.get("feeds_id", "")
            result["title"] = post.get("title", "")
            result["author"] = post.get("author", "")

            # 保存单篇总结为 markdown 文件
            summary_rel_path = save_summary_file(post, result)
            result["summary_file"] = summary_rel_path

            with lock:
                completed[idx] = result
                save_counter += 1
                usage = result.get("usage", {})
                total_tokens += usage.get("total_tokens", 0)
                if save_counter % SAVE_INTERVAL == 0:
                    save_checkpoint(checkpoint_file, completed)
                    elapsed = time.perf_counter() - t_start
                    rate = save_counter / elapsed if elapsed > 0 else 0
                    eta = (len(pending) - save_counter) / rate if rate > 0 else 0
                    status = get_backend_pool_status()
                    print(f"\n  === 断点保存 {len(completed)}/{len(all_posts)} | "
                          f"token={total_tokens} | {rate:.1f}篇/s | ETA {eta/60:.1f}min ===")
                    for k in status.get("keys", []):
                        s = k.get("stats", {})
                        print(f"    {k.get('name',''):15s} ok={s.get('ok',0):4d} "
                              f"fail={s.get('fail',0):3d} "
                              f"429={s.get('rate_limited',0):3d} "
                              f"tok={s.get('total_tokens',0)}")
                    print()
            return result

        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = {executor.submit(_process, idx, post): idx for idx, post in pending}
            done = 0
            for future in as_completed(futures):
                idx = futures[future]
                done += 1
                try:
                    r = future.result()
                    score = r.get("weighted_score", 0)
                    ep = r.get("endpoint", "?")
                    author = r.get("author", "?")[:10]
                    print(f"  [{done}/{len(pending)}] {author} → {score}分 [{ep}]")
                except Exception as e:
                    print(f"  [{done}/{len(pending)}] 第{idx}篇异常: {e}")

        save_checkpoint(checkpoint_file, completed)
        elapsed_total = time.perf_counter() - t_start
        print(f"\n全部完成！耗时 {elapsed_total/60:.1f} 分钟，消耗 token: {total_tokens}")
        # 打印后端池状态
        try:
            st = get_backend_pool_status()
            if st.get("initialized"):
                print(f"[backend] {st.get('active_keys',0)}/{st.get('total_keys',0)} keys, "
                      f"并发 {st.get('current_active',0)}/{st.get('total_max_concurrency',0)}, "
                      f"tokens={st.get('total_tokens_consumed',0)}")
        except Exception:
            pass

    # 生成报告
    print(f"\n{'='*80}")
    print("生成报告")
    print(f"{'='*80}")

    report = []
    for i, post in enumerate(all_posts):
        r = completed.get(i, {
            "summary": "[无总结]", "scores": {}, "weighted_score": 0,
            "has_external_link": False, "needs_manual_review": False,
            "is_homework": False, "endpoint": "none",
        })
        content = post.get("content", "")
        has_external_link = any(kw in content for kw in EXTERNAL_LINK_KEYWORDS)
        needs_manual_review = r.get("weighted_score", 0) == 0 and has_external_link

        report.append({
            "index": i + 1,
            "board": post.get("board", ""),
            "author": post["author"],
            "time": post.get("time", ""),
            "ip": post.get("ip", ""),
            "title": post["title"],
            "tags": post.get("tags", ""),
            "is_selected": post.get("is_selected", False),
            "likes_count": post.get("likes_count", 0),
            "comments_count": post.get("comments_count", 0),
            "url": post.get("url", ""),
            "content_length": len(content),
            "has_external_link": has_external_link,
            "needs_manual_review": needs_manual_review,
            "summary": r.get("summary", ""),
            "scores": r.get("scores", {}),
            "weighted_score": r.get("weighted_score", 0),
            "endpoint": r.get("endpoint", ""),
            # 映射字段
            "original_file": post.get("file", ""),
            "file_stem": post.get("file_stem", ""),
            "feeds_id": post.get("feeds_id", ""),
            "summary_file": r.get("summary_file", ""),
        })

    report.sort(key=lambda x: x["weighted_score"], reverse=True)

    # JSON 报告
    ts = now_filename()
    report_file = REPORT_DIR / f"report_direct_{board_tag}{limit_tag}_{ts}.json"
    report_data = {
        "generated_at": now_iso(),
        "board": board_tag,
        "total": len(report),
        "score_weights": SCORE_WEIGHTS,
        "posts": report,
    }
    report_file.write_text(json.dumps(report_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON报告: {report_file}")

    # 映射索引文件（summary_file → 原始帖子信息）
    index_file = SUMMARIES_DIR / "index.json"
    index_data = {
        "generated_at": now_iso(),
        "total": len(report),
        "summaries_dir": str(SUMMARIES_DIR),
        "mapping": [],
    }
    for r in report:
        index_data["mapping"].append({
            "summary_file": r.get("summary_file", ""),
            "original_file": r.get("original_file", ""),
            "file_stem": r.get("file_stem", ""),
            "feeds_id": r.get("feeds_id", ""),
            "board": r.get("board", ""),
            "title": r.get("title", ""),
            "author": r.get("author", ""),
            "time": r.get("time", ""),
            "url": r.get("url", ""),
            "weighted_score": r.get("weighted_score", 0),
            "scores": r.get("scores", {}),
            "endpoint": r.get("endpoint", ""),
        })
    index_file.write_text(json.dumps(index_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"映射索引: {index_file}")

    # Markdown 报告（前N篇）
    md_file = REPORT_DIR / f"report_direct_{board_tag}{limit_tag}_{ts}.md"
    md_lines = [
        f"# 社群帖子筛选报告 - {board_tag}",
        "",
        f"生成时间: {report_data['generated_at']}",
        f"总计: {len(report)} 篇",
        f"展示前 {args.report_top} 篇",
        f"权重: {', '.join(f'{k}={v}' for k, v in SCORE_WEIGHTS.items())}",
        "",
        "---",
        "",
    ]

    # 统计
    score_dist = {"0分": 0, "1-3分": 0, "4-6分": 0, "7-10分": 0}
    for r in report:
        s = r["weighted_score"]
        if s == 0:
            score_dist["0分"] += 1
        elif s <= 3:
            score_dist["1-3分"] += 1
        elif s <= 6:
            score_dist["4-6分"] += 1
        else:
            score_dist["7-10分"] += 1
    md_lines.append("## 分数分布")
    for k, v in score_dist.items():
        md_lines.append(f"- {k}: {v} 篇 ({v*100//max(len(report),1)}%)")
    md_lines.append("")
    md_lines.append("---")
    md_lines.append("")

    for r in report[:args.report_top]:
        sel = " [精选]" if r["is_selected"] else ""
        manual = " [待人工阅读]" if r.get("needs_manual_review") else ""
        ext = " [外部链接]" if r.get("has_external_link") and not r.get("needs_manual_review") else ""
        scores_str = " | ".join(f"{k.split('/')[-1][:2]}{v}" for k, v in r["scores"].items()) if r["scores"] else "无"
        md_lines.append(f"## [{r['weighted_score']}分]{sel}{manual}{ext} {r['author']} - {r['title'][:60]}")
        md_lines.append(f"- 板块: {r['board']} | 时间: {r['time']} | IP: {r.get('ip','')}")
        md_lines.append(f"- 加权分: {r['weighted_score']} | 维度: {scores_str}")
        md_lines.append(f"- 点赞: {r.get('likes_count',0)} | 评论: {r.get('comments_count',0)} | 内容长度: {r.get('content_length',0)}")
        md_lines.append(f"- [打开原文]({r['url']})")
        md_lines.append("")
        md_lines.append(r["summary"])
        md_lines.append("")
        md_lines.append("---")
        md_lines.append("")

    md_file.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"Markdown报告: {md_file}")

    # 清理断点
    if checkpoint_file.exists():
        checkpoint_file.unlink()
        print("断点文件已清理")


if __name__ == "__main__":
    main()
