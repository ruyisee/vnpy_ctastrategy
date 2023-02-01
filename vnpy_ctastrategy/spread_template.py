# -*- coding: utf-8 -*-
"""
@Time    : 2023/1/31 9:13
@Author  : fsksf
@File    : spread_template.py
"""
from collections import defaultdict

from typing import Any, Dict
from vnpy import WORK_DIR
from vnpy.trader.object import TickData
from vnpy_ctastrategy.template import CtaTemplate


class CtaSpreadTemplate(CtaTemplate):

    leg_a = ''
    leg_b = ''
    spread_pos_name = ''
    leg_a_mult = 100
    leg_b_mult = 100
    target_pos = {}

    author = "kangyuqiang"
    parameters = ['leg_a_mult', 'leg_b_mult', 'spread_pos_name', 'leg_a', 'leg_b']
    variables = ['target_pos']

    def __init__(
        self,
        cta_engine: Any,
        strategy_name: str,
        vt_symbol: str,
        setting: dict,
        trade_basket=False
    ):
        """

        :param cta_engine:
        :param strategy_name:
        :param vt_symbol: 两只标的用逗号隔开， 如 'IH2301.CFFEX,159845.SSE'
        :param setting:
        :param trade_basket:
        """
        super().__init__(cta_engine=cta_engine, strategy_name=strategy_name, vt_symbol=vt_symbol, setting=setting,
                         trade_basket=trade_basket)

        self.pos = defaultdict(lambda: 0)
        self.target_pos = {'leg_a': 0, 'leg_b': 0}
        self.leg_a_pos = 0
        self.leg_b_pos = 0
        self.ticks: Dict[str, TickData] = {}

    def get_symbols(self):
        return self.leg_a, self.leg_b

    def on_tick(self, tick: TickData):
        self.ticks[tick.vt_symbol] = tick
        data = self.get_spread_pos_factor()
        spread_pos_data = data[self.spread_pos_name]
        self.target_pos['leg_a'] = target_a = spread_pos_data['legA']['targetPos']
        self.target_pos['leg_b'] = target_b = spread_pos_data['legB']['targetPos']
        leg_a_lack = target_a - self.pos[self.leg_a] / self.leg_a_mult
        leg_b_lack = target_b - self.pos[self.leg_b] / self.leg_b_mult
        if leg_b_lack == leg_a_lack == 0:
            return
        print(leg_a_lack, leg_b_lack, self.leg_a_mult, self.leg_b_mult)
        self.handle_tick(leg_a_lack, leg_b_lack)

    def handle_tick(self, leg_a_lack, leg_b_lack):
        """
        只需要在这里写买卖逻辑
        :param leg_a_lack: legA缺口
        :param leg_b_lack: legB缺口
        :return:
        """
        pass

    def get_report_data(self):
        pos_a = self.pos[self.leg_a]
        pos_b = self.pos[self.leg_b]
        return {
            'name': self.strategy_name,
            'running': self.trading,
            'legA': self.leg_a,
            'legB': self.leg_b,
            'posA': pos_a,
            'posB': pos_b,
            'multA': self.leg_a_mult,
            'multB': self.leg_b_mult,
            'targetA': self.target_pos['leg_a'],
            'targetB': self.target_pos['leg_b'],
            'statue':
                'warning'
                if pos_a / self.leg_a_mult + pos_b / self.leg_b_mult != 0
                else 'success',
            'client': WORK_DIR
        }
