# 数据库：只负责存和取，不写业务规则。
# 用 Python 自带的 sqlite3，不用装任何东西，整个数据库就是一个文件 wealth.db。

import sqlite3
import time
from pathlib import Path

数据库文件 = Path(__file__).with_name("wealth.db")

建表语句 = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    student_no    TEXT UNIQUE NOT NULL,
    nickname      TEXT NOT NULL,
    cash          INTEGER NOT NULL,        -- 现金（分）。注意：含被冻结的部分
    initial_cash  INTEGER NOT NULL,        -- 开赛资金（分）
    frozen_cash   INTEGER NOT NULL DEFAULT 0   -- cash 里被挂单冻住的部分，不是额外加的钱
);

CREATE TABLE IF NOT EXISTS positions (
    user_id       INTEGER NOT NULL,
    symbol        TEXT NOT NULL,
    quantity      INTEGER NOT NULL DEFAULT 0,   -- 总持股
    available_qty INTEGER NOT NULL DEFAULT 0,   -- 可卖（T+1 后才有）
    frozen_qty    INTEGER NOT NULL DEFAULT 0,   -- 挂了卖单被锁住的股数
    avg_cost      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, symbol)
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        TEXT,                  -- 对外的单号，形如 ORD000000000012（契约要求字符串）
    user_id         INTEGER NOT NULL,
    client_order_id TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    order_type      TEXT NOT NULL DEFAULT 'LIMIT',   -- LIMIT / MARKET
    price           INTEGER NOT NULL,                -- 限价；市价单这里存下单时的市价快照
    quantity        INTEGER NOT NULL,
    filled_qty      INTEGER NOT NULL DEFAULT 0,
    fee             INTEGER NOT NULL DEFAULT 0,
    frozen_amount   INTEGER NOT NULL DEFAULT 0,      -- 挂单期间冻住的钱（分）
    status          TEXT NOT NULL,
    reject_reason   TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (user_id, client_order_id)      -- 防重复提交的保险栓
);

CREATE TABLE IF NOT EXISTS round_results (
    user_id      INTEGER NOT NULL,
    round_index  INTEGER NOT NULL,
    start_equity INTEGER NOT NULL,        -- 本回合开始时的总资产
    end_equity   INTEGER NOT NULL,        -- 强制平仓后的总资产
    pnl          INTEGER NOT NULL,
    forced_close INTEGER NOT NULL DEFAULT 1,
    settled_at   TEXT NOT NULL DEFAULT '',   -- 这一回合结算的真实时刻，资产曲线的横轴要用
    PRIMARY KEY (user_id, round_index)
);

-- 只有一行（id 恒为 1）：把「比赛进行到哪」存下来，服务重启后能接着跑。
-- 以前这些只在内存里，窗口一关就退回「未开始」，钱和订单还在库里，比赛却没了。
CREATE TABLE IF NOT EXISTS game_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    status       TEXT    NOT NULL,
    round_index  INTEGER NOT NULL,
    total_rounds INTEGER NOT NULL,
    round_ms     INTEGER NOT NULL,
    remain_ms    INTEGER NOT NULL,           -- 存下来的那一瞬间还剩多久；停机时间当暂停处理
    started_at   TEXT    NOT NULL DEFAULT '',
    start_assets TEXT    NOT NULL DEFAULT '{}',   -- JSON：每个选手本回合的起始资产
    logs         TEXT    NOT NULL DEFAULT '[]'    -- JSON：最近若干条比赛日志
);
"""


def 连接() -> sqlite3.Connection:
    # timeout：被别人锁住时最多等 10 秒再报错，而不是立刻就失败。
    # 注意：这里**不要**再执行 PRAGMA journal_mode=WAL。WAL 是写在数据库文件里的
    # 长期属性，初始化时设置一次就够了；每次连接都设一次，在多个线程同时开连接时
    # 会互相抢独占锁，偶尔就会冒出「attempt to write a readonly database」。
    c = sqlite3.connect(数据库文件, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=8000")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


# 这些错误是「一时的」：另一个进程正在写、杀毒软件临时扫描文件、刚好在做检查点。
# 等几十毫秒再试通常就好了，不该让选手点的那一笔单白白失败。
_可重试 = ("locked", "busy", "readonly")


def 重试(fn, 次数: int = 4):
    """把一段数据库操作包起来，遇到「一时的」错误自动重试。

    为什么重试是安全的：一次下单要么在 commit 那一刻整体生效，要么整体没生效，
    不存在「做了一半」。再加上 client_order_id 的唯一索引，即使上一把其实已经
    写进去了，重试也只会被唯一约束挡下来，绝不会变成两笔。
    """
    最后错误 = None
    for i in range(次数):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            文案 = str(e).lower()
            if i == 次数 - 1 or not any(k in 文案 for k in _可重试):
                raise                       # 不是可重试的错误，立刻抛出，别掩盖真问题
            最后错误 = e
            time.sleep(0.05 * (i + 1))       # 50ms / 100ms / 150ms 递增，给对面时间松手
    raise 最后错误


def _补列(c: sqlite3.Connection, 表: str, 列们: list[tuple[str, str]]):
    """老库升级用：表已经存在时 CREATE TABLE IF NOT EXISTS 不会加新列，
    所以要挨个试 ADD COLUMN，已经有了就跳过（SQLite 报「duplicate column」）。"""
    现有 = {r["name"] for r in c.execute(f"PRAGMA table_info({表})")}
    for 列名, 定义 in 列们:
        if 列名 not in 现有:
            c.execute(f"ALTER TABLE {表} ADD COLUMN {列名} {定义}")
    c.commit()


def 初始化():
    """第一次运行时建表，并塞三个测试选手。重复运行不会重复插入。"""
    c = 连接()
    c.execute("PRAGMA journal_mode=WAL")   # 允许一边读一边写。只需设一次，持久保存在库里
    c.executescript(建表语句)
    _补列(c, "users", [("frozen_cash", "INTEGER NOT NULL DEFAULT 0")])
    _补列(c, "positions", [("frozen_qty", "INTEGER NOT NULL DEFAULT 0")])
    _补列(c, "orders", [("order_id", "TEXT"),
                        ("order_type", "TEXT NOT NULL DEFAULT 'LIMIT'"),
                        ("filled_qty", "INTEGER NOT NULL DEFAULT 0"),
                        ("frozen_amount", "INTEGER NOT NULL DEFAULT 0")])
    _补列(c, "round_results", [("settled_at", "TEXT NOT NULL DEFAULT ''")])
    # 老订单补上单号，并建唯一索引（挂单要靠它对上号去撤）
    c.execute("UPDATE orders SET order_id = 'ORD' || printf('%012d', id) "
              "WHERE order_id IS NULL OR order_id = ''")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id)")
    c.commit()
    已有 = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    if 已有 == 0:
        开赛资金 = 10_000_000      # 10 万元
        选手 = [("20260001", "小王"), ("20260002", "小李"), ("20260003", "小张")]
        for 学号, 昵称 in 选手:
            c.execute(
                "INSERT INTO users (student_no, nickname, cash, initial_cash) VALUES (?, ?, ?, ?)",
                (学号, 昵称, 开赛资金, 开赛资金),
            )
            c.execute(
                "INSERT INTO positions (user_id, symbol, quantity, available_qty, avg_cost) "
                "SELECT id, '600030', 0, 0, 0 FROM users WHERE student_no = ?",
                (学号,),
            )
        c.commit()
    c.close()
