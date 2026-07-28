# DevHard 端到端验证报告

> 基于 `docs/devhard-verification-plan.md`(commit `5ac0b3b`,后于 `a186dd4` 被替换为 `devhard-agent.md` 设计文档)执行。
> 执行日期:2026-07-22。模型:`deepseek-v4-flash`。

## ✅ 修复状态(2026-07-22,本报告发现的全部 6 个缺陷已修复)

修复后重跑全套测试 **652 passed**,并完成端到端回归:

- **e2e 通过路径**(calc 任务):钩子输出 `[onAgentEnd] All tests passed.`,**零崩溃**,独立 pytest 5/5 通过。钩子用 `$LETSCODE_PYTHON -m pytest` 真实判定,而非 LLM 自报。
- **e2e 失败→修复循环**(预置必失败 `test_buggy.py` + 只读任务):钩子检测失败 → 用 `$LETSCODE_PYTHON -m letscode --as Worker --config $LETSCODE_CONFIG` 链式 spawn 真实 Worker(拿到 config,32k tokens)→ Worker 修复 → 钩子**重跑 pytest 复验**确认修复 → 报告 "Worker fixed the failures. All tests passed (iteration 1)."。

| 缺陷 | 修复 | 回归测试 |
|------|------|---------|
| BUG-1 钩子崩溃 | `on_session_end` 移到 `onAgentEnd` 之后(`agent.py`)| `test_stdout_emitted_without_crash`(回退顺序后确认失败、修复后通过)|
| BUG-2 pytest 不在 PATH | 钩子用 `$LETSCODE_PYTHON -m pytest`;`LETSCODE_PYTHON=sys.executable` 始终注入 | `test_works_without_pytest_on_path` |
| BUG-3 链式 Worker 无 config | `run_agent` 新增 `config_path`,注入 `LETSCODE_CONFIG`;钩子用 `$PY -m letscode --config $LETSCODE_CONFIG` | `test_config_path_threaded_to_hook_env` |
| BUG-4 `\|\| true` 掩盖失败 | spawn Worker 后**重跑 pytest 复验**,据实报告 | `test_worker_runs_but_tests_still_fail`、`test_tests_fail_with_state_chains_worker_and_state_written` |
| BUG-5 裸目录被跳过 | 检测条件增加 `test_*.py`/`*_test.py` glob(分别 `ls` 避免 zsh glob 报错)| `test_detects_test_glob_without_project_marker` |
| BUG-6(新发现)Worker 被父沙箱挡写 | `onAgentEnd` 钩子 preset 提升到至少 `default`(`safe`→`default`,`risk` 保留)| `test_preset_elevated_to_default_for_safe_card` |

---

## 原始发现(修复前)

## TL;DR

**DevHard 的子代理编排(LLM 驱动的 Plan→Worker→Tester)功能正常,能产出正确代码并通过真实测试。但 DevHard 的核心卖点——`onAgentEnd` 钩子的确定性测试循环——完全不可用:每次运行都会崩溃,且即使不崩溃,钩子的测试检测/重试链路也存在三个独立 bug,从未真正工作过。**

所有测试中观察到的"成功"全部来自 LLM 自驱(子代理自己跑 `python -m pytest`、自己修 bug),而非钩子的确定性判定。这恰恰是 DevHard 设计文档(`docs/devhard-agent.md`)要消除的"LLM 自报成功"问题——钩子本应是它的解药,但目前钩子本身就是坏的。

---

## 执行的测试

| # | 任务 | 代码产出 | 独立 pytest | 钩子判定 | 循环触发 | 功能结论 |
|---|------|---------|------------|---------|---------|---------|
| 1 | 计算器(从零) | ✅ calc.py + test_calc.py(15 测试) | ✅ 15 passed | ⚠️ 跳过(无 pyproject.toml) | 否 | **PASS** |
| 3 | 修改现有代码 | ✅ bank.py 校验 + test_bank.py(10 测试) | ✅ 10 passed | ⚠️ 崩溃 | 否 | **PASS** |
| 6 | bug 修复循环 | ✅ calculator.py(已修)+ test_calculator.py(17 测试) | ✅ 17 passed | ⚠️ 崩溃 | 否 | **PASS(LLM 自修)** |
| L | 决定性循环测试(预置必失败用例 + 只读任务) | ❌ 未修复 | ❌ 1 failed | ❌ 写了 state 但链式 Worker 空转 | **名义触发,实际空转** | **FAIL** |

> 测试 2/4/5 未执行:已用 3 个代表性场景 + 1 个决定性循环测试充分定位了所有问题,继续运行只会重复相同模式并消耗 token,不产生新信息。

