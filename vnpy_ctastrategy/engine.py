""""""

import importlib
import json
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, List, Iterable
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from copy import copy

import requests
from tzlocal import get_localzone
from glob import glob
from vnpy.trader.setting import SETTINGS
from vnpy.event import Event, EventEngine
from vnpy.trader.engine import BaseEngine, MainEngine
from vnpy.trader.object import (
    OrderRequest,
    SubscribeRequest,
    HistoryRequest,
    LogData,
    TickData,
    BarData,
    ContractData
)
from vnpy.trader.event import (
    EVENT_TICK,
    EVENT_ORDER,
    EVENT_TRADE,
    EVENT_POSITION
)
from vnpy.trader.constant import (
    Direction,
    OrderType,
    Interval,
    Exchange,
    Offset,
    Status
)
from vnpy.trader.utility import load_json, save_json, extract_vt_symbol, round_to
from vnpy.trader.converter import OffsetConverter
from vnpy.trader.database import BaseDatabase, get_database
from vnpy.trader.datafeed import BaseDatafeed, get_datafeed

from .base import (
    APP_NAME,
    EVENT_CTA_LOG,
    EVENT_CTA_STRATEGY,
    EVENT_CTA_STOPORDER,
    EngineType,
    StopOrder,
    StopOrderStatus,
    STOPORDER_PREFIX
)
from .template import CtaTemplate
from .spread_template import CtaSpreadTemplate
from .etf_template import ETFTemplate


STOP_STATUS_MAP = {
    Status.SUBMITTING: StopOrderStatus.WAITING,
    Status.NOTTRADED: StopOrderStatus.WAITING,
    Status.PARTTRADED: StopOrderStatus.TRIGGERED,
    Status.ALLTRADED: StopOrderStatus.TRIGGERED,
    Status.CANCELLED: StopOrderStatus.CANCELLED,
    Status.REJECTED: StopOrderStatus.CANCELLED
}

LOCAL_TZ = get_localzone()


