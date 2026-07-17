# 标准 Harness Agent 架构

> 目标：一套可复用的 Harness 内核，承载聊天助手、AI 短剧制作、自动化等 Agent 产品，而非为每种 Agent 复制一套循环。

## 1. 架构定位

这不是"Prompt + 工具"的聊天循环，而是一套由六层职责边界组成的**受控 Agent Harness**：

- Agent 产品声明身份、知识策略、Skill 与申请的能力；
- Task 与 Plan 描述需要长期推进的工作；
- Harness 让工作以可控、可恢复、可审计的方式运行和交付；其内核以受治理的执行循环推进单个 Agent Run。

模型可以提出下一步动作，但永远不拥有最终执行权限；⑤ 层决定能否继续和能否执行，⑥ 层只执行已批准的动作。

## 2. 六层架构

六层是职责边界，不意味着要拆成六个服务，也不是一次从 ① 到 ⑥ 后就结束的线性管道。一次渠道请求先经过 ①–③ 确定 Agent、持久化工作与委派，再进入由 ④、⑤、⑥ 组成的受控执行循环；循环结束后，③ 汇总结果，② 记录可追溯事实。

![标准 Harness Agent 架构图](assets/architecture.svg)

> 左栏为接入渠道与 Agent 产品；①–③ 是请求、Task 与委派控制流，④–⑥ 是 Harness 内核的受控执行循环。橙色弧线表示 ⑥ 的 Observation / Artifact / Effect 结果回流给 ④；⑤ 可拒绝、暂停或因预算耗尽而把控制权交回 ③ 与 ②。右栏三块为全层可读写的横切平面。

### 2.1 两条控制流

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryTextColor':'#1E1B4B','lineColor':'#94A3B8','fontFamily':'Inter, sans-serif'}}}%%
flowchart TB
    IN["渠道输入"]:::edge --> A1["① Agent 路由"]:::agent
    A1 --> T2["② Task / Plan"]:::task
    T2 --> O3["③ 编排 / WorkOrder"]:::orch

    subgraph CORE["Harness 内核：受控执行循环"]
        direction LR
        R4["④ Runtime<br/>决策"]:::runtime --> G5{"⑤ 治理<br/>裁决"}:::gate
        G5 -->|"已授权动作"| C6["⑥ 能力<br/>执行"]:::cap
        C6 -. "Observation / 证据" .-> R4
    end

    O3 --> R4
    G5 -->|"终态输出"| DONE["③ 汇总<br/>② Run / Artifact"]:::done
    G5 -->|"拒绝 / 暂停 / 预算耗尽"| PAUSE["③ 编排<br/>② checkpoint"]:::pause

    classDef edge fill:#E0F2FE,stroke:#0284C7,color:#0C4A6E;
    classDef agent fill:#EEF4FF,stroke:#3B82F6,color:#1E3A5F;
    classDef task fill:#DCFCE7,stroke:#16A34A,color:#14532D;
    classDef orch fill:#FEF3C7,stroke:#D97706,color:#78350F;
    classDef runtime fill:#EDE9FE,stroke:#7C3AED,color:#3B0764;
    classDef gate fill:#FFE4E6,stroke:#E11D48,color:#881337;
    classDef cap fill:#CCFBF1,stroke:#0F766E,color:#134E4A;
    classDef done fill:#DCFCE7,stroke:#16A34A,color:#14532D;
    classDef pause fill:#FFF7ED,stroke:#EA580C,color:#7C2D12;
```

`Harness.handle()` 可以是渠道侧的全链路单入口，但不等同于模型/动作循环本身：它负责把请求交给各层；单 Agent Run 的多轮决策、裁决、执行和观察发生在 ④–⑥。

### 2.2 每层的唯一职责

| 层 | 负责什么 | 不负责什么 |
|---|---|---|
| ① Agent 定义层 | 当前启用哪个 Agent 产品；其身份、知识策略、Skill 授权、申请能力、默认工作流 | 实际权限裁决或工具实现 |
| ② Task 与 Plan 层 | Task、持久化 TaskPlan、依赖图、验收标准、计划版本与重规划 | 模型临时的思考过程 |
| ③ 编排与协作层 | 调度计划节点、选择单/多 Agent、委派、汇总、暂停与恢复 | 供应商相关的模型调用或直接外部副作用 |
| ④ 单 Agent Runtime 层 | 隔离上下文与上下文工程、模型调用、结构化决策、本地动作提议 | 修改全局计划或提升权限 |
| ⑤ 运行控制与治理层 | 防循环、预算、能力授权、输入/输出 Guardrail、人工 interrupt 与审批 | 业务内容创作或外部 API 细节 |
| ⑥ 能力与交付层 | Skill 实现、工具适配、工件生成、重试分类、幂等副作用 | 决定一个动作是否被允许 |

渠道适配器刻意放在六层之外：它只把输入转成 `IncomingRequest`，不定义业务逻辑。因此同一个 `chat_assistant` 可跨多个渠道运行，`short_drama_producer` 可同时拥有聊天入口和项目工作台。

下面 §3 自 ① 层起，逐层详解每一层的职责、核心对象与边界；横切平面（数据/记忆/观测）单独放在 §4。

## 3. 六层详解

### 3.1 ① Agent 定义层：让产品不同，但不复制 Harness

第 ① 层决定"当前是哪个 Agent 产品在跑"。每个产品是一份版本化的 `AgentDefinition`，被组合进共享运行时，而非拥有独立循环。

```python
@dataclass(frozen=True)
class AgentDefinition:
    id: str
    version: str
    identity: IdentityProfile
    task_types: list[TaskType]
    knowledge_policy: KnowledgePolicy
    skill_grants: set[str]
    capability_grants: set[str]
    workflow_template: WorkflowTemplate
    governance_profile: GovernanceProfile
    presentation_profile: PresentationProfile
```

一份 `AgentDefinition` 由四个核心组件拼装而成——它们回答"这个 Agent 是谁、知道什么、能用什么、怎么干活"：

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryTextColor':'#1E1B4B','lineColor':'#94A3B8','fontFamily':'Inter, sans-serif'}}}%%
flowchart TB
    AD(["AgentDefinition\n版本化 · 组合进共享运行时"]):::root
    AD --> I["身份 Profile<br/>是谁"]:::comp
    AD --> K["知识策略<br/>知道什么"]:::comp
    AD --> S["Skill 授权<br/>能用什么"]:::comp
    AD --> W["工作流模板<br/>怎么干活"]:::comp

    classDef root fill:#EDE9FE,stroke:#7C3AED,color:#3B0764;
    classDef comp fill:#F3E8FF,stroke:#9333EA,color:#581C87;
```

