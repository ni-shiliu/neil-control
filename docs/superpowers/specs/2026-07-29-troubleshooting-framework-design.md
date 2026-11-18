# 通用可视化问题排查框架 — 设计文档

> 状态:**主体设计已定稿**,待用户复审 → 转 writing-plans。细节待细化项见 §6。
>
> 日期:2026-07-29 ·  归属项目:`neil-control`(Claude Code skill 插件仓库)

---

## 1. 目标(一句话)

**Web Investigator** —— 一个**通用的、可开源的、基于 Chrome 的可视化问题排查框架**,以 Claude Code skill(`web-investigator`)形式提供:人输入一条自由文本线索,框架驱动浏览器操作若干个"待排查网页系统",交叉验证信息后产出「排查 + 修复建议」报告,**不自动执行修复**。

## 2. 已锁定的设计决策

| # | 决策项 | 结论 |
|---|--------|------|
| D1 | 是否绑定公司 | **不绑定**。核心引擎不认识任何具体系统名;六翼/绿洲只是用户的私有配置实例。 |
| D2 | 浏览器驱动方式 | **chrome-devtools MCP**(snapshot → click/fill 循环)。连本机已登录的 Chrome,复用登录态。这是框架唯一硬前提。 |
| D3 | 平台可配置性 | **动态可配置,数量不限**。不是固定"日志+数据库两个角色",用户可配 N 个待排查系统。 |
| D4 | 平台能力分类 | 每个平台标注 `capability`:`logs` / `trace` / `metrics` / `database` / 其他。引擎靠 capability 编排。 |
| D5 | 配置模型 | **C 方案**:声明式配置(YAML/JSON)为主 + 自定义脚本逃生舱(声明式表达不了的复杂页面交互挂脚本)。第一版先把声明式跑通,逃生舱预留接口。 |
| D6 | 排查编排 | **证据驱动自适应**(非固定管线)。`logs` 为默认起点(信息最丰富、最可能一步定位)。**每查一步 → 评估已有证据是否足以定位根因**:够了就收尾出报告(哪怕只查了日志);不够则选**最能补当前证据缺口**的下一个平台。固定序 `logs→trace→metrics→database` 仅作"信息价值默认倾向"的提示,不是强制顺序,可提前终止。 |
| D7 | traceId 场景顺序 | 带 traceId 时**倾向**:先日志(看报错详情、定位服务与时间点)→ 若日志未定位到根因,再顺 traceId 查链路(找失败/超时节点)→ 按需 metrics/database 验证。此为倾向而非强制:若日志已定位到具体代码行/明确根因,直接收尾。 |
| D8 | 输入 | 自由文本线索:问题描述 / traceId / 报错 / 订单号 …… 皆可。第一步是"读懂线索 → 抽取关键标识"。 |
| D9 | 输出 | 「排查 + 修复建议」报告(现象 / 根因定位 / 证据链 / 修复建议)。**只出建议,不自动改代码、不自动执行修复。** |
| D10 | 数据库平台安全 | **只读硬约束**:database 类平台只允许 SELECT;任何写操作(UPDATE/DELETE/DDL)一律停下、报告人。 |
| D11 | 绿洲/数据库查库触发时机 | **按需触发**,非每次都查。仅当日志/链路指向"数据可能不对"时才下钻到 database 平台做只读验证。 |
| D12 | 实现方案 | **方案 C:声明式配置 + 薄编排脚本**。平台配置用声明式 YAML;执行器与只读拦截用确定性脚本(硬拦截,非 prompt 祈求);SKILL.md 负责线索理解、交叉验证、报告生成等需要智能的环节。确定性走脚本(快/稳/省 token),需智能处走模型。 |
| D13 | 平台间联动方式 | **靠引擎持有的"共享标识"在多个独立平台间流转**,平台之间零页面耦合。每个平台只管"给我标识 → 返回信息"。是否查某平台(含 trace)由引擎按需决策;若都查,则各查各的独立页面。 |
| D14 | 查询步骤描述形态 | **粗粒度意图声明为主 + 逐步逃生舱**。默认用自然语言 `intent` 声明"搜什么/填哪/读什么",覆盖多数平台;某平台确需细控时,挂 `script` 逃生舱脚本。第一版先跑通粗粒度。 |

## 3. 架构分层

```
┌──────────────────────────────────────────────┐
│  排查引擎(通用开源核心)                          │
│  · 读线索 → 抽标识                                │
│  · 证据驱动自适应编排:logs 起点,每步评估证据是否   │
│    足够 → 够则收尾 / 不够则选最补缺口的下一平台      │
│  · 交叉验证逻辑                                   │
│  · 报告生成(排查+修复建议)                        │
│  · 安全约束执行(database 只读 / 不自动修复)       │
│  —— 不含任何具体平台名                            │
└──────────────────────────────────────────────┘
                    │ 读取
                    ▼
┌──────────────────────────────────────────────┐
│  平台注册表(用户配置,N 个,可插拔)               │
│  每个平台(一 YAML 一 capability):               │
│    name / capability / url / login_check /       │
│    query(intent+inputs) / extract(yields) /      │
│    (可选) script 逃生舱                           │
│                                                │
│  用户私有实例(不进开源仓库):                     │
│    六翼-日志 → logs   六翼-链路 → trace          │
│    绿洲      → database (read-only)              │
└──────────────────────────────────────────────┘
```

**开源边界**:核心引擎 + 配置 schema + 示例配置进开源仓库;六翼/绿洲等真实内网平台配置作为私有配置,不进仓库(天然隔离敏感的内网 URL 与页面结构)。

