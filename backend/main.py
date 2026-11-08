# 第四步：回合引擎接上服务。到点自动强平、结算、钱带进下一轮。
# 运行：python -m uvicorn main:app --port 8000
# 打开 http://127.0.0.1:8000 ，点「开始比赛」，就能看到一回合自己跑完。

import csv
import io
import json
import random
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

import db
from trading_core import 默认费率, 算费用, 校验, 买入, 卖出, 规则拒绝
from round_engine import 回合引擎

app = FastAPI()
db.初始化()

北京时间 = timezone(timedelta(hours=8))

# 界面版本号：每次重启服务就换一个。页面会拿自己和它比对，
# 不一致就自动刷新——这样「你在看旧页面」这件事不会再发生。
构建 = datetime.now(北京时间).strftime("%m-%d %H:%M:%S")

# ---------------- 状态 ----------------
账本锁 = threading.Lock()          # 改钱、改持仓、推进回合，都要排队
行情锁 = threading.Lock()
引擎 = 回合引擎()
起始资产: dict[int, int] = {}      # 每个回合开始时，各选手的总资产快照
开赛时刻: str = ""                 # 开赛的真实时间，资产曲线第 0 个点的横轴
日志: list[str] = []

随机 = random.Random(20261107)     # 固定种子，方便复盘


# ---------------- 存档 / 读档：让比赛活过重启 ----------------
def _存比赛状态():
    """把「第几回合、还剩多久、各人本回合从多少本金起算」写进数据库。

    为什么要存：回合状态原来只在内存里，黑色窗口一关（或者程序崩一次），
    比赛就退回「未开始」，钱和订单还在库里，比赛却没了 —— 演示时被人问「怎么又变回没开始」
    就是这么来的。存下来之后重启能接着跑，停机那段时间按暂停算。
    """
    c = db.连接()
    try:
        c.execute(
            "INSERT OR REPLACE INTO game_state "
            "(id, status, round_index, total_rounds, round_ms, remain_ms,"
            " started_at, start_assets, logs) VALUES (1,?,?,?,?,?,?,?,?)",
            (引擎.状态, 引擎.回合序号, 引擎.总回合数, 引擎.回合毫秒, 引擎.剩余毫秒(),
             开赛时刻, json.dumps(起始资产), json.dumps(日志[-50:], ensure_ascii=False)))
        c.commit()
    finally:
        c.close()


def _载比赛状态():
    """启动时读一次存档。没有存档、或者存档坏了，就当没比过，服务照常起来。"""
    global 开赛时刻
    c = db.连接()
    try:
        行 = c.execute("SELECT * FROM game_state WHERE id = 1").fetchone()
    finally:
        c.close()
    if 行 is None:
        return False
    if not 引擎.恢复({"状态": 行["status"], "回合序号": 行["round_index"],
                    "总回合数": 行["total_rounds"], "回合毫秒": 行["round_ms"],
                    "剩余毫秒": 行["remain_ms"]}):
        return False
    try:
        起始资产.update({int(k): int(v) for k, v in json.loads(行["start_assets"]).items()})
        日志.extend(json.loads(行["logs"]))
    except (ValueError, TypeError):
        pass
    开赛时刻 = 行["started_at"]
    if 引擎.状态 in ("RUNNING", "PAUSED"):
        日志.append(f"服务重启，已从存档恢复：第 {引擎.回合序号} 回合，剩余 "
                    f"{引擎.剩余毫秒() // 1000} 秒（停机时间按暂停算）")
    return True

# ---------------- 假行情（分支一交付后整块替换） ----------------
行情表 = {
    "600030": {"name": "中信证券", "last": 2450, "prevClose": 2450},
    "601688": {"name": "华泰证券", "last": 1830, "prevClose": 1830},
}
for _项 in 行情表.values():
    _项["limitUp"] = int(_项["prevClose"] * 1.1 + 0.5)
    _项["limitDown"] = int(_项["prevClose"] * 0.9 + 0.5)

事件池 = [
    {"id": "EV201506_LEVERAGE_CRASH", "title": "2015 年 6 月去杠杆股灾", "date": "2015-06-15",
     "category": "CRISIS", "dataQuality": "DAILY",
     "summary": "清查场外配资，杠杆资金撤离，指数快速回落。", "direction": "BEAR"},
    {"id": "EV202002_PANDEMIC_DROP", "title": "2020 年 2 月疫情冲击", "date": "2020-02-03",
     "category": "CRISIS", "dataQuality": "DAILY",
     "summary": "春节后首个交易日大幅低开，随后分化。", "direction": "BEAR"},
    {"id": "EV202409_POLICY_RALLY", "title": "2024 年 9 月政策转向", "date": "2024-09-24",
     "category": "POLICY", "dataQuality": "MINUTE",
     "summary": "一揽子政策落地，成交量快速放大。", "direction": "BULL"},
]


def 当前事件() -> dict:
    序号 = max(1, 引擎.回合序号)
    return 事件池[(序号 - 1) % len(事件池)]


# ---------------- 统一返回结构 ----------------
def 成功(data):
    return {"code": 0, "message": "ok", "data": data}


@app.exception_handler(规则拒绝)
def 业务拒绝(_: Request, e: 规则拒绝):
    return JSONResponse(status_code=422,
                        content={"code": e.code, "message": e.message, "data": None})


@app.exception_handler(RequestValidationError)
async def 参数不对(_: Request, e: RequestValidationError):
    """字段缺失 / 类型不对时，FastAPI 默认回 {"detail":[...]}。
    前端按约定找 code，找不到就显示成「undefined [undefined]」——看着像后端崩了。
    这里统一成 {code,message}，并用中文点明是哪个字段错了。"""
    明细 = []
    for 错 in e.errors():
        字段 = ".".join(str(x) for x in 错["loc"] if x != "body") or "整体"
        明细.append(f"{字段}：{错['msg']}")
    return JSONResponse(status_code=422, content={
        "code": "BAD_REQUEST",
        "message": "请求参数不对 → " + "；".join(明细),
        "data": None})


@app.exception_handler(Exception)
async def 服务器出错(_: Request, e: Exception):
    """兜底：没预料到的异常也按 {code,message} 回，页面永远不会再出现 undefined。"""
    return JSONResponse(status_code=500, content={
        "code": "INTERNAL_ERROR",
        "message": f"服务端出错（{type(e).__name__}）：{e}",
        "data": None})


