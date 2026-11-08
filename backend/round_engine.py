# 回合引擎：只管"现在第几回合、还剩多少毫秒、能不能交易"。
# 不碰数据库、不碰行情、不碰网页。时钟可以换，所以能在测试里把时间"快进"。

import time


class 回合引擎:
    def __init__(self, 时钟=time.monotonic):
        self.时钟 = 时钟
        self.状态 = "IDLE"        # IDLE 未开始 / RUNNING / PAUSED / SETTLED 已结算 / ENDED 已锁定
        self.回合序号 = 0
        self.总回合数 = 0
        self.回合毫秒 = 0
        self._本轮开始 = 0.0
        self._暂停剩余毫秒 = None

    def 开赛(self, 总回合数: int, 回合毫秒: int):
        if 总回合数 <= 0 or 回合毫秒 <= 0:
            raise ValueError("回合数和每回合时长必须是正数")
        self.总回合数 = 总回合数
        self.回合毫秒 = 回合毫秒
        self.回合序号 = 1
        self._本轮开始 = self.时钟()
        self._暂停剩余毫秒 = None
        self.状态 = "RUNNING"

    def 剩余毫秒(self) -> int:
        if self.状态 == "PAUSED":
            return int(self._暂停剩余毫秒)
        if self.状态 != "RUNNING":
            return 0
        已用 = (self.时钟() - self._本轮开始) * 1000
        return max(0, int(self.回合毫秒 - 已用))

    def 暂停(self) -> bool:
        if self.状态 != "RUNNING":
            return False
        self._暂停剩余毫秒 = self.剩余毫秒()      # 把剩余时间"冻"下来
        self.状态 = "PAUSED"
        return True

    def 继续(self) -> bool:
        if self.状态 != "PAUSED":
            return False
        # 把起点往前挪，让"已用时间"仍然是 总时长-剩余，倒计时就不会跳
        self._本轮开始 = self.时钟() - (self.回合毫秒 - self._暂停剩余毫秒) / 1000
        self._暂停剩余毫秒 = None
        self.状态 = "RUNNING"
        return True

    def 进入下一回合(self) -> bool:
        """当前回合已经结算完，推进到下一回合。返回 False 表示整场比赛结束。"""
        if self.回合序号 >= self.总回合数:
            self.状态 = "SETTLED"
            return False
        self.回合序号 += 1
        self._本轮开始 = self.时钟()
        self._暂停剩余毫秒 = None
        self.状态 = "RUNNING"
        return True

    def 锁定(self) -> bool:
        """结束并锁定：关门，之后谁都不能再交易，也不再推进回合。

        和「结算」的区别要说清：结算要强制平仓、算成绩、把钱带下去；
        锁定只是把门关上，账还是现在这个账。用于比赛被临时叫停的场合。
        """
        if self.状态 in ("IDLE", "ENDED"):
            return False
        self.状态 = "ENDED"
        self._暂停剩余毫秒 = None
        return True

    def 结束(self):
        self.状态 = "ENDED"

    def 可交易(self) -> bool:
        return self.状态 == "RUNNING"