| 组件 | 字段 | 回答 | 内容 | 边界 |
|---|---|---|---|---|
| **身份 Profile** | `identity` | 是谁 | Agent 的角色定位、语气人格、目标边界、面向的任务类型 | 只定义"我是谁"，不含权限 |
| **知识策略** | `knowledge_policy` | 知道什么 | 声明 `conversation`、`memory.user`、`memory.project`、`personal_config` 等可读写域，而非塞一份大 Prompt | 只声明可访问范围，检索由 ④ 层执行 |
| **Skill 授权** | `skill_grants` + `capability_grants` | 能用什么 | 授予哪些 Skill 与底层能力（网络、文件、发送等） | 只**声明**授权，实际裁决在 ⑤ 层 |
| **工作流模板** | `workflow_template` | 怎么干活 | 该产品的默认推进骨架（如"写→审→交付"），作为 ②/③ 层生成 Plan 的起点 | 是模板不是硬编码，运行时可被 `PlanPatch` 调整 |

> 另有 `governance_profile`（治理画像，供 ⑤ 层）与 `presentation_profile`（呈现画像，供渠道层）两个配套字段，分别把治理策略和输出风格也做成可版本化配置。

由此不同 Agent 有真正的产品差异，却共用同一套 Harness：

| Agent 产品 | 主要任务形态 | 典型 Skill | 主要交付 |
|---|---|---|---|
| `chat_assistant` | 短时、回合制 Task | 对话、检索、简单动作 | 回复或简洁结果 |
| `short_drama_producer` | 长周期项目 Plan | 故事 bible、剧本、分镜、生成、连续性审查 | 版本化制作工件 |
| `email_automation` | 周期性运营 Task | 读信、分类、起草、发送 | Effect 与可审计结果 |

**关于 Skill 授权——① 层只声明，⑤ 层才裁决。** 有效能力始终是以下交集：

```text
有效能力 = AgentDefinition 声明的能力
        ∩ 用户 / 租户权限  ∩ 当前 Task scope
        ∩ 环境策略        ∩ 必要的审批结果
```

**关于知识策略——它是策略，不是巨型 Prompt。** `knowledge_policy` 声明 Agent 能读写哪些知识域，按类别分级授权，既避免依赖一份庞大陈旧的 Prompt，也避免一个 Agent 的私有上下文泄漏进其他 Agent 的 WorkOrder：

| 知识类别 | 示例 | 访问规则 |
|---|---|---|
| 共享核心规则 | 安全、输出规范、平台行为 | 所有 Agent 都可使用 |
| Agent 专属知识 | 剧本结构、品牌语气、工具手册 | 由 `knowledge_policy` 选择 |
| Task / 项目证据 | 角色 bible、当前剧本、历史审查意见 | 通过版本化 Artifact 精确引用 |
| Conversation / 用户记忆 | 最近会话记录、稳定偏好 | 分别受 thread 保留期与用户写入策略限制 |
| 项目记忆 / 个人配置 | 项目派生事实、用户明确设置 | 仅在对应 Agent 策略和可信身份允许时读取 |

### 3.2 ② Task 与 Plan 层：持久化领域对象

第 ② 层把工作沉淀成**长期存在的对象**，而非临时的类或 Prompt。它有四项核心职责：

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryTextColor':'#1E1B4B','lineColor':'#94A3B8','fontFamily':'Inter, sans-serif'}}}%%
flowchart TB
    L2(["② Task 与 Plan 层"]):::root
    L2 --> A["Task 目标<br/>结果+约束+验收标准"]:::comp
    L2 --> B["TaskPlan DAG<br/>版本化依赖图"]:::comp
    L2 --> C["验收标准<br/>可判定的完成条件"]:::comp
    L2 --> D["版本与重规划<br/>PlanPatch 校验后版本化"]:::comp

    classDef root fill:#DCFCE7,stroke:#16A34A,color:#14532D;
    classDef comp fill:#F0FDF4,stroke:#22C55E,color:#14532D;
```

入口很短：渠道来的 `IncomingRequest` 经 ① 层路由授权后，由 ② 层据其新建或推进一个 Task，从此进入下面这条持久化对象链——它们才是系统的事实源：

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryTextColor':'#1E1B4B','lineColor':'#94A3B8','fontFamily':'Inter, sans-serif'}}}%%
flowchart LR
    IR(["IncomingRequest\n瞬时 · 六层之外"]):::edge -->|"① 路由授权 · ② 落库"| T
    T["Task\n目标 + 验收标准"]:::core -->|拥有| P["Plan\n版本化依赖图"]:::core
    P -->|节点| R["Run\n一次执行尝试<br/>（由 Agent 执行）"]:::core
    R -->|产出| AF[("Artifact\n产物 / 证据")]:::art
    R -->|提交| EF["Effect\n外部副作用"]:::eff

    classDef edge fill:#E0F2FE,stroke:#0284C7,color:#0C4A6E;
    classDef core fill:#EDE9FE,stroke:#7C3AED,color:#3B0764;
    classDef art fill:#ECFDF5,stroke:#059669,color:#064E3B;
    classDef eff fill:#FFE4E6,stroke:#E11D48,color:#881337;
```

| 对象 | 定义 | 从属关系 |
|---|---|---|
| `Task` | 预期结果、约束、负责人、验收标准 | 由 ② 层据请求创建；一个 Task 可被多次请求跨渠道推进 |
| `Plan` | 推进 Task 的版本化依赖图 | 隶属一个 Task |
| `Run` | 推进某个计划节点的一次尝试 | 隶属一个 Plan 节点，由 Agent 执行 |
| `Artifact` | 可版本化的产物或证据：剧本、调研、图片、草稿 | 由 Run 产出 |
| `Effect` | 改变外部世界的动作：发送、发布、保存、提交 | 由 Run 提交，需幂等边界 |

`Agent` 是接收边界明确 WorkOrder 的执行者，它**执行 Run 但不拥有 Run**，故不列入这条对象链。

