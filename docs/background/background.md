我的问题：
现在我会让强的模型A中进行规划，写specs和plans，然后再让在其他Agent中使用原生模型B（区别于强模型A）来执行plans，最后再让另一款Agent中使用原生模型C来进行review；这是我的Agent开发模式，但我现在还是任务做起来很麻烦，是否有办法可以直接一套工作流下来，我直接把目标写好，harness直接使用相关工具，例如superpowers的brainstorming来收集用户需求，确定需求后自动用规定好的模型A写specs和plans，自动用模型B来执行，模型C来review，这样下来，人的参与感会很少，符合自动化的要求；我现在认为做到的问题就是人的在这个流程的参与感太强了，自动化不够；我备注一下，我是在codex中使用自带的gpt模型（也就是模型A）写specs和plans，然后再回到Zcode这个Agent使用原生字段的glm-5.2（也就是模型B）来执行，最后在grok build cli中使用原生的grok-4.5（也就是模型C）来进行review；

Agent的回答：
**可以做，而且你的 A→B→C 模式非常适合封装成一套 Harness。**

但先给出一个关键结论：

> 你现在的问题不是“缺少更强的 Agent”，而是三个 Agent 之间没有统一的任务协议、状态机和自动交接机制。

你现在实际上是在人工扮演 orchestrator：

```text
把目标告诉 Codex
→ 等它 brainstorming
→ 手动确认需求
→ 让它写 specs
→ 让它写 plans
→ 找到 plans 文件
→ 打开 ZCode
→ 告诉 GLM-5.2 执行
→ 中途不断确认或继续
→ 执行完成后打开 Grok Build
→ 告诉 Grok 审核
→ 再把审核结果复制回 ZCode
```

真正需要开发的是一个位于三者上方的 **Agent Workflow Harness**：

```text
                        ┌──────────────┐
                        │  用户目标     │
                        └──────┬───────┘
                               ↓
                  ┌────────────────────────┐
                  │ Agent Workflow Harness │
                  │ 状态、文件、权限、重试 │
                  └───────────┬────────────┘
                              ↓
             ┌─────────────────────────────────┐
             │ A：Codex / GPT                   │
             │ brainstorming → spec → plan     │
             └───────────────┬─────────────────┘
                             ↓
             ┌─────────────────────────────────┐
             │ B：ZCode / GLM-5.2               │
             │ execute → test → commit          │
             └───────────────┬─────────────────┘
                             ↓
             ┌─────────────────────────────────┐
             │ C：Grok Build / Grok-4.5         │
             │ review → PASS / FAIL             │
             └───────────────┬─────────────────┘
                             ↓
                 FAIL ──→ B 修复 ──→ C 再审
                             ↓
                           PASS
                             ↓
                      等待用户最终合并
```

## 一、三个环节中，A 和 C 已经比较容易自动化

### 模型 A：Codex

Codex 官方已经提供非交互模式：

```bash
codex exec "任务"
```

它可以：

* 在脚本和 CI 中运行；
* 使用 stdin 接收上下文；
* 输出 JSONL 事件；
* 通过 JSON Schema 输出结构化结果；
* 设置 sandbox 权限；
* 恢复前一次非交互会话。

因此，Codex 很适合作为规划器，由 Harness 直接启动，而不需要人工打开 TUI。([ChatGPT Learn][1])

例如：

```bash
codex exec \
  --sandbox workspace-write \
  --json \
  --output-schema .agentflow/schemas/planning-result.schema.json \
  -o .agentflow/runs/20260729-001/planning-result.json \
  "读取 GOAL.md，执行需求分析、设计和计划生成流程。
   将设计保存到 SPEC.md，将实施计划保存到 PLAN.md。
   不得修改产品代码。"
```

### 模型 C：Grok Build

Grok Build 官方也支持 headless 模式：

```bash
grok -p "Explain this codebase"
grok -p "Review the current changes" --output-format streaming-json
```

官方明确将其定位为可用于脚本、自动化和其他应用集成的模式。Grok Build 还支持 ACP，长期看也可以由其他支持 ACP 的 orchestrator 直接调度。([SpaceXAI Docs][2])

因此审核阶段可以这样执行：

```bash
grok -p "$(cat .agentflow/prompts/review.md)" \
  --output-format streaming-json \
  > .agentflow/runs/20260729-001/review-events.jsonl
```

## 二、真正的瓶颈是 ZCode

从目前公开的 ZCode 官方文档看，ZCode 已经有：

* Goal Mode；
* 自动迭代和目标验证；
* Bot Channel；
* Skills；
* Commands；
* Plugins；
* Subagents；
* MCP；
* Hooks。

其中 `/goal` 很接近你需要的执行机制：设置目标之后，ZCode 会持续迭代，并在每轮结束时自动验证目标；验证不通过就继续下一轮，而不是等待用户不断输入“继续”。([ZCode][3])

ZCode 也支持从微信或飞书 Bot 创建任务、切换模型、切换执行模式和继续长任务。([ZCode][4])

但是，我在当前官方文档中**没有找到类似下面这样的稳定外部接口**：

```bash
zcode exec --project xxx --prompt xxx
```

也没有找到公开的本地任务创建 API。

因此，按照当前公开能力判断：

> **要同时保留“ZCode 原生 GLM-5.2 订阅额度”和“完全由外部 Harness 无人值守调用”，目前 ZCode 是最难打通的一环。**

