#!/usr/bin/env python

from os.path import join, realpath
import sys; sys.path.insert(0, realpath(join(__file__, "../../")))

from decimal import Decimal
import logging; logging.basicConfig(level=logging.ERROR)
import pandas as pd
from typing import List
import unittest

from hummingbot.cli.utils.exchange_rate_conversion import ExchangeRateConversion
from hummingsim.backtest.backtest_market import BacktestMarket
from hummingsim.backtest.market import (
    AssetType,
    Market,
    MarketConfig,
    QuantizationParams
)
from hummingsim.backtest.mock_order_book_loader import MockOrderBookLoader
from wings.clock import (
    Clock,
    ClockMode
)
from wings.event_logger import EventLogger
from wings.events import (
    MarketEvent,
    OrderBookTradeEvent,
    TradeType,
    OrderType,
    OrderFilledEvent,
    BuyOrderCompletedEvent,
    SellOrderCompletedEvent
)
from wings.order_book import OrderBook
from wings.order_book_row import OrderBookRow
from wings.limit_order import LimitOrder
from hummingbot.strategy.pure_market_making import PureMarketMakingStrategy
from hummingbot.strategy.pure_market_making.pure_market_pair import PureMarketPair


class PureMarketMakingUnitTest(unittest.TestCase):
    start: pd.Timestamp = pd.Timestamp("2019-01-01", tz="UTC")
    end: pd.Timestamp = pd.Timestamp("2019-01-01 01:00:00", tz="UTC")
    start_timestamp: float = start.timestamp()
    end_timestamp: float = end.timestamp()
    maker_symbols: List[str] = ["COINALPHA-WETH", "COINALPHA", "WETH"]

    @classmethod
    def setUpClass(cls):
        ExchangeRateConversion.set_global_exchange_rate_config([
            ("WETH", 1.0, "None"),
            ("QETH", 0.95, "None"),
        ])

    def setUp(self):
        self.clock: Clock = Clock(ClockMode.BACKTEST, 60.0, self.start_timestamp, self.end_timestamp)
        self.maker_market: BacktestMarket = BacktestMarket()
        self.maker_data: MockOrderBookLoader = MockOrderBookLoader(*self.maker_symbols)
        self.maker_data.set_balanced_order_book(mid_price= 1.0, min_price= 0.5, max_price= 1.5, price_step_size= 0.01, volume_step_size= 10)
        self.maker_market.add_data(self.maker_data)
        self.maker_market.set_balance("COINALPHA", 5)
        self.maker_market.set_balance("WETH", 5)
        self.maker_market.set_balance("QETH", 5)
        self.maker_market.set_quantization_param(
            QuantizationParams(
                self.maker_symbols[0], 5, 5, 5, 5
            )
        )

        self.market_pair: PureMarketPair = PureMarketPair(
            *(
                [self.maker_market] + self.maker_symbols
            )
        )

        logging_options: int = (PureMarketMakingStrategy.OPTION_LOG_ALL &
                                (~PureMarketMakingStrategy.OPTION_LOG_NULL_ORDER_SIZE))
        self.strategy: {PureMarketMakingStrategy} = PureMarketMakingStrategy(
            [self.market_pair],
            order_size=1,
            bid_place_threshold=0.003,
            ask_place_threshold=0.003,
            logging_options=logging_options
        )
        self.logging_options = logging_options
        self.clock.add_iterator(self.maker_market)
        self.clock.add_iterator(self.strategy)

        self.maker_order_fill_logger: EventLogger = EventLogger()
        self.cancel_order_logger: EventLogger = EventLogger()
        self.maker_market.add_listener(MarketEvent.OrderFilled, self.maker_order_fill_logger)
        self.maker_market.add_listener(MarketEvent.OrderCancelled, self.cancel_order_logger)

    def simulate_maker_market_trade(self, is_buy: bool, quantity: float):
        maker_symbol: str = self.maker_symbols[0]
        order_book: OrderBook = self.maker_market.get_order_book(maker_symbol)
        trade_price: float = order_book.get_price(True) if is_buy else order_book.get_price(False)
        trade_event: OrderBookTradeEvent = OrderBookTradeEvent(
            maker_symbol,
            self.clock.current_timestamp,
            TradeType.BUY if is_buy else TradeType.SELL,
            trade_price,
            quantity
        )
        order_book.apply_trade(trade_event)

    # Update the orderbook so that the top bids and asks are lower than actual for a wider bid ask spread
    # this basially removes the orderbook entries above top bid and below top ask
    @staticmethod
    def simulate_order_book_widening(order_book: OrderBook, top_bid: float, top_ask: float):
        bid_diffs: List[OrderBookRow] = []
        ask_diffs: List[OrderBookRow] = []
        update_id: int = order_book.last_diff_uid + 1
        for row in order_book.bid_entries():
            if row.price > top_bid:
                bid_diffs.append(OrderBookRow(row.price, 0, update_id))
            else:
                break
        for row in order_book.ask_entries():
            if row.price < top_ask:
                ask_diffs.append(OrderBookRow(row.price, 0, update_id))
            else:
                break
        order_book.apply_diffs(bid_diffs, ask_diffs, update_id)


    @staticmethod
    def simulate_limit_order_fill(market: Market, limit_order: LimitOrder):
        quote_currency_traded: float = float(limit_order.price * limit_order.quantity)
        base_currency_traded: float = float(limit_order.quantity)
        quote_currency: str = limit_order.quote_currency
        base_currency: str = limit_order.base_currency
        config: MarketConfig = market.config

        if limit_order.is_buy:
            market.set_balance(quote_currency, market.get_balance(quote_currency) - quote_currency_traded)
            market.set_balance(base_currency, market.get_balance(base_currency) + base_currency_traded)
            market.trigger_event(MarketEvent.OrderFilled, OrderFilledEvent(
                market.current_timestamp,
                limit_order.client_order_id,
                limit_order.symbol,
                TradeType.BUY,
                OrderType.LIMIT,
                float(limit_order.price),
                float(limit_order.quantity)
            ))
            market.trigger_event(MarketEvent.BuyOrderCompleted, BuyOrderCompletedEvent(
                market.current_timestamp,
                limit_order.client_order_id,
                base_currency,
                quote_currency,
                base_currency if config.buy_fees_asset is AssetType.BASE_CURRENCY else quote_currency,
                base_currency_traded,
                quote_currency_traded,
                0.0,
                OrderType.LIMIT
            ))
        else:
            market.set_balance(quote_currency, market.get_balance(quote_currency) + quote_currency_traded)
            market.set_balance(base_currency, market.get_balance(base_currency) - base_currency_traded)
            market.trigger_event(MarketEvent.OrderFilled, OrderFilledEvent(
                market.current_timestamp,
                limit_order.client_order_id,
                limit_order.symbol,
                TradeType.SELL,
                OrderType.LIMIT,
                float(limit_order.price),
                float(limit_order.quantity)
            ))
            market.trigger_event(MarketEvent.SellOrderCompleted, SellOrderCompletedEvent(
                market.current_timestamp,
                limit_order.client_order_id,
                base_currency,
                quote_currency,
                base_currency if config.sell_fees_asset is AssetType.BASE_CURRENCY else quote_currency,
                base_currency_traded,
                quote_currency_traded,
                0.0,
                OrderType.LIMIT
            ))

    def test_confirm_active_orders(self):
        self.clock.backtest_til(self.start_timestamp + 65)
        self.assertEqual(1, len(self.strategy.active_bids))
        self.assertEqual(1, len(self.strategy.active_asks))

    def test_check_sufficient_balance(self):
        pass

    def test_check_order_fails(self):
        pass

    def test_check_if_active_orders_are_cancelled_every_tick(self):
        pass

    def test_order_fills(self):
        self.clock.backtest_til(self.start_timestamp + 65)
        self.assertEqual(1, len(self.strategy.active_bids))
        self.assertEqual(1, len(self.strategy.active_asks))

        bid_order: LimitOrder = self.strategy.active_bids[0][1]
        ask_order: LimitOrder = self.strategy.active_asks[0][1]
        self.assertEqual(Decimal("0.99501"), bid_order.price)
        self.assertEqual(Decimal("1.0049"), ask_order.price)
        self.assertEqual(Decimal("3.0"), bid_order.quantity)
        self.assertEqual(Decimal("3.0"), ask_order.quantity)

        self.simulate_maker_market_trade(True, 1.0)

        self.clock.backtest_til(self.start_timestamp + 65)
        self.assertEqual(1, len(self.maker_order_fill_logger.event_log))
        self.assertEqual(1, len(self.taker_order_fill_logger.event_log))

        maker_fill: OrderFilledEvent = self.maker_order_fill_logger.event_log[0]
        taker_fill: OrderFilledEvent = self.taker_order_fill_logger.event_log[0]
        self.assertEqual(TradeType.SELL, maker_fill.trade_type)
        self.assertEqual(TradeType.BUY, taker_fill.trade_type)
        self.assertAlmostEqual(1.0049, maker_fill.price)
        self.assertAlmostEqual(1.0005, taker_fill.price)
        self.assertAlmostEqual(3.0, maker_fill.amount)
        self.assertAlmostEqual(3.0, taker_fill.amount)

    def test_market_became_wider(self):
        self.clock.backtest_til(self.start_timestamp + 5)

        bid_order: LimitOrder = self.strategy.active_bids[0][1]
        ask_order: LimitOrder = self.strategy.active_asks[0][1]
        self.assertEqual(Decimal("0.99501"), bid_order.price)
        self.assertEqual(Decimal("1.0049"), ask_order.price)
        self.assertEqual(Decimal("3.0"), bid_order.quantity)
        self.assertEqual(Decimal("3.0"), ask_order.quantity)

        self.simulate_order_book_widening(self.maker_data.order_book, 0.99, 1.01)

        self.clock.backtest_til(self.start_timestamp + 90)
        self.assertEqual(2, len(self.cancel_order_logger.event_log))
        self.assertEqual(1, len(self.strategy.active_bids))
        self.assertEqual(1, len(self.strategy.active_asks))

        bid_order = self.strategy.active_bids[0][1]
        ask_order = self.strategy.active_asks[0][1]
        self.assertEqual(Decimal("0.98501"), bid_order.price)
        self.assertEqual(Decimal("1.0149"), ask_order.price)

    def test_market_became_narrower(self):
        self.clock.backtest_til(self.start_timestamp + 5)
        bid_order: LimitOrder = self.strategy.active_bids[0][1]
        ask_order: LimitOrder = self.strategy.active_asks[0][1]
        self.assertEqual(Decimal("0.99501"), bid_order.price)
        self.assertEqual(Decimal("1.0049"), ask_order.price)
        self.assertEqual(Decimal("3.0"), bid_order.quantity)
        self.assertEqual(Decimal("3.0"), ask_order.quantity)

        self.maker_data.order_book.apply_diffs([OrderBookRow(0.996, 30, 2)], [OrderBookRow(1.004, 30, 2)], 2)

        self.clock.backtest_til(self.start_timestamp + 10)
        self.assertEqual(2, len(self.cancel_order_logger.event_log))
        self.assertEqual(1, len(self.strategy.active_bids))
        self.assertEqual(1, len(self.strategy.active_asks))

        bid_order = self.strategy.active_bids[0][1]
        ask_order = self.strategy.active_asks[0][1]
        self.assertEqual(Decimal("0.99601"), bid_order.price)
        self.assertEqual(Decimal("1.0039"), ask_order.price)

    def test_order_fills_after_cancellation(self):
        self.clock.backtest_til(self.start_timestamp + 5)
        bid_order: LimitOrder = self.strategy.active_bids[0][1]
        ask_order: LimitOrder = self.strategy.active_asks[0][1]
        self.assertEqual(Decimal("0.99501"), bid_order.price)
        self.assertEqual(Decimal("1.0049"), ask_order.price)
        self.assertEqual(Decimal("3.0"), bid_order.quantity)
        self.assertEqual(Decimal("3.0"), ask_order.quantity)

        bid_order: LimitOrder = self.strategy.active_bids[0][1]
        ask_order: LimitOrder = self.strategy.active_asks[0][1]

        self.maker_data.order_book.apply_diffs([OrderBookRow(0.996, 30, 2)], [OrderBookRow(1.004, 30, 2)], 2)

        self.clock.backtest_til(self.start_timestamp + 10)
        self.assertEqual(2, len(self.cancel_order_logger.event_log))
        self.assertEqual(Decimal("0.99601"), self.strategy.active_bids[0][1].price)
        self.assertEqual(Decimal("1.0039"), self.strategy.active_asks[0][1].price)
        self.assertEqual(0, len(self.taker_order_fill_logger.event_log))

        self.clock.backtest_til(self.start_timestamp + 20)
        self.simulate_limit_order_fill(self.maker_market, bid_order)
        self.simulate_limit_order_fill(self.maker_market, ask_order)

        self.clock.backtest_til(self.start_timestamp + 25)
        fill_events: List[OrderFilledEvent] = self.taker_order_fill_logger.event_log
        bid_hedges: List[OrderFilledEvent] = [evt for evt in fill_events if evt.trade_type is TradeType.SELL]
        ask_hedges: List[OrderFilledEvent] = [evt for evt in fill_events if evt.trade_type is TradeType.BUY]
        self.assertEqual(1, len(bid_hedges))
        self.assertEqual(1, len(ask_hedges))
        self.assertGreater(
            self.maker_market.get_balance(self.maker_symbols[2]) + self.taker_market.get_balance(self.taker_symbols[2]),
            10
        )

    def test_create_new_orders(self):
        # Test the case where the profitable hedging depth is less than order size limit based on balance.
        self.taker_data.set_balanced_order_book(1.0, 0.5, 1.5, 0.001, 3)
        bid_price, bid_size_limit = self.strategy.get_market_making_price_and_size_limit(
            self.market_pair,
            True
        )
        ask_price, ask_size_limit = self.strategy.get_market_making_price_and_size_limit(
            self.market_pair,
            False
        )
        self.assertEqual((Decimal("0.99501"), Decimal("2.25")), (bid_price, bid_size_limit))
        self.assertEqual((Decimal("1.0049"), Decimal("2.25")), (ask_price, ask_size_limit))

        # Test the case where the profitable hedging depth is equal to order size limit based on balance.
        self.taker_data.set_balanced_order_book(1.0, 0.5, 1.5, 0.001, 4)
        bid_price, bid_size_limit = self.strategy.get_market_making_price_and_size_limit(
            self.market_pair,
            True
        )
        ask_price, ask_size_limit = self.strategy.get_market_making_price_and_size_limit(
            self.market_pair,
            False
        )
        self.assertEqual((Decimal("0.99501"), Decimal("3")), (bid_price, bid_size_limit))
        self.assertEqual((Decimal("1.0049"), Decimal("3")), (ask_price, ask_size_limit))

        # Test the case where the hedging trade is numerically profitable but below the min profit setting.
        self.simulate_order_book_widening(self.taker_data.order_book, 0.9975, 1.0025)
        bid_price, bid_size_limit = self.strategy.get_market_making_price_and_size_limit(
            self.market_pair,
            True
        )
        ask_price, ask_size_limit = self.strategy.get_market_making_price_and_size_limit(
            self.market_pair,
            False
        )
        self.assertEqual((Decimal("0.99501"), Decimal("0")), (bid_price, bid_size_limit))
        self.assertEqual((Decimal("1.0049"), Decimal("0")), (ask_price, ask_size_limit))

        # Make sure the strategy doesn't emit any orders in this case
        self.clock.backtest_til(self.start_timestamp + 5)
        self.assertEqual(0, len(self.strategy.active_bids))
        self.assertEqual(0, len(self.strategy.active_asks))
        self.assertEqual(0, len(self.cancel_order_logger.event_log))


def main():
    unittest.main()


if __name__ == "__main__":
    main()