**请求是触发器，不是事实。** `IncomingRequest` 不进数据库：① 层只做路由与能力校验、不建对象，② 层据其新建/推进 Task。因此同一个 Task 可以今天在聊天室发起、明天用 CLI 补充——请求是多条临时的，Task 是一条持久的（呼应 §2 把渠道适配器放在六层之外）。

**三种"计划"必须严格分开**，分属不同层、持久化程度不同：

| 计划 | 所属层 | 持久化 | 示例 |
|---|---|---|---|
| `TaskPlan` | ② | 必须持久化、可版本化 | "写剧集 → 审查连续性 → 生成分镜" |
| `ExecutionPlan` | ③ | 必须 checkpoint | "将第 3 场交给编剧，再交给审查者" |
| `ReasoningPlan` | ④ | 短暂存在或压缩摘要 | "先读取角色 bible，再设计冲突" |

Agent 可提出 `PlanPatch`，但不能直接改全局 `TaskPlan`；由 ② 层校验并版本化被接受的变更，让项目始终可解释、可恢复。

### 3.3 ③ 编排与协作层：单 Agent 与多 Agent

第 ③ 层调度计划节点、决定用单还是多 Agent、做委派与汇总。它有四项核心职责：

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryTextColor':'#1E1B4B','lineColor':'#94A3B8','fontFamily':'Inter, sans-serif'}}}%%
flowchart TB
    L3(["③ 编排与协作层"]):::root
    L3 --> A["节点调度<br/>按 DAG 依赖推进"]:::comp
    L3 --> B["单 / 多 Agent<br/>选择协作拓扑"]:::comp
    L3 --> C["WorkOrder<br/>类型化委派"]:::comp
    L3 --> D["汇总 / 恢复<br/>合并结果·暂停续跑"]:::comp

    classDef root fill:#FEF3C7,stroke:#D97706,color:#78350F;
    classDef comp fill:#FFFBEB,stroke:#F59E0B,color:#78350F;
```

多 Agent 不是新运行时、也不是模型群聊，而是 ③ 层基于同一套 Runtime 和类型化交接协议构建的协作拓扑。

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'background':'#FCFCFF','primaryTextColor':'#1E1B4B','lineColor':'#94A3B8',
  'fontFamily':'Inter, ui-sans-serif, system-ui, sans-serif'
}}}%%
flowchart LR
    U["Task + 验收标准"]:::task --> C["协调者\n创建 WorkOrder"]:::coord

    C --> W["执行者\n生成 Artifact"]:::worker
    C --> R["审查者\n校验证据"]:::reviewer
    W --> A[("版本化 Artifact\n剧本 · 调研 · 草稿")]:::artifact
    A --> R
    R -->|Verdict + Evidence| C
    C -->|仅已批准的 Effect| O["操作员\n执行交付"]:::operator
    O --> D["结果 + 审计轨迹"]:::result

    classDef task fill:#E0F2FE,stroke:#0284C7,color:#0C4A6E,stroke-width:1.5px;
    classDef coord fill:#FEF3C7,stroke:#D97706,color:#78350F,stroke-width:1.5px;
    classDef worker fill:#EDE9FE,stroke:#7C3AED,color:#3B0764,stroke-width:1.5px;
    classDef reviewer fill:#FCE7F3,stroke:#DB2777,color:#831843,stroke-width:1.5px;
    classDef artifact fill:#ECFDF5,stroke:#059669,color:#064E3B,stroke-width:1.5px;
    classDef operator fill:#FFE4E6,stroke:#E11D48,color:#881337,stroke-width:1.5px;
    classDef result fill:#F1F5F9,stroke:#64748B,color:#0F172A,stroke-width:1.25px;
```

初始职责集合应保持很小：

| 角色 | 权限边界 |
|---|---|
| Planner | 提出或修订 `TaskPlan`；不能直接产生外部 Effect |
| Coordinator | 生成 WorkOrder、调度和汇总；不能静默篡改全局事实 |
| Worker | 完成一个边界明确的 Task 节点并产出 Artifact；不能改写全局 Plan |
| Reviewer | 依据验收标准返回 Verdict 与证据；不能审批自己创建的产物 |
| Operator | 执行已授权的 Effect；不能决定策略或修改内容 |

Agent 只能通过类型化对象协作：`WorkOrder`、`Artifact`、`ReviewVerdict`、`PlanPatch`、`EffectIntent`；自由形式的对话不是系统契约。

默认单 Agent，仅当满足任一条件时升级为多 Agent：Plan 有可独立执行的节点、创作与审查须独立、不同节点需不同知识/模型/工具/预算、项目长期运行需清晰归属。

### 3.4 ④ 单 Agent Runtime 层：模型调用与上下文工程

第 ④ 层是隔离的单 Agent 执行环境，**不能改全局计划、也不能提升权限**。它有四项核心职责：

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryTextColor':'#1E1B4B','lineColor':'#94A3B8','fontFamily':'Inter, sans-serif'}}}%%
flowchart TB
    L4(["④ 单 Agent Runtime 层"]):::root
    L4 --> A["上下文工程<br/>组装最小 token 集"]:::comp
    L4 --> B["模型调用<br/>供应商无关"]:::comp
    L4 --> C["结构化决策<br/>产出类型化意图"]:::comp
    L4 --> D["动作提议<br/>提议而非执行"]:::comp

    classDef root fill:#EDE9FE,stroke:#7C3AED,color:#3B0764;
    classDef comp fill:#F5F3FF,stroke:#8B5CF6,color:#3B0764;
```

#### Harness 内核：受控 Agent 执行循环

④–⑥ 共同构成一次单 Agent `Run` 的内核；它是运行时允许多轮往返的唯一位置，而不是 ③ 层 DAG 调度的 `while`。每一轮都遵循同一条稳定路径：

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryTextColor':'#1E1B4B','lineColor':'#94A3B8','fontFamily':'Inter, sans-serif'}}}%%
flowchart LR
    R["④ Runtime<br/>最小上下文 + 模型决策"]:::runtime -->|"回复或动作提议"| G{"⑤ 继续 / 授权<br/>Guardrail / 预算"}:::gate
    G -->|"已批准动作"| C["⑥ Capability<br/>执行 Skill / 工具"]:::cap
    C -->|"Observation / Artifact / Effect 结果"| R
    G -->|"终态输出"| DONE["交付结果"]:::done
    G -->|"拒绝 / 暂停 / 预算耗尽"| OUT["③ 编排 + ② checkpoint"]:::pause

    classDef runtime fill:#EDE9FE,stroke:#7C3AED,color:#3B0764;
    classDef gate fill:#FFE4E6,stroke:#E11D48,color:#881337;
    classDef cap fill:#CCFBF1,stroke:#0F766E,color:#134E4A;
    classDef done fill:#DCFCE7,stroke:#16A34A,color:#14532D;
    classDef pause fill:#FFF7ED,stroke:#EA580C,color:#7C2D12;
```

