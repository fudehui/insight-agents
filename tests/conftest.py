"""
pytest 全局配置：让测试可以从项目根目录导入 app 包
"""

import sys
from pathlib import Path

# tests/ 的上级即项目根目录，加入 sys.path 后测试可直接 import app.*
project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
