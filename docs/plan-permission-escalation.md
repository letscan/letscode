# 方案:权限被动升级(harness 判定 + probe 提取 + server 驱动)

> 状态:**待实现**。来源:LetsBot 下游功能请求第二项(LETCODE_FEATURE_REQUEST.md)。
> 核心思路(下游提出):不做"工具调用即弹窗"的主动拦截(打断多、体验差);而是让 agent 在沙箱内尽最大努力,仅在确实无法完成时,**CLI 子进程退出并报特殊错误** → ACP 层拦截 → probe 确认 + 提取权限请求 → 弹窗 → 用户批准后 server 用 `--allow` respawn 续跑。

---

## 核心原则(贯穿全设计)

**尽最大努力的前提是:agent 不知道升级可能性的存在。** 一旦给 agent loop 挂上"可申请权限"的工具或引导 prompt,它的最优策略就偏向"早申请、少费力"(申请比死磕便宜),"尽力"激励被破坏。因此:

- **主尽力循环里没有任何 RequestPermission 工具**,没有任何"你可以申请权限"的 system prompt。
- **判定"是否卡死"由 harness(CLI)做**,不让 agent 自报。agent 全程只感知到普通的 `<error>` 拒绝,换路子重试。
- **结构化权限请求由独立 probe(`call_llm`,无状态、不落 feed)提取**,与尽力循环隔离。

被否决的替代方案(记录在此以防重蹈覆辙):在 agent loop 中途动态注入 RequestPermission 工具 + 引导 system prompt。否决理由:① 破坏"尽力"激励(agent 有捷径);② 中途改 tools/system prompt 打穿缓存(废掉 `cache_markers` 的 system_plus_rolling 策略);③ 把提取过程混进 session 状态(污染 feed/msg_sub)。

---

## 触发判据(CLI 侧,harness 判定)

循环自然结束后(`agent.py:165-168` session-end 收尾点,不 break),检查本 session 收集的 denial 记录。**满足以下任一即 emit `error` 事件(`code="permission_denied"`):**

1. **累计 denial 次数 ≥ 2**;或
2. **最近一次 tool call 的结果是 denial**。

### 设计依据
- **宽松优先**:假阳性成本低(probe 两段式会过滤,用户看不到弹窗),假阴性无法补救。所以判据偏宽。
- **覆盖两种失败形态**:条件 1 抓"反复被权限打断、挣扎后放弃";条件 2 抓"一路顺畅、末尾被一道墙挡住"(累计=1 但末次是 denial)。
- **不依赖"同一目标"判定**:那是 NLP 问题,不引入 AI 做不到精确(路径变体:`/etc/hosts` vs `/private/etc/hosts` vs 相对路径)。用累计计数 + 末次状态,绕开语义匹配。
- **不去重目标、不要求连续失败**:宽松。反复被权限打断本身就是信号。
- **唯一被漏的假阴性**:被拒 1 次且非末次就放弃 —— 这本身不算"尽力后无法完成",漏掉合理。
- **不 break**:不在 mid-turn 打断 agent 正在转的思路;让当前 turn 跑完、feed 完整,probe 才有完整上下文。

### denial 收集点(结构化留存)
denial 发生在 `rules.py` 的三个 check 函数,返回 `<error>...denied...</error>` 字符串。改造为**同时留存结构化记录** `{type, target}`(拒绝发生时路径/命令就在入参手边,零信息损失):
- `check_write(path, rules)` → `{type:"write", target:path}`(`rules.py:308-338`)
- `check_read(path, rules)` → `{type:"read", target:path}`(`rules.py:279-305`)
- `check_cmd(command, rules)` → `{type:"cmd", target:command[:80]}`(`rules.py:342-357`)

收集容器:`ToolRunner` 加 `self._denials: list[dict] = []`,在 `execute`(`runner.py:117-122` check_cmd 分支)和 `_make_validate_path`(`runner.py:79-88`)里追加。session-end 时 `agent.py` 读 `tools._denials` 做触发判定,并随 error 事件一并 emit。

### error 事件载荷
```json
{
  "type": "error",
  "data": {
    "message": "Permission escalation available: N tool calls denied",
    "code": "permission_denied",
    "recoverable": true,
    "denials": [
      {"type": "write", "target": "/etc/hosts"},
      {"type": "cmd", "target": "rm -rf node_modules"}
    ]
  }
}
```
复用现有 `emit_error(message, code, recoverable)`(`events.py:207-213`),仅扩展 data 带上 `denials` 列表。

---

## 纯 CLI bonus(零额外机制)

