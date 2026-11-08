# 两件事的测试：比赛活过重启（存档/读档）、成绩能导出成 CSV。
#
# 这两个功能最容易「看起来做了其实没做」：
#   · 存档写成「重启后状态还在」，但剩余时间算错，一重启就立刻结算；
#   · 导出写成「能下载」，但打开是乱码 —— 中文 CSV 少了 BOM 就会这样。
# 所以这里直接断言剩余时间和 BOM 字节。
import db
import main as S
from round_engine import 回合引擎


def 清场():
    S.引擎.开赛(总回合数=1, 回合毫秒=600_000)
    c = db.连接()
    c.execute("DELETE FROM orders")
    c.execute("DELETE FROM round_results")
    c.execute("UPDATE users SET cash = initial_cash, frozen_cash = 0")
    c.execute("UPDATE positions SET quantity=0, available_qty=0, frozen_qty=0, avg_cost=0")
    c.commit()
    return c


def 用户行(c, uid=1):
    return c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


# ---------------- 存档 / 读档 ----------------

def test_引擎导出再恢复能接着倒计时():
    钟 = [1000.0]
    原 = 回合引擎(时钟=lambda: 钟[0])
    原.开赛(总回合数=3, 回合毫秒=60_000)
    钟[0] += 20                                   # 已经跑掉 20 秒，剩 40 秒
    档 = 原.导出()
    assert 39_000 <= 档["剩余毫秒"] <= 41_000, f"导出时剩余时间就不对：{档['剩余毫秒']}"

    钟[0] += 5_000                                # 服务「停机」很久
    新 = 回合引擎(时钟=lambda: 钟[0])
    assert 新.恢复(档)
    assert 新.状态 == "RUNNING" and 新.回合序号 == 1
    # 停机时间必须按暂停算：还是接着 40 秒左右，不能一口气扣光
    assert 38_000 <= 新.剩余毫秒() <= 41_000, f"恢复后剩余 {新.剩余毫秒()}"


def test_暂停中的剩余时间是冻住的():
    钟 = [0.0]
    原 = 回合引擎(时钟=lambda: 钟[0])
    原.开赛(总回合数=2, 回合毫秒=30_000)
    钟[0] += 10
    原.暂停()
    档 = 原.导出()
    钟[0] += 1000
    新 = 回合引擎(时钟=lambda: 钟[0])
    新.恢复(档)
    assert 新.状态 == "PAUSED"
    assert 19_000 <= 新.剩余毫秒() <= 20_000


def test_坏存档不能把服务搞挂():
    for 坏 in ({}, {"状态": "瞎写的"}, {"状态": "RUNNING", "总回合数": 0, "回合毫秒": 1,
                                    "回合序号": 1, "剩余毫秒": 10},
              {"状态": "RUNNING", "总回合数": 3, "回合毫秒": 1, "回合序号": 1}):
        assert 回合引擎().恢复(坏) is False, f"这种档应该被拒绝：{坏}"


def test_重启之后比赛还在():
    清场()
    S.引擎.开赛(总回合数=3, 回合毫秒=120_000)
    S.起始资产.clear()
    S._快照起始资产()
    S.日志.clear()
    S.日志.append("比赛开始")
    S._存比赛状态()

    # 模拟进程重启：内存里的一切清空，只留数据库
    S.引擎.状态 = "IDLE"
    S.引擎.回合序号 = 0
    S.引擎.总回合数 = 0
    S.起始资产.clear()
    S.日志.clear()

    assert S._载比赛状态() is True
    assert S.引擎.状态 == "RUNNING"
    assert S.引擎.回合序号 == 1 and S.引擎.总回合数 == 3
    assert 115_000 <= S.引擎.剩余毫秒() <= 120_000
    assert S.起始资产, "各选手本回合的起始资产也要跟着回来，否则收益率会算错"
    assert any("服务重启" in m for m in S.日志)


def test_没存过档就当没比过():
    c = db.连接()
    c.execute("DELETE FROM game_state")
    c.commit()
    S.引擎.状态 = "IDLE"
    assert S._载比赛状态() is False
    assert S.引擎.状态 == "IDLE"


# ---------------- 导出 CSV ----------------

def test_导出成绩有BOM和表头并且金额换成元():
    c = 清场()
    S.引擎.开赛(总回合数=1, 回合毫秒=600_000)
    S.起始资产.clear()
    S._快照起始资产()
    S.行情表["600030"]["last"] = 2450
    S._下单(c, 1, S.下单请求(clientOrderId="exp1", symbol="600030", side="BUY",
                            type="LIMIT", price=2450, quantity=100))
    S._结算并推进()

    响应 = S.导出成绩("results")
    assert 响应.body[:3] == b"\xef\xbb\xbf", "没有 BOM，Excel 打开中文会乱码"
    文本 = 响应.body.decode("utf-8-sig")
    行们 = [r for r in 文本.split("\r\n") if r]
    assert 行们[0].startswith("回合,学号,昵称,事件")
    assert "强制平仓" in 行们[0]
    # 3 个选手各一行，第一列是回合号
    assert len(行们) == 4, f"表头 + 3 个人应该 4 行，实际 {len(行们)} 行：{行们}"
    assert all(r.startswith("1,") for r in 行们[1:])
    # 金额必须是元（带两位小数），不是一长串分
    assert any("." in r for r in 行们[1:])
    assert "100000.00" in 文本                     # 10 万元本金


def test_导出成交只包含真正成交的单():
    c = 清场()
    S.引擎.开赛(总回合数=1, 回合毫秒=600_000)
    S.行情表["600030"]["last"] = 2450
    S._下单(c, 1, S.下单请求(clientOrderId="f1", symbol="600030", side="BUY",
                           type="LIMIT", price=2450, quantity=100))       # 立刻成交
    S._下单(c, 1, S.下单请求(clientOrderId="p1", symbol="600030", side="BUY",
                           type="LIMIT", price=2000, quantity=100))       # 挂着不成交

    成交表 = S.导出成绩("trades").body.decode("utf-8-sig")
    委托表 = S.导出成绩("orders").body.decode("utf-8-sig")
    assert 成交表.count("\r\n") == 2, "成交表只该有表头 + 1 笔成交"
    assert 委托表.count("\r\n") == 3, "委托表应该有表头 + 2 笔委托（含挂着的）"
    assert "PENDING" not in 成交表 and "PENDING" in 委托表


def test_导出类型写错要明确报错():
    from trading_core import 规则拒绝
    try:
        S.导出成绩("随便写")
        assert False, "未知类型竟然没报错"
    except 规则拒绝 as e:
        assert e.code == "NOT_FOUND"
