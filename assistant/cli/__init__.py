"""CLI 渠道适配器包。

从 main.py 抽出的全部 CLI 逻辑：命令分发、handler、渲染、模板、补全。
main.py 只保留启动与 REPL 骨架；cli 位于六层内核（harness/）之外，
对应架构文档中的「渠道适配器」。
"""
