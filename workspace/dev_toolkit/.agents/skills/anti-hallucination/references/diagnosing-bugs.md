# Diagnosing Bugs（棘手 bug 诊断循环）

> 来源：借鉴 mattpocock/skills 的 `diagnosing-bugs` skill（中译版位于 `_vendor/mattpocock-zh/skills/engineering/diagnosing-bugs/`），本地化调整：去掉 issue tracker 引用，加入项目特定 feedback loop 选择建议。
> 调用入口：本 skill 不单独注册到 `_index.md` / `agent_guide.py`，作为 `anti_hallucination` Step 5 的升级路径，由 anti_hallucination 主动转入。
> 触发场景：并发/异步/竞态/非确定性复现 bug、性能回退、anti_hallucination Step 5 加 log 3 轮未定位根因。

## 核心原则

面向棘手 bugs 的纪律：**先构建 tight feedback loop，再 hypothesise**。只有在明确说明理由时才跳过阶段。

没有 loop，盯着代码看多久都救不了你；loop 不 tight，调试就是在浪费上下文。

## Phase 1 — Build a feedback loop

**这就是这个 skill 的核心。** 其他所有内容都是机械步骤。如果你拥有一个针对该 bug 的 **tight** pass/fail signal，即它会在 _这个_ bug 上变红，你就能找到原因；bisection、hypothesis-testing 和 instrumentation 都只是消费这个 signal。

在这里投入不成比例的精力。**要强硬、要有创造力、拒绝放弃。**

### Loop 构造方式（按优先级）

1. **Failing test**，放在能触达 bug 的 seam 上：unit、integration、e2e 都可以。项目内用 `uv run pytest tests/test_<module>.py::<test_name>` 跑。
2. **Curl / HTTP script**，打到运行中的 dev server（`http://127.0.0.1:8766/...`）。适合后端端点 bug。
3. **CLI invocation**，使用 fixture input，并把 stdout 与 known-good snapshot diff。
4. **Headless browser script**（Playwright / Puppeteer），驱动 UI，并断言 DOM/console/network。项目内有 CDP 9222 调试浏览器可用。
5. **Replay a captured trace**：把真实 network request / payload / event log 保存到磁盘，并在隔离环境中 replay 到代码路径。
6. **Throwaway harness**：启动系统的最小子集（一个 service、mocked deps），用一次 function call 触发 bug code path。项目内可用 `exec_python` 跑临时脚本。
7. **Property / fuzz loop**：如果 bug 是 "sometimes wrong output"，运行 1000 个 random inputs 并寻找 failure mode。
8. **Bisection harness**：如果 bug 出现在两个已知状态之间，自动化 "boot at state X, check, repeat"，这样可以 `git bisect run`。
9. **Differential loop**：用同一 input 跑 old-version vs new-version（或两个 configs），然后 diff outputs。
10. **HITL bash script**：最后手段。如果必须由人点击，用结构化脚本驱动 _人_，捕获的输出反馈给你。

构建正确的 feedback loop，bug 就修好了 90%。

### Tighten the loop

把 loop 当作产品。只要有了 _一个_ loop，就继续 **tighten** 它：

- **更快**：Cache setup、跳过无关 init、缩小 test scope。
- **更尖锐**：断言具体 symptom，而不是 "didn't crash"。
- **更 deterministic**：Pin time、seed RNG、isolate filesystem、freeze network。

一个 30 秒且 flaky 的 loop 几乎不比没有 loop 好；一个 2 秒 deterministic loop 才是 tight 的调试超能力。

### Non-deterministic bugs

目标不是 clean repro，而是 **higher reproduction rate**。循环触发 100x、parallelise、加 stress、缩小 timing windows、注入 sleeps。50%-flake bug 可以调试；1% 不行。持续提高复现率，直到它可调试。

### When you genuinely cannot build a loop

停下来并明确说明。列出你尝试过什么。向用户请求：(a) 能复现的环境访问权限，(b) 捕获的 artifact（HAR file、log dump、core dump、带 timestamps 的 screen recording），或 (c) 添加临时 production instrumentation 的许可。**不要** 在没有 loop 时继续 hypothesise。

### Phase 1 完成条件

- [ ] **Red-capable** — 它驱动真实 bug code path，并断言 **用户的 exact symptom**，因此能在该 bug 上变红、修复后变绿。不是 "runs without erroring"，而是必须能 _catch this specific bug_。
- [ ] **Deterministic** — 每次运行 verdict 相同（flaky bugs：按上文固定到高复现率）。
- [ ] **Fast** — 秒级，而不是分钟级。
- [ ] **Agent-runnable** — 你可以无人值守运行。

如果你发现自己在 command 存在前就读代码构建理论，**停下；直接跳到 hypothesis 正是这个 skill 要防止的失败。** 没有 red-capable command，就没有 Phase 2。

## Phase 2 — Reproduce + minimise

运行 loop。看它变红，也就是 bug 出现。