class CtaEngine(BaseEngine):
    """"""

    engine_type = EngineType.LIVE  # live trading engine

    setting_filename = "cta_strategy_setting.json"
    data_filename = "cta_strategy_data.json"

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine):
        """"""
        super(CtaEngine, self).__init__(
            main_engine, event_engine, APP_NAME)

        self.strategy_setting = {}  # strategy_name: dict
        self.strategy_data = {}     # strategy_name: dict

        self.classes = {}           # class_name: stategy_class
        self.strategies = {}        # strategy_name: strategy

        self.symbol_strategy_map = defaultdict(
            list)                   # vt_symbol: strategy list
        self.orderid_strategy_map = {}  # vt_orderid: strategy
        self.strategy_orderid_map = defaultdict(
            set)                    # strategy_name: orderid list

        self.stop_order_count = 0   # for generating stop_orderid
        self.stop_orders = {}       # stop_orderid: stop_order

        self.init_executor = ThreadPoolExecutor(max_workers=1)

        self.rq_client = None
        self.rq_symbols = set()

        self.vt_tradeids = set()    # for filtering duplicate trade

        self.offset_converter = OffsetConverter(self.main_engine)

        self.database: BaseDatabase = get_database()
        self.datafeed: BaseDatafeed = get_datafeed()

    def init_engine(self):
        """
        """
        self.init_datafeed()
        self.load_strategy_class()
        self.load_strategy_setting()
        self.load_strategy_data()
        self.register_event()
        self.write_log("CTA策略引擎初始化成功")

    def close(self):
        """"""
        self.stop_all_strategies()

    def register_event(self):
        """"""
        self.event_engine.register(EVENT_TICK, self.process_tick_event)
        self.event_engine.register(EVENT_ORDER, self.process_order_event)
        self.event_engine.register(EVENT_TRADE, self.process_trade_event)
        self.event_engine.register(EVENT_POSITION, self.process_position_event)

    def init_datafeed(self):
        """
        Init datafeed client.
        """
        result = self.datafeed.init()
        if result:
            self.write_log("数据服务初始化成功")

    def query_bar_from_datafeed(
        self, symbol: str, exchange: Exchange, interval: Interval, start: datetime, end: datetime
    ):
        """
        Query bar data from datafeed.
        """
        req = HistoryRequest(
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            start=start,
            end=end
        )
        data = self.datafeed.query_bar_history(req)
        return data

    def process_tick_event(self, event: Event):
        """"""
        tick = event.data

        strategies = self.symbol_strategy_map[tick.vt_symbol]
        if not strategies:
            return
        self.offset_converter.before_handle_tick(tick)
        self.check_stop_order(tick)

        for strategy in strategies:
            if strategy.inited:
                self.call_strategy_func(strategy, strategy.on_tick, tick)

    def process_order_event(self, event: Event):
        """"""
        order = event.data
        if isinstance(order, Iterable):
            for _order in order:
                self._on_order(_order)
        else:
            self._on_order(order)

    def _on_order(self, order):
        self.offset_converter.update_order(order)

        strategy = self.orderid_strategy_map.get(order.vt_orderid, None)
        if not strategy:
            return

        # Remove vt_orderid if order is no longer active.
        vt_orderids = self.strategy_orderid_map[strategy.strategy_name]
        if order.vt_orderid in vt_orderids and not order.is_active():
            vt_orderids.remove(order.vt_orderid)

        # For server stop order, call strategy on_stop_order function
        if order.type == OrderType.STOP:
            so = StopOrder(
                vt_symbol=order.vt_symbol,
                direction=order.direction,
                offset=order.offset,
                price=order.price,
                volume=order.volume,
                stop_orderid=order.vt_orderid,
                strategy_name=strategy.strategy_name,
                datetime=order.datetime,
                status=STOP_STATUS_MAP[order.status],
                vt_orderids=[order.vt_orderid],
            )
            self.call_strategy_func(strategy, strategy.on_stop_order, so)

        # Call strategy on_order function
        self.call_strategy_func(strategy, strategy.on_order, order)

    def process_trade_event(self, event: Event):
        """"""
        trade = event.data
        if isinstance(trade, Iterable):
            for _trade in trade:
                self._on_trade(_trade)
        else:
            self._on_trade(trade)

    def _on_trade(self, trade):

        # Filter duplicate trade push
        if trade.vt_tradeid in self.vt_tradeids:
            return
        self.vt_tradeids.add(trade.vt_tradeid)

        self.offset_converter.update_trade(trade)

        strategy = self.orderid_strategy_map.get(trade.vt_orderid, None)
        if not strategy:
            return

        # Update strategy pos before calling on_trade method
        if isinstance(strategy.pos, dict):
            if trade.direction in (Direction.LONG, Direction.LoanBuy, Direction.EnBuyBack):
                strategy.pos[trade.vt_symbol] = strategy.pos[trade.vt_symbol] + trade.volume
            elif trade.direction in (Direction.SHORT, Direction.LoanSell, Direction.PreBookLoanSell,
                                     Direction.EnSellBack):
                strategy.pos[trade.vt_symbol] = strategy.pos[trade.vt_symbol] - trade.volume
            else:
                print(trade.direction)
                raise NotImplementedError()
        else:
            if trade.direction in (Direction.LONG, Direction.LoanBuy, Direction.EnBuyBack):
                strategy.pos += trade.volume
            elif trade.direction in (Direction.SHORT, Direction.LoanSell, Direction.PreBookLoanSell,
                                     Direction.EnSellBack):
                strategy.pos -= trade.volume
            else:
                raise NotImplementedError()

        self.call_strategy_func(strategy, strategy.on_trade, trade)

        # Sync strategy variables to data file
        self.sync_strategy_data(strategy)

        # Update GUI
        self.put_strategy_event(strategy)

    def process_position_event(self, event: Event):
        """"""
        position = event.data
        if isinstance(position, Iterable):
            for _pos in position:
                self._on_position(_pos)
        else:
            self._on_position(position)

    def _on_position(self, position):

        self.offset_converter.update_position(position)

    def check_stop_order(self, tick: TickData):
        """"""
        for stop_order in list(self.stop_orders.values()):
            if stop_order.vt_symbol != tick.vt_symbol:
                continue

            long_triggered = (
                stop_order.direction == Direction.LONG and tick.last_price >= stop_order.price
            )
            short_triggered = (
                stop_order.direction == Direction.SHORT and tick.last_price <= stop_order.price
            )

            if long_triggered or short_triggered:
                strategy = self.strategies[stop_order.strategy_name]

                # To get excuted immediately after stop order is
                # triggered, use limit price if available, otherwise
                # use ask_price_5 or bid_price_5
                if stop_order.direction == Direction.LONG:
                    if tick.limit_up:
                        price = tick.limit_up
                    else:
                        price = tick.ask_price_5
                else:
                    if tick.limit_down:
                        price = tick.limit_down
                    else:
                        price = tick.bid_price_5

                contract = self.main_engine.get_contract(stop_order.vt_symbol)

                vt_orderids = self.send_limit_order(
                    strategy,
                    contract,
                    stop_order.direction,
                    stop_order.offset,
                    price,
                    stop_order.volume,
                    stop_order.lock,
                    stop_order.net
                )

                # Update stop order status if placed successfully
                if vt_orderids:
                    # Remove from relation map.
                    self.stop_orders.pop(stop_order.stop_orderid)

                    strategy_vt_orderids = self.strategy_orderid_map[strategy.strategy_name]
                    if stop_order.stop_orderid in strategy_vt_orderids:
                        strategy_vt_orderids.remove(stop_order.stop_orderid)

                    # Change stop order status to cancelled and update to strategy.
                    stop_order.status = StopOrderStatus.TRIGGERED
                    stop_order.vt_orderids = vt_orderids

                    self.call_strategy_func(
                        strategy, strategy.on_stop_order, stop_order
                    )
                    self.put_stop_order_event(stop_order)

    def send_server_order(
        self,
        strategy: CtaTemplate,
        contract: ContractData,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: float,
        type: OrderType,
        lock: bool,
        net: bool,
        signal_price
    ):
        """
        Send a new order to server.
        """
        if signal_price is None:
            signal_price = price
        # Create request and send order.
        original_req = OrderRequest(
            symbol=contract.symbol,
            exchange=contract.exchange,
            direction=direction,
            offset=offset,
            type=type,
            price=price,
            signal_price=signal_price,
            volume=volume,
            reference=f"{APP_NAME}_{strategy.strategy_name}"
        )

        # Convert with offset converter
        req_list = self.offset_converter.convert_order_request(original_req, lock, net)

        # Send Orders
        vt_orderids = []

        for req in req_list:
            if req.direction in (Direction.BUY_BASKET, Direction.SELL_BASKET):
                req_l, vt_orderid_l = self.main_engine.send_basket_order(req, contract.gateway_name)
            else:
                vt_orderid = self.main_engine.send_order(req, contract.gateway_name)
                req_l = [req]
                if isinstance(vt_orderid, (tuple, list, set)):
                    vt_orderid_l = vt_orderid
                else:
                    vt_orderid_l = [vt_orderid, ]
            # Check if sending order successful
            for rq, vt_o_id in zip(req_l, vt_orderid_l):
                if not vt_o_id:
                    continue
                vt_orderids.append(vt_o_id)
                if rq is None or vt_o_id is None :
                    continue
                self.offset_converter.update_order_request(rq, vt_o_id)

                # Save relationship between orderid and strategy.
                self.orderid_strategy_map[vt_o_id] = strategy
                self.strategy_orderid_map[strategy.strategy_name].add(vt_o_id)

        return vt_orderids

    def send_limit_order(
        self,
        strategy: CtaTemplate,
        contract: ContractData,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: float,
        lock: bool,
        net: bool,
        signal_price
    ):
        """
        Send a limit order to server.
        """
        return self.send_server_order(
            strategy=strategy,
            contract=contract,
            direction=direction,
            offset=offset,
            price=price,
            volume=volume,
            type=OrderType.LIMIT,
            lock=lock,
            net=net,
            signal_price=signal_price
        )

    def send_server_stop_order(
        self,
        strategy: CtaTemplate,
        contract: ContractData,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: float,
        lock: bool,
        net: bool,
        signal_price
    ):
        """
        Send a stop order to server.

        Should only be used if stop order supported
        on the trading server.
        """
        return self.send_server_order(
            strategy,
            contract,
            direction,
            offset,
            price,
            volume,
            OrderType.STOP,
            lock,
            net,
            signal_price
        )

    def send_order_many(self, strategy, req_list: List[OrderRequest]):
        order_ids = self.main_engine.send_order_many(req_list)
        for vt_o_id in order_ids:
            self.orderid_strategy_map[vt_o_id] = strategy
            self.strategy_orderid_map[strategy.strategy_name].add(vt_o_id)
        return order_ids

    def send_local_stop_order(
        self,
        strategy: CtaTemplate,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: float,
        lock: bool,
        net: bool,
        signal_price
    ):
        """
        Create a new local stop order.
        """
        self.stop_order_count += 1
        stop_orderid = f"{STOPORDER_PREFIX}.{self.stop_order_count}"

        stop_order = StopOrder(
            vt_symbol=strategy.vt_symbol,
            direction=direction,
            offset=offset,
            price=price,
            volume=volume,
            stop_orderid=stop_orderid,
            strategy_name=strategy.strategy_name,
            datetime=datetime.now(LOCAL_TZ),
            lock=lock,
            net=net,
            signal_price=signal_price
        )

        self.stop_orders[stop_orderid] = stop_order

        vt_orderids = self.strategy_orderid_map[strategy.strategy_name]
        vt_orderids.add(stop_orderid)

        self.call_strategy_func(strategy, strategy.on_stop_order, stop_order)
        self.put_stop_order_event(stop_order)

        return [stop_orderid]

    def cancel_server_order(self, strategy: CtaTemplate, vt_orderid: str):
        """
        Cancel existing order by vt_orderid.
        """
        order = self.main_engine.get_order(vt_orderid)
        if not order:
            self.write_log(f"撤单失败，找不到委托{vt_orderid}", strategy)
            return

        req = order.create_cancel_request()
        self.main_engine.cancel_order(req, order.gateway_name)

    def cancel_local_stop_order(self, strategy: CtaTemplate, stop_orderid: str):
        """
        Cancel a local stop order.
        """
        stop_order = self.stop_orders.get(stop_orderid, None)
        if not stop_order:
            return
        strategy = self.strategies[stop_order.strategy_name]

        # Remove from relation map.
        self.stop_orders.pop(stop_orderid)

        vt_orderids = self.strategy_orderid_map[strategy.strategy_name]
        if stop_orderid in vt_orderids:
            vt_orderids.remove(stop_orderid)

        # Change stop order status to cancelled and update to strategy.
        stop_order.status = StopOrderStatus.CANCELLED

        self.call_strategy_func(strategy, strategy.on_stop_order, stop_order)
        self.put_stop_order_event(stop_order)

    def send_order(
        self,
        strategy: CtaTemplate,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: float,
        stop: bool,
        lock: bool,
        net: bool,
        signal_price,
        vt_symbol=None,
        type=OrderType.LIMIT
    ):
        """
        """
        if vt_symbol is None:
            vt_symbol = strategy.vt_symbol
        contract = self.main_engine.get_contract(vt_symbol)
        if not contract:
            self.write_log(f"委托失败，找不到合约：{vt_symbol}", strategy)
            return ""

        # Round order price and volume to nearest incremental value
        price = round_to(price, contract.pricetick)
        volume = round_to(volume, contract.min_volume)

        if stop:
            if contract.stop_supported:
                return self.send_server_stop_order(
                    strategy, contract, direction, offset, price, volume, lock, net, signal_price
                )
            else:
                return self.send_local_stop_order(
                    strategy, direction, offset, price, volume, lock, net, signal_price
                )
        elif type == OrderType.LIMIT:
            return self.send_limit_order(
                strategy, contract, direction, offset, price, volume, lock, net, signal_price
            )
        else:
            return self.send_server_order(
                strategy=strategy,
                contract=contract,
                type=type,
                price=price,
                volume=volume,
                direction=direction,
                offset=offset,
                lock=lock,
                net=net,
                signal_price=signal_price
            )

    def cancel_order(self, strategy: CtaTemplate, vt_orderid: str):
        """
        """
        if vt_orderid.startswith(STOPORDER_PREFIX):
            self.cancel_local_stop_order(strategy, vt_orderid)
        else:
            self.cancel_server_order(strategy, vt_orderid)

    def cancel_all(self, strategy: CtaTemplate):
        """
        Cancel all active orders of a strategy.
        """
        vt_orderids = self.strategy_orderid_map[strategy.strategy_name]
        if not vt_orderids:
            return

        for vt_orderid in copy(vt_orderids):
            self.cancel_order(strategy, vt_orderid)

    def get_engine_type(self):
        """"""
        return self.engine_type

    def get_pricetick(self, strategy: CtaTemplate):
        """
        Return contract pricetick data.
        """
        contract = self.main_engine.get_contract(strategy.vt_symbol)

        if contract:
            return contract.pricetick
        else:
            return None

    def load_bar(
        self,
        vt_symbol: str,
        days: int,
        interval: Interval,
        callback: Callable[[BarData], None],
        use_database: bool
    ):
        """"""
        symbol, exchange = extract_vt_symbol(vt_symbol)
        end = datetime.now(LOCAL_TZ)
        start = end - timedelta(days)
        bars = []

        # Pass gateway and datafeed if use_database set to True
        if not use_database:
            # Query bars from gateway if available
            contract = self.main_engine.get_contract(vt_symbol)

            if contract and contract.history_data:
                req = HistoryRequest(
                    symbol=symbol,
                    exchange=exchange,
                    interval=interval,
                    start=start,
                    end=end
                )
                bars = self.main_engine.query_history(req, contract.gateway_name)

            # Try to query bars from datafeed, if not found, load from database.
            else:
                bars = self.query_bar_from_datafeed(symbol, exchange, interval, start, end)

        if not bars:
            bars = self.database.load_bar_data(
                symbol=symbol,
                exchange=exchange,
                interval=interval,
                start=start,
                end=end,
            )

        for bar in bars:
            callback(bar)

    def load_tick(
        self,
        vt_symbol: str,
        days: int,
        callback: Callable[[TickData], None]
    ):
        """"""
        symbol, exchange = extract_vt_symbol(vt_symbol)
        end = datetime.now(LOCAL_TZ)
        start = end - timedelta(days)

        ticks = self.database.load_tick_data(
            symbol=symbol,
            exchange=exchange,
            start=start,
            end=end,
        )

        for tick in ticks:
            callback(tick)

    def call_strategy_func(
        self, strategy: CtaTemplate, func: Callable, params: Any = None
    ):
        """
        Call function of a strategy and catch any exception raised.
        """
        try:
            if params:
                func(params)
            else:
                func()
        except Exception:
            strategy.trading = False
            strategy.inited = False

            msg = f"触发异常已停止\n{traceback.format_exc()}"
            self.write_log(msg, strategy)

    def add_strategy(
        self, class_name: str, strategy_name: str, vt_symbol: str, setting: dict,
            trade_basket=False
    ):
        """
        Add a new strategy.
        """
        if strategy_name in self.strategies:
            self.write_log(f"创建策略失败，存在重名{strategy_name}")
            return

        strategy_class = self.classes.get(class_name, None)
        if not strategy_class:
            self.write_log(f"创建策略失败，找不到策略类{class_name}")
            return

        if "." not in vt_symbol:
            self.write_log("创建策略失败，本地代码缺失交易所后缀")
            return

        try:
            strategy = strategy_class(self, strategy_name, vt_symbol, setting, trade_basket)
        except TypeError:
            strategy = strategy_class(self, strategy_name, vt_symbol, setting)
        self.strategies[strategy_name] = strategy

        # Add vt_symbol to strategy map.
        for symbol in strategy.get_symbols():
            strategies = self.symbol_strategy_map[symbol]
            strategies.append(strategy)

        if isinstance(strategy, ETFTemplate):
            symbols = strategy.get_etf_stocks_sub_reqs()
            try:
                self.main_engine.subscribe_many(symbols, 'JG')
            except Exception:
                self.main_engine.subscribe_many(symbols, 'QMT')

        # Update to setting file.
        self.update_strategy_setting(strategy_name, setting)

        self.put_strategy_event(strategy)

    def init_strategy(self, strategy_name: str):
        """
        Init a strategy.
        """
        self.init_executor.submit(self._init_strategy, strategy_name)

    def _init_strategy(self, strategy_name: str):
        """
        Init strategies in queue.
        """
        strategy = self.strategies[strategy_name]

        if strategy.inited:
            self.write_log(f"{strategy_name}已经完成初始化，禁止重复操作")
            return

        self.write_log(f"{strategy_name}开始执行初始化")

        # Call on_init function of strategy
        self.call_strategy_func(strategy, strategy.on_init)

        # Restore strategy data(variables)
        data = self.strategy_data.get(strategy_name, None)
        if data:
            for name in strategy.variables:
                value = data.get(name, None)
                if isinstance(value, dict):
                    _value = value
                    value = defaultdict(lambda: 0)
                    value.update(_value)
                if value:
                    setattr(strategy, name, value)

        # Subscribe market data
        for vt_symbol in strategy.get_symbols():
            contract = self.main_engine.get_contract(vt_symbol)
            if contract:
                req = SubscribeRequest(
                    symbol=contract.symbol, exchange=contract.exchange)
                self.main_engine.subscribe(req, contract.gateway_name)
            else:
                self.write_log(f"行情订阅失败，找不到合约{vt_symbol}", strategy)

        if getattr(strategy, 'trade_basket', False):
            self.write_log(f'订阅 <{strategy.vt_symbol}> 篮子')
            sub_list = []
            for comp in self.main_engine.get_basket_components(strategy.vt_symbol):
                cont = self.main_engine.get_contract(comp.vt_symbol)
                if not cont:
                    continue
                sub_list.append(
                    SubscribeRequest(symbol=cont.symbol, important=False, exchange=cont.exchange)
                )
            try:
                self.main_engine.subscribe_many(req_list=sub_list, gateway_name=comp.gateway_name)
            except:
                for req in sub_list:
                    self.main_engine.subscribe(req=req, gateway_name=comp.gateway_name)

        # Put event to update init completed status.
        strategy.inited = True
        self.put_strategy_event(strategy)
        self.write_log(f"{strategy_name}初始化完成")

    def start_strategy(self, strategy_name: str):
        """
        Start a strategy.
        """
        strategy = self.strategies[strategy_name]
        if not strategy.inited:
            self.write_log(f"策略{strategy.strategy_name}启动失败，请先初始化")
            return

        if strategy.trading:
            self.write_log(f"{strategy_name}已经启动，请勿重复操作")
            return

        self.call_strategy_func(strategy, strategy.on_start)
        strategy.trading = True

        self.put_strategy_event(strategy)

    def stop_strategy(self, strategy_name: str):
        """
        Stop a strategy.
        """
        strategy = self.strategies[strategy_name]
        if not strategy.trading:
            return

        # Call on_stop function of the strategy
        self.call_strategy_func(strategy, strategy.on_stop)

        # Change trading status of strategy to False
        strategy.trading = False

        # Cancel all orders of the strategy
        self.cancel_all(strategy)

        # Sync strategy variables to data file
        self.sync_strategy_data(strategy)

        # Update GUI
        self.put_strategy_event(strategy)

    def edit_strategy(self, strategy_name: str, setting: dict):
        """
        Edit parameters of a strategy.
        """
        strategy = self.strategies[strategy_name]
        strategy.update_setting(setting)

        self.update_strategy_setting(strategy_name, setting)
        self.put_strategy_event(strategy)

    def remove_strategy(self, strategy_name: str):
        """
        Remove a strategy.
        """
        strategy = self.strategies[strategy_name]
        if strategy.trading:
            self.write_log(f"策略{strategy.strategy_name}移除失败，请先停止")
            return

        # Remove setting
        self.remove_strategy_setting(strategy_name)

        # Remove from symbol strategy map
        strategies = self.symbol_strategy_map[strategy.vt_symbol]
        strategies.remove(strategy)

        # Remove from active orderid map
        if strategy_name in self.strategy_orderid_map:
            vt_orderids = self.strategy_orderid_map.pop(strategy_name)

            # Remove vt_orderid strategy map
            for vt_orderid in vt_orderids:
                if vt_orderid in self.orderid_strategy_map:
                    self.orderid_strategy_map.pop(vt_orderid)

        # Remove from strategies
        self.strategies.pop(strategy_name)

        self.write_log(f"策略{strategy.strategy_name}移除移除成功")
        return True

    def load_strategy_class(self):
        """
        Load strategy class from source code.
        """
        path1 = Path(__file__).parent.joinpath("strategies")
        self.load_strategy_class_from_folder(path1, "vnpy_ctastrategy.strategies")

        path2 = Path.cwd().joinpath("strategies")
        self.load_strategy_class_from_folder(path2, "strategies")

    def load_strategy_class_from_folder(self, path: Path, module_name: str = ""):
        """
        Load strategy class from certain folder.
        """
        for suffix in ["py", "pyd", "so"]:
            pathname: str = str(path.joinpath(f"*.{suffix}"))
            for filepath in glob(pathname):
                filename: str = Path(filepath).stem
                name: str = f"{module_name}.{filename}"
                self.load_strategy_class_from_module(name)

    def load_strategy_class_from_module(self, module_name: str):
        """
        Load strategy class from module file.
        """
        try:
            module = importlib.import_module(module_name)

            # 重载模块，确保如果策略文件中有任何修改，能够立即生效。
            importlib.reload(module)

            for name in dir(module):
                value = getattr(module, name)
                if (isinstance(value, type) and issubclass(value, CtaTemplate) and value is not CtaTemplate):
                    self.classes[value.__name__] = value
        except:  # noqa
            msg = f"策略文件{module_name}加载失败，触发异常：\n{traceback.format_exc()}"
            self.write_log(msg)

    def load_strategy_data(self):
        """
        Load strategy data from json file.
        """
        self.strategy_data = load_json(self.data_filename)

    def sync_strategy_data(self, strategy: CtaTemplate):
        """
        Sync strategy data into json file.
        """
        data = strategy.get_variables()
        data.pop("inited")      # Strategy status (inited, trading) should not be synced.
        data.pop("trading")

        self.strategy_data[strategy.strategy_name] = data
        save_json(self.data_filename, self.strategy_data)

    def get_all_strategy_class_names(self):
        """
        Return names of strategy classes loaded.
        """
        return list(self.classes.keys())

    def get_strategy_class_parameters(self, class_name: str):
        """
        Get default parameters of a strategy class.
        """
        strategy_class = self.classes[class_name]

        parameters = {}
        for name in strategy_class.parameters:
            parameters[name] = getattr(strategy_class, name)

        return parameters

    def get_strategy_parameters(self, strategy_name):
        """
        Get parameters of a strategy.
        """
        strategy = self.strategies[strategy_name]
        return strategy.get_parameters()

    def init_all_strategies(self):
        """
        """
        for strategy_name in self.strategies.keys():
            self.init_strategy(strategy_name)

    def start_all_strategies(self):
        """
        """
        for strategy_name in self.strategies.keys():
            self.start_strategy(strategy_name)

    def stop_all_strategies(self):
        """
        """
        for strategy_name in self.strategies.keys():
            self.stop_strategy(strategy_name)

    def load_strategy_setting(self):
        """
        Load setting file.
        """
        self.strategy_setting = load_json(self.setting_filename)

        for strategy_name, strategy_config in self.strategy_setting.items():
            self.add_strategy(
                strategy_config["class_name"],
                strategy_name,
                strategy_config["vt_symbol"],
                strategy_config["setting"],
                trade_basket=strategy_config.get('trade_basket', False)
            )

    def update_strategy_setting(self, strategy_name: str, setting: dict):
        """
        Update setting file.
        """
        strategy = self.strategies[strategy_name]

        self.strategy_setting[strategy_name] = {
            "class_name": strategy.__class__.__name__,
            "vt_symbol": strategy.vt_symbol,
            "setting": setting,
            "trade_basket": getattr(strategy, 'trade_basket', False)
        }
        save_json(self.setting_filename, self.strategy_setting)

    def remove_strategy_setting(self, strategy_name: str):
        """
        Update setting file.
        """
        if strategy_name not in self.strategy_setting:
            return

        self.strategy_setting.pop(strategy_name)
        save_json(self.setting_filename, self.strategy_setting)

    def put_stop_order_event(self, stop_order: StopOrder):
        """
        Put an event to update stop order status.
        """
        event = Event(EVENT_CTA_STOPORDER, stop_order)
        self.event_engine.put(event)

    def put_strategy_event(self, strategy: CtaTemplate):
        """
        Put an event to update strategy status.
        """
        data = strategy.get_data()
        event = Event(EVENT_CTA_STRATEGY, data)
        self.event_engine.put(event)

        if not SETTINGS["signal.report"]:
            return

        data = {'strategyData': [strategy.get_report_data()]}
        self.main_engine.send_hedging_report(data)

    def write_log(self, msg: str, strategy: CtaTemplate = None):
        """
        Create cta engine log event.
        """
        if strategy:
            msg = f"[{strategy.strategy_name}]  {msg}"

        log = LogData(msg=msg, gateway_name=APP_NAME)
        event = Event(type=EVENT_CTA_LOG, data=log)
        self.event_engine.put(event)

    def send_email(self, msg: str, strategy: CtaTemplate = None):
        """
        Send email to default receiver.
        """
        if strategy:
            subject = f"{strategy.strategy_name}"
        else:
            subject = "CTA策略引擎"

        self.main_engine.send_email(subject, msg)
