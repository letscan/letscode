# DevHard Agent

## 背景

纯 LLM 编排的 agent harness 有一个核心可靠性问题:LLM 自己决定"任务是否完成"是不可靠的。它会提前宣布成功(实际没跑测试)、或者反复修同一个 bug 卡死。把 pass/fail 判定从 orchestrator 下放到 Tester sub-agent 也没用 —— Tester 也是 LLM,同样会造假。

DevHard 的设计目标是:**把"是否继续循环"的判定权从 LLM 手中拿走,交给确定性的 harness 代码(shell 脚本)。**

## 工作原理

DevHard 是一个 AgentCard。它的 agent loop **只负责 Plan**(调查代码库、产出 `plan.md`,含实现步骤 + 验收要点)。Plan 完成后 agent loop 结束,**`onAgentEnd` 钩子(`hooks/devhard_loop.sh`)接管全部 Worker-Tester 循环**——这是确定性的 shell 循环,不靠 LLM 编排。

### 三层脚本

| 脚本 | 角色 | 产出方 | 内容 |
|------|------|--------|------|
| `devhard_loop.sh` | 循环控制 | harness 内置 | spawn Tester/Worker、verify、iteration、修复循环 |
| `verify.sh` | 固定验收契约 | harness 内置 | 查 `run_test.sh` 存在性 → 执行 → 透传 exit code |
| `run_test.sh` | 验收命令实例 | Tester 子代理生成 | 具体测试命令(pytest/swift test/go test...) |

### 完整流程

```
letscode --as DevHard "需求"
  │
  ├─ DevHard agent loop(LLM):
  │    Plan 子代理 → plan.md(实现步骤 + 验收要点)
  │    [loop 结束]
  │
  └─ onAgentEnd 钩子 → devhard_loop.sh(确定性):
       ① spawn Tester(读 plan.md → 写测试用例 + run_test.sh)   ← TDD:测试先行
       ② spawn Worker(读 plan.md → 写实现代码)
       while iteration < MAX(5):
         verify.sh → 执行 run_test.sh → exit code
         通过 → "All tests passed." → exit 0
         失败 → spawn Worker(读失败输出修复,不改测试)→ 复验
       达 MAX → exit 2(放弃)
```

关键点:
- **验收标准(Tester 写)与实现(Worker 写)由不同 LLM 角色产出**,由确定性脚本(verify.sh)裁定。Worker 无法通过改测试作弊(它不写测试)。
- **`run_test.sh` 是语言/框架无关的**:Tester 知道项目是 Python 就写 `pytest -x -q`,是 Swift 就写 `swift test`。钩子不猜测试框架——它只执行 `run_test.sh`。
- **LLM 无法干预循环判定**:pass/fail 是 `run_test.sh` 的 exit code,不是 LLM 自报。

### 状态共享

`--state <path>` flag 指定一个 JSON 文件,用于在 DevHard 的多次循环之间共享状态(当前迭代次数、上次测试输出)。经 `$LETSCODE_STATE` 环境变量传给 hook 脚本,经 `--state` flag 传给 spawn 的 sub-agent。

## 使用方法

```bash
# 基本用法:DevHard 接管从规划到验证的全流程
letscode --as DevHard --state .letscode/state.json -c config.json "add a GET /health endpoint to app.py"

# DevHard 会:
# 1. (可选)向用户澄清需求
# 2. spawn Plan 调查代码库,产出 plan.md(实现步骤 + 验收要点)
# 3. [agent loop 结束,钩子接管]
# 4. 钩子 spawn Tester → 写测试 + run_test.sh
# 5. 钩子 spawn Worker → 写实现
# 6. 钩子循环:verify.sh 跑 run_test.sh → 通过则结束,失败则 spawn Worker 修复后重验
```

需要 `-c config.json`:钩子 spawn sub-agent 时需要 config 提供模型/API。

## 适用场景

DevHard 适合**有明确验收标准的编码任务**,尤其是:

- **新功能开发**:需要先理解代码库、再实现、再验证的完整流程
- **Bug 修复**:Tester 写测试确认修复有效,钩子的循环保证不会"假装修好"
- **跨语言/框架任务**:Tester 在 run_test.sh 里写对应语言的测试命令,钩子照样能验证

不适合:
- 纯调研/只读任务(用 Explore 或 Plan 更合适)
- 一步就能完成的简单改动(直接用 letscode 即可,DevHard 的编排开销不划算)

## 设计决策记录

1. **钩子是 Worker-Tester 循环的主体,不是 LLM 编排器** —— DevHard 的 agent loop 只做 Plan;Worker/Tester 的 spawn 和 verify 循环全部在 `devhard_loop.sh` 里。这彻底把循环控制权从 LLM 手中拿走。
2. **verify.sh → run_test.sh 两层契约** —— verify.sh 是固定脚本(查 run_test.sh 存在/执行),run_test.sh 是 Tester 按任务生成的验收命令。验收方法属于 Plan/Tester 的知识,不属于钩子——钩子不再猜测试框架。
3. **TDD 顺序:Tester 先于 Worker** —— 测试先行,测试不依赖实现,验收基准更独立。
4. **Tester 只写测试,不跑测试、不写实现** —— 跑测试是 verify.sh 的事(确定性),写实现是 Worker 的事。Tester 和 Worker 的分离保证验收标准与实现由不同角色产出。
5. **Worker 不许改测试文件** —— 防止 Worker 通过改测试让 run_test.sh "通过"。
6. **Worker 没有 Agent 工具** —— 防止 sub-agent 无限递归 spawn。
7. **最大重试 5 次** —— 防止死循环烧 token。超过后 exit 2(abort)。
