"""
nanobot - 轻量级 AI 智能体框架 (A lightweight AI agent framework)

本模块是 nanobot 包的根模块，负责：
- 定义包版本信息（支持从 pyproject.toml 或已安装的包元数据读取）
- 提供懒加载导出，避免启动时加载所有重型依赖
- 导出核心公共 API：Nanobot 类和 RunResult 数据类

使用方式：
    import nanobot
    bot = nanobot.Nanobot.from_config()
    result = await bot.run("你好")
"""

import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path


def _read_pyproject_version() -> str | None:
    """
    从源码树的 pyproject.toml 文件读取版本号。
    
    当包尚未安装（如开发环境直接 import）时，
    无法通过 importlib.metadata 获取版本，此时回退到读取 pyproject.toml。
    
    Returns:
        str | None: 版本号字符串，如果文件不存在则返回 None
    """
    # 定位项目根目录下的 pyproject.toml
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.exists():
        return None
    # 解析 TOML 格式的配置文件
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return data.get("project", {}).get("version")


def _resolve_version() -> str:
    """
    解析当前安装的 nanobot 版本号。
    
    优先级：
    1. 从已安装的包元数据获取（pip install 后的标准方式）
    2. 回退到从 pyproject.toml 读取（开发环境）
    3. 最终回退到硬编码的默认版本 "0.2.0"
    
    Returns:
        str: 解析出的版本号字符串
    """
    try:
        # 尝试从已安装的包元数据中获取版本
        return _pkg_version("nanobot-ai")
    except PackageNotFoundError:
        # 源码 checkout 时经常没有安装 dist-info，此时从 pyproject.toml 读取
        return _read_pyproject_version() or "0.2.0"


# ==================== 模块级变量 ====================

__version__ = _resolve_version()   # 当前版本号
__logo__ = "🐈"                     # 项目 Logo（猫咪 emoji）

# ==================== 懒加载导出配置 ====================
# 使用懒加载模式导出核心类，避免用户只是 import nanobot 时就触发重型依赖的导入
# 例如：Nanobot 类依赖 AgentLoop、Provider 等大量模块
_LAZY_EXPORTS = {
    "Nanobot": ".nanobot",     # Nanobot 主类定义在 nanobot/nanobot.py
    "RunResult": ".nanobot",   # RunResult 数据类同样定义在 nanobot/nanobot.py
}


def __getattr__(name: str):
    """
    自定义属性访问钩子，实现懒加载机制。
    
    当用户访问 nanobot.Nanobot 或 nanobot.RunResult 时，
    才真正去导入对应的模块，而不是在 import nanobot时就全部加载。
    
    这种设计的好处：
    - 减少启动时间
    - 降低内存占用
    - 允许部分依赖缺失时仍能 import 成功
    
    Args:
        name: 要访问的属性名
        
    Returns:
        对应的属性值
        
    Raises:
        AttributeError: 如果属性名不在懒加载列表中
    """
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    # 动态导入目标模块
    from importlib import import_module
    mod = import_module(module_path, __name__)
    val = getattr(mod, name)
    # 缓存到 globals，后续访问无需再走懒加载
    globals()[name] = val
    return val


# ==================== 公共 API 导出列表 ====================
# 定义 `from nanobot import *` 时会导入的名称
__all__ = ["Nanobot", "RunResult"]