同一个 `code="permission_denied"` error 事件,在纯 CLI(无 ACP server)场景下,直接打印提示:
```
[权限不足] 2 次工具调用被拒。若需提权重试:
  letscode <原命令> --allow write:/etc/hosts
当前权限下无法继续完成任务。
```
这是方案一被低估的优点 —— **方案二(动态注入工具)做不到**(它依赖 server 接收结构化请求,纯 CLI 无 server)。

---

## ACP 侧:拦截 → probe → 弹窗 → 授权执行

### Step 1:server 拦截特殊报错(复用现有 error 链路)
现状的 error 传递链(api_error 的路径):
- CLI `agent.py:105` emit `error(code="api_error")` → stdout JSONL。
- server `server.py:495-497` 读到 `type=="error"` → 取 `message` 存局部 `error_msg` → `continue`。
- 退出后 `server.py:543-545`:若 `error_msg` 非空(且非 cancel)→ `raise RequestError.internal_error`。

改造:`server.py:495-497` 额外读 `code` 字段:
```python
if event.get("type") == "error":
    edata = event.get("data", {})
    code = edata.get("code", "unknown")
    if code == "permission_denied":
        pending_denials = edata.get("denials", [])   # 留给 probe
        error_msg = None   # 不当致命错误
    else:
        error_msg = edata.get("message", "unknown error")
    continue
```
`code == "permission_denied"` 短路掉 `raise`(`:543-545` 的 `if error_msg` 不命中),进入升级流程。其它 code 保持致命。

### Step 2:probe(两段式,用户不可见,不落 feed)
用 `call_llm`(`llm.py`)做单次调用,**不 spawn CLI 子进程**(避免事件重放开销 + feed 污染)。

**probe 上下文重建**:从 feed 用 `feed_util.extract_conversation_text`(`feed_util.py:113`)生成可读 transcript(已有,为 LLM 摘要设计)。把 denial 列表拼进去。

**两段式**(合并成一次 call,靠 tool_calls 是否非空区分):
- **判定段 + 提取段合一**:给 `call_llm` 传一个临时 SCHEMA `RequestPermission`,**只产出精确目标**:
  ```json
  {
    "name": "RequestPermission",
    "parameters": {
      "type": "object",
      "properties": {
        "reason": {"type": "string", "description": "为何受阻/需要什么权限"},
        "permission": {
          "type": "object",
          "properties": {
            "type": {"enum": ["write", "read", "cmd"]},
            "target": {"type": "string", "description": "精确的被拒命令或路径"}
          },
          "required": ["type", "target"]
        }
      },
      "required": ["reason", "permission"]
    }
  }
  ```
  - **泛化不在 probe 里做**:LLM 只回精确 `target`(如 `npm run dev`、`/a/b/c.txt`)。泛化由 **server 程序生成**(见下"泛化规则"),不依赖 LLM 语义判断 —— 可控、可测。
  system prompt 说明"基于上下文判断你是否因权限受阻;若是,调用工具声明所需的精确权限;若任务已完成或非权限问题,不要调用"。

**泛化规则**(server 侧,程序生成,allow-always 用):
由精确 `target` 派生 `always_pattern`(比 allow-once 宽一档)。无需 AI,确定性规则:
- **cmd**:`shlex.split(target)`,按 token 判断:
  - 命令名在**内置子命令名单**(`npm`、`git`、`cargo`、`pip`、`python -m` 等)→ 泛化到子命令层级。如 `npm run dev` → `npm run *`;`git push origin` → `git push *`。
  - 不在名单 → 泛化到命令名层级。如 `make build` → `make *`;`curl ...` → `curl *`。
  - 无子命令(单 token,如 `ls`)→ 不泛化,`always_pattern` = target(等同 once 粒度)。
- **path/write/read**:剥最后一段加通配。如 `/a/b/c.txt` → `/a/b/*`;`./src/x.py` → `./src/*`。
- **过宽兜底**:结果若是 `/**`、`*`、根目录裸通配等,回退到精确 target(防全权放行)。

**需要扩展 `call_llm`**:现状 `llm.py:49,76` 硬编码 `tools=[]`(single-shot)。改为参数 `tools=None`(默认 `[]` → 现状行为不变)。probe 传 `tools=[RequestPermission_SCHEMA]`。模型返回的 `tool_calls[0].arguments` 经 SDK 解析即合法结构体。

**probe 不可见、不落 feed**:call_llm 是独立 HTTP 调用,不经 hub、不写 stdout、不进 msg_sub。probe 结果只在 server 内存。

### Step 3:弹窗(ACP 原生 request_permission)
probe 拿到结构化请求 → `await self._conn.request_permission(options=[...], session_id=..., tool_call=...)`(`acp/agent/connection.py:103`,JSON-RPC 双向请求)。阻塞到用户响应,返回 `AllowedOutcome`(`outcome=="selected"`,带 `option_id`)/`DeniedOutcome`(`outcome=="cancelled"`)。