1. ④ 按 Agent 身份、知识策略、Task / WorkOrder 和上一轮 Observation 组装最小上下文，并让模型产出终态回复或**动作提议**。
2. ⑤ 对每次继续和每个动作作确定性裁决：检查预算、无进展与重复，执行 Guardrail 和授权；它可以允许、拒绝、暂停或结束本次 Run。
3. ⑥ 只执行获准的 Skill、工具或交付操作，返回结构化 Observation、Artifact 引用或 Effect 结果；它不自行选择下一步。
4. ④ 消费这些结果后再决策，直到 ⑤ 放行终态输出，或把拒绝、失败、暂停和预算耗尽交回 ③/② 汇总、checkpoint 与恢复。

因此模型既不能直接执行能力，也不能直接修改全局 TaskPlan：前者必须穿过 ⑤→⑥，后者只能通过类型化 `PlanPatch` 回到 ② 层校验。

#### 上下文预算：⑤触发，④压缩

每次模型调用前，⑤必须对**完整请求**计数：system prompt、全部 `RuntimeState.messages` 和当前 tool schema 均在范围内。默认窗口为 1M tokens，固定预留 32k 输出空间，所以输入硬上限是 968k：

```text
< 800k       → 允许④调用模型
800k–968k    → ⑤要求④执行滚动压缩，再次计数
≥ 968k       → 禁止直接调用模型；压缩与强制裁剪后才可重试
```

④的 `RuntimeCompactor` 只折叠本 Run 已完成的历史与 Observation，产出带目标、约束、决策、工具结果/Artifact 引用、未解决项和下一步的 `RunSummary`。它不会改写 Conversation、用户/项目记忆或 Task 事实。当前用户输入、Task/WorkOrder 验收条件、system/Skill、未完成工具事务以及 Artifact/Effect 引用是受保护锚点；若当前输入本身过大，原文仍保留在 `RunRequest`，④按块生成临时输入摘要。压缩事件及其 token 前后值写入 RunJournal；Task Run 同时写 checkpoint。

其中最难的是上下文工程，展开如下。

记忆与会话平面（§4.1）回答"存什么、谁能读"，上下文工程回答"每一步实际喂给模型哪些 token"——这是 ④ 层组装工作记忆的核心职责。上下文是有限资源，token 越多召回越差（context rot），目标始终是**信噪比最高的最小 token 集**。

长周期任务必然超出单次上下文窗口，需要三种可组合策略：

| 策略 | 做法 | 何时用 |
|---|---|---|
| 窗口裁剪 | Conversation 只取最近 8 回合；当前 Run 只保留必要 Observation，超预算时淘汰低优先级内容 | 高频来回的对话式推进 |
| 结构化笔记 | 里程碑写入持久笔记（`NOTES.md`、待办），按需拉回，而非全靠窗口记住 | 达成里程碑、跨 Run 续接 |
| 子 Agent 隔离 | 子 Agent 用干净上下文做子任务，只回传 1~2K token 摘要，而非全部中间过程 | 并行调研、多节点协作 |

按需检索优于预加载：④ 层应通过轻量标识符（Artifact 引用、记忆 scope、文件路径）**渐进式加载**上下文，而非开局就把整个角色 bible 灌进 Prompt——这与 §3.1"知识是策略，不是巨型 Prompt"是同一反模式的两面。

这些策略都不得丢弃**可追溯性**：Task / Plan / Run / Artifact 仍是事实源；长期记忆保留 `source_ref` 回链。Conversation 不做自动摘要或自动晋升。

### 3.5 ⑤ 运行控制与治理层：Agent 自主性外侧的确定性边界

第 ⑤ 层是 Agent 自主性外侧的确定性边界。它不是从 ④ 到 ⑥ 的一次性线性步骤，而是 ④–⑥ 内循环每一轮的控制闸门：首个模型调用前可拦截输入；模型提出动作后决定是否放行；返回结果后决定是否继续、收口或暂停。它有四项核心职责：

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryTextColor':'#1E1B4B','lineColor':'#94A3B8','fontFamily':'Inter, sans-serif'}}}%%
flowchart TB
    L5(["⑤ 运行控制与治理层"]):::root
    L5 --> A["预算 / 防循环<br/>轮数·成本·无进展"]:::comp
    L5 --> B["输入 Guardrail<br/>入口拦截"]:::comp
    L5 --> C["能力授权<br/>允许·拒绝·审批"]:::comp
    L5 --> D["人工 interrupt<br/>暂停·等待·恢复"]:::comp

    classDef root fill:#FFE4E6,stroke:#E11D48,color:#881337;
    classDef comp fill:#FFF1F2,stroke:#F43F5E,color:#881337;