这不代表不能自动化，而是分为两个实现级别。

---

# 三、方案一：保留三个原生 Agent，做到约 85% 自动化

这是最适合你当前情况的方案。

## 自动化程度

| 阶段          | 执行方式                  |    是否需要人 |
| ----------- | --------------------- | -------: |
| 输入初始目标      | Harness CLI           |     需要一次 |
| 需求澄清        | Codex + Superpowers   |   可能需要回答 |
| Spec/Plan   | Codex headless        |      不需要 |
| 启动 ZCode 执行 | `/goal` 或 Bot Channel |   需要一次触发 |
| 实施和测试       | ZCode Goal Mode       |    通常不需要 |
| Grok 审核     | Grok headless         |      不需要 |
| 审核不通过后修复    | ZCode Goal Mode       | 可能需要一次触发 |
| 最终合并        | Harness               |   建议人工确认 |

你最终只需参与三个关键点：

```text
1. 输入目标并回答真正必要的问题
2. 确认需求和设计
3. 最终确认是否合并
```

中间的计划、执行、测试、审核、返修尽量全部自动化。

## ZCode 的接入方式

Harness 完成规划后自动生成：

```text
.agentflow/runs/20260729-001/
├── GOAL.md
├── REQUIREMENTS.md
├── SPEC.md
├── PLAN.md
├── EXECUTOR_PROMPT.md
├── REVIEW_PROMPT.md
└── state.json
```

其中 `EXECUTOR_PROMPT.md` 可以是：

```text
请进入 Goal Mode，完成以下目标：

严格按照：
- .agentflow/runs/20260729-001/SPEC.md
- .agentflow/runs/20260729-001/PLAN.md

执行本次开发任务。

要求：
1. 不得重新解释或扩大需求。
2. 按 PLAN.md 的任务编号依次执行。
3. 每个任务完成后运行对应验证命令。
4. 每个任务单独提交。
5. 所有测试通过后生成 execution-result.json。
6. 不得修改 SPEC.md 和 PLAN.md。
7. 以所有验收条件通过作为 Goal 完成标准。
```

你在 ZCode 中只需输入：

```text
/goal 按照 .agentflow/runs/20260729-001/EXECUTOR_PROMPT.md 完成任务
```

然后配合 `Auto Edit` 或合适的执行模式，ZCode 会尽可能持续执行。ZCode 的 Goal Mode 本身会保存目标、预算、迭代次数和验证时间线，并采用验证失败后继续执行的 failed-closed 方式。([ZCode][3])

### 进一步减少人工操作

可以用 ZCode Bot Channel 作为桥梁：

```text
Harness
  ↓
飞书机器人发送消息
  ↓
ZCode Bot Channel
  ↓
ZCode Agent 创建或继续任务
```

但这属于通过聊天通道间接调度，不如正式 CLI/API 稳定。我会把它定位成：

> 可以实验，但不要作为第一版 Harness 的核心依赖。

---

# 四、方案二：真正全自动，但执行阶段不再依赖 ZCode 桌面端

如果你的首要目标是：

> 从目标到代码、测试、审核全部无人值守。

那么执行器 B 最好换成一个支持以下能力的接口：

```text
非交互启动
结构化输出
可设置工作目录
可设置权限
可读取 Plan
可运行 Shell
可修改文件
能够返回退出状态
```

这时可以：

```text
A：Codex CLI / GPT
B：GLM-5.2 API + 自建 Agent Loop
C：Grok Build CLI
```

模型 B 仍然是 GLM-5.2，但不再使用 ZCode 的原生 Agent Runtime，而是由你自己的 Harness 给它提供：

* 文件读取工具；
* 文件修改工具；
* Shell 工具；
* Git 工具；
* 测试工具；
* Plan 状态管理；
* 任务完成判断。

代价是：

* 可能不能使用 ZCode 账户的原生订阅额度；
* 需要使用 API 计费；
* 需要自己实现 ZCode 已经做好的 Goal、权限和验证机制。

所以你的现实选择是：

| 目标                                | 推荐方式                 |
| --------------------------------- | -------------------- |
| 保留 Codex、ZCode、Grok Build 原生体验和订阅 | 半自动 Harness          |
| 真正一键全自动                           | 三个可编程接口/API          |
| 现在先落地，将来再完全自动化                    | Harness 加可替换 Adapter |

我更推荐第三种。

---

# 五、Harness 最重要的设计：Agent 之间不能靠聊天交接

你现在的交接方式大概率是：

```text
模型 A 说了一段话
→ 你理解这段话
→ 再组织语言告诉模型 B
```

Harness 中必须改成：

> A、B、C 通过版本化文件和 JSON Schema 交接，不通过自然语言聊天记录交接。

## 规划器 A 的输出协议

```json
{
  "run_id": "20260729-001",
  "status": "READY_FOR_EXECUTION",
  "goal": "重构 DevSetPage 但保持现有行为",
  "spec_file": "SPEC.md",
  "plan_file": "PLAN.md",
  "base_commit": "abc123",
  "tasks": [
    {
      "id": "T001",
      "title": "建立现有行为测试",
      "dependencies": [],
      "files": [
        "src/DevSetPage.cpp",
        "tests/DevSetPageTests.cpp"
      ],
      "verification": [
        "run-tests.bat DevSetPageTests"
      ],
      "acceptance_criteria": [
        "现有行为测试通过",
        "未修改生产逻辑"
      ]
    }
  ],
  "human_gate": {
    "required": false,
    "reason": null
  }
}
```

