"""让测试像 AstrBot 加载插件那样 import 本仓库的代码。

插件内部用了相对导入（`from .errors import ...`），必须以包的形式导入。
这里把仓库根目录注册成包名 `astrbot_plugin_dsh_go`（插件的真实名字），
而不是目录名——否则本机其他同名目录会抢先生效，测到的就不是本仓库的代码。
"""
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PKG_NAME = "astrbot_plugin_dsh_go"

if PKG_NAME in sys.modules:
    del sys.modules[PKG_NAME]

pkg = types.ModuleType(PKG_NAME)
pkg.__path__ = [str(PLUGIN_DIR)]
pkg.__file__ = str(PLUGIN_DIR / "__init__.py")
sys.modules[PKG_NAME] = pkg

# 让 `import astrbot_plugin_dsh_go` 一定解析到本仓库，而不是别的同名目录
sys.path.insert(0, str(PLUGIN_DIR.parent))