```

这四项归为两个不混淆的关注点——运行安全（能不能继续跑）与治理授权（这个动作允不允许）：

| 关注点 | 示例 | 决策 |
|---|---|---|
| 运行安全 | 最大轮数、执行时间、工具调用预算、上下文 token 预算、无进展阈值、重复动作/结果、委派深度 | 继续、压缩、收口、暂停、取消 |
| 治理与授权 | 身份、能力范围、数据敏感度、收件人/域名规则、审批策略 | 允许、拒绝、要求审批 |

#### Guardrail：入口与出口双向拦截

授权决定"动作允不允许"，Guardrail 决定"这段输入/输出该不该进出流程"。两者互补，分布在三个位置：

| 位置 | 拦什么 | 例子 |
|---|---|---|
| 输入 Guardrail | 进入 Runtime 前的请求 | 提示注入、越权/越范围、有害或跑题输入 |
| 工具 Guardrail | 工具调用前后 | 危险参数、超范围收件人；可拦截、替换、改写 |
| 输出 Guardrail | 交付前的产物 | 敏感信息泄漏、不合规内容、格式违约 |

输入 Guardrail 此前薄弱：应在**首个 Agent 开跑前**拦截，避免为本该拒绝的请求浪费算力；高成本/有副作用的流程用**阻塞式**、普通流程可**并行**跑、触发即中止。命中须产生可审计的拒绝理由，而非静默丢弃。

#### 人工介入（interrupt）：审批只是它的特例

不要把"审批"做成孤立的 UI 特例。它是一个通用原语的实例——**暂停 → 持久化 → 等待外部决定 → 恢复**。整个过程复用 §4.2/§4.3 的同一套 checkpoint 机制，因此可跨进程重启、可等数天、可审计：

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryTextColor':'#1E1B4B','lineColor':'#94A3B8','fontFamily':'Inter, sans-serif'}}}%%
flowchart LR
    RUN["Run 运行中"]:::run --> HIT{"遇到需人工<br/>决定的点?"}:::gate
    HIT -->|否| RUN
    HIT -->|是| P["暂停 + 存 checkpoint<br/>（完整状态持久化）"]:::pause
    P --> W["等待外部决定<br/>可跨重启 / 数天"]:::wait
    W --> D["收到决定"]:::dec
    D --> R["rehydrate 从暂停点续跑"]:::run

    classDef run fill:#EDE9FE,stroke:#7C3AED,color:#3B0764;
    classDef gate fill:#FEF3C7,stroke:#D97706,color:#78350F;
    classDef pause fill:#FFE4E6,stroke:#E11D48,color:#881337;
    classDef wait fill:#FFF7ED,stroke:#EA580C,color:#7C2D12;
    classDef dec fill:#DCFCE7,stroke:#16A34A,color:#14532D;
```

**触发时机不止审批**——任何需要"外部拿主意"的点都可 interrupt：

| 决定类型 | 场景 | 恢复后 |
|---|---|---|
| 批准 / 拒绝 | 高风险 Effect（发邮件、发布、删数据） | 批准则提交 Effect，拒绝则收口 |
| 二选一 / 多选一 | 有多个候选方案需人拍板 | 按所选分支继续 |
| 补充输入 | 缺关键参数或素材 | 带着补入的信息续跑 |
| 澄清歧义 | 事实/需求有歧义，猜错代价大 | 按澄清结果继续 |

所以"审批高风险 Effect"只是 interrupt 最常见的形态。统一到这一原语后，长周期项目里的"等用户拍板"才能像审批一样被持久化、恢复、审计——而不是把 Run 卡死或丢弃。

#### 防死循环需要多道闸门

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'background':'#FCFCFF','primaryTextColor':'#1E1B4B','lineColor':'#94A3B8',
  'fontFamily':'Inter, ui-sans-serif, system-ui, sans-serif'
}}}%%
flowchart TB
    P["TaskPlan 防线\n无依赖环 · 每个节点有出口"]:::plan
    O["编排防线\n委派深度 · 子任务预算 · 终态"]:::orch
    R["Runtime 防线\n重复动作/结果 · 无进展 · 轮数上限"]:::run
    E["执行防线\n超时 · 重试预算 · 幂等 key"]:::exec
    F["安全收口\n摘要 · checkpoint · 必要时请求用户决定"]:::finish

    P --> O --> R --> E
    P -. 违规 .-> F
    O -. 违规 .-> F
    R -. 违规 .-> F
    E -. 耗尽 .-> F

    classDef plan fill:#DCFCE7,stroke:#16A34A,color:#14532D,stroke-width:1.5px;
    classDef orch fill:#FEF3C7,stroke:#D97706,color:#78350F,stroke-width:1.5px;
    classDef run fill:#EDE9FE,stroke:#7C3AED,color:#3B0764,stroke-width:1.5px;
    classDef exec fill:#CCFBF1,stroke:#0F766E,color:#134E4A,stroke-width:1.5px;
    classDef finish fill:#FFE4E6,stroke:#E11D48,color:#881337,stroke-width:1.5px;
```

现有 `RunPolicy` 的概念（轮数、工具调用、墙钟时间上限，重复动作/结果，无进展阈值）都属于这一层，且必须随 Run 持久化，不能只存在于模型循环的局部变量里。

### 3.6 ⑥ 能力与交付层：Skill、工具与副作用

第 ⑥ 层实现具体能力，**只执行，不决定动作是否被允许**（那是 ⑤ 层的事）。每次执行都必须把结构化 Observation、Artifact 引用或 Effect 结果交回 ④，作为下一轮上下文的证据；它有四项核心职责：

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryTextColor':'#1E1B4B','lineColor':'#94A3B8','fontFamily':'Inter, sans-serif'}}}%%
flowchart TB
    L6(["⑥ 能力与交付层"]):::root
    L6 --> A["Skill 实现<br/>可复用工作单元"]:::comp
    L6 --> B["工具适配器<br/>MCP 等统一接入"]:::comp
    L6 --> C["Artifact 生成<br/>版本化产物"]:::comp
    L6 --> D["幂等 Effect<br/>outbox + 幂等 key"]:::comp

    classDef root fill:#CCFBF1,stroke:#0F766E,color:#134E4A;
    classDef comp fill:#F0FDFA,stroke:#14B8A6,color:#134E4A;
```

Skill 是可复用、可版本化的工作单元：① 层授权，⑥ 层实现。

```python
@dataclass(frozen=True)
class SkillManifest:
    id: str
    version: str
    input_schema: dict
    output_schema: dict
    required_capabilities: set[str]
    readable_artifact_types: set[str]
    produced_artifact_types: set[str]
    acceptance_checks: list[str]
    risk_level: str
```

示例：

- `write_episode_script` 消费故事 bible，产出版本化剧本 Artifact；
- `review_continuity` 消费剧本和角色事实，产出带证据的结构化 Verdict；
- `send_email` 消费已批准草稿，创建一个幂等的外部 Effect。

Skill 永远不能绕过控制层：即使被授予 `send_email`，在具体 Run 中仍可能被 ⑤ 层拒绝、限流或转入审批。