## 执行器 B 的输出协议

```json
{
  "run_id": "20260729-001",
  "status": "READY_FOR_REVIEW",
  "base_commit": "abc123",
  "head_commit": "def456",
  "tasks": [
    {
      "id": "T001",
      "status": "COMPLETED",
      "commit": "111aaa",
      "verification": [
        {
          "command": "run-tests.bat DevSetPageTests",
          "exit_code": 0
        }
      ]
    }
  ],
  "remaining_risks": []
}
```

## 审核器 C 的输出协议

```json
{
  "decision": "FAIL",
  "spec_compliance": false,
  "issues": [
    {
      "id": "R001",
      "severity": "HIGH",
      "category": "SPEC_VIOLATION",
      "file": "src/DevSetPage.cpp",
      "line": 214,
      "description": "修改了未在计划中授权的初始化顺序",
      "required_action": "恢复初始化顺序并补充回归测试"
    }
  ],
  "re_review_required": true
}
```

这样 Harness 不需要“理解 Grok 说了什么”，只需要判断：

```python
if review["decision"] == "PASS":
    state = "AWAITING_FINAL_MERGE"
else:
    state = "REPAIR_REQUIRED"
```

---

# 六、自动返修循环

你的工作流不应该在 Grok Review 后结束，而应该形成闭环：

```text
B 执行
  ↓
运行确定性测试
  ↓
C 审核
  ↓
PASS ─────────→ 最终合并
  ↓ FAIL
生成 REPAIR_PLAN.md
  ↓
B 只修复审核问题
  ↓
重新运行测试
  ↓
C 再次审核
```

但要设置上限：

```yaml
repair:
  max_rounds: 2
  same_issue_limit: 1
  stop_on:
    - architecture_conflict
    - requirement_ambiguity
    - destructive_migration
    - repeated_test_failure
```

不能让 B 和 C 无限循环争论。

建议规则：

```text
第一次 FAIL：
自动交给 B 修复。

第二次 FAIL：
如果是新问题，再允许修复一次；
如果是同一个问题，停止并交给人。

连续两次审核结论矛盾：
停止并交给人。

涉及需求解释：
交回 A，而不是让 B 自行决定。
```

这也是为什么 Harness 必须有状态机，而不能只是三条 Shell 命令串起来。

---

# 七、Superpowers brainstorming 不能原样用于“完全无人值守”

这里存在一个容易忽略的冲突。

Superpowers 当前的 `brainstorming` skill 明确要求：

* 逐个询问澄清问题；
* 展示多个方案；
* 分段获得用户批准；
* 用户审核写入的 spec；
* 用户批准后才能进入 writing-plans。

也就是说，它本身就是一个强调 human-in-the-loop 的流程，而不是无人值守流程。([GitHub][5])

Superpowers 的完整工作流也是：

```text
brainstorming
→ worktree
→ writing-plans
→ executing-plans / subagent-driven-development
→ TDD
→ code review
→ finishing branch
```

其中原生 `executing-plans` 也包含人工 checkpoint。([GitHub][6])

所以你不能简单地写：

```bash
codex exec "使用 superpowers brainstorming 自动完成所有事情"
```

否则会有三个可能结果：

1. Agent 等待用户回答，自动化卡住；
2. Agent 自己假装获得了用户批准；
3. Agent 跳过 skill 规则继续执行。

## 正确做法：派生一个 bounded-brainstorming skill

例如：

```text
bounded-brainstorming/
└── SKILL.md
```

核心规则：

```text
1. 先分析仓库和用户目标。
2. 只有答案会明显改变架构或验收标准时才提问。
3. 最多提出 3 个问题。
4. 可以从代码和已有文档推断的内容，不要询问用户。
5. 非关键缺失信息写入 ASSUMPTIONS.md。
6. 所有假设必须标注风险等级。
7. 只有高风险假设才阻塞流程。
8. 中低风险假设自动采用推荐默认值。
9. 设计完成后只设置一次用户批准门。
10. 未获得批准时不得进入实施阶段。
```

规划输出状态只允许三种：

```text
NEEDS_USER_INPUT
NEEDS_SPEC_APPROVAL
READY_FOR_EXECUTION
```

这样人的参与从“不断交流”压缩成：

```text
最多回答三项真正关键的问题
+ 一次批准 Spec
```

---

# 八、推荐的状态机

不用一开始就上 LangGraph。一个 Python 状态机已经够用。

```text
NEW
 ↓
DISCOVERING_CONTEXT
 ↓
NEEDS_USER_INPUT ──→ 用户回答 ──┐
 ↓                              │
DRAFTING_SPEC ←─────────────────┘
 ↓
NEEDS_SPEC_APPROVAL
 ↓
PLANNING
 ↓
READY_FOR_EXECUTION
 ↓
EXECUTING
 ↓
VERIFYING
 ↓
READY_FOR_REVIEW
 ↓
REVIEWING
 ├── PASS → AWAITING_FINAL_MERGE
 ├── FAIL → REPAIRING → VERIFYING
 └── BLOCKED → NEEDS_HUMAN
```

每次状态变化都写入：

```json
{
  "run_id": "20260729-001",
  "state": "REVIEWING",
  "previous_state": "READY_FOR_REVIEW",
  "updated_at": "2026-07-29T11:30:00",
  "attempt": 1,
  "planner": "codex-gpt",
  "executor": "zcode-glm-5.2",
  "reviewer": "grok-build-grok-4.5"
}
```