确认：

- [ ] Loop 产出的 failure mode 是 **用户** 描述的那个，而不是附近另一个失败。Wrong bug = wrong fix。
- [ ] Failure 能在多次运行中复现（或对于 non-deterministic bugs，复现率足够高，能用来调试）。
- [ ] 你已捕获 exact symptom（error message、wrong output、slow timing），后续阶段可以验证 fix 确实解决它。

### Minimise

一旦变红，就把 repro 缩到 **仍会变红的最小场景**。逐个削减 inputs、callers、config、data 和 steps，每次削减后重新运行 loop；只保留 failure 的 load-bearing 部分。

原因：minimal repro 会缩小 Phase 3 的 hypothesis space（可怀疑的 moving parts 更少），并成为 Phase 5 中干净的 regression test。

完成条件：**每个剩余元素都是 load-bearing**，移除任意一个都会让 loop 变绿。

在 reproduce 并 minimise 之前不要继续。

## Phase 3 — Hypothesise

在测试任何假设前，生成 **3-5 个 ranked hypotheses**。单假设会锚定在第一个看似合理的想法上。

每个 hypothesis 必须 **falsifiable**：说明它会做出什么 prediction。

> Format: "If <X> is the cause, then <changing Y> will make the bug disappear / <changing Z> will make it worse."

如果无法说明 prediction，这就是 vibe；丢弃或打磨它。

**测试前把 ranked list 展示给用户。** 用户常常有 domain knowledge，可以立即重排（"we just deployed a change to #3"），或知道哪些 hypotheses 已被排除。便宜 checkpoint，大幅省时。不要因此阻塞；如果用户 AFK，就按你的排序继续。

## Phase 4 — Instrument

每个 probe 都必须映射到 Phase 3 的某个具体 prediction。**一次只改变一个变量。**

Tool preference：

1. **Debugger / REPL inspection**，如果环境支持。一个 breakpoint 胜过十条 logs。
2. **Targeted logs**，放在能区分 hypotheses 的 boundaries。
3. 永远不要 "log everything and grep"。

**给每条 debug log 加唯一 prefix**，例如 `[DEBUG-a4f2]`。最后 cleanup 就能一次 grep。未打 tag 的 logs 会存活；带 tag 的 logs 要删除。项目内可用 `exec_python` 在关键位置加 `print("[DEBUG-xxxx] ...")`，事后用 Grep 工具清理。

**Perf branch**：对 performance regressions，logs 通常不对。改为先建立 baseline measurement（timing harness、`time.perf_counter()`、profiler、query plan），然后 bisect。先 measure，再 fix。

## Phase 5 — Fix + regression test

在 fix 前写 regression test，但前提是存在 **correct seam**。

Correct seam 是 test 能以 call site 中真实发生的方式触发 **real bug pattern** 的地方。如果唯一可用 seam 太 shallow（bug 需要多个 callers，但 test 只有 single-caller；unit test 无法复制触发 bug 的 chain），那里的 regression test 会给出 false confidence。

**如果不存在 correct seam，这本身就是发现。** 记录下来。Codebase architecture 阻止你锁住 bug。把它标记给 Phase 6 的 post-mortem。

如果存在 correct seam：

1. 把 minimised repro 变成该 seam 上的 failing test。项目用 `tests/test_<module>.py`，遵循已有 `tests/conftest.py` 约定。
2. 看它 fail。
3. 应用 fix。
4. 看它 pass。
5. 重新针对原始（未 minimised）场景运行 Phase 1 feedback loop。

## Phase 6 — Cleanup + post-mortem

声明完成前必须做：

- [ ] Original repro 不再复现（重跑 Phase 1 loop）
- [ ] Regression test 通过（或记录缺少 seam）
- [ ] 所有 `[DEBUG-...]` instrumentation 已移除（用 Grep 工具按 prefix 搜并清理）
- [ ] Throwaway prototypes 已删除（或移动到明确标记的 `temp/debug/` 位置）
- [ ] 正确 hypothesis 已写进 commit / PR message，让下一个 debugger 能学习

**然后问：什么本可以预防这个 bug？** 如果答案涉及 architecture change（没有好 test seam、callers 缠绕、hidden coupling），把它记到 `wip_create` 留待后续处理；或如果是紧迫问题，触发 `dev/codebase-design` 评估深化机会。这个建议要在 fix 之后提出，不要在之前提出；现在你比开始时知道得更多。

## 与 anti_hallucination 的衔接

| 触发条件 | 路径 |
|---------|------|
| 简单 bug（读代码就能看出嫌疑） | 留在 anti_hallucination Step 5 轻量 log |
| 并发/异步/竞态/非确定性 | 转本 skill Phase 1 |
| 性能回退/slow | 转本 skill Phase 1（Perf branch） |
| Step 5 加 log 3 轮未定位 | 转本 skill Phase 1 |
| 多模块联动 | 转本 skill Phase 1 |