可重入性已确认:`prompt()` 跑在独立 asyncio task,接收循环独立,await 期间主循环照常收响应。无管道回压(probe 是 call_llm,没有子进程在跑)。

**option 设计**(`default_permission_options()` 的 3 选项,`acp/contrib/permissions.py:29`):
- `approve` → allow once
- `approve_for_session` → allow always
- `reject` → deny

### Step 4:授权执行(三种分支,按用户选的 option 取对应粒度)

**allow once**(approve):
- 用 probe 产出的**精确 `target`**。重新 spawn 子进程,**不带原始 prompt**(feed 已含),带 server 生成的固定续跑 prompt("权限已更新,请继续")+ `--feed --append`(恢复上下文)+ `--allow <type>:<target>`(新 flag,见下)。
- 例:`--allow 'cmd:npm run dev'`、`--allow 'write:/a/b/c.txt'`(精确,不泛化)。
- 续跑在一个 `prompt()` 内,不违反 request/response(一个 request → 多次内部 spawn → 最终一个 response)。

**allow always**(approve_for_session):
- 用 server **程序派生的泛化模式**(见 Step 2"泛化规则";过宽则回退到精确 target)。写入 **session 级**独立文件 `.letscode/config.<session_id>.json`(格式 `{"allowWrite":[...], "allowCmd":[...]}`),**不写 config.json**。
- 例:`{"allowCmd": ["npm run *"]}`、`{"allowWrite": ["/a/b/*"]}`(比 allow-once 宽一档)。
- **仅该 session 合并**:启动子进程时若存在该文件,读入并 merge 进 rules(见下"授权加载")。其它 session 看不到,不泄漏。
- 理由:① config.json 是静态共享配置(AGENTS.md 明确 holds API keys,勿提交 secrets),运行时授予不应混入;② 用户批准的是"这个会话里的这个操作",应绑定 session 而非 workspace —— 会话级持久让该 session 的后续 prompt(respawn、续跑)自动继承,但不泄漏到同 workspace 的其它会话;③ 泛化模式参考竞品设计,覆盖同类后续操作,减少反复弹窗;④ 独立文件语义干净、与 session 生命周期一致、可独立清理。

**deny**(reject):
- 什么都不做。`prompt()` 正常返回(尽力阶段的"我做不到"文本已在弹窗前流给用户,时序:失败文本先于弹窗)。等用户自己发下次 prompt。

---

## `--allow` flag(新实现)

### CLI 侧
现状无此 flag(`cli.py` 全部 flag 已核实,只有 `--preset`、`--no-sandbox` 影响权限)。新增:
- argparse:`--allow`,可重复(append),格式 `<type>:<target>`(如 `write:/etc/hosts`、`cmd:"npm test"`)。
- 解析成 partial rules dict:例如 `{"allowWrite": ["/etc/hosts"], "allowCmd": ["npm test"]}`。
- 接入点 `cli.py:142-143`:`load_rules(args.allow_dict)` → 与 `overrides.rules_raw` 合并后再 `merge_rules(config.preset, user_rules)`(`rules.py:51-67` 是 list 拼接,天然支持)。

### 语义验证:`--allow` 能穿透 deny 吗?
**能。** 经核实 `_pattern_specificity`(`rules.py:132-163`):
- `safe` preset 的 `deny_write=["/**"]` specificity = `(1, 0, 0)`(depth 0)。
- `--allow write:/etc/hosts` → `allow_write=["/etc/hosts"]` specificity = `(1, 1, 8)`(depth 1)。
- `check_write`(`rules.py:326-329`):allow specificity > deny specificity → 返回 None(放行)。**most-specific-pattern-wins 的"逃生舱"机制生效。**

### 授权加载(allow always)
启动子进程时,若 `.letscode/config.<session_id>.json` 存在,读入并 merge 进 rules(优先级低于 `--allow`,高于 config.preset)。这是 `--allow` 的会话级持久化形态,机制复用。

**session_id 的获取(约定,无需新 flag)**:`--feed <path>` 已传入,feed 文件名就是 session_id。CLI 从 `Path(args.feed).stem` 取 session_id,据此拼 `config.<session_id>.json`。无需额外 `--session-id` flag。纯 CLI 若用 `--feed` 也能加载;不传 `--feed` 则无 session_id,不加载(一次性运行,无会话内继承需求,合理)。

---

## 改动清单