---

# 九、适合你的项目目录

```text
your-project/
├── .agentflow/
│   ├── config.yaml
│   ├── prompts/
│   │   ├── planner.md
│   │   ├── executor.md
│   │   ├── reviewer.md
│   │   └── repair.md
│   ├── schemas/
│   │   ├── planning-result.schema.json
│   │   ├── execution-result.schema.json
│   │   └── review-result.schema.json
│   ├── skills/
│   │   ├── bounded-brainstorming/
│   │   ├── writing-specs/
│   │   └── writing-plans/
│   └── runs/
│       └── 20260729-001/
│           ├── GOAL.md
│           ├── REQUIREMENTS.md
│           ├── ASSUMPTIONS.md
│           ├── SPEC.md
│           ├── PLAN.md
│           ├── planning-result.json
│           ├── execution-result.json
│           ├── review-result.json
│           ├── test-results/
│           └── state.json
├── src/
├── tests/
└── AGENTS.md
```

ZCode 支持标准 `SKILL.md`，还可以从 Codex 等外部 Agent 导入或建立符号链接，因此你的部分 skill 可以在 Codex 和 ZCode 之间共享，不必各维护一份。([ZCode][7])

---

# 十、一份实际的配置文件

```yaml
version: 1

workflow:
  worktree: true
  branch_prefix: agentflow
  max_repair_rounds: 2

human_gates:
  requirements:
    mode: only_when_blocked
    max_questions: 3

  spec_approval:
    required: true

  execution:
    required: false

  destructive_commands:
    required: true

  final_merge:
    required: true

planner:
  type: codex_cli
  command: codex
  mode: exec
  sandbox: workspace-write
  output_schema: .agentflow/schemas/planning-result.schema.json
  skills:
    - bounded-brainstorming
    - writing-specs
    - writing-plans

executor:
  type: zcode_desktop
  model: glm-5.2
  mode: goal
  delivery: manual_or_bot_channel
  instructions_file: EXECUTOR_PROMPT.md

reviewer:
  type: grok_build_cli
  command: grok
  model: grok-4.5
  output_format: streaming-json
  read_only: true

verification:
  commands:
    - python .agentflow/scripts/check_plan_completion.py
    - python .agentflow/scripts/run_project_tests.py
    - python .agentflow/scripts/check_unplanned_changes.py

review:
  require:
    - spec_compliance
    - plan_compliance
    - tests_passed
    - no_high_severity_issues
```

注意 `executor.type` 要设计成 Adapter，而不是写死。

未来 ZCode 如果增加正式 headless CLI，只需要替换：

```yaml
executor:
  type: zcode_cli
```

其余状态机、文件协议、审核循环都不用重写。

---

# 十一、哪些地方一定要保留人的参与

我不建议追求彻底的零参与，而是把人放在**语义决策和不可逆决策**上。

建议只保留以下门：

### 必须保留

```text
需求存在重大歧义
多个方案会产生不同产品结果
数据库破坏性迁移
删除大量代码或文件
修改外部接口
涉及生产环境
连续审核失败
最终合并
```

### 可以完全自动化

```text
读取仓库
生成 Spec 和 Plan
创建 worktree
按照 Plan 修改代码
运行编译和测试
收集日志
生成执行报告
调用 Grok Review
根据明确审核问题返修
重新测试和再审
整理最终报告
```

因此比较理想的目标不是：

> 完全没有人参与。

而是：

> 人只负责目标、重大语义选择和最终授权；Harness 负责整个工程执行过程。

---

# 十二、我的具体建议

你的第一版不要尝试一次性打通所有客户端，先做一个 **AgentFlow v0.1**：

```text
agentflow start goal.md
```

它自动完成：

```text
1. 创建 worktree
2. 调用 Codex 生成 Spec 和 Plan
3. 输出一次 Spec 审批
4. 生成 ZCode Goal 指令
5. 你在 ZCode 中触发一次 /goal
6. Harness 检测 execution-result.json 或 Git 提交
7. 自动运行确定性测试
8. 自动调用 Grok Build Review
9. 审核通过后输出最终报告
10. 审核失败则生成 ZCode Repair 指令
```

这已经可以把原来十几次人工复制、切换、提醒和判断，压缩成：

```text
一次输入目标
一次批准 Spec
一次触发 ZCode Goal
一次批准最终合并
```

等 ZCode 提供正式 headless CLI、外部任务 API 或更稳定的 Bot 自动接入后，再把中间那次触发去掉。

**你的下一步不应该是继续研究更多 Agent 使用技巧，而是先定义 A、B、C 三者的文件协议和状态机。**只要这两个部分建立起来，Codex、ZCode、Grok Build 都只是可以替换的执行 Adapter。

[1]: https://learn.chatgpt.com/docs/non-interactive-mode "Non-interactive mode | ChatGPT Learn"
[2]: https://docs.x.ai/build/overview "Grok Build | SpaceXAI Docs"
[3]: https://zcode.z.ai/en/docs/goal?utm_source=chatgpt.com "Goal Mode | ZCODE Docs"
[4]: https://zcode.z.ai/en/docs/bot-channel?utm_source=chatgpt.com "Bot Channel | ZCODE Docs"
[5]: https://github.com/obra/superpowers/blob/main/skills/brainstorming/SKILL.md "superpowers/skills/brainstorming/SKILL.md at main · obra/superpowers · GitHub"
[6]: https://github.com/obra/Superpowers "GitHub - obra/superpowers: An agentic skills framework & software development methodology that works. · GitHub"
[7]: https://zcode.z.ai/en/docs/skill?utm_source=chatgpt.com "Skill | ZCODE Docs"