每个测试的产出文件保存在 `/tmp/devhard-test-{1,3,6,loop}/`,完整日志在 `/tmp/devhard-test-{1,3,6,loop}.log`。

---

## 发现的缺陷

### 🔴 BUG-1(致命,阻断性):`onAgentEnd` 钩子每次运行必崩溃

**现象**:每次 DevHard 运行结束时,都会抛出未捕获异常:
```
ValueError: I/O operation on closed file.
  File ".../agent.py", line 266, in run_agent
    hub.emit_agent_message_chunk(f"\n[onAgentEnd] {hook_result.stdout}\n")
  File ".../events.py", line 355, in _write
    self._log_file.write(line)
```

**根因(执行顺序错误)**:
1. `agent.py:243` 调用 `hub.on_session_end(stop_reason)` → `events.py:291-292` 的 `emit_result()` 以 `self.close()` 结尾,关闭所有订阅者,包括 `LogSubscriber`(`self._log_file.close()`)。
2. 然后 `agent.py:250` 才运行 `on_agent_end` 钩子。
3. 钩子返回非空 stdout 后,`agent.py:264-268` 试图通过 `hub.emit_agent_message_chunk(...)` 输出 `[onAgentEnd] ...`,但日志文件已关闭 → `ValueError`。

即:**session 结束(关日志)发生在 onAgentEnd 钩子之前,而钩子后又试图写已关闭的日志。**

**影响**:
- 进程以未捕获异常退出(但 `PIPESTATUS` 仍为 0,因为崩溃发生在结果已发出之后)——用户看到"成功",实际是带着崩溃的脏退出。
- `--event-stream` 模式下,钩子的 stdout 永远发不出去(崩溃前)。
- 日志文件最后一条事件(result)之后没有钩子结论的记录,可观测性受损。

**修复方向**:将 `on_session_end` / `emit_result` / `close` 移到 `on_agent_end` 钩子**之后**;或在 `emit_agent_message_chunk` 中容忍已关闭的文件(LogSubscriber 检查 `self._log_file.closed`)。前者更正确(钩子结论应入日志)。

---

### 🔴 BUG-2(致命,核心功能失效):钩子找不到 `pytest`

**现象**:直接调用钩子复现:
```
$ echo '{"turn":1}' | LETSCODE_STATE=... bash devhard_end.sh
Tests failed (iteration 1). Worker has been asked to fix.
$ cat state.json
{"iteration": 1, "last_test_output": ".../devhard_end.sh: line 25: pytest: command not found\n"}
```

**根因**:钩子用裸 `pytest`(`devhard_end.sh:25`),但 `pytest` 不在钩子子进程的 PATH 里——它只装在项目的 `.venv/bin/`(全局 `which pytest` → not found)。钩子是一个用全局 PATH 启动的裸 `bash` 子进程。

**影响**:`TEST_OUTPUT` 变成 shell 报错 `pytest: command not found`,`$?` 非零 → `TEST_PASSED=false`(错误归因:把 shell 错误当成测试失败)。于是钩子永远走"测试失败"分支。

**对比**:子代理(Worker/Tester)能成功跑测试,是因为它们通过 Bash 工具用 `python -m pytest`,且继承了 letscode 进程的环境。钩子没有这个环境。

**修复方向**:钩子用 `python -m pytest` 或 `uv run pytest`,而非裸 `pytest`;或解析项目 venv。

---

### 🔴 BUG-3(致命,核心功能失效):链式 Worker 拿不到模型配置

**现象**:`devhard_end.sh:88` 的链式调用:
```bash
letscode --as Worker --state "$STATE" --config "${LETSCODE_CONFIG:-}" "..."
```
实际输出:`No model specified. Use --model or set default_model in config file.` —— Worker 空转退出,什么都没做。

**根因**:`LETSCODE_CONFIG` **从未被 letscode 设置**。`agent.py:88` 只设了 `hook_env = {"LETSCODE_STATE": state_file}`,没有 `LETSCODE_CONFIG`。因此 `"${LETSCODE_CONFIG:-}"` 恒为空 → `--config ""` → `load_config("")` 当作无配置 → 测试目录里又没有 `config.json` → Worker 既无模型也无 API key → 立即报错退出。

**影响**:钩子的"失败→spawn Worker 修复→重验"循环名存实亡:Worker 每次都因缺配置而空转,但钩子用 `|| true` 吞掉退出码,仍打印"Worker has been asked to fix",并写 `state.json` 递增 iteration。最终在 iteration=5 时 exit 2 放弃——期间没有任何有效修复尝试。

**修复方向**:`agent.py` 在 `hook_env` 中加入 `LETSCODE_CONFIG`(传入实际使用的 config 路径);或钩子 fallback 到 `--model` + 环境变量里的 base_url/api_key。

