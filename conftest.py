"""让未安装的源码目录也能被 pytest 直接导入（CI 里装了包，这行是多余的但无害）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