我的问题：
其实，换个方式，能不能把主会话有Zcode来驱动，也就是Zcode来指挥codex cli使用指定的模型和思考强度来写specs和plans，然后Zcode自生调用原生的模型来执行任务，每次执行完一个小任务需要review，则调用grok build cli来进行review任务，如果中途遇到需要修改plans的情况，也是由Zcode来驱动 codex cli来修改plans，你认为这种模式是否可行？（比上面的方案更友好？）

Agent的回答：
**可行，而且对你现在的使用习惯来说，确实比“额外开发一个独立 Harness 作为总控”更友好。**

更准确地说，你可以把架构改成：

> **ZCode 是主控 Agent 和工作台，Codex CLI 是规划专家，Grok Build CLI 是独立审查专家。**

```text
用户
  ↓
ZCode 主会话（GLM-5.2）
  ├─ 调用 Codex CLI：需求分析、Spec、Plan
  ├─ 自己执行 Plan 中的小任务
  ├─ 调用 Grok Build CLI：逐任务 Review
  ├─ 根据 Review 自己修复
  ├─ 需要改计划时调用 Codex CLI：Replan
  └─ 全部完成后调用 Grok Build：最终 Review
```

这是一种典型的 **Supervisor + Specialist Agents** 模式。

## 一、为什么它比上一种方案更适合你

上一种方案是：

```text
独立 Harness
  ├─ 调 Codex
  ├─ 调 ZCode
  └─ 调 Grok
```

问题是 ZCode 缺少成熟的外部 headless 调用接口，因此独立 Harness 很难真正自动启动和控制 ZCode。

现在反过来：

```text
ZCode
  ├─ 调 Codex CLI
  └─ 调 Grok Build CLI
```

就避开了这个瓶颈。

Codex 官方支持 `codex exec` 非交互运行、JSONL 事件、JSON Schema 结构化输出、指定 sandbox，以及继续已有非交互会话，非常适合作为被 ZCode 调用的规划服务。([ChatGPT Learn][1])

Grok Build 也原生支持 headless 模式，可以指定工作目录、模型、会话 ID，并输出 JSON 或 streaming JSON；它还支持 ACP，因此很适合作为独立审核器。([SpaceXAI Docs][2])

ZCode 本身则能在一个任务中访问工作区、运行命令、使用 Skill、Command、MCP、Hooks 和 Goal Mode，并且 Full Access 模式可以减少执行过程中的确认。因此，让 ZCode 持有主会话和执行上下文是合理的。([ZCode][3])

对你个人实际使用而言，两种方案可以这样比较：

| 维度                  | 独立 Harness 总控 | ZCode 主控 |
| ------------------- | ------------: | -------: |
| 保留 ZCode 原生 GLM-5.2 |            困难 |     完整保留 |
| 只使用一个主界面            |             否 |        是 |
| 开发成本                |             高 |       中低 |
| 自动调用 Codex          |            可以 |       可以 |
| 自动调用 Grok Build     |            可以 |       可以 |
| 主执行上下文连续性           |            较差 |       很好 |
| 可测试、可观测性            |            更强 |     需要补充 |
| 后期平台化               |            更好 |       稍弱 |
| 当前适用性               |            一般 |  **更合适** |

所以我的判断是：

> **作为你的第一版 Agent 开发项目，ZCode 主控比独立 Harness 总控更合适。**

但不能只靠一段很长的提示词让 ZCode“自由发挥”，需要给它加一层确定性的工作流约束。

---

# 二、不要让 ZCode 直接随意拼接 Shell 命令

最低级的实现方式是直接告诉 ZCode：

```text
需要规划时运行 codex exec……
需要审核时运行 grok -p……
```

这虽然能运行，但会有很多不稳定因素：

* ZCode 每次生成的 Codex prompt 可能不同；
* 可能忘记指定模型和思考强度；
* 可能忘记设置只读权限；
* 可能错误解析 Grok 的自然语言结果；
* 可能跳过某次 Review；
* 可能自行修改 Plan；
* 可能连续调用导致死循环；
* 上下文压缩后可能忘记流程状态。

因此更好的实现不是：

```text
ZCode → 任意 Shell 命令
```

而是：

```text
ZCode
  ↓
固定的 AgentFlow Skill
  ↓
一组确定性工具
  ├─ create_plan()
  ├─ review_task()
  ├─ revise_plan()
  ├─ get_workflow_state()
  └─ complete_task()
```

也就是说，**ZCode 负责决策和执行，但 Codex/Grok 的调用参数由脚本或 MCP 工具固定下来。**

---

# 三、推荐架构