---

### 🟠 BUG-4(可靠性):`|| true` 掩盖链式 Worker 失败

**现象**:`devhard_end.sh:88` 行尾 `2>&1 || true`。

**影响**:无论 Worker 是否真的修复了问题(甚至是否真的运行了),钩子都报告"Worker has been asked to fix"并 exit 0。结合 BUG-3,用户/编排器完全无法感知修复从未发生。

**修复方向**:检查 Worker 退出码;或在 spawn Worker 后立即重跑测试验证,而非假设 Worker 成功。

---

### 🟠 BUG-5(设计缺陷):钩子的测试套件检测过窄

**现象**:测试 1(无 `pyproject.toml`/`pytest.ini`)→ 钩子输出 `No test suite detected. Skipping verification.` → exit 0。即对"裸 `.py` + `test_*.py` 目录"这种最简单的场景,钩子完全不验证。

**根因**:`devhard_end.sh:20` 只检测 `pytest.ini || pyproject.toml || setup.cfg || tox.ini` 等项目标记文件。验证方案里的测试 1/2/4 都没有这些文件(只有源码 + 测试)。

**影响**:简单任务场景下 DevHard 退化成纯 LLM 编排,钩子的确定性保障为零。与 `devhard-agent.md` 声称的"不适合没有 test suite 的项目"形成灰色地带——有 `test_*.py` 但无项目标记时,钩子静默跳过。

**修复方向**:额外检测 `test_*.py` / `*_test.py` 文件存在;或文档明确要求测试目录必须有项目标记。

---

### 🟡 观察项 1:Plan 子代理可能超时(已被优雅降级)

测试 3 中 Plan 子代理超时,但 DevHard 正确处理:`"The plan agent timed out, but the requirements are crystal clear. Let me proceed directly to Phase 3: Implement."` —— 这是好的健壮性表现,非 bug。

### 🟡 观察项 2:测试 6 的"成功"完全是 LLM 自驱

测试 6 的任务包含"先创建含 bug 代码 → 再修复"。Worker(第 3 个进程)自己创建了 buggy 版本、自己跑 `python -m pytest` 发现失败、自己修复;Tester(第 4 个进程)再跑一次确认 17 passed。**钩子的确定性循环全程未生效**。这正好印证了 `devhard-agent.md` 背景 §1 描述的问题——只是这里"可信的"恰好是子代理而非主循环的钩子。

---

## 与设计目标的对照

`docs/devhard-agent.md` 的核心论点:

> **把"是否继续循环"的判定权从 LLM 手中拿走,交给确定性的 harness 代码(shell 脚本)。**
> 关键点:脚本直接跑测试套件,用 exit code 判定 pass/fail。LLM 无法干预这个判定。

**实际状态**:

| 设计承诺 | 实测 | 证据 |
|---------|------|------|
| 钩子直接跑测试套件 | ❌ `pytest` not found | BUG-2 |
| 用 exit code 判定 pass/fail | ❌ 判的是 shell 报错而非测试结果 | BUG-2 |
| 失败时 spawn Worker 修复 | ❌ Worker 因缺配置空转 | BUG-3 |
| 达上限 exit 2 放弃 | ⚠️ 会发生,但期间无有效修复 | BUG-3+4 |
| LLM 无法干预判定 | ⚠️ 判定本身没工作,实际全靠 LLM | BUG-2+3 |
| 脚本不读 Tester 的输出 | ✅ 符合 | — |

**结论:DevHard 的设计方向正确,但 `onAgentEnd` 钩子实现目前无法兑现任何一项核心承诺。** 子代理编排层(Plan/Worker/Tester 的 spawn 与上下文传递)工作良好,是当前唯一真正有效的部分。

---

## 建议的修复优先级

1. **BUG-1**(崩溃)— 最先修。不修则钩子的任何输出都无法正常呈现,`--event-stream`/ACP 集成下钩子结论丢失。修复简单(调整 `on_session_end` 与 `on_agent_end` 的顺序,或让 LogSubscriber 容忍 closed)。
2. **BUG-2 + BUG-3**(核心循环)— 一起修,是 DevHard 价值所在。需让钩子能用项目环境跑 pytest(`uv run` / `python -m`),并把 config 路径传给钩子(设 `LETSCODE_CONFIG`)。
3. **BUG-4 + BUG-5**(健壮性)— 修完上面两项后,提升循环可信度与场景覆盖。
4. 修复后重跑本报告的 4 个测试(尤其是"决定性循环测试 L")作为回归验证——测试 L 是唯一能证明"钩子而非 LLM 在驱动修复"的用例。
