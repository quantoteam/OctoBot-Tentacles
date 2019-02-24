"""
OctoBot Tentacle

$tentacle_description: {
    "name": "staggered_orders_trading_mode",
    "type": "Trading",
    "subtype": "Mode",
    "version": "1.1.0",
    "requirements": ["staggered_orders_strategy_evaluator"],
    "config_files": ["StaggeredOrdersTradingMode.json"],
    "tests":["test_staggered_orders_trading_mode"],
    "developing": true
}
"""

#  Drakkar-Software OctoBot-Tentacles
#  Copyright (c) Drakkar-Software, All rights reserved.
#
#  This library is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 3.0 of the License, or (at your option) any later version.
#
#  This library is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public
#  License along with this library.

from ccxt import InsufficientFunds
from enum import Enum
from dataclasses import dataclass
from math import floor
from copy import copy

from config import EvaluatorStates, TraderOrderType, ExchangeConstantsTickersInfoColumns, INIT_EVAL_NOTE, \
    START_PENDING_EVAL_NOTE, TradeOrderSide
from evaluator.Strategies import StaggeredStrategiesEvaluator
from trading.trader.modes.abstract_mode_creator import AbstractTradingModeCreator
from trading.trader.modes.abstract_mode_decider import AbstractTradingModeDecider
from trading.trader.modes.abstract_trading_mode import AbstractTradingMode
from trading.trader.portfolio import Portfolio
from tools.symbol_util import split_symbol


class StrategyModes(Enum):
    NEUTRAL = "neutral"
    MOUNTAIN = "mountain"
    VALLEY = "valley"
    SELL_SLOPE = "sell slope"
    BUY_SLOPE = "buy slope"


INCREASING = "increasing_towards_current_price"
DECREASING = "decreasing_towards_current_price"
MULTIPLIER = "multiplier"


StrategyModeMultipliersDetails = {
    StrategyModes.NEUTRAL: {
        MULTIPLIER: 0.3,
        TradeOrderSide.BUY: INCREASING,
        TradeOrderSide.SELL: INCREASING
    },
    StrategyModes.MOUNTAIN: {
        MULTIPLIER: 1,
        TradeOrderSide.BUY: INCREASING,
        TradeOrderSide.SELL: INCREASING
    },
    StrategyModes.VALLEY: {
        MULTIPLIER: 1,
        TradeOrderSide.BUY: DECREASING,
        TradeOrderSide.SELL: DECREASING
    },
    StrategyModes.BUY_SLOPE: {
        MULTIPLIER: 1,
        TradeOrderSide.BUY: DECREASING,
        TradeOrderSide.SELL: INCREASING
    },
    StrategyModes.SELL_SLOPE: {
        MULTIPLIER: 1,
        TradeOrderSide.BUY: INCREASING,
        TradeOrderSide.SELL: DECREASING
    }
}


@dataclass
class OrderData:
    side: TradeOrderSide = None
    quantity: float = 0
    price: float = 0
    symbol: str = 0
    is_virtual: bool = True


class StaggeredOrdersTradingMode(AbstractTradingMode):

    DESCRIPTION = 'Places a large amount of buy and sell orders at certain intervals, covering the orderbook from ' \
                  'very low prices to very high prices. The range is supposed to cover all conceivable prices for as ' \
                  'long as the user intends to run the strategy. That could be from -100x to +100x ' \
                  '(-99% to +10000%). Profits will be made from price movements, and the strategy introduces ' \
                  'friction to such movements. It gives markets depth, and makes them look better. It never ' \
                  '"sells at a loss", but always at a profit. Description from ' \
                  'https://github.com/Codaone/DEXBot/wiki/The-Staggered-Orders-strategy. Full documentation ' \
                  'available there.'
    CONFIG_MODE = "mode"
    CONFIG_SPREAD = "spread_percent"
    CONFIG_INCREMENT_PERCENT = "increment_percent"
    CONFIG_LOWER_BOUND = "lower_bound"
    CONFIG_UPPER_BOUND = "upper_bound"
    CONFIG_ALLOW_INSTANT_FILL = "allow_instant_fill"
    CONFIG_OPERATIONAL_DEPTH = "operational_depth"

    def __init__(self, config, exchange):
        super().__init__(config, exchange)
        self.load_config()

    def create_deciders(self, symbol, symbol_evaluator):
        self.add_decider(symbol, StaggeredOrdersTradingModeDecider(self, symbol_evaluator, self.exchange))

    def create_creators(self, symbol, symbol_evaluator):
        self.add_creator(symbol, StaggeredOrdersTradingModeCreator(self))

    async def order_filled_callback(self, order):
        decider = self.get_only_decider_key(order.get_order_symbol())
        await decider.order_filled_callback(order)

    def set_default_config(self):
        raise RuntimeError(f"Impossible to start {self.get_name()} without {self.get_config_file_name()} "
                           f"configuration file.")