```text
┌────────────────────────────────────┐
│ ZCode 主会话 / GLM-5.2              │
│                                    │
│ 职责：                              │
│ · 与用户沟通                        │
│ · 维护整体目标                      │
│ · 执行具体代码任务                  │
│ · 调用规划和审核工具                │
│ · 根据审核意见修复                  │
└──────────────┬─────────────────────┘
               │
       ┌───────┴────────┐
       ↓                ↓
┌──────────────┐  ┌──────────────┐
│ Codex Planner│  │ Grok Reviewer│
│ GPT 模型 A    │  │ Grok-4.5     │
│              │  │              │
│ · Spec       │  │ · Task Review│
│ · Plan       │  │ · Final Review│
│ · Replan     │  │ · PASS/FAIL   │
└──────────────┘  └──────────────┘
               │
               ↓
┌────────────────────────────────────┐
│ .agentflow/ 持久化状态              │
│                                    │
│ SPEC.md                            │
│ PLAN.md                            │
│ state.json                         │
│ task-results/                      │
│ reviews/                           │
│ plan-history/                      │
└────────────────────────────────────┘
```

这里最重要的一点是：

> **主会话在 ZCode，但工作流状态不应该只存在于 ZCode 的聊天上下文里。**

聊天上下文负责理解，文件负责记忆和约束。

---

# 四、建议开发一个 ZCode Plugin

ZCode 的 Plugin 可以同时打包：

* Skill；
* Command；
* Subagent；
* MCP Server；
* Hook。

因此，你可以做一个本地插件，例如：

```text
zcode-agentflow/
├── .zcode-plugin/
│   └── plugin.json
├── skills/
│   └── agentflow/
│       └── SKILL.md
├── commands/
│   ├── agentflow-start.md
│   └── agentflow-status.md
├── mcp/
│   └── server/
├── hooks/
│   ├── hooks.json
│   └── validate-stop.js
├── schemas/
└── prompts/
```

ZCode 官方的插件体系正适合把 Skill、命令、MCP 和 Hook 打包成一个工作区能力。([ZCode][4])

你最终在 ZCode 中只需要输入：

```text
/agentflow-start 实现某某功能……
```

或者：

```text
$agentflow
目标：重构某模块，保持现有行为不变……
```

然后 ZCode 按固定流程执行。

---

# 五、ZCode 应该调用的工具

## 1. `create_plan`

输入：

```json
{
  "goal": "实现……",
  "model": "gpt-5.6",
  "reasoning_effort": "xhigh",
  "run_id": "20260729-001"
}
```

工具内部启动 Codex：

```bash
codex exec \
  --model gpt-5.6 \
  --config model_reasoning_effort=xhigh \
  --sandbox read-only \
  --output-schema .agentflow/schemas/planning-result.schema.json \
  -o .agentflow/runs/20260729-001/planning-result.json \
  -
```

完整 prompt 通过 stdin 传入。

Codex 配置目前支持 `model_reasoning_effort`，可用值包括 `minimal`、`low`、`medium`、`high`、`xhigh`，实际是否支持 `xhigh`取决于所选模型；显式的模型和 reasoning 配置可以覆盖默认配置。([ChatGPT Learn][5])

这里推荐让 Codex 运行在 `read-only`：

* Codex读取代码库；
* 输出结构化 JSON；
* MCP wrapper 再把 JSON 中的 `spec`、`plan` 写入文件。

不要直接让规划 Codex 获得整个代码库的写权限。

返回结果：

```json
{
  "status": "READY",
  "spec_markdown": "...",
  "plan_markdown": "...",
  "tasks": [
    {
      "id": "T001",
      "title": "建立基线测试",
      "dependencies": [],
      "acceptance_criteria": [],
      "verification_commands": []
    }
  ],
  "questions": [],
  "assumptions": []
}
```

如果 Codex 判断信息不足：

```json
{
  "status": "NEEDS_USER_INPUT",
  "questions": [
    {
      "id": "Q001",
      "question": "旧接口是否必须保持二进制兼容？"
    }
  ]
}
```

ZCode 将问题转交用户，而不是自行猜测。

---

## 2. `review_task`

当 ZCode 完成一个小任务时，先执行确定性验证：

```text
编译
→ 测试
→ 静态检查
→ Git diff 检查
→ Grok Review
```

然后调用：

```bash
grok \
  --no-auto-update \
  -p "读取审核提示词并审核任务 T001" \
  --model grok-4.5 \
  --cwd <project-path> \
  --output-format json
```

Grok Build 官方支持 `--model`、`--cwd`、`--session-id` 和 JSON 输出，适合由工具包装后调用。([SpaceXAI Docs][2])

但不要让 Grok 只返回一段文章，要求固定结构：

```json
{
  "decision": "PASS",
  "task_id": "T001",
  "plan_compliance": true,
  "spec_compliance": true,
  "tests_sufficient": true,
  "issues": [],
  "replan_required": false
}
```

或者：

```json
{
  "decision": "FAIL",
  "task_id": "T001",
  "issues": [
    {
      "id": "R001",
      "severity": "HIGH",
      "file": "src/Test.cpp",
      "line": 118,
      "description": "行为与验收条件 AC-03 不一致",
      "required_action": "恢复旧行为并补充回归测试"
    }
  ],
  "replan_required": false
}
```

ZCode只能依据字段进入下一状态：

```text
PASS → 下一个任务
FAIL → 修复当前任务
REPLAN → 调用 Codex
BLOCKED → 询问用户
```

不能由 ZCode自己主观解释成“问题不大，可以继续”。

---

## 3. `revise_plan`

这一步正是你提出的关键场景：

> 执行过程中发现原 Plan 不合理，由 ZCode 驱动 Codex 修改 Plan。

它完全可行，但要限制权责：