外部工具与数据源统一走 **MCP（Model Context Protocol）** 接入，作为跨 Agent 复用的标准通道，避免为每个工具写一次性胶水。但 MCP 只是 ⑥ 层的一种能力适配器：接入的工具同样要有 `SkillManifest`、声明所需能力、并受 ⑤ 层授权与 Guardrail 约束——MCP 扩大了工具来源，不放宽任何控制边界。

外部 Effect 使用 outbox 和稳定的 idempotency key。重试或恢复时，绝不能重复发送邮件或重复发布同一 Artifact（恢复语义见 §4.3）。

## 4. 横切平面：状态、证据、记忆、观测与评测

横切平面不属于任何单层，而是**六层每一层都会读写**的公共底座。三大平面各司其职：

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryTextColor':'#1E1B4B','lineColor':'#94A3B8','fontFamily':'Inter, sans-serif'}}}%%
flowchart LR
    L(["① ~ ⑥ 六层<br/>每层都读写"]):::layers
    L -.-> S
    L -.-> M
    L -.-> V

    subgraph PLANE[三大横切平面]
      direction TB
      S["💾 数据与证据<br/>Task·Plan·Run·Artifact<br/>Effect·checkpoint"]:::data
      M["🧠 上下文与记忆<br/>Conversation·用户·项目·个人配置"]:::mem
      V["📊 观测与评测<br/>trace·审计·成本<br/>回放·场景评测·门禁"]:::obs
    end

    classDef layers fill:#EDE9FE,stroke:#7C3AED,color:#3B0764;
    classDef data fill:#EFF6FF,stroke:#3B82F6,color:#1E3A5F;
    classDef mem fill:#FFF7ED,stroke:#EA580C,color:#7C2D12;
    classDef obs fill:#F1F5F9,stroke:#64748B,color:#0F172A;
    style PLANE fill:#FAFAF9,stroke:#D6D3D1;
```

| 平面 | 保存什么 | 为什么需要 |
|---|---|---|
| 💾 数据与证据 | Task、Plan、Run、WorkOrder、Artifact、Effect、checkpoint | 恢复、溯源、审批、可复现 |
| 🧠 上下文与记忆 | Conversation、用户记忆、项目记忆、个人配置及其访问策略 | 个性化、连续性、可控复用与知识隔离 |
| 📊 观测与评测 | 结构化事件、trace、模型/工具版本、token/cost、场景集、回放报告 | 排障、审计、安全升级模型/Prompt/Skill |

三者是有层次的：**数据与证据是原始事实，记忆是其派生，观测记录整个过程。**

观测与评测不能只是"事后能查"，必须**闭环、连回版本门禁**：模型、Prompt、Skill、策略的升级，都先在固定场景集上跑可回放评测，结果作为发布 gate。

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryTextColor':'#1E1B4B','lineColor':'#94A3B8','fontFamily':'Inter, sans-serif'}}}%%
flowchart LR
    T["线上 trace<br/>+ 失败案例"]:::obs --> SET["沉淀为场景集"]:::obs
    SET --> EV{"候选版本<br/>回放评测"}:::gate
    EV -->|指标达标| PASS["放行 → 更新<br/>AgentDefinition / SkillManifest"]:::pass
    EV -->|指标回退| BLOCK["阻断该版本"]:::block

    classDef obs fill:#F1F5F9,stroke:#64748B,color:#0F172A;
    classDef gate fill:#FEF3C7,stroke:#D97706,color:#78350F;
    classDef pass fill:#DCFCE7,stroke:#16A34A,color:#14532D;
    classDef block fill:#FFE4E6,stroke:#E11D48,color:#881337;
```

指标至少覆盖：是否达成验收标准、工具调用次数与错误率、token/成本、延迟；判定用确定性断言或 LLM-as-judge。这是 §6"升级前必须通过可回放场景评测"的落地机制。

`Artifact` 是可追溯的原始事实和产物；`Memory` 是从中提炼、可检索可纠正的派生认知。记忆不能替代 Artifact 作事实源，须保留 `source_ref` 回链。

### 4.1 上下文、会话与记忆

不要把聊天记录、临时状态和长期记忆混成一个库。下图按 Runtime 从外到内缩小上下文范围的顺序表达五层；图中的包含关系表示**本次 Run 可见范围**，不是文件目录或数据所有权关系：

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryTextColor':'#1E1B4B','lineColor':'#94A3B8','fontFamily':'Inter, sans-serif'}}}%%
flowchart TB
    subgraph CONFIG["个人配置（可选）"]
        subgraph PROJECT["项目记忆（可选）"]
            subgraph USER["用户记忆（可选）"]
                subgraph CONVERSATION["Conversation（可选）"]
                    RUN["工作记忆（必有）<br/>RuntimeState"]:::run
                end
            end
        end
    end

    POLICY["Agent knowledge_policy<br/>+ 可信请求身份"]:::gate
    POLICY -.-> CONFIG

    classDef run fill:#EDE9FE,stroke:#7C3AED,color:#3B0764;
    classDef gate fill:#FEF3C7,stroke:#D97706,color:#78350F;
    style CONFIG fill:#F0FDF4,stroke:#16A34A,color:#14532D;
    style PROJECT fill:#FFF7ED,stroke:#EA580C,color:#7C2D12;
    style USER fill:#FFEDD5,stroke:#F97316,color:#7C2D12;
    style CONVERSATION fill:#E0F2FE,stroke:#0284C7,color:#0C4A6E;
```

个人配置由用户或宿主明确设置，模型不能自动改写；项目记忆仅 project Agent 且有 `project_id` 时可用；用户记忆保存跨会话稳定偏好；Conversation 只取当前 thread 最近 8 回合；工作记忆保存当前 Run 的 Observation 和临时状态。CLI `chat` Agent 展开个人配置 → 用户记忆 → Conversation → 工作记忆，跳过项目记忆层。

这些不是每个渠道都有的固定五层。渠道提供可信 `tenant_id / user_id / thread_id`，可选 `project_id`；**Agent 的 `knowledge_policy` 决定该 Agent 对本次渠道可读写哪些域。** 例如 CLI 的 `chat` Agent 只读 `conversation`、`memory.user`、`personal_config`，不声明也不读取 `memory.project`。没有 project 身份或未获授权时，项目记忆不会进入上下文。目录结构、保留期与 token 预算统一见本节后面的“本地 Harness 存储约定”。

长期记忆的共同契约如下；首期 scope 仅为 `user` 与 `project`：

```python
@dataclass(frozen=True)
class MemoryRecord:
    scope: str          # user / project
    kind: str           # preference / fact / decision / summary
    content: dict
    source_ref: str     # conversation、TaskPlan、Artifact、Run 等来源
    confidence: float
    sensitivity: str
    ttl: str | None
    version: int
    write_policy: str