## 4. 排查主流程(骨架)

主流程是**证据驱动的循环**(D6),不是固定管线:

```
自由文本线索
  → ① 读懂线索,抽取关键标识(traceId / 订单号 / 时间段 / 报错关键词 / 服务名 / 用户ID)
  → ② logs 平台(默认起点):搜日志,找报错堆栈与上下文,定位服务与时间点
  → ③ 【评估门】已有证据是否足以定位根因?
        ├─ 够(如日志已指到具体代码行/明确根因) → 直接跳 ⑤ 出报告
        └─ 不够 → 选最能补当前缺口的平台继续:
              · 需定位全链路失败/超时节点 → trace(顺 traceId)
              · 需确认影响面/量级          → metrics
              · 怀疑数据不对              → database(只读验证,SELECT ONLY)
           查完回到 ③ 再评估(循环,直到够了或无更多平台可查)
  → ⑤ 交叉验证 + 出报告(现象 / 根因 / 证据链 / 修复建议)
```

## 5. 平台配置 schema(草案,字段实现时细化)

每个平台一个 YAML 文件,放**私有配置目录**(不进开源仓库)。核心思路:平台只声明"认哪些标识 → 返回哪些信息",引擎靠 `inputs`/`yields` 做标识流转编排(D13);查询用粗粒度 `intent` 声明(D14);database 用 `readonly` 把只读约束落到执行器脚本层(D10)。

```yaml
name: 六翼-日志                    # 人类可读名,报告里引用
capability: logs                  # logs | trace | metrics | database | other
url: https://liuyi.internal/logs  # 平台入口

login_check:
  logged_in_when: "页面出现搜索框"   # 粗粒度自然语言判据;未登录走 T6 请人接管

query:
  intent: "在搜索框输入 {{trace_id}},搜索,打开最相关的报错日志条目"
  inputs: [trace_id]              # 认哪些标识(引擎据此决定能否调用它)

extract:
  intent: "提取报错堆栈、发生时间、涉及服务名;若日志含 traceId 一并提取"
  yields: [error_stack, timestamp, service_name, trace_id]  # 产出的标识/信息

script: null                      # 逃生舱(D14):有值则执行器改跑脚本,不走 intent
```

database 平台额外带只读硬约束(D10):

```yaml
name: 绿洲
capability: database
url: https://oasis.internal
query:
  intent: "选中库 {{db}},在 SQL 框执行 {{sql}}"
  inputs: [db, sql]
  readonly: true                  # 执行器 fill SQL 前先解析,非 SELECT 直接拒绝并报告
extract:
  intent: "读取结果表格的行"
  yields: [rows]
```

**开源边界**:核心引擎 + 配置 schema + 示例(脱敏)配置进开源仓库;六翼/绿洲等真实内网配置作为私有配置,不进仓库。

## 6. 待细化项(实现阶段处理,不阻塞定稿)

主体设计已定稿;以下是实现时才需要坐实的细节,不影响架构:

- [ ] **T1 schema 细节字段**:是否需 `time_range` 等通用查询参数显式声明;登录判据是否要 CSS 选择器备选。(主体已定,见 §5)
- [x] **T2 六翼日志↔链路跳转**:已定 —— 靠共享标识(traceId)在两个独立页面间流转,是否查 trace 由引擎按需决策(D13)。
- [ ] **T3 交叉验证逻辑**:日志说法与数据库状态"不一致"如何判定与呈现。(SKILL.md 层的模型职责,实现时写 prompt)
- [ ] **T4 报告模板结构**:markdown,含现象 / 根因 / 证据链 / 修复建议四段(D9)。确切格式实现时定。
- [ ] **T5 逃生舱脚本形式**:独立脚本文件(`script:` 指向),形态实现时定。
- [ ] **T6 登录态检测与请人接管**:执行器检测 `logged_in_when` 不满足时停下、报告人接管。细节实现时定。
- [ ] **T7 只读硬约束技术实现**:执行器脚本层解析 SQL(SELECT 白名单),非 SELECT 拒绝。实现时定解析方式。
- [x] **T8 skill 落位**:目录结构已定,见 §7;仅 skill 命名待实现时敲定。

## 7. skill 落位(T8)

```
skills/web-investigator/
  SKILL.md              # 高层编排:读线索→抽标识→按 capability 编排→交叉验证→报告
  references/
    config-schema.md    # 平台配置 schema 说明 + 示例(脱敏)
  scripts/
    executor.*          # 确定性执行器:驱动 chrome-devtools MCP、只读拦截、标识流转
  examples/
    logs-platform.yaml  # 脱敏示例配置
    database-platform.yaml
```

**命名(已定)**:skill 名 `web-investigator`,框架名 **Web Investigator**(网页排查调查框架)。取"investigator"点出核心特征——顺一条线索、跨多个网页系统串证据、交叉验证、定位根因;不绑公司、不绑手段(不写死 chrome),与 `browser-use`(操作浏览器)区分清晰。

## 8. 相关参照(同机已有)

- `~/.claude/skills/browser-use/SKILL.md` — 浏览器操作 skill 的写法参照。
- `~/.claude/skills/yeepay-payment-integration/` — 结构成熟的公司级 skill 范式(SKILL.md + references/ + scripts/),可借骨架。
- chrome-devtools MCP 工具集:`navigate_page` / `take_snapshot` / `click` / `fill` / `fill_form` / `wait_for` / `evaluate_script` 等。

## 9. 下一步

1. 用户复审本 spec。
2. 走 `superpowers:writing-plans` 产出实现计划。