```text
ZCode：
发现问题、收集证据、发起 Replan Request

Codex：
分析影响、修改剩余 Plan

ZCode：
不能直接改 Plan
```

ZCode生成：

```json
{
  "run_id": "20260729-001",
  "current_task": "T004",
  "reason": "原计划假设接口 X 可复用，但实际缺少 Y 能力",
  "evidence": [
    "src/X.cpp:120",
    "test failure: ..."
  ],
  "completed_tasks": [
    "T001",
    "T002",
    "T003"
  ],
  "constraints": [
    "已完成任务不可无理由重做",
    "SPEC 中的 AC-01 至 AC-08 不可修改"
  ]
}
```

然后调用 Codex：

```bash
codex exec \
  --model gpt-5.6 \
  --config model_reasoning_effort=xhigh \
  --sandbox read-only \
  --output-schema .agentflow/schemas/replan-result.schema.json \
  -o .agentflow/runs/20260729-001/replan-result.json \
  -
```

Codex输出：

```json
{
  "decision": "PLAN_UPDATED",
  "spec_change_required": false,
  "superseded_tasks": ["T004"],
  "new_tasks": ["T004-R1", "T004-R2"],
  "plan_markdown": "...",
  "change_summary": "..."
}
```

如果发现不是 Plan 问题，而是需求本身需要变化：

```json
{
  "decision": "SPEC_CHANGE_REQUIRED",
  "reason": "...",
  "questions": ["..."]
}
```

这时必须停下来问用户。

---

# 六、完整状态机

ZCode不能只根据自然语言“记住现在做到哪里”，应使用 `state.json`：

```text
NEW
 ↓
PLANNING
 ├─ NEEDS_USER_INPUT → 用户回答 → PLANNING
 └─ READY
       ↓
EXECUTING_TASK
       ↓
VERIFYING_TASK
       ├─ 验证失败 → FIXING_TASK
       └─ 验证通过
              ↓
REVIEWING_TASK
       ├─ PASS → COMPLETE_TASK → NEXT_TASK
       ├─ FAIL → FIXING_TASK → VERIFYING_TASK
       ├─ REPLAN → REPLANNING → EXECUTING_TASK
       └─ BLOCKED → NEEDS_USER_INPUT
              ↓
FINAL_VERIFY
              ↓
FINAL_REVIEW
       ├─ PASS → READY_TO_MERGE
       └─ FAIL → FINAL_REPAIR
```

`state.json` 示例：

```json
{
  "run_id": "20260729-001",
  "state": "REVIEWING_TASK",
  "plan_version": 2,
  "current_task": "T004-R1",
  "completed_tasks": [
    "T001",
    "T002",
    "T003"
  ],
  "review_round": 1,
  "repair_round": 0,
  "base_commit": "abc123",
  "task_start_commit": "def456"
}
```

---

# 七、这套模式最大的风险：让较弱的 B 当总指挥

你的架构中：

* 模型 A 更强，负责规划；
* 模型 B 相对弱，负责执行；
* 模型 C 负责审核；
* 但模型 B 同时负责决定什么时候调用 A 和 C。

这会产生一个潜在问题：

> **执行模型虽然不负责做顶层设计，却负责判断什么时候需要重新设计。**

如果 GLM-5.2误判，它可能：

* 明明需要 Replan，却自行绕过去；
* 将 Plan 问题当成代码问题；
* 为了完成当前任务而扩大改动范围；
* 对 Grok 的 FAIL 进行辩解；
* 没有调用 Codex 就自行修改 Plan；
* 跳过某个审核步骤。

所以这套架构能不能稳定，不取决于 GLM-5.2有多聪明，而取决于你是否建立以下硬规则。

## 规则一：Plan 文件只允许 Codex工具修改

ZCode如果尝试修改：

```text
SPEC.md
PLAN.md
plan-history/*
```

由 Hook 拒绝。

ZCode Hooks 可以在 `PreToolUse` 阶段允许、询问或拒绝工具调用，也可以在 Stop 阶段检查结果并让主模型继续执行。([ZCode][6])

## 规则二：没有 Review PASS，任务不能标记完成

`complete_task()` 工具必须验证：

```text
测试结果存在
review-result.json 存在
decision == PASS
被审核的 commit 与当前 HEAD 一致
```

否则拒绝状态转移。

## 规则三：重复失败必须停止

例如：

```yaml
limits:
  max_task_repair_rounds: 2
  max_replan_rounds: 2
  max_final_review_rounds: 2
```

超过上限就进入：

```text
NEEDS_HUMAN
```

## 规则四：审核对象必须固定

Review时记录：

```json
{
  "base_commit": "abc123",
  "head_commit": "def456",
  "task_id": "T003",
  "plan_version": 1
}
```

避免 Grok审核的是一个版本，ZCode后来又改了代码，却继续沿用旧 PASS。

---

# 八、不是每完成一个“很小的修改”都调用 Grok

这里我建议对你的想法稍微调整。

不要这样：

```text
改一个函数
→ Grok Review
改一个测试
→ Grok Review
调整一个参数
→ Grok Review
```

这样会产生：

* 审核次数太多；
* 上下文碎片化；
* Grok看不到完整意图；
* 时间和额度消耗大；
* 审核报告大量重复。

更合适的审核单位是：

> **Plan 中一个可独立验收、可独立提交的任务。**

例如：

```text
T001：冻结当前行为并建立基线测试
T002：提取配置解析组件
T003：迁移调用方
T004：删除旧实现
```

