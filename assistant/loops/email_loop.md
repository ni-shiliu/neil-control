# email_loop

## 目标

- 处理/回复邮件：拉取 163 邮箱未读邮件，Claude 分析生成回复，自动发送或存草稿

## 触发方式

- 支持模式：cron, goal, event

## 工具依赖

- imap, smtp, claude, telegram

## 输入输出

- 输入：goal 配置、ctx.memory、ctx.goal_memory、ctx.recent_runs
- 输出：结构化 result，可选 `result.outputs`

## 长期规则

- 在这里记录这个 loop 的长期稳定策略
- 只写人工确认过的规则，不写一次性运行结果

## Memory 边界

- loop memory：跨 goal 共用的稳定经验
- goal memory：某个 goal 的短期状态
- recent runs：最近运行轨迹，只做辅助上下文

## 注意事项

- 副作用通过 effect 提交
- 不要把高频临时数据写进 Markdown
