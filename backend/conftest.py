# 所有测试共用一个临时数据库。
#
# 为什么要有这个文件：以前每个测试文件在自己的开头改 db.数据库文件 再 import main。
# 但 pytest 会先把所有测试文件都 import 一遍，后 import 的那个会把路径改成新文件，
# 而 main 只在第一次 import 时建表 —— 结果就是后面那个文件的所有用例都报
# 「no such table」。测试改成两份文件后就撞上了，所以把库路径收到这里统一设一次。
import pathlib
import tempfile

import db

db.数据库文件 = pathlib.Path(tempfile.mkdtemp(prefix="wb_test_")) / "t.db"
db.初始化()