### A. CLI 层
- **`rules.py`**:三个 check 函数额外记录 `{type, target}`(返回值不变,仍是 `<error>` 字符串;通过回调或返回结构化副产物留存)。
- **`tools/runner.py`**:加 `self._denials` 收集器;在 check_cmd 分支(`:117-122`)和 validate_path(`:79-88`)记录。
- **`agent.py:165-168`**:session-end 读 `tools._denials`,按触发判据决定 emit `error(code="permission_denied")` + denials 列表;纯 CLI 模式打印提示。
- **`cli.py`**:加 `--allow` flag + 解析;接入 `merge_rules`。加载 `.letscode/config.<session_id>.json`(session_id 取自 `--feed` 文件名 stem)。
- **`events.py`**:`emit_error` 已支持 code/recoverable,仅需 CLI 侧调用时带 `code="permission_denied"` + 在 data 里加 `denials`(可能需小幅扩展 emit_error 接受 extra data,或新增专用 emit)。

### B. LLM 层
- **`llm.py`**:`call_llm` 加 `tools=None` 参数(默认 `[]`),透传给 `consume_stream_async`(`:76`)。现状行为不变。

### C. ACP 层
- **`server.py:495-497`**:读 `code`,分流 `permission_denied`(存 denials,不致命)。
- **`server.py:543-587`**:退出后若有 `pending_denials`,进入升级流程(probe → request_permission → respawn / config.<session_id>.json 写入 / 返回)。
- probe 实现:用 `call_llm(tools=[RequestPermission_SCHEMA])` + `feed_util.extract_conversation_text` 重建上下文。
- respawn argv(`server.py:409`):allow once 时拼 `--allow <type>:<target>` + 续跑 prompt(`--text`)。

### D. 测试
- `rules.py`:check_* 记录 denial 的结构化留存。
- `agent.py`:触发判据(≥2 / 末次 denial)emit 正确 error 事件;假阳性(被拒后成功)不 emit。
- `llm.py`:call_llm 传 tools 时 tool_calls 正常返回。
- `server.py`:拦截 permission_denied 不 raise;probe mock;request_permission mock(approve/approve_for_session/reject 三分支);respawn argv 含 `--allow`;config.<session_id>.json 写入(allow always 分支)。
- `cli.py`:`--allow` 解析 + merge;safe preset 下 `--allow write:/path` 穿透 deny。

---

## 不改动的部分
- **主尽力循环零改动**:不注入工具、不改 system prompt、不 break。agent 全程不知升级存在。
- **不改 `stopReason`**:严格枚举(`schema.py:14`),用 `error.code` 通道。
- **不建反向 IPC / stdin PIPE**:升级在子进程退出后。
- **不主动拦截**:被动升级。

## 风险与回滚
- **probe 判定不准**:call_llm 可能误判(说需要但其实不需要,或反之)。靠宽松触发 + 两段式降低影响;probe 成本低(单次调用),用户不可见。
- **respawn 循环**:每轮 respawn 前都有用户人工审批(`request_permission`),用户是天然刹车 —— 觉得够了自然会拒绝,无需硬上限。授权对了则续跑成功自然终止(stop_reason 变 end_turn,不再触发升级)。
- **`--allow` 误授权**:用户人工审批,责任在用户;allow once 仅本次,allow always 落可清理文件。
- **向后兼容**:默认行为不变(无 denial → 无 error 事件 → 无升级);`call_llm` tools=None 默认;`--allow` 不传则无影响。
- 分层回滚:CLI 收集/触发、probe、server 升级三段解耦。

## 验证
- 上述测试全绿。
- 手动:构造必然被拒的写操作,观察 agent 尽力 → 子进程退出 → server 拦截 → probe → 弹窗 → 批准 → respawn 续跑成功。
- 纯 CLI:同场景观察打印 `--allow` 提示。

## 与其它需求的关系
- 与需求一(AgentCard)、需求三(无需改动)、需求四(已修复)正交。
- 下游 LetsBot 已实现"客户端侧审批卡 + 按钮回调",本方案补上 letscode 侧的"判定 + 提取 + 发起 + 自动续跑"。

## 待确认 / 开放问题
1. ~~probe 续跑 prompt 措辞~~ → **已定:固定串"权限已更新,请继续"**。
2. ~~MAX_RESPAWN 上限~~ → **已定:不设上限**(每轮人工审批即刹车)。
3. ~~config.<session_id>.json 生命周期~~ → **已定:暂不考虑清理**(残留无害;授权是会话内语义,文件孤立不影响其它 session)。
4. ~~CLI 如何获取 session_id~~ → **已定:`--feed` 文件名即 session_id**(`Path(args.feed).stem`),无需新 flag。纯 CLI 不传 `--feed` 则不加载 allow-always(合理)。