class StaggeredOrdersTradingModeCreator(AbstractTradingModeCreator):
    def __init__(self, trading_mode):
        super().__init__(trading_mode)

    # should never be called
    def create_new_order(self, eval_note, symbol, exchange, trader, portfolio, state):
        super().create_new_order(eval_note, symbol, exchange, trader, portfolio, state)

    # creates a new order
    async def create_order(self, order_data, current_price, exchange, trader, portfolio):
        created_order = None
        try:
            symbol_market = exchange.get_market_status(order_data.symbol, with_fixer=False)
            for order_quantity, order_price in self.check_and_adapt_order_details_if_necessary(order_data.quantity,
                                                                                               order_data.price,
                                                                                               symbol_market):
                order_type = TraderOrderType.SELL_LIMIT if order_data.side == TradeOrderSide.SELL \
                    else TraderOrderType.BUY_LIMIT
                created_order = trader.create_order_instance(order_type=order_type,
                                                             symbol=order_data.symbol,
                                                             current_price=current_price,
                                                             quantity=order_quantity,
                                                             price=order_price)
                await trader.create_order(created_order, portfolio)
        except InsufficientFunds as e:
            raise e
        except Exception as e:
            self.logger.error(f"Failed to create order : {e}. "
                              f"Order: "
                              f"{created_order.get_string_info() if created_order else None}")
            self.logger.exception(e)
            return None
        return created_order


