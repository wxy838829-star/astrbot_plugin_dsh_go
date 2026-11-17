"""把插件目录当成一个包注册进 sys.path，这样测试能像 AstrBot 加载时那样 import。

插件里的模块用了相对导入（`from .errors import ...`），直接以顶层模块
import 会报 "attempted relative import with no known parent package"。
这里注册一个同名包就解决了，而且更贴近真实加载方式。
"""
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PKG_NAME = PLUGIN_DIR.name

if str(PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR.parent))

if PKG_NAME not in sys.modules:
    pkg = types.ModuleType(PKG_NAME)
    pkg.__path__ = [str(PLUGIN_DIR)]
    sys.modules[PKG_NAME] = pkg
