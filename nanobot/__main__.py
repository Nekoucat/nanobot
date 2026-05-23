"""
模块入口点：支持以 `python -m nanobot` 方式运行 nanobot

本文件是 nanobot 的命令行入口，当用户执行：
    python -m nanobot
    
Python 会自动查找并执行本文件的 __main__ 块。
它会启动基于 Typer 的 CLI 应用程序。

使用示例：
    # 启动交互式聊天
    python -m nanobot start
    
    # 运行单次对话
    python -m nanobot run "你好"
    
    # 查看配置
    python -m nanobot config show
"""

from nanobot.cli.commands import app

# 当执行 `python -m nanobot` 时，Python 会运行此块
if __name__ == "__main__":
    # app() 是定义在 cli/commands.py 中的 Typer 应用主函数
    # 它会解析命令行参数并分发到对应的处理函数
    app()