class StaggeredOrdersTradingModeDecider(AbstractTradingModeDecider):
    def __init__(self, trading_mode, symbol_evaluator, exchange):
        super().__init__(trading_mode, symbol_evaluator, exchange)
        # no state for this evaluator: always neutral
        self.state = EvaluatorStates.NEUTRAL

        # staggered orders strategy parameters
        self.mode = StrategyModes(self.trading_mode.get_trading_config_value(self.trading_mode.CONFIG_MODE))
        self.spread = self.trading_mode.get_trading_config_value(self.trading_mode.CONFIG_SPREAD) / 100
        self.increment = self.trading_mode.get_trading_config_value(self.trading_mode.CONFIG_INCREMENT_PERCENT) / 100
        self.operational_depth = self.trading_mode.get_trading_config_value(self.trading_mode.CONFIG_OPERATIONAL_DEPTH)
        self.lowest_buy = self.trading_mode.get_trading_config_value(self.trading_mode.CONFIG_LOWER_BOUND)
        self.highest_sell = self.trading_mode.get_trading_config_value(self.trading_mode.CONFIG_UPPER_BOUND)
        self.fees = 0.001

    def set_final_eval(self):
        # Strategies analysis
        for evaluated_strategies in self.symbol_evaluator.get_strategies_eval_list(self.exchange):
            if isinstance(evaluated_strategies, StaggeredStrategiesEvaluator) or \
               evaluated_strategies.has_class_in_parents(StaggeredStrategiesEvaluator):
                evaluation = evaluated_strategies.get_eval_note()
                if evaluation != START_PENDING_EVAL_NOTE:
                    self.final_eval = evaluation[ExchangeConstantsTickersInfoColumns.LAST_PRICE.value]
                    return

    async def create_state(self):
        if self.final_eval != INIT_EVAL_NOTE:
            if self.symbol_evaluator.has_trader_simulator(self.exchange):
                trader_simulator = self.symbol_evaluator.get_trader_simulator(self.exchange)
                if trader_simulator.is_enabled():
                    await self._handle_staggered_orders(self.final_eval, trader_simulator)
            if self.symbol_evaluator.has_trader(self.exchange):
                real_trader = self.symbol_evaluator.get_trader(self.exchange)
                if real_trader.is_enabled():
                    await self._handle_staggered_orders(self.final_eval, real_trader)

    async def order_filled_callback(self, filled_order):
        # create order on the order side
        now_selling = filled_order.get_side() == TradeOrderSide.BUY
        new_side = TradeOrderSide.SELL if now_selling else TradeOrderSide.BUY
        trader = filled_order.trader
        closest_order_with_price = self._get_closest_price_order(trader, new_side)
        price = closest_order_with_price.origin_price * ((1 - self.increment) if now_selling else (1 + self.increment))
        quantity_change = self.increment - self.fees
        quantity = filled_order.origin_quantity * ((1 - quantity_change) if now_selling else (1 + quantity_change))
        new_order = OrderData(new_side, quantity, price, self.symbol)

        creator_key = self.trading_mode.get_only_creator_key(self.symbol)
        order_creator = self.trading_mode.get_creator(self.symbol, creator_key)
        new_orders = []
        async with trader.get_portfolio().get_lock():
            pf = trader.get_portfolio()
            await self._create_order(new_order, trader, order_creator, new_orders, pf)
        await self.push_order_notification_if_possible(new_orders, self.notifier)

    async def _handle_staggered_orders(self, final_eval, trader):
        async with trader.get_portfolio().get_lock():
            buy_orders, sell_orders = await self._generate_staggered_orders(final_eval, trader)
            staggered_orders = self._alternate_not_virtual_orders(buy_orders, sell_orders)
            await self._create_multiple_not_virtual_orders(staggered_orders, trader)

    async def _generate_staggered_orders(self, current_price, trader):
        existing_orders = trader.get_open_orders()
        to_consider_orders = [order for order in existing_orders if order.get_order_symbol() == self.symbol]
        portfolio = trader.get_portfolio().get_portfolio()

        buy_orders = await self._generate_buy_orders(current_price, to_consider_orders, portfolio)
        sell_orders = await self._generate_sell_orders(current_price, to_consider_orders, portfolio)

        self._set_virtual_orders(buy_orders, sell_orders, self.operational_depth)

        return buy_orders, sell_orders

    async def _generate_buy_orders(self, current_price, existing_orders, portfolio):
        return await self._create_orders(self.lowest_buy, current_price, TradeOrderSide.BUY, existing_orders,
                                         portfolio, current_price)

    async def _generate_sell_orders(self, current_price, existing_orders, portfolio):
        return await self._create_orders(current_price, self.highest_sell, TradeOrderSide.SELL, existing_orders,
                                         portfolio, current_price)

    async def _create_orders(self, lower_bound, upper_bound, side, existing_orders, portfolio, current_price):
        full_init = False
        if not existing_orders:
            full_init = True
        orders = []
        selling = side == TradeOrderSide.SELL
        self.total_orders_count = self.highest_sell - self.lowest_buy

        currency, market = split_symbol(self.symbol)
        order_limiting_currency = currency if selling else market
        order_limiting_currency_amount = portfolio[order_limiting_currency][Portfolio.AVAILABLE] \
            if order_limiting_currency in portfolio else 0

        orders_count, average_order_quantity = \
            self._get_order_count_and_average_quantity(current_price, selling, lower_bound,
                                                       upper_bound, order_limiting_currency_amount)

        starting_bound = upper_bound if selling else lower_bound
        if full_init:
            for i in range(orders_count):
                price = self._get_price_from_iteration(current_price, starting_bound, selling, i, self.increment)
                quantity = self._get_quantity_from_iteration(average_order_quantity, self.mode,
                                                             side, i, orders_count, price)
                orders.append(OrderData(side, quantity, price, self.symbol))
        else:
            pass
        return orders

    @staticmethod
    def _get_closest_price_order(trader, side):
        order_list = [order for order in copy(trader.get_order_manager().get_open_orders()) if order.side == side]
        invert_sorting = True if side == TradeOrderSide.BUY else False
        return sorted(order_list, key=lambda order: order.origin_price, reverse=invert_sorting)[0]

    @staticmethod
    def _alternate_not_virtual_orders(buy_orders, sell_orders):
        not_virtual_buy_orders = StaggeredOrdersTradingModeDecider._filter_virtual_order(buy_orders)
        not_virtual_sell_orders = StaggeredOrdersTradingModeDecider._filter_virtual_order(sell_orders)
        alternated_orders_list = []
        for i in range(max(len(not_virtual_buy_orders), len(not_virtual_sell_orders))):
            if i < len(not_virtual_buy_orders):
                alternated_orders_list.append(not_virtual_buy_orders[i])
            if i < len(not_virtual_sell_orders):
                alternated_orders_list.append(not_virtual_sell_orders[i])
        return alternated_orders_list

    @staticmethod
    def _filter_virtual_order(orders):
        return [order for order in orders if not order.is_virtual]

    @staticmethod
    def _set_virtual_orders(buy_orders, sell_orders, operational_depth):
        # reverse orders to put orders closer to the current price first in order to set virtual orders
        buy_orders.reverse()
        sell_orders.reverse()

        # all orders that are further than self.operational_depth are virtual
        orders_count = 0
        buy_index = 0
        sell_index = 0
        at_least_one_added = True
        while orders_count < operational_depth and at_least_one_added:
            # priority to orders closer to current price
            at_least_one_added = False
            if len(buy_orders) > buy_index:
                buy_orders[buy_index].is_virtual = False
                buy_index += 1
                orders_count += 1
                at_least_one_added = True
            if len(sell_orders) > sell_index and orders_count < operational_depth:
                sell_orders[sell_index].is_virtual = False
                sell_index += 1
                orders_count += 1
                at_least_one_added = True

        # reverse back
        buy_orders.reverse()
        sell_orders.reverse()

    def _get_order_count_and_average_quantity(self, current_price, selling, lower_bound, upper_bound, holdings):
        if selling:
            order_distance = upper_bound - lower_bound * (1 + self.spread / 2)
        else:
            order_distance = upper_bound * (1 - self.spread / 2) - lower_bound
        orders_count = order_distance / (current_price * self.increment) + 1
        average_order_quantity = holdings / orders_count
        return floor(orders_count), average_order_quantity

    @staticmethod
    def _get_price_from_iteration(current_price, starting_bound, is_selling, iteration, increment):
        price_step = current_price * increment * iteration
        return starting_bound - price_step if is_selling else starting_bound + price_step

    @staticmethod
    def _get_quantity_from_iteration(average_order_quantity, mode, side, iteration, max_iteration, price):
        multiplier_price_ratio = 1
        mode_multiplier = StrategyModeMultipliersDetails[mode][MULTIPLIER]
        min_quantity = average_order_quantity * (1 - mode_multiplier/2)
        max_quantity = average_order_quantity * (1 + mode_multiplier/2)
        delta = max_quantity - min_quantity
        if StrategyModeMultipliersDetails[mode][side] == INCREASING:
            multiplier_price_ratio = iteration/(max_iteration - 1)
        elif StrategyModeMultipliersDetails[mode][side] == DECREASING:
            multiplier_price_ratio = 1 - iteration/(max_iteration - 1)
        quantity = min_quantity + (delta * multiplier_price_ratio)
        return quantity / price

    async def _create_multiple_not_virtual_orders(self, orders_to_create, trader):
        creator_key = self.trading_mode.get_only_creator_key(self.symbol)
        await self._create_not_virtual_orders(self.notifier, trader, orders_to_create, creator_key)

    async def _create_order(self, order, trader, order_creator, new_orders, portfolio):
        try:
            created_order = await order_creator.create_order(order, self.final_eval, self.exchange, trader, portfolio)
            new_orders.append(created_order)
        except InsufficientFunds:
            if not trader.get_simulate():
                try:
                    # second chance: force portfolio update and retry
                    await trader.force_refresh_orders_and_portfolio()
                    created_order = await order_creator.create_order(order, self.final_eval,
                                                                     self.exchange, trader, portfolio)
                    new_orders.append(created_order)
                except InsufficientFunds as e:
                    self.logger.error(f"Failed to create order on second attempt : {e})")

    async def _create_not_virtual_orders(self, notifier, trader, orders_to_create, creator_key):
        order_creator = self.trading_mode.get_creator(self.symbol, creator_key)
        new_orders = []
        pf = trader.get_portfolio()
        for order in orders_to_create:
            await self._create_order(order, trader, order_creator, new_orders, pf)

        await self.push_order_notification_if_possible(new_orders, notifier)

    @classmethod
    def get_should_cancel_loaded_orders(cls):
        return False