```

读写边界：①层声明 `knowledge_policy`；④层只按授权检索最小上下文；⑤层检查租户、scope、来源与写入策略；⑥层由模型提出记忆更新。`MemoryProposal` 只存在于当前 Run 内存，`MemoryService` 校验后按语义键覆盖 Markdown 的当前值。个人配置没有模型写入 Skill。

影响项目事实的记忆不能直接落库，须经校验才能写入对应的当前记忆文档；提案不会成为文件，用户纠正直接覆盖相同语义键：

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryTextColor':'#1E1B4B','lineColor':'#94A3B8','fontFamily':'Inter, sans-serif'}}}%%
flowchart LR
    C["内存提案<br/>（⑥ 层 Skill 产出）"]:::cand --> G{"权限 / 来源 / scope 校验"}:::gate
    G -->|通过| L["Markdown 当前值<br/>按 semantic_key 覆盖"]:::long
    G -->|拒绝| V["仅返回 Observation<br/>不写任何文件"]:::ver

    classDef cand fill:#FFF7ED,stroke:#F59E0B,color:#7C2D12;
    classDef gate fill:#FEF3C7,stroke:#D97706,color:#78350F;
    classDef long fill:#DCFCE7,stroke:#16A34A,color:#14532D;
    classDef ver fill:#F1F5F9,stroke:#64748B,color:#0F172A;
```

**校验通过≠追加文件。** 同一个 `scope + owner + semantic_key` 永远只对应 Markdown 中一行当前值；模型判断需要更新时直接覆盖该行。会话记录保留原始对话，Task / Plan / Run / Artifact 保留任务事实，因此用户/项目记忆不再复制候选或版本历史。这与 §3.4 的上下文压缩同源，但对象不同：

| | §3.4 上下文压缩 | §4.1 记忆落库压缩 |
|---|---|---|
| 压缩对象 | 喂给模型的 token 窗口 | 写入记忆库的长期记忆 |
| 目的 | 单次运行的信噪比 | 每个语义键只有一个当前值 |
| 触发 | 近上下文上限时 | 每次成功记忆更新时 |

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryTextColor':'#1E1B4B','lineColor':'#94A3B8','fontFamily':'Inter, sans-serif'}}}%%
flowchart LR
    N["通过校验的内存提案"]:::cand --> Q{"同 scope + owner + key<br/>已有当前值?"}:::gate
    Q -->|无| W["新增 Markdown 行"]:::long
    Q -->|有| C["覆盖同一 Markdown 行"]:::comp
    C --> W

    classDef cand fill:#DCFCE7,stroke:#16A34A,color:#14532D;
    classDef gate fill:#FEF3C7,stroke:#D97706,color:#78350F;
    classDef comp fill:#FFF7ED,stroke:#EA580C,color:#7C2D12;
    classDef long fill:#EDE9FE,stroke:#7C3AED,color:#3B0764;
```

可追溯性不由记忆文件承担：对话从 Conversation JSONL 查询，任务事实从 Task / Plan / Run / Artifact / Checkpoint 查询。

#### 本地 Harness 存储约定

新 Harness 的 Conversation、长期记忆与个人配置都收口在 `harness/memory/` 或 `harness/config/`；旧 `engine/memory.py`、Loop / Goal memory 和 CLI 专属 `conversation_records` 不属于该路径：

```text
harness/memory/memory_store/
  conversations/tenants/<tenant>/users/<user>/<YYYY-MM-DD>_<part>.jsonl
  user/<tenant>/<user>.md
  project/<tenant>/<project>.md

harness/config/personal_store/
  tenants/<tenant>/users/<user>.json
