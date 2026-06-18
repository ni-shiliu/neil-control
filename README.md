# neil-control

一个面向 macOS 用户的工具集，包含两个相对独立的部分：

- **Claude Code Skills** — 让 Claude Code 在对话中主动控制本地应用
- **Neil Assistant** — 一个 Python 编写的命令行个人助手框架，用自然语言告诉它目标，它自己记住、调度、执行、通知

## 1. Claude Code Skills

### 核心理念

让 Claude Code 不只是写代码的工具，而是能与本地应用无缝协作的智能体。基于 macOS 生态，通过 `/command` 触发相应的 skill，实现音乐播放、效率工具调用、浏览器自动化等场景，让 AI 主动感知你的工作节奏。

### Skills 列表

- **music-control** — 根据对话氛围主动控制 macOS 上的音乐播放（支持 KuGou 和 Spotify）。专注、放松、兴奋、调试模式自动匹配，无需手动操作。

更多 skill 持续开发中。

### 安装

```bash
git clone https://github.com/ni-shiliu/neil-control ~/.claude/skills/neil-control
```

下次启动 Claude Code 时，skills 会自动加载。

### 使用方式

在 Claude Code 对话中直接描述需求，skill 会自动触发。例如：

- "播放周杰伦的歌"
- "来点放松的音乐"
- "切到专注模式"

### 贡献

欢迎提交新的 skill 提案。每个 skill 应该是单一职责、可独立触发的能力模块。

---

## 2. Neil Assistant

`assistant/` 是一个独立的 Python 命令行个人助手框架，不是 Claude Code skill 体系。

**核心能力**：用自然语言告诉它目标（例如"每天早上 8 点给我发每日简报"），Claude 自动解析为定时任务，APScheduler 调度执行，macOS 系统通知 + Telegram Bot 双向推送结果。

### 当前已实现的 Loop

| Loop | 触发示例 | 做什么 |
|---|---|---|
| `daily_briefing_loop` | 每天早上 8 点给我发每日简报 | 并发抓取天气 + 头条 + GitHub + HN + 36kr，Claude 动态生成 HTML，Telegram 发送 |
| `email_loop` | 每天 10 点帮我处理邮件 | IMAP 读取未读邮件，Claude Maker-Checker 生成回复，SMTP 发送或存草稿 |

### 快速上手

```bash
cd assistant
pip install -r requirements.txt
cp .env.example .env       # 填入邮箱 / Claude / Telegram / 和风天气 key
python main.py
```

启动后输入自然语言添加目标，支持 `list` / `pause` / `resume` / `delete` / `help` 管理命令。Loop 框架自发现——在 `assistant/loops/` 下新建一个继承 `BaseLoop` 的类即可被自动注册，**无需修改其他文件**。

详细架构、配置项、扩展方法见 [`assistant/README.md`](./assistant/README.md)。

---

## License

MIT