def 认人(authorization: str | None = Header(None)) -> sqlite3.Row:
    """demo 版登录：token 就是用户 id。参数名必须是英文（HTTP 头名只能是 latin-1）。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    try:
        用户id = int(authorization.split(" ", 1)[1])
    except ValueError:
        raise HTTPException(401, "登录凭证无效")
    c = db.连接()
    用户 = c.execute("SELECT * FROM users WHERE id = ?", (用户id,)).fetchone()
    c.close()
    if 用户 is None:
        raise HTTPException(401, "用户不存在")
    return 用户


def 取持仓(c: sqlite3.Connection, user_id: int, symbol: str) -> dict:
    行 = c.execute("SELECT * FROM positions WHERE user_id = ? AND symbol = ?",
                   (user_id, symbol)).fetchone()
    if 行 is None:
        c.execute("INSERT INTO positions (user_id, symbol, quantity, available_qty, frozen_qty, "
                  "avg_cost) VALUES (?, ?, 0, 0, 0, 0)", (user_id, symbol))
        c.commit()
        return {"quantity": 0, "available_qty": 0, "frozen_qty": 0, "avg_cost": 0}
    return dict(行)


def 市值(c: sqlite3.Connection, user_id: int) -> int:
    with 行情锁:
        仓们 = c.execute("SELECT symbol, quantity FROM positions WHERE user_id = ?",
                         (user_id,)).fetchall()
        return sum(r["quantity"] * 行情表[r["symbol"]]["last"]
                   for r in 仓们 if r["symbol"] in 行情表)


# ---------------- 行情波动 + 回合推进（后台线程） ----------------
def _波动():
    倾向 = 当前事件()["direction"]
    with 行情锁:
        for 项 in 行情表.values():
            幅度 = 随机.randint(-4, 4)
            if 倾向 == "BEAR":
                幅度 -= 1
            elif 倾向 == "BULL":
                幅度 += 1
            变动 = 项["last"] * 幅度 // 1000          # 每次最多千分之五
            项["last"] = max(1, min(项["last"] + 变动, 项["limitUp"]))
            项["last"] = max(项["last"], 项["limitDown"])


def _快照起始资产():
    c = db.连接()
    try:
        for u in c.execute("SELECT id, cash FROM users").fetchall():
            起始资产[u["id"]] = u["cash"] + 市值(c, u["id"])
    finally:
        c.close()


def _结算并推进():
    """当前回合结束：所有人强制平仓 → 结算 → 资金带进下一回合。"""
    序号 = 引擎.回合序号
    _撤销全部挂单()             # 回合结束，没成交的挂单作废，冻结的钱股退回
    c = db.连接()
    try:
        for u in c.execute("SELECT id, cash FROM users").fetchall():
            终 = u["cash"] + 市值(c, u["id"])
            起 = 起始资产.get(u["id"], 终)
            # 强制平仓免手续费：市值直接全额变成现金，持仓清零
            c.execute("UPDATE users SET cash = ?, frozen_cash = 0 WHERE id = ?", (终, u["id"]))
            c.execute("UPDATE positions SET quantity = 0, available_qty = 0, frozen_qty = 0,"
                      " avg_cost = 0 WHERE user_id = ?", (u["id"],))
            c.execute("INSERT OR REPLACE INTO round_results "
                      "(user_id, round_index, start_equity, end_equity, pnl, forced_close,"
                      " settled_at) VALUES (?,?,?,?,?,1,?)",
                      (u["id"], 序号, 起, 终, 终 - 起,
                       datetime.now(北京时间).isoformat(timespec="seconds")))
        c.commit()
    finally:
        c.close()

    还有下一回合 = 引擎.进入下一回合()
    日志.append(f"第 {序号} 回合结束：已按最后成交价强制平仓并结算")
    if 还有下一回合:
        日志.append(f"第 {引擎.回合序号} 回合开始：{当前事件()['title']}")
        _快照起始资产()
    else:
        日志.append("整场比赛已结算，可以导出成绩了")
    _存比赛状态()


# ---------------- 两个自动交易的选手（让排行榜有可比对象） ----------------
# 小李追着涨的买、下手重；小张只小额试水。它们走的是和真人**完全同一条**校验链，
# 所以被现金不足、涨跌停拦下时，和你看到的是同一个错误码。
机器人 = {
    2: {"昵称": "小李", "股数": 800, "概率": 0.18},
    3: {"昵称": "小张", "股数": 200, "概率": 0.10},
}


def _机器人行动():
    for 用户id, 设定 in 机器人.items():
        if 随机.random() > 设定["概率"]:
            continue
        买涨 = 当前事件()["direction"] == "BULL" or 随机.random() < 0.5
        标的 = sorted(行情表)[0] if 买涨 else sorted(行情表)[-1]
        with 行情锁:
            市价 = 行情表[标的]["last"]
        请求 = 下单请求(
            clientOrderId=f"bot{用户id}-{int(time.time()*1000)}-{随机.randint(0, 9999)}",
            symbol=标的, side="BUY", price=市价, quantity=设定["股数"])

        def 做一次():
            c = db.连接()
            try:
                return _下单(c, 用户id, 请求)
            finally:
                c.close()

        try:
            db.重试(做一次)
        except 规则拒绝:
            pass        # 钱不够、涨停、停牌：机器人和真人一样，被拒就算了
        except Exception as e:
            # 记上异常类型，不然「只读数据库」这类消息看不出是谁报的、报的什么
            日志.append(f"{设定['昵称']}下单异常：{type(e).__name__}: {e}")


def _行情线程():
    下次存盘 = 0.0
    while True:
        time.sleep(0.3)
        try:
            with 账本锁:
                if 引擎.状态 == "RUNNING":
                    _波动()
                    _机器人行动()
                    _撮合挂单()
                    if 引擎.剩余毫秒() <= 0:
                        _结算并推进()
                # 每 5 秒把剩余时间刷新一次。只在状态切换时存的话，
                # 跑到一半断电，重启会退回存盘那一刻的时间，白白多给选手几十秒。
                if time.monotonic() >= 下次存盘 and 引擎.状态 in ("RUNNING", "PAUSED"):
                    _存比赛状态()
                    下次存盘 = time.monotonic() + 5
        except Exception as e:                       # 后台线程不能死，否则比赛卡住
            日志.append(f"后台出错：{e}")


threading.Thread(target=_行情线程, daemon=True).start()
_载比赛状态()      # 有存档就接着上次的进度；没有就维持「未开始」


# ---------------- 比赛状态与控制 ----------------
@app.get("/api/v1/game/state")
def 比赛状态():
    ev = 当前事件()
    return 成功({
        "status": 引擎.状态,
        "roundIndex": 引擎.回合序号,
        "totalRounds": 引擎.总回合数,
        "roundRemainMs": 引擎.剩余毫秒(),
        "roundTotalMs": 引擎.回合毫秒,
        "simDate": ev["date"],                    # 模拟日期：T+1 判断用这个，不用真实日期
        "serverNow": datetime.now(北京时间).isoformat(timespec="seconds"),
        "event": ev,
        "logs": 日志[-10:],
    })


@app.post("/api/v1/admin/game/practice")
def 练习模式():
    """不限时的练习场：能一直买卖、不会自动结算。专门用来熟悉操作。"""
    global 开赛时刻
    with 账本锁:
        c = db.连接()
        try:
            c.execute("UPDATE users SET cash = initial_cash, frozen_cash = 0")
            c.execute("UPDATE positions SET quantity=0, available_qty=0, frozen_qty=0, avg_cost=0")
            c.execute("DELETE FROM orders")
            c.execute("DELETE FROM round_results")
            c.commit()
        finally:
            c.close()
        起始资产.clear()
        开赛时刻 = datetime.now(北京时间).isoformat(timespec="seconds")
        日志.clear()
        引擎.开赛(总回合数=1, 回合毫秒=24 * 3600 * 1000)     # 24 小时，等于不会结束
        _快照起始资产()
        日志.append("练习模式开始：不限时，可以随便买卖，不会结算")
        _存比赛状态()
    return 比赛状态()


class 开赛请求(BaseModel):
    rounds: int = 3
    seconds: int = 120


@app.post("/api/v1/admin/game/start")
def 开始比赛(请求: 开赛请求):
    """demo 里不校验管理员身份。正式版 /admin/** 必须要求 role=ADMIN。"""
    global 开赛时刻
    with 账本锁:
        c = db.连接()
        try:
            c.execute("UPDATE users SET cash = initial_cash, frozen_cash = 0")
            c.execute("UPDATE positions SET quantity=0, available_qty=0, frozen_qty=0, avg_cost=0")
            c.execute("DELETE FROM orders")
            c.execute("DELETE FROM round_results")
            c.commit()
        finally:
            c.close()
        起始资产.clear()
        开赛时刻 = datetime.now(北京时间).isoformat(timespec="seconds")
        日志.clear()
        引擎.开赛(总回合数=请求.rounds, 回合毫秒=请求.seconds * 1000)
        _快照起始资产()
        日志.append(f"比赛开始：共 {请求.rounds} 回合，每回合 {请求.seconds} 秒")
        日志.append(f"第 1 回合：{当前事件()['title']}")
        _存比赛状态()
    return 比赛状态()


@app.post("/api/v1/admin/game/pause")
def 暂停():
    with 账本锁:
        if not 引擎.暂停():
            raise 规则拒绝("ROUND_NOT_RUNNING", "当前不是进行中状态")
        日志.append(f"管理员暂停，剩余 {引擎.剩余毫秒() // 1000} 秒已冻结")
        _存比赛状态()
    return 比赛状态()


@app.post("/api/v1/admin/game/resume")
def 继续():
    with 账本锁:
        if not 引擎.继续():
            raise 规则拒绝("ROUND_NOT_RUNNING", "当前不是暂停状态")
        日志.append("管理员继续比赛")
        _存比赛状态()
    return 比赛状态()


@app.post("/api/v1/admin/game/skip")
def 跳过本回合():
    with 账本锁:
        if 引擎.状态 not in ("RUNNING", "PAUSED"):
            raise 规则拒绝("ROUND_NOT_RUNNING", "比赛还没开始")
        日志.append("管理员跳过本回合，立即结算")
        _结算并推进()
    return 比赛状态()


@app.post("/api/v1/admin/game/settle")
def 结算整场():
    """立刻结束整场比赛：先结算当前回合，之后不再开新回合计。

    小技巧：把「总回合数」改成「当前回合」，结算完引擎就认为已经到最后一回合，
    自己进入 SETTLED，不会再推进。这样回合序号和成绩表的行数一定对得上。
    """
    with 账本锁:
        if 引擎.状态 not in ("RUNNING", "PAUSED"):
            raise 规则拒绝("ROUND_NOT_RUNNING", "比赛还没开始")
        引擎.总回合数 = 引擎.回合序号
        日志.append("管理员结束整场比赛，立即强制平仓并结算")
        _结算并推进()
    return 比赛状态()


@app.post("/api/v1/admin/game/end")
def 结束锁定():
    """结束并锁定：不解算、不平仓，只是把门关上，账停在当前状态。"""
    with 账本锁:
        if not 引擎.锁定():
            raise 规则拒绝("ROUND_NOT_RUNNING", "比赛还没开始或已经锁定")
        _撤销全部挂单()          # 锁了就别再留挂单，冻结的钱股要退回给选手
        日志.append("管理员结束并锁定比赛，之后不再接受下单")
        _存比赛状态()
    return 比赛状态()


# ---------------- 行情 / 账户 / 交易 ----------------
@app.get("/api/v1/market/quotes")
def 行情():
    with 行情锁:
        项目 = [{"symbol": k, "name": v["name"], "last": v["last"],
                "prevClose": v["prevClose"], "limitUp": v["limitUp"],
                "limitDown": v["limitDown"]} for k, v in 行情表.items()]
    return 成功({"items": 项目})


@app.get("/api/v1/users")
def 选手列表():
    c = db.连接()
    结果 = c.execute("SELECT id, student_no, nickname FROM users ORDER BY id").fetchall()
    c.close()
    return 成功([dict(r) for r in 结果])


@app.get("/api/v1/account")
def 查账户(用户: sqlite3.Row = Depends(认人)):
    c = db.连接()
    try:
        # 重新读一次：余额每时每刻都在被回合结算改，不能用手上这份旧数据
        用户 = c.execute("SELECT * FROM users WHERE id = ?", (用户["id"],)).fetchone()
        市值_ = 市值(c, 用户["id"])
        总 = 用户["cash"] + 市值_
        持仓数 = c.execute("SELECT COUNT(*) AS n FROM positions "
                          "WHERE user_id = ? AND quantity > 0",
                          (用户["id"],)).fetchone()["n"]
        return 成功({
            "initialCash": 用户["initial_cash"], "cash": 用户["cash"],
            "frozenCash": 用户["frozen_cash"],
            "availableCash": 用户["cash"] - 用户["frozen_cash"],
            "marketValue": 市值_, "totalAssets": 总,
            "pnl": 总 - 用户["initial_cash"],
            "pnlPct": (总 - 用户["initial_cash"]) / 用户["initial_cash"],
            "positionCount": 持仓数,
        })
    finally:
        c.close()


@app.get("/api/v1/positions")
def 查持仓(用户: sqlite3.Row = Depends(认人)):
    c = db.连接()
    try:
        结果 = c.execute("SELECT * FROM positions WHERE user_id = ?", (用户["id"],)).fetchall()
        项目 = []
        for 仓 in 结果:
            if 仓["symbol"] not in 行情表 or 仓["quantity"] == 0:
                continue
            with 行情锁:
                行 = 行情表[仓["symbol"]]
                最新 = 行["last"]
            项目.append({"symbol": 仓["symbol"], "name": 行["name"],
                        "quantity": 仓["quantity"], "availableQty": 仓["available_qty"],
                        "avgCost": 仓["avg_cost"], "last": 最新,
                        "marketValue": 仓["quantity"] * 最新,
                        # 浮盈按「现价 - 成本价」算，成本价是买入口径的平均成本（不含手续费）
                        "pnl": (最新 - 仓["avg_cost"]) * 仓["quantity"],
                        "pnlPct": ((最新 - 仓["avg_cost"]) / 仓["avg_cost"]
                                   if 仓["avg_cost"] else 0)})
        return 成功({"items": 项目})
    finally:
        c.close()


class 下单请求(BaseModel):
    clientOrderId: str
    symbol: str
    side: str
    type: str = "LIMIT"          # LIMIT / MARKET，契约要求；市价单可以不传 price
    price: int | None = None
    quantity: int


def _单号(订单id: int) -> str:
    """对内用自增整数，对外一律这个字符串。契约里 orderId 是字符串，前端直接展示。"""
    return f"ORD{订单id:012d}"


def _冻结额(价格: int, 数量: int) -> int:
    """买入挂单要冻住多少钱：成交金额 + 预估佣金。
    多冻一点没关系（撤单会全额退回），少冻会导致成交时钱不够。"""
    return 价格 * 数量 + 算费用(价格 * 数量, False).佣金


@app.post("/api/v1/orders")
def 下单(请求: 下单请求, 用户: sqlite3.Row = Depends(认人)):
    if 请求.symbol not in 行情表:
        raise 规则拒绝("SYMBOL_NOT_TRADABLE", "标的不在本回合股票池")

    def 做一次():
        with 账本锁:
            c = db.连接()
            try:
                return 成功(_下单(c, 用户["id"], 请求))
            finally:
                c.close()

    # 数据库偶尔被别的进程/杀毒软件短暂锁住，重试一次就好；
    # 不能让这种情况变成「点了买入却提示数据库只读」。
    return db.重试(做一次)


def _下单(c: sqlite3.Connection, 用户id: int, 请求: 下单请求) -> dict:
    if not 引擎.可交易():
        raise 规则拒绝("ROUND_NOT_RUNNING", "当前不在交易状态（未开始/暂停/已结算）")
    if 请求.side not in ("BUY", "SELL"):
        raise 规则拒绝("INVALID_SIDE", "side 只能是 BUY 或 SELL")
    if 请求.type not in ("LIMIT", "MARKET"):
        raise 规则拒绝("INVALID_ORDER_TYPE", "type 只能是 LIMIT 或 MARKET")

    with 行情锁:
        市价 = 行情表[请求.symbol]["last"]
        涨停 = 行情表[请求.symbol]["limitUp"]
        跌停 = 行情表[请求.symbol]["limitDown"]

    # 市价单不传价，就用当前市价当限价（这样它必然能成交）
    限价 = 市价 if 请求.type == "MARKET" else (请求.price or 0)
    if 限价 <= 0:
        raise 规则拒绝("INVALID_PRICE", "限价单必须给 price")

    用户 = c.execute("SELECT * FROM users WHERE id = ?", (用户id,)).fetchone()
    仓 = 取持仓(c, 用户id, 请求.symbol)
    可用现金 = 用户["cash"] - 用户["frozen_cash"]

    校验(请求.side, 限价, 请求.quantity, 可用现金,
         仓["quantity"], 仓["available_qty"], 涨停, 跌停)

    能成交 = 限价 >= 市价 if 请求.side == "BUY" else 限价 <= 市价
    if not 能成交:
        # 挂单：钱（或股）先锁住，但现金和持股总数都不动 —— 只是"不能再拿去用"。
        冻结 = _冻结额(限价, 请求.quantity) if 请求.side == "BUY" else 0
        try:
            c.execute("INSERT INTO orders (user_id, client_order_id, symbol, side, order_type,"
                      " price, quantity, fee, frozen_amount, status, created_at)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (用户id, 请求.clientOrderId, 请求.symbol, 请求.side, 请求.type,
                       限价, 请求.quantity, 0, 冻结, "PENDING",
                       datetime.now(北京时间).isoformat(timespec="seconds")))
            新id = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
            c.execute("UPDATE orders SET order_id = ? WHERE id = ?", (_单号(新id), 新id))
            if 请求.side == "BUY":
                c.execute("UPDATE users SET frozen_cash = frozen_cash + ? WHERE id = ?",
                          (冻结, 用户id))
            else:
                c.execute("UPDATE positions SET available_qty = available_qty - ?,"
                          " frozen_qty = frozen_qty + ? WHERE user_id = ? AND symbol = ?",
                          (请求.quantity, 请求.quantity, 用户id, 请求.symbol))
            c.commit()
        except sqlite3.IntegrityError:
            raise 规则拒绝("DUPLICATE_CLIENT_ORDER_ID", "重复的下单请求")
        return {"orderId": _单号(新id), "status": "PENDING", "filledQty": 0,
                "avgPrice": 0, "fee": 0, "tax": 0, "transferFee": 0,
                "message": f"已挂单，现价 {市价} 还没到你的限价 {限价}，资金已冻结"}

    if 请求.side == "BUY":
        新现金, 新持股, 新成本, 费 = 买入(用户["cash"], 仓["quantity"],
                                          仓["avg_cost"], 市价, 请求.quantity)
        新可卖 = 仓["available_qty"]
    else:
        新现金, 新持股, 新成本, 费 = 卖出(用户["cash"], 仓["quantity"],
                                          仓["avg_cost"], 市价, 请求.quantity)
        新可卖 = 仓["available_qty"] - 请求.quantity

    try:
        c.execute("INSERT INTO orders (user_id, client_order_id, symbol, side, order_type,"
                  " price, quantity, filled_qty, fee, status, created_at)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (用户id, 请求.clientOrderId, 请求.symbol, 请求.side, 请求.type,
                   限价, 请求.quantity, 请求.quantity, 费.合计, "FILLED",
                   datetime.now(北京时间).isoformat(timespec="seconds")))
        新id = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        c.execute("UPDATE orders SET order_id = ? WHERE id = ?", (_单号(新id), 新id))
    except sqlite3.IntegrityError:
        raise 规则拒绝("DUPLICATE_CLIENT_ORDER_ID", "重复的下单请求")

    c.execute("UPDATE users SET cash = ? WHERE id = ?", (新现金, 用户id))
    c.execute("UPDATE positions SET quantity=?, available_qty=?, avg_cost=? "
              "WHERE user_id=? AND symbol=?",
              (新持股, 新可卖, 新成本, 用户id, 请求.symbol))
    c.commit()
    return {"orderId": _单号(新id), "status": "FILLED", "filledQty": 请求.quantity,
            "avgPrice": 市价, "fee": 费.佣金, "tax": 费.印花税, "transferFee": 费.过户费}


@app.get("/api/v1/orders")
def 查委托(status: str | None = None, page: int = 1, pageSize: int = 20,
          用户: sqlite3.Row = Depends(认人)):
    """委托列表。契约里有分页，前端列表页直接用。"""
    page, pageSize = max(1, page), min(100, max(1, pageSize))
    条件, 参数 = "user_id = ?", [用户["id"]]
    if status:
        条件 += " AND status = ?"
        参数.append(status)
    c = db.连接()
    try:
        总 = c.execute(f"SELECT COUNT(*) AS n FROM orders WHERE {条件}", 参数).fetchone()["n"]
        行们 = c.execute(f"SELECT * FROM orders WHERE {条件} ORDER BY id DESC LIMIT ? OFFSET ?",
                        参数 + [pageSize, (page - 1) * pageSize]).fetchall()
    finally:
        c.close()
    return 成功({
        "items": [{
            "orderId": r["order_id"], "clientOrderId": r["client_order_id"],
            "symbol": r["symbol"], "side": r["side"], "type": r["order_type"],
            "price": r["price"], "quantity": r["quantity"], "filledQty": r["filled_qty"],
            "status": r["status"], "fee": r["fee"], "frozenAmount": r["frozen_amount"],
            "rejectReason": r["reject_reason"], "createdAt": r["created_at"],
        } for r in 行们],
        "page": page, "pageSize": pageSize, "total": 总,
    })


@app.get("/api/v1/trades")
def 成交明细(symbol: str | None = None, page: int = 1, pageSize: int = 20,
              用户: sqlite3.Row = Depends(认人)):
    """成交明细：只有真正成交的委托才在这里。

    和 /orders 的区别必须讲清楚，这是新手最容易混的一对：
      orders = 「我提交过什么」，含挂单（PENDING）和撤单（CANCELED）
      trades = 「实际发生了什么」，只有 FILLED

    画资产曲线、算胜率、导对账单都要用 trades，不能用 orders 过滤——
    挂单和撤单没有产生任何现金流，混进去账就不平了。
    """
    page, pageSize = max(1, page), min(100, max(1, pageSize))
    条件, 参数 = "user_id = ? AND status = 'FILLED'", [用户["id"]]
    if symbol:
        条件 += " AND symbol = ?"
        参数.append(symbol)

    c = db.连接()
    try:
        总 = c.execute(f"SELECT COUNT(*) AS n FROM orders WHERE {条件}", 参数).fetchone()["n"]
        行们 = c.execute(f"SELECT * FROM orders WHERE {条件} ORDER BY id DESC LIMIT ? OFFSET ?",
                        参数 + [pageSize, (page - 1) * pageSize]).fetchall()
    finally:
        c.close()

    项目 = []
    for r in 行们:
        金额 = r["price"] * r["quantity"]
        费 = 算费用(金额, r["side"] == "SELL")
        # 三费拆分按当前费率重算，合计以库里存的为准：
        # 万一以后费率改过，也不会出现「佣金+印花税+过户费 ≠ 实收」这种对不上的账。
        佣 = r["fee"] - 费.印花税 - 费.过户费
        项目.append({
            "tradeId": "TRD" + r["order_id"].replace("ORD", ""),
            "orderId": r["order_id"],
            "symbol": r["symbol"],
            "name": 行情表.get(r["symbol"], {}).get("name", r["symbol"]),
            "side": r["side"],
            "price": r["price"],
            "quantity": r["quantity"],
            "amount": 金额,                       # 成交金额，不含费用
            "fee": 佣, "tax": 费.印花税, "transferFee": 费.过户费,
            "feeTotal": r["fee"],
            # 现金流：买入是负数（钱出去），卖出是正数（钱进来）。
            # 一列加起来就应该是现金的变化，这是最好用的一栏。
            "cashFlow": -(金额 + r["fee"]) if r["side"] == "BUY" else 金额 - r["fee"],
            "ts": r["created_at"],                # 契约字段名是 ts，前端按文档取这个
            "tradedAt": r["created_at"],
        })
    return 成功({"items": 项目, "page": page, "pageSize": pageSize, "total": 总})


@app.get("/api/v1/account/equity")
def 资产曲线(用户: sqlite3.Row = Depends(认人)):
    """资产曲线：开赛一个点，每个回合结束一个点，最后补上「现在」。

    前端别自己拿 round_results 拼——横轴怎么对齐、当前回合算不算进去，
    每个页面各写一套，两个页面的曲线就会长得不一样。
    """
    c = db.连接()
    try:
        用户 = c.execute("SELECT * FROM users WHERE id = ?", (用户["id"],)).fetchone()
        轮 = c.execute("SELECT * FROM round_results WHERE user_id = ? ORDER BY round_index",
                       (用户["id"],)).fetchall()
        现 = 用户["cash"] + 用户["frozen_cash"] + 市值(c, 用户["id"])
    finally:
        c.close()

    点 = [{"roundIndex": 0, "label": "开赛", "equity": 用户["initial_cash"], "pnl": 0}]
    for r in 轮:
        点.append({"roundIndex": r["round_index"], "label": f"第 {r['round_index']} 回合",
                   "equity": r["end_equity"], "pnl": r["end_equity"] - r["start_equity"]})

    盈亏 = 现 - 用户["initial_cash"]

    # 契约里这个接口要的是 points[{ts, totalAssets}]，横轴是真实时刻。
    # 老库可能有结算过但没记时刻的回合，settled_at 是空的 → 用「现在」顶上，
    # 宁可横轴挤在一起，也不要给前端一个 null 让图表画不出来。
    现在 = datetime.now(北京时间).isoformat(timespec="seconds")
    曲线 = [{"ts": 开赛时刻 or (轮[0]["settled_at"] if 轮 else 现在) or 现在,
             "totalAssets": 用户["initial_cash"]}]
    for r in 轮:
        曲线.append({"ts": r["settled_at"] or 现在, "totalAssets": r["end_equity"]})
    曲线.append({"ts": 现在, "totalAssets": 现})

    return 成功({
        "points": 曲线,                               # 契约字段：画曲线用这个
        "items": 点,                                  # 已结算的每回合一个点（带中文标签，页面用）
        "current": {                                  # 还没结算的「现在」
            "equity": 现, "pnl": 盈亏,
            "pnlPct": 盈亏 / 用户["initial_cash"] if 用户["initial_cash"] else 0,
            "roundIndex": 引擎.回合序号, "status": 引擎.状态,
        },
    })


@app.delete("/api/v1/orders/{orderId}")
def 撤单(orderId: str, 用户: sqlite3.Row = Depends(认人)):
    """撤掉还没成交的挂单，把冻住的钱/股原路退回。"""
    with 账本锁:
        c = db.连接()
        try:
            单 = c.execute("SELECT * FROM orders WHERE order_id = ? AND user_id = ?",
                          (orderId, 用户["id"])).fetchone()
            if 单 is None:
                raise 规则拒绝("ORDER_NOT_FOUND", "找不到这笔订单")
            if 单["status"] != "PENDING":
                raise 规则拒绝("ORDER_NOT_CANCELABLE", f"订单已是 {单['status']}，不能撤")
            if 单["side"] == "BUY":
                c.execute("UPDATE users SET frozen_cash = MAX(0, frozen_cash - ?) WHERE id = ?",
                          (单["frozen_amount"], 用户["id"]))
            else:
                c.execute("UPDATE positions SET available_qty = available_qty + ?,"
                          " frozen_qty = MAX(0, frozen_qty - ?) WHERE user_id = ? AND symbol = ?",
                          (单["quantity"], 单["quantity"], 用户["id"], 单["symbol"]))
            c.execute("UPDATE orders SET status = 'CANCELED', frozen_amount = 0 WHERE id = ?",
                      (单["id"],))
            c.commit()
        finally:
            c.close()
    return 成功({"orderId": orderId, "status": "CANCELED"})


def _撮合挂单():
    """行情一变，就把所有能成交的挂单成交掉。
    没有这一步，挂单会永远躺在 PENDING 里 —— 等于一个假功能。
    注意：成交价永远是市价，限价只决定「能不能成交」。"""
    with 行情锁:
        报价 = {k: v["last"] for k, v in 行情表.items()}

    c = db.连接()
    try:
        挂单们 = c.execute("SELECT * FROM orders WHERE status = 'PENDING' AND order_type = 'LIMIT'"
                         ).fetchall()
        for 单 in 挂单们:
            市价 = 报价.get(单["symbol"])
            if 市价 is None:
                continue
            可成交 = 市价 <= 单["price"] if 单["side"] == "BUY" else 市价 >= 单["price"]
            if not 可成交:
                continue

            数量 = 单["quantity"]
            用户 = c.execute("SELECT * FROM users WHERE id = ?", (单["user_id"],)).fetchone()
            仓 = 取持仓(c, 单["user_id"], 单["symbol"])

            if 单["side"] == "BUY":
                新现金, 新持股, 新成本, 费 = 买入(用户["cash"], 仓["quantity"],
                                                  仓["avg_cost"], 市价, 数量)
                单行更新 = ("UPDATE users SET cash = ?, frozen_cash = MAX(0, frozen_cash - ?) "
                          "WHERE id = ?")
                参数 = (新现金, 单["frozen_amount"], 单["user_id"])
                c.execute(单行更新, 参数)
                c.execute("UPDATE positions SET quantity = ?, avg_cost = ? "
                          "WHERE user_id = ? AND symbol = ?",
                          (新持股, 新成本, 单["user_id"], 单["symbol"]))
            else:
                新现金, 新持股, 新成本, 费 = 卖出(用户["cash"], 仓["quantity"],
                                                  仓["avg_cost"], 市价, 数量)
                c.execute("UPDATE users SET cash = ? WHERE id = ?", (新现金, 单["user_id"]))
                c.execute("UPDATE positions SET quantity = ?, frozen_qty = MAX(0, frozen_qty - ?),"
                          " avg_cost = ? WHERE user_id = ? AND symbol = ?",
                          (新持股, 数量, 新成本, 单["user_id"], 单["symbol"]))

            c.execute("UPDATE orders SET status = 'FILLED', filled_qty = ?, fee = ?,"
                      " frozen_amount = 0 WHERE id = ?", (数量, 费.合计, 单["id"]))
            日志.append(f"挂单成交：{单['order_id']} 按市价 {市价} 成交 {数量} 股")
        c.commit()
    finally:
        c.close()


def _撤销全部挂单():
    """回合结束，所有没成交的挂单作废，冻结的钱和股全部退回。"""
    c = db.连接()
    try:
        for 单 in c.execute("SELECT * FROM orders WHERE status = 'PENDING'").fetchall():
            if 单["side"] == "BUY":
                c.execute("UPDATE users SET frozen_cash = MAX(0, frozen_cash - ?) WHERE id = ?",
                          (单["frozen_amount"], 单["user_id"]))
            else:
                c.execute("UPDATE positions SET available_qty = available_qty + ?,"
                          " frozen_qty = MAX(0, frozen_qty - ?) WHERE user_id = ? AND symbol = ?",
                          (单["quantity"], 单["quantity"], 单["user_id"], 单["symbol"]))
            c.execute("UPDATE orders SET status = 'CANCELED', frozen_amount = 0 WHERE id = ?",
                      (单["id"],))
        c.commit()
    finally:
        c.close()


@app.post("/api/v1/debug/next_day")
def 下一交易日(用户: sqlite3.Row = Depends(认人)):
    """demo 用：把持仓解禁，模拟 T+1。正式版应该随回合切换自动处理。"""
    with 账本锁:
        c = db.连接()
        c.execute("UPDATE positions SET available_qty = quantity WHERE user_id = ?",
                  (用户["id"],))
        c.commit()
        c.close()
    return 成功({"ok": True})


@app.get("/api/v1/ranking")
def 排行榜():
    c = db.连接()
    try:
        结果 = c.execute("SELECT id, nickname, cash, initial_cash FROM users").fetchall()
        成交数 = {r["user_id"]: r["n"] for r in c.execute(
            "SELECT user_id, COUNT(*) AS n FROM orders WHERE status = 'FILLED' "
            "GROUP BY user_id").fetchall()}
        榜 = []
        for r in 结果:
            总 = r["cash"] + 市值(c, r["id"])
            本回合起 = 起始资产.get(r["id"]) or r["initial_cash"]
            榜.append({
                "userId": r["id"], "nickname": r["nickname"], "totalAssets": 总,
                "returnPct": (总 - r["initial_cash"]) / r["initial_cash"],
                "roundReturnPct": (总 - 本回合起) / 本回合起 if 本回合起 else 0,
                "trades": 成交数.get(r["id"], 0),
            })
    finally:
        c.close()
    # 契约：按累计收益率排，并列先比最大回撤再比交易次数。
    # 最大回撤还没算（要历史资产序列），先退一步用交易次数作次级顺序，并列时至少是确定的。
    榜.sort(key=lambda x: (x["returnPct"], x["trades"]), reverse=True)
    for i, 项 in enumerate(榜, 1):
        项["rank"] = i
    return 成功({"items": 榜,
                "updatedAt": datetime.now(北京时间).isoformat(timespec="seconds")})


@app.get("/api/v1/results/me")
def 我的成绩(用户: sqlite3.Row = Depends(认人)):
    c = db.连接()
    try:
        用户 = c.execute("SELECT * FROM users WHERE id = ?", (用户["id"],)).fetchone()
        行们 = c.execute("SELECT * FROM round_results WHERE user_id = ? ORDER BY round_index",
                        (用户["id"],)).fetchall()
        总 = 用户["cash"] + 市值(c, 用户["id"])
        return 成功({
            "rounds": [{"roundIndex": r["round_index"],
                        "eventId": 事件池[(r["round_index"] - 1) % len(事件池)]["id"],
                        "startEquity": r["start_equity"],
                        "endEquity": r["end_equity"],
                        "pnl": r["pnl"],
                        # 收益率必须由后端给，不能让前端自己算：两边算法一旦不一致就会吵
                        "pnlPct": (r["pnl"] / r["start_equity"]) if r["start_equity"] else 0.0,
                        "forcedClose": bool(r["forced_close"])}
                       for r in 行们],
            "final": {"totalAssets": 总,
                      "totalReturnPct": (总 - 用户["initial_cash"]) / 用户["initial_cash"]},
        })
    finally:
        c.close()


# ---------------- 导出成绩 ----------------
# 文档里写的是 GET /admin/export?type=results|trades|orders|events，返回文件流。
# 这里出 CSV 而不是 XLSX：不引第三方库，Excel/WPS 双击就能开，也够交作业用。
def _元(分: int) -> str:
    """给导出的表格用「元」，行内选手看得懂；接口内部仍然全是整数分。"""
    return f"{分 / 100:.2f}"


def _csv文本(表头: list[str], 行们: list[list]) -> str:
    buf = io.StringIO()
    # Excel 只认 \r\n 换行；用 \n 在部分版本里会把整表挤成一行
    写 = csv.writer(buf, lineterminator="\r\n")
    写.writerow(表头)
    写.writerows(行们)
    return buf.getvalue()


@app.get("/api/v1/admin/export")
def 导出成绩(type: str = "results"):
    """导出 CSV。中文不乱码靠两点：utf-8-sig（带 BOM）+ CRLF 换行。"""
    类型 = (type or "results").lower()
    c = db.连接()
    try:
        if 类型 == "results":
            表头 = ["回合", "学号", "昵称", "事件", "期初资产(元)", "期末资产(元)",
                    "盈亏(元)", "收益率", "强制平仓", "结算时刻"]
            行们 = [[r["round_index"], r["student_no"], r["nickname"],
                     事件池[(r["round_index"] - 1) % len(事件池)]["title"],
                     _元(r["start_equity"]), _元(r["end_equity"]), _元(r["pnl"]),
                     f"{r['pnl'] / r['start_equity'] * 100:.2f}%" if r["start_equity"] else "0.00%",
                     "是" if r["forced_close"] else "否", r["settled_at"] or ""]
                    for r in c.execute(
                        "SELECT r.*, u.student_no, u.nickname FROM round_results r "
                        "JOIN users u ON u.id = r.user_id "
                        "ORDER BY r.round_index, r.user_id").fetchall()]

        elif 类型 in ("trades", "orders"):
            成交 = 类型 == "trades"
            条件 = "WHERE o.status = 'FILLED'" if 成交 else ""
            表头 = ["订单号", "学号", "昵称", "代码", "名称", "方向", "类型",
                    "价格(元)", "数量", "已成交", "状态", "费用合计(元)", "创建时间"]
            行们 = []
            for r in c.execute(
                    f"SELECT o.*, u.student_no, u.nickname FROM orders o "
                    f"JOIN users u ON u.id = o.user_id {条件} ORDER BY o.id").fetchall():
                行们.append([
                    r["order_id"], r["student_no"], r["nickname"], r["symbol"],
                    行情表.get(r["symbol"], {}).get("name", r["symbol"]),
                    "买入" if r["side"] == "BUY" else "卖出",
                    "限价" if r["order_type"] == "LIMIT" else "市价",
                    _元(r["price"]), r["quantity"], r["filled_qty"], r["status"],
                    _元(r["fee"]), r["created_at"]])

        elif 类型 == "events":
            表头 = ["事件编码", "标题", "日期", "类别", "方向", "数据精度", "摘要"]
            行们 = [[e["id"], e["title"], e["date"], e["category"], e["direction"],
                     e["dataQuality"], e["summary"]] for e in 事件池]

        else:
            raise 规则拒绝("NOT_FOUND", "type 只能是 results / trades / orders / events")
    finally:
        c.close()

    文本 = _csv文本(表头, 行们)
    文件名 = f"{类型}-{datetime.now(北京时间).strftime('%Y%m%d-%H%M')}.csv"
    return Response(
        content=文本.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{文件名}"',
                 "Cache-Control": "no-store"},
    )


# ---------------- 页面 ----------------
@app.get("/api/v1/version")
def 版本():
    return 成功({"build": 构建})


@app.get("/", response_class=HTMLResponse)
def 首页():
    # no-store：不许浏览器缓存这个页面。
    # 之前页面被缓存成旧版，你看到的按钮和提示全都是上一版的，白折腾半天。
    return HTMLResponse(页面.replace("__BUILD__", 构建), headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })


页面 = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>财富盲盒 · 回合引擎</title>
<style>
body{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;background:#f5f4ef;
     color:#2c2c2a;margin:0;padding:20px}
.wrap{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-start;max-width:1120px;margin:0 auto}
.card{background:#fff;border:1px solid #e2e0d8;border-radius:12px;padding:16px}
.card h2{font-size:13px;font-weight:500;margin:0 0 10px;color:#555}
.row{display:flex;justify-content:space-between;padding:5px 0;font-size:13px;
     border-bottom:1px solid #f4f2ec}
.k{color:#8a8780}.v{font-weight:500;font-variant-numeric:tabular-nums}
select,input{padding:6px;border:1px solid #d5d3cb;border-radius:7px;font-size:13px;
             width:100%;box-sizing:border-box;margin-bottom:7px;background:#fff}
button{padding:7px 12px;border:1px solid #d5d3cb;border-radius:7px;background:#fff;
       font-size:13px;cursor:pointer}
button:hover{background:#f5f4ef}
button:disabled{opacity:.4;cursor:default}
.buy{border-color:#edb9b9;color:#a32d2d}.sell{border-color:#a8d5c5;color:#0f6e56}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:5px 3px;border-bottom:1px solid #f4f2ec}
th{color:#8a8780;font-weight:400}
#bar{height:6px;background:#eee;border-radius:3px;overflow:hidden;margin-top:8px}
#fill{height:100%;background:#0f6e56;width:0%;transition:width .3s linear}
.log{font-size:12px;color:#6b6862;line-height:1.7;max-height:120px;overflow:auto}
</style></head><body>
<div class="wrap">

  <div class="card" style="width:100%">
    <div id="banner" style="font-size:13px;padding:7px 10px;border-radius:8px;margin-bottom:10px;
         background:#f1efe8;color:#5f5e5a">比赛还没开始</div>
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
      <div>
        <span id="st" style="font-size:15px;font-weight:500">未开始</span>
        <span id="ev" style="font-size:13px;color:#8a8780;margin-left:8px"></span>
        <span id="build" style="font-size:11px;color:#c0bdb4;margin-left:8px"></span>
      </div>
      <div>
        <button id="btnPractice" onclick="练习()" style="border-color:#a8d5c5;color:#0f6e56">练习模式（不限时，随时能买）</button>
        <button id="btnStart" onclick="控制('start')">正式比赛（3 回合 × 2 分钟）</button>
        <button id="btnPause" onclick="控制('pause')">暂停</button>
        <button id="btnResume" onclick="控制('resume')">继续</button>
        <button id="btnSkip" onclick="控制('skip')">跳过本回合</button>
        <button onclick="location.href='/api/v1/admin/export?type=results'"
                style="border-color:#cfc3ae;color:#8a6d3b">导出成绩 CSV</button>
        <button onclick="location.href='/api/v1/admin/export?type=trades'"
                style="border-color:#cfc3ae;color:#8a6d3b">导出成交 CSV</button>
      </div>
    </div>
    <div id="bar"><div id="fill"></div></div>
    <div style="font-size:12px;color:#8a8780;margin-top:6px" id="clock"></div>
  </div>

  <div class="card" style="width:250px">
    <h2>当前选手 / 账户</h2>
    <select id="who" onchange="刷新全部()"></select>
    <div class="row"><span class="k">可用现金</span><span class="v" id="cash"></span></div>
    <div class="row"><span class="k">冻结资金</span><span class="v" id="frozen"></span></div>
    <div class="row"><span class="k">持仓市值</span><span class="v" id="mv"></span></div>
    <div class="row"><span class="k">总资产</span><span class="v" id="total"></span></div>
    <div class="row"><span class="k">累计盈亏</span><span class="v" id="pnl"></span></div>
    <div class="row"><span class="k">持股 / 可卖</span><span class="v" id="holdQty"></span></div>
  </div>

  <div class="card" style="width:250px">
    <h2>下单</h2>
    <select id="symbol" onchange="选股票()"></select>

    <div style="background:#faf9f6;border:1px solid #e8e5dc;border-radius:8px;padding:9px 10px;margin-bottom:8px">
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <span style="font-size:12px;color:#8a8780">现价</span>
        <span id="lastPrice" style="font-size:19px;font-weight:500">—</span>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:11px;margin-top:5px;gap:6px">
        <span id="chg">—</span>
        <span id="limits" style="color:#b0ada6;text-align:right">—</span>
      </div>
      <div id="availHint" style="font-size:11px;color:#8a8780;margin-top:6px;line-height:1.5">—</div>
    </div>

    <select id="otype">
      <option value="MARKET">市价单（立刻按现价成交）</option>
      <option value="LIMIT">限价单（价格不到就挂着，等它回来）</option>
    </select>
    <div style="display:flex;gap:6px;align-items:center">
      <input id="price" type="number" step="0.01" placeholder="限价（元）" style="flex:1;min-width:0">
      <button style="padding:8px 8px;font-size:11px;white-space:nowrap" onclick="用现价()">用现价</button>
    </div>
    <input id="qty" type="number" value="100" step="100" placeholder="数量（股）">
    <div style="display:flex;gap:7px">
      <button class="buy" id="btnBuy" onclick="下单('BUY')">买入</button>
      <button class="sell" id="btnSell" onclick="下单('SELL')">卖出</button>
    </div>
    <div id="msg" style="font-size:12px;margin-top:8px;min-height:16px"></div>
    <button style="margin-top:8px;width:100%" onclick="下一日()">进入下一交易日（T+1 解禁）</button>
  </div>

  <div class="card" style="width:340px">
    <h2>实时行情（点一行切换股票）</h2>
    <table><thead><tr><th>股票</th><th>现价</th><th>涨跌</th><th>涨停 / 跌停</th></tr></thead>
    <tbody id="quotes"></tbody></table>
  </div>

  <div class="card" style="width:330px">
    <h2>我的委托</h2>
    <table><thead><tr><th>单号</th><th>方向</th><th>价/量</th><th>状态</th><th></th></tr></thead>
    <tbody id="orders"></tbody></table>
  </div>

  <div class="card" style="width:220px">
    <h2>排行榜</h2>
    <table><tbody id="rank"></tbody></table>
  </div>

  <div class="card" style="width:330px">
    <h2>我的每回合成绩</h2>
    <table><thead><tr><th>回合</th><th>期初</th><th>期末</th><th>收益率</th><th>盈亏</th></tr></thead>
    <tbody id="results"></tbody></table>
  </div>

  <div class="card" style="width:100%">
    <h2>比赛日志</h2>
    <div class="log" id="log"></div>
  </div>

</div>
<script>
const 元 = f => (f/100).toFixed(2);
const 选 = () => ({'Content-Type':'application/json','Authorization':'Bearer '+document.getElementById('who').value});
const 调 = (u, o={}) => fetch(u, {...o, headers: 选()}).then(r => r.json());
let 行情 = {}, 上次总资产 = null, 总资产历史 = [];
// 下单面板要算「最多能买多少股」，得记住最新的可用现金和每只股票的可卖量。
// 不要从页面文字里反解析数字，那是最容易出错的做法。
let 现金分 = 0, 每只可卖 = {}, 手动改过价 = false;

// 这一页是哪个版本构建出来的。服务端每次重启都会换号，
// 对不上就自动刷新，省得你对着旧页面研究半天。
const 本页版本 = "__BUILD__";
build.textContent = '界面版本 ' + 本页版本;
const 版本有效 = 本页版本.indexOf('__') === -1;   // 占位符没被替换就不自动刷新，免得死循环
async function 查版本(){
  if (!版本有效) return;
  try {
    const v = (await (await fetch('/api/v1/version', {cache:'no-store'})).json()).data.build;
    if (v && v !== 本页版本) location.reload();
  } catch (e) {}
}

function 画状态(s){
  const 名 = {IDLE:'未开始', RUNNING:'进行中', PAUSED:'已暂停', SETTLED:'已结算', ENDED:'已锁定'};
  st.textContent = '第 ' + (s.roundIndex||0) + ' / ' + (s.totalRounds||0) + ' 回合 · ' + (名[s.status]||s.status);
  ev.textContent = s.event ? s.event.title : '';

  // 顶部横幅：一句话说清「现在能不能买」。这是整个页面最容易让人困惑的地方。
  const 幅 = {
    RUNNING: ['比赛进行中，可以正常下单', '#e1f5ee', '#0f6e56'],
    PAUSED:  ['比赛已暂停，下单会被拒绝 —— 点「继续」', '#faeeda', '#854f0b'],
    SETTLED: ['比赛已结束，现在下单一定会失败 —— 点「练习模式」继续试', '#fcebeb', '#a32d2d'],
    IDLE:    ['比赛还没开始，下单一定失败 —— 先点「练习模式」或「正式比赛」', '#f1efe8', '#5f5e5a'],
    ENDED:   ['比赛已锁定，只能查看成绩', '#f1efe8', '#5f5e5a'],
  }[s.status] || ['', '#f1efe8', '#5f5e5a'];
  banner.textContent = 幅[0];
  banner.style.background = 幅[1];
  banner.style.color = 幅[2];

  const 总 = s.roundTotalMs || 1;
  fill.style.width = s.status === 'RUNNING' ? (100 - s.roundRemainMs/总*100) + '%'
                   : s.status === 'PAUSED' ? (100 - s.roundRemainMs/总*100) + '%' : '0%';
  clock.textContent = s.status === 'RUNNING' ? '剩余 ' + (s.roundRemainMs/1000).toFixed(1) + ' 秒'
                    : s.status === 'PAUSED' ? '已冻结，剩余 ' + (s.roundRemainMs/1000).toFixed(1) + ' 秒'
                    : '';
  // 重开比赛任何时候都允许（会清空当前这场的成绩），否则练习模式下就再也开不了正式赛
  btnStart.disabled = false;
  btnPause.disabled = s.status !== 'RUNNING';
  btnResume.disabled = s.status !== 'PAUSED';
  btnSkip.disabled = !(s.status === 'RUNNING' || s.status === 'PAUSED');

  // 只有 RUNNING 才能下单。按钮直接灰掉，比点了再报错友好得多。
  const 能下单 = s.status === 'RUNNING';
  btnBuy.disabled = !能下单;
  btnSell.disabled = !能下单;
  if (!能下单) {
    const 提示 = {IDLE:'比赛还没开始', PAUSED:'比赛已暂停', SETTLED:'比赛已结束',
                ENDED:'比赛已锁定'}[s.status] || '当前不能交易';
    msg.textContent = 提示 + '，下单按钮暂时不可用';
    msg.style.color = '#8a8780';
  }
  log.innerHTML = (s.logs||[]).slice().reverse().map(x => '· ' + x).join('<br>');
}

async function 刷新状态(){
  const r = await (await fetch('/api/v1/game/state')).json();
  画状态(r.data);
}

async function 刷新全部(){
  const a = (await 调('/api/v1/account')).data;
  const ps = (await 调('/api/v1/positions')).data.items;
  const q = (await (await fetch('/api/v1/market/quotes')).json()).data.items;
  行情 = {}; q.forEach(x => 行情[x.symbol] = x);
  现金分 = a.availableCash;
  每只可卖 = {}; ps.forEach(x => 每只可卖[x.symbol] = x.availableQty);
  画行情();
  画现价();
  const 总股 = ps.reduce((s,x)=>s+x.quantity,0), 可卖 = ps.reduce((s,x)=>s+x.availableQty,0);
  cash.textContent = 元(a.availableCash) + ' 元'; mv.textContent = 元(a.marketValue) + ' 元';
  total.textContent = 元(a.totalAssets) + ' 元';
  pnl.textContent = 元(a.pnl) + ' 元';
  pnl.style.color = a.pnl >= 0 ? '#a32d2d' : '#0f6e56';
  frozen.textContent = a.frozenCash ? 元(a.frozenCash) + ' 元（挂单占用）' : '0.00 元';
  holdQty.textContent = 总股 + ' / ' + 可卖 + ' 股';

  if (上次总资产 === null) { 上次总资产 = a.totalAssets; }
  上次总资产 = a.totalAssets;

  // 收益率要 3 位小数：比赛里 0.01% 就可能决定名次，两位小数会把差别抹成 0.00%
  const 率 = v => (v*100).toFixed(3) + '%';
  const 率色 = v => v > 0 ? '#a32d2d' : v < 0 ? '#0f6e56' : '#8a8780';

  rank.innerHTML = (await (await fetch('/api/v1/ranking')).json()).data.items
    .map(x => '<tr><td>' + x.rank + '</td><td>' + x.nickname + '</td><td>' + 元(x.totalAssets) +
      '</td><td style="color:' + 率色(x.returnPct) + '">' + 率(x.returnPct) + '</td></tr>').join('');

  const res = (await 调('/api/v1/results/me')).data;
  results.innerHTML = res.rounds.length ? res.rounds.map(r =>
    '<tr><td>' + r.roundIndex + '</td><td>' + 元(r.startEquity) + '</td><td>' + 元(r.endEquity) +
      '</td><td style="color:' + 率色(r.pnlPct) + '">' + 率(r.pnlPct) +
      '</td><td>' + 元(r.pnl) + '</td></tr>').join('')
    : '<tr><td colspan="5" style="color:#b0ada6">还没有回合结束</td></tr>';

  // 委托列表：只有 PENDING 的才给「撤单」按钮
  const os = (await 调('/api/v1/orders?pageSize=6')).data.items;
  orders.innerHTML = os.length ? os.map(o =>
    '<tr><td style="font-size:11px">' + o.orderId.slice(-6) + '</td>' +
    '<td style="color:' + (o.side === 'BUY' ? '#a32d2d' : '#0f6e56') + '">' + (o.side === 'BUY' ? '买' : '卖') + '</td>' +
    '<td>' + 元(o.price) + ' × ' + o.quantity + '</td>' +
    '<td>' + ({PENDING:'挂单中', FILLED:'已成交', CANCELED:'已撤单'}[o.status] || o.status) + '</td>' +
    '<td>' + (o.status === 'PENDING'
      ? '<button style="padding:2px 8px;font-size:11px" onclick="撤单(&quot;' + o.orderId +
        '&quot;)">撤单</button>' : '') +
    '</td></tr>').join('')
    : '<tr><td colspan="5" style="color:#b0ada6">还没有委托</td></tr>';
}

// 涨了红色、跌了绿色：这是 A 股习惯，和欧美正好相反。
function 涨跌色(v){ return v > 0 ? '#a32d2d' : v < 0 ? '#0f6e56' : '#8a8780'; }
function 带符号(v){ return (v > 0 ? '+' : '') + 元(v); }

// 行情表格：每次刷新重画，点一行就切到那只股票
function 画行情(){
  const 行们 = Object.values(行情);
  if (!行们.length) return;
  quotes.innerHTML = 行们.map(x => {
    const 涨 = x.last - x.prevClose;
    const 率 = x.prevClose ? 涨 / x.prevClose : 0;
    const 选中 = x.symbol === symbol.value;
    return '<tr onclick="选这只(&quot;' + x.symbol + '&quot;)" style="cursor:pointer;'
      + (选中 ? 'background:#eef4fb;' : '') + '">'
      + '<td style="font-size:12px">' + x.name
      + '<br><span style="color:#b0ada6;font-size:10px">' + x.symbol + '</span></td>'
      + '<td style="font-size:13px;font-weight:500;color:' + 涨跌色(涨) + '">' + 元(x.last) + '</td>'
      + '<td style="font-size:11px;color:' + 涨跌色(涨) + '">' + 带符号(涨)
      + '<br>' + (率*100).toFixed(2) + '%</td>'
      + '<td style="font-size:10px;color:#b0ada6">' + 元(x.limitUp)
      + '<br>' + 元(x.limitDown) + '</td></tr>';
  }).join('');
}

// 下单卡片里的现价区。股价每 0.3 秒跳一次，这里必须跟着跳，
// 否则你看到的价格和你真正成交的价格不是一回事。
function 画现价(){
  const s = symbol.value, x = 行情[s];
  if (!x) { lastPrice.textContent = '—'; chg.textContent = '—';
            limits.textContent = '—'; availHint.textContent = '—'; return; }
  const 涨 = x.last - x.prevClose;
  const 率 = x.prevClose ? 涨 / x.prevClose : 0;
  const 色 = 涨跌色(涨);

  lastPrice.textContent = 元(x.last) + ' 元';
  lastPrice.style.color = 色;
  chg.textContent = 带符号(涨) + '  ' + (率*100).toFixed(2) + '%';
  chg.style.color = 色;
  limits.textContent = '涨停 ' + 元(x.limitUp) + ' / 跌停 ' + 元(x.limitDown);

  // 没手动改过价就跟着现价走；改过就不覆盖，免得边打字边被冲掉
  if (!手动改过价) price.value = (x.last / 100).toFixed(2);

  const 一手 = x.last * 100;
  const 手数 = 一手 > 0 ? Math.floor(现金分 / 一手) : 0;
  availHint.innerHTML = '按现价最多约买 <b>' + (手数 * 100) + '</b> 股（' + 手数 + ' 手）'
    + '　该股可卖 <b>' + (每只可卖[s] || 0) + '</b> 股';
}

function 选股票(){
  手动改过价 = false;
  画行情();
  画现价();
}

function 选这只(代码){
  symbol.value = 代码;
  选股票();
}

function 用现价(){
  手动改过价 = false;
  画现价();
}

// 行情单独一个更快的循环：股价 0.3 秒变一次，跟着状态一起刷太慢
async function 刷新行情(){
  try {
    const q = (await (await fetch('/api/v1/market/quotes', {cache:'no-store'})).json()).data.items;
    行情 = {}; q.forEach(x => 行情[x.symbol] = x);
    画行情();
    画现价();
  } catch (e) {}
}

// 错误码对机器友好，对人极不友好。前端必须把 code 翻译成「你该怎么办」。
const 怎么办 = {
  ROUND_NOT_RUNNING:      '比赛没开始、暂停中或已结束 —— 点左上角「开始比赛」',
  T1_RESTRICTED:          '今天买的股票今天不能卖（T+1）—— 点「进入下一交易日」解禁',
  INSUFFICIENT_CASH:      '可用现金不够（挂单冻住的钱不能再花）',
  INSUFFICIENT_POSITION:  '持仓不够卖',
  INVALID_LOT:            '数量必须是 100 的整数倍',
  LIMIT_UP:               '已涨停，买不进去',
  LIMIT_DOWN:             '已跌停，卖不出去',
  SYMBOL_NOT_TRADABLE:    '这只股票不在本回合的股票池里',
  INVALID_PRICE:          '限价单必须填价格',
  ORDER_NOT_CANCELABLE:   '这笔单已经成交或已撤，撤不了',
  DUPLICATE_CLIENT_ORDER_ID: '这笔单提交过两次（网络卡了一下），不会重复成交',
  BAD_REQUEST:            '请求参数不对（这条是程序问题，不是你的操作问题）',
  INTERNAL_ERROR:         '服务端出错了，看一下黑色窗口里的报错',
};

function 报错(文字){ msg.textContent = 文字; msg.style.color = '#a32d2d'; }

async function 下单(side){
  const t = otype.value;

  // 先在页面上自检一遍。以前这里直接读输入框，一旦读到空值就变成 NaN，
  // 发出去是 "quantity": null，服务器回一个 {detail:[...]}，页面就显示成
  // 「undefined [undefined]」——看着像后端崩了，其实是前端没拦住。
  const 数量 = Number(qty.value);
  const 标 = symbol.value;
  if (!标) { 报错('还没选股票'); return; }
  if (!Number.isFinite(数量) || 数量 <= 0) { 报错('数量没填对：填 100 的整数倍，比如 100'); return; }
  if (数量 % 100 !== 0) { 报错('A 股必须 100 股整手，' + 数量 + ' 股不行'); return; }

  const body = {clientOrderId:'c'+Date.now()+Math.random().toString(16).slice(2,6),
    symbol: 标, side, type: t, quantity: 数量};
  if (t === 'LIMIT') {
    const 元价 = Number(price.value);
    if (!Number.isFinite(元价) || 元价 <= 0) { 报错('限价单要先填价格（单位是元，比如 24.52）'); return; }
    // 页面用「元」，接口收「分」。必须 Math.round：24.52×100 在浮点里是
    // 2451.9999999999995，直接取整会变成 2451，差一分钱。
    body.price = Math.round(元价 * 100);              // 市价单不传价，契约允许
  }

  const r = await 调('/api/v1/orders', {method:'POST', body: JSON.stringify(body)});
  if (r.code === 0) {
    msg.textContent = r.data.status === 'PENDING' ? r.data.message
      : '成交 @' + 元(r.data.avgPrice) + ' 费用 ' + 元(r.data.fee + r.data.tax + r.data.transferFee);
    msg.style.color = '#0f6e56';
  } else if (r.code === undefined) {
    // 服务端没按 {code,message} 的约定回话，把原文露出来，别让人猜
    报错('服务端返回了意外内容：' + JSON.stringify(r).slice(0, 160));
  } else {
    msg.textContent = (怎么办[r.code] || r.message) + ' [' + r.code + ']';
    msg.style.color = '#a32d2d';
  }
  刷新全部();
}

async function 撤单(orderId){
  const r = await 调('/api/v1/orders/' + orderId, {method:'DELETE'});
  msg.textContent = r.code === 0 ? '已撤单，冻结的钱/股已退回' : '撤单失败：' + r.code;
  msg.style.color = r.code === 0 ? '#0f6e56' : '#a32d2d';
  刷新全部();
}

async function 下一日(){
  await 调('/api/v1/debug/next_day', {method:'POST'});
  msg.textContent = '持仓已解禁'; msg.style.color = '#0f6e56'; 刷新全部();
}

async function 练习(){
  const r = await (await fetch('/api/v1/admin/game/practice', {method:'POST'})).json();
  if (r.code === 0) {
    画状态(r.data);
    msg.textContent = '练习模式已开启：不限时，可以随便买卖';
    msg.style.color = '#0f6e56';
  } else { alert(r.message); }
  刷新全部();
}

async function 控制(动作){
  const r = await (await fetch('/api/v1/admin/game/' + 动作, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: 动作 === 'start' ? JSON.stringify({rounds:3, seconds:120}) : undefined})).json();
  if (r.code === 0) 画状态(r.data); else alert(r.message);
  刷新全部();
}

(async function 启动(){
  const u = (await (await fetch('/api/v1/users')).json()).data;
  who.innerHTML = u.map(x => '<option value="' + x.id + '">' + x.nickname + '</option>').join('');
  const q = (await (await fetch('/api/v1/market/quotes')).json()).data.items;
  symbol.innerHTML = q.map(x => '<option value="' + x.symbol + '">' + x.symbol + ' ' +
    x.name + '</option>').join('');
  行情 = {}; q.forEach(x => 行情[x.symbol] = x);
  选股票();
  // 用户一旦手动输入限价，就不再让现价自动覆盖他的数字
  price.addEventListener('input', () => { 手动改过价 = true; });
  await 刷新状态();
  刷新全部();
  setInterval(刷新状态, 400);
  setInterval(刷新行情, 400);
  setInterval(刷新全部, 1500);
  setInterval(查版本, 3000);
})();
</script></body></html>"""