```

`ConversationRecord` 是不可变的短期会话记录，属于 `harness/memory/`：按租户、用户和日期分区、每条一行写入 append-only JSONL；每行以 `created_at` 开头，便于直接排查，`thread_id` 是记录字段，只在读取当前会话时过滤，**不参与目录结构**。单日段文件达到 8 MiB 后滚动新 part。记录还保存可查看的 `decision_trace`（模型动作提议、治理后执行结果与终态），供未来 CLI 检视；它不保存或展示模型私有思维链。④ 层只取当前 thread 最近 8 回合并限制在 2,400 tokens；它不会自动晋升为长期记忆。普通会话记录默认保留 30 天。`CandidateMemory` 只在当前 Run 内存中存在，成功或拒绝后立即丢弃。

用户记忆在 `user/<tenant>/<user>.md`，项目记忆在 `project/<tenant>/<project>.md`：每份都是该 owner 的**唯一当前记忆文件**。Markdown 表格的每一行是 `key / kind / value`，读取时可直接编辑或新增；相同 key 的更新会覆盖原行。**是否需要记忆、保存什么语义键以及 `sensitivity` 分类均由 Agent 模型依据身份、当前表达和 Conversation 判断**；例如 chat Agent 通常会把清晰的姓名自我介绍视为稳定事实。Runtime 不按“我是/我叫”等句式创建记忆。代码不以 `low / normal / high` 或文本模式决定保存与否，只校验分类枚举、来源、Agent 授权、scope 和写入策略；它不能把推测自动写成用户记忆。

上下文压缩与长期记忆更新必须分离：④ 层按 token 预算选择本次 Run 的最小上下文；记忆服务只按 `scope + owner + semantic_key` 更新当前 Markdown 行。会话记录只做窗口读取，不再额外维护 thread 长期记忆或自动摘要。

### 4.2 checkpoint 边界

checkpoint 是正在跑的 Run 的**存档点**：在关键节点把状态（当前计划节点、工作记忆摘要、已产出 Artifact 引用）持久化进数据与证据平面。它是"可恢复、可审计"承诺的物理基础，支撑三件事：

- **恢复**：崩溃/重启后从最近 checkpoint 续跑，跳过已完成步骤，不从头重来（见 §4.3）；
- **人工介入**：停下等审批时把状态存成 checkpoint，人回来后从该点 rehydrate 继续（见 §3.5）；
- **审计/复现**：每个 checkpoint 是可回溯快照，能还原"某一步当时的状态"。

存档不能太稀（否则一断丢很多进度），也不必每行都存。**最小 checkpoint 边界**是保证任意中断都能从不太远处续上的必存时机：

| # | 触发时机 | 存档意义 |
|---|---|---|
| 1 | `TaskPlan` / `ExecutionPlan` 变化 | 计划演进可回溯、可回滚 |
| 2 | 发出一个 WorkOrder | 委派了什么、给了谁有记录 |
| 3 | 记录一个模型决策 | 恢复时不重复推理 |
| 4 | 提交一个工具结果或 Artifact | 已完成的产出不重跑 |
| 5 | Run 进入审批 / 暂停 / 终态 | 中间态可持久等待、可恢复 |
| 6 | 提交一个 Effect | 配合幂等 key，恢复时已发的不重发 |

### 4.3 恢复与重放

checkpoint 只解决"存"，恢复要解决"崩溃后怎么续"。核心原则：**加载最近 checkpoint，逐步续跑；每一步按"能否安全重放"分流处理，绝不重复已完成的副作用。**

```mermaid
%%{init: {'theme':'base','themeVariables':{'primaryTextColor':'#1E1B4B','lineColor':'#94A3B8','fontFamily':'Inter, sans-serif'}}}%%
flowchart TB
    X["Run 中断<br/>（崩溃 / 重启 / 换机器）"]:::bad --> LD["加载最近 checkpoint<br/>还原节点·工作记忆·Artifact 引用"]:::load
    LD --> STEP{"下一个未完成步骤<br/>是什么类型?"}:::gate
    STEP -->|内部状态| RE["确定性重放<br/>计划推进·决策记录"]:::in
    STEP -->|外部 Effect| ID{"幂等 key<br/>已提交?"}:::gate
    ID -->|是| SKIP["跳过，不重发"]:::skip
    ID -->|否| SEND["提交并记 outbox"]:::eff
    STEP -->|等待人工| WAIT["保持暂停<br/>收到决定后 rehydrate 续跑"]:::wait
    RE --> STEP
    SEND --> STEP

    classDef bad fill:#FFE4E6,stroke:#E11D48,color:#881337;
    classDef load fill:#EDE9FE,stroke:#7C3AED,color:#3B0764;
    classDef gate fill:#FEF3C7,stroke:#D97706,color:#78350F;
    classDef in fill:#F0FDF4,stroke:#22C55E,color:#14532D;
    classDef eff fill:#EFF6FF,stroke:#3B82F6,color:#1E3A5F;
    classDef skip fill:#F1F5F9,stroke:#64748B,color:#0F172A;
    classDef wait fill:#FFF7ED,stroke:#EA580C,color:#7C2D12;
```

分三种情况处理，取决于该步骤**能否安全重放**：

| 步骤类型 | 能否重放 | 恢复方式 |
|---|---|---|
| 内部状态（计划、决策、工作记忆） | 能 | 从 checkpoint 确定性重放，结果与首次一致 |
| 外部 Effect（发送、发布、提交） | 不能 | 靠 outbox + 幂等 key 去重，已提交则跳过、绝不重发 |
| 等待人工（审批、interrupt） | —— | 保持暂停的中间态，收到决定后 rehydrate 续跑（见 §3.5）|

**为什么内部状态能"确定性重放"？** 因为编排与决策逻辑不含随机副作用——同样的输入（checkpoint 里的状态）重跑得到同样的结果。真正不可重放的是"改变外部世界"的 Effect，所以架构把两者严格分开：内部状态尽管重放，外部 Effect 一律走幂等边界。这与 §6"外部 Effect 没有幂等边界不得重试"互为表里——**幂等边界既服务重试，也服务恢复。**

## 5. 本仓库的实现方向

现有代码是迁移参考，而不是目标架构本身。新 Harness 的核心必须在 `harness/` 内拥有 ④–⑥ 的受控执行循环；旧 `engine` 只在迁移期提供行为参照与兼容支撑：

| 现有组件 | 在目标架构中的位置 |
|---|---|
| `engine/harness.py::HarnessRunner` | 旧的模型→工具→结果内循环；待拆入新 Harness 的 ④ Runtime、⑤ 控制闸门与 ⑥ 执行端口 |
| `HarnessContextManager`、`RunPolicy` | ④ 层上下文工程与 ⑤ 层运行安全的遗留参考；`RunPolicy` 必须随 Run 持久化 |
| `ChatHarness` | 旧聊天适配；目标是由 ① 的 `chat` Agent 包与共享 Harness 承担产品差异 |
| `BaseLoop`、`LoopEngine`、`scheduler.py` | 独立的旧外部触发/推进机制，不是新 Harness 的单 Agent 执行内循环；后续单独迁移或删除 |
| `effects.py` | ⑥ 层的第一版 Effect / outbox 实现 |
| `harness/memory/` | 已实现会话 JSONL、用户/项目单 Markdown 当前记忆，以及仅运行期存在的记忆提案 |
| `harness/memory/conversation/` | 已实现按租户、用户、日期分区的短期会话记录；thread 只用于读取过滤，不作为目录 |
| `harness/config/` | 已实现用户或宿主维护的个人配置；模型无自动写入权 |
| 旧 `engine/tools/*` | 已迁移或待迁移到 Skill Registry 下方的能力适配器 |

迁移顺序是：先稳定持久化契约 `AgentDefinition`、`Task`、`Plan`、`Run`、`Artifact`、`Effect`、`WorkOrder`；再让新 Harness 在 ④–⑥ 中承载受控执行循环；最后才在同一套契约上扩展多 Agent 协作。外部 Loop 的替换不属于当前 Harness 内核工作。

## 6. 不可妥协的规则

- Agent 不得绕过注册能力和控制闸门，直接拥有 shell、网络或发布权限。
- Worker 不能直接改全局 Plan，只能带证据提出 `PlanPatch`。
- Reviewer 不能审批自己创建的 Artifact。
- 外部 Effect 没有幂等边界不得重试。
- 审批不是 UI 特例，而是可持久化的 Run 状态。
- 模型、Prompt、Skill 或策略升级前，必须通过可回放的场景评测。