每个任务完成后：

```text
测试
→ 生成 commit
→ Grok审核该 commit 相对于上一个任务 commit 的 diff
→ PASS 后进入下一任务
```

最后再做一次全局 Review：

```text
整个功能分支
vs.
初始 base commit
```

---

# 九、ZCode Skill 中应写的核心规则

可以将工作流定义成一个 `$agentflow` Skill：

```text
你是本工作流的 Supervisor 和 Executor。

你的职责：
1. 与用户沟通并维护当前目标。
2. 通过 planner_create 工具生成 SPEC 和 PLAN。
3. 不得自行创建或修改 SPEC、PLAN。
4. 按 PLAN 中的 task 顺序逐项执行。
5. 每完成一个 task，必须先运行确定性验证。
6. 验证通过后调用 reviewer_review_task。
7. reviewer 返回 PASS 后，才可标记 task 完成。
8. reviewer 返回 FAIL 时，只修复列出的有效问题。
9. reviewer 返回 REPLAN 时，必须调用 planner_revise_plan。
10. 不得擅自改变验收条件。
11. 超过修复或重规划次数上限时停止并请求用户处理。
12. 全部 task 完成后执行 final review。
13. 未获得 final PASS 不得声明任务完成。
```

但 Skill 只是行为指导，真正的约束还得放进 MCP 工具和 Hooks。

---

# 十、ZCode Hooks 应该承担什么

Hooks 不适合承担整个状态机。官方说明中，Hook 本质上是一个本地子进程协议，它不能直接调用 ZCode 模型；Stop Hook 连续让模型继续的次数也有上限。([ZCode][6])

所以合理分工是：

| 部件           | 作用                 |
| ------------ | ------------------ |
| ZCode Skill  | 告诉模型完整工作方式         |
| MCP Server   | 调用 Codex、Grok、读写状态 |
| Hook         | 防止违规和漏步骤           |
| `state.json` | 持久化流程状态            |
| Codex        | Spec、Plan、Replan   |
| ZCode        | 代码执行和修复            |
| Grok         | 独立审核               |
| 用户           | 高风险语义决策和最终合并       |

Stop Hook 可以检查：

```text
当前任务是否有测试结果？
是否有对应 Review？
Review是否 PASS？
HEAD是否与被审核 commit 一致？
```

没有满足就反馈给 ZCode继续完成，而不是允许结束。

---

# 十一、第一版不必做得太复杂

第一版可以只实现四个 MCP 工具：

```text
planner_create(goal)
planner_revise(reason)
reviewer_review_task(task_id)
workflow_status()
```

再增加两个本地状态工具：

```text
workflow_start_task(task_id)
workflow_complete_task(task_id)
```

最终体验可以是：

```text
你：
$agentflow 重构 DevSetPage，保持行为不变。

ZCode：
正在调用 Codex Planner……
Codex 生成了 SPEC 和 8 个任务。
发现两个关键需求问题，请确认……

你：
回答问题。

ZCode：
计划已经确定，开始执行 T001。
……
T001 测试通过，正在调用 Grok Review。
Grok：PASS。
开始执行 T002。
……
T004 发现原计划对接口生命周期的假设不成立，
正在调用 Codex Replan。
Codex 已生成 Plan v2，T004 拆分为 T004-R1 和 T004-R2。
……
全部任务完成，正在进行最终审核。
Grok：PASS。
等待你确认合并。
```

这已经非常接近你想要的体验。

---

# 十二、最终判断

## 是否可行

**可行。**

Codex CLI 和 Grok Build CLI 都具备较好的非交互调用能力；ZCode则拥有主会话、原生 GLM-5.2执行能力、工作区上下文、Goal Mode、Skill、MCP、Plugin 和 Hook，能够承担 Supervisor 的用户界面和执行角色。([ChatGPT Learn][1])

## 是否比上一种方案友好

对你目前的情况，**是的，更友好**：

```text
ZCode = 主工作台 + Supervisor + Executor
Codex = Planner
Grok Build = Reviewer
```

它能保留三款工具各自的原生模型权益，同时把人的操作集中在一个 ZCode 主会话中。

但我建议把它理解成：

> **ZCode驱动的 AgentFlow**

而不是：

> **让 GLM-5.2随意指挥两个 CLI。**

前者有：

* 固定工具；
* 结构化输入输出；
* 状态机；
* 权限边界；
* 版本化 Plan；
* 审核门禁；
* 失败次数上限。

后者只是一个很长的提示词，很容易在长任务中逐渐失控。

**最适合你的落地顺序是：先做 ZCode Skill + 三个调用脚本，验证流程；稳定后再把调用脚本封装为 ZCode MCP Server 和 Plugin。**

[1]: https://learn.chatgpt.com/codex/non-interactive-mode "Non-interactive mode | ChatGPT Learn"
[2]: https://docs.x.ai/build/cli/headless-scripting "Headless & Scripting | SpaceXAI Docs"
[3]: https://zcode.z.ai/en/docs/agents "ZCode Agent | ZCODE Docs"
[4]: https://zcode.z.ai/en/docs/plugin "Plugin | ZCODE Docs"
[5]: https://learn.chatgpt.com/codex/config-file/config-reference "Configuration Reference | ChatGPT Learn"
[6]: https://zcode.z.ai/en/docs/hooks "Hooks | ZCODE Docs"
