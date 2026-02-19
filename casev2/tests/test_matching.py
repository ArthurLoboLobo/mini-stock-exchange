import uuid
from datetime import datetime, timedelta, timezone

from app.engine import Order, Trade
from app.engine.matching import match_order
from app.engine.order_book import OrderBook
from app.models import OrderSide, OrderType, OrderStatus


BROKER_A = uuid.uuid4()
BROKER_B = uuid.uuid4()


def _make_order(
    side: OrderSide,
    price: int | None = None,
    quantity: int = 100,
    order_type: OrderType = OrderType.limit,
    symbol: str = "PETR4",
    broker_id: uuid.UUID | None = None,
    valid_until: datetime | None = None,
) -> Order:
    now = datetime.now(timezone.utc)
    if valid_until is None:
        valid_until = now + timedelta(hours=1)
    return Order(
        id=uuid.uuid4(),
        broker_id=broker_id or uuid.uuid4(),
        symbol=symbol,
        side=side,
        order_type=order_type,
        price=price,
        quantity=quantity,
        remaining_quantity=quantity,
        status=OrderStatus.open,
        document_number="12345678901",
        valid_until=valid_until,
        created_at=now,
    )


class TestSimpleMatch:
    def test_exact_match_buy_hits_ask(self):
        """Buy at 1000 matches ask at 1000."""
        book = OrderBook()
        ask = _make_order(OrderSide.ask, price=1000, broker_id=BROKER_A)
        book.insert(ask)

        bid = _make_order(OrderSide.bid, price=1000, broker_id=BROKER_B)
        trades, expired = match_order(bid, book)

        assert len(trades) == 1
        assert trades[0].price == 1000
        assert trades[0].quantity == 100
        assert trades[0].buy_order_id == bid.id
        assert trades[0].sell_order_id == ask.id
        assert trades[0].buyer_broker_id == BROKER_B
        assert trades[0].seller_broker_id == BROKER_A
        assert bid.status == OrderStatus.closed
        assert ask.status == OrderStatus.closed
        assert bid.remaining_quantity == 0
        assert ask.remaining_quantity == 0
        assert len(expired) == 0
        # Book should be empty
        assert book.get_best_ask("PETR4") is None

    def test_exact_match_sell_hits_bid(self):
        """Sell at 1000 matches bid at 1000."""
        book = OrderBook()
        bid = _make_order(OrderSide.bid, price=1000, broker_id=BROKER_A)
        book.insert(bid)

        ask = _make_order(OrderSide.ask, price=1000, broker_id=BROKER_B)
        trades, expired = match_order(ask, book)

        assert len(trades) == 1
        assert trades[0].price == 1000  # seller's price
        assert trades[0].buy_order_id == bid.id
        assert trades[0].sell_order_id == ask.id
        assert bid.status == OrderStatus.closed
        assert ask.status == OrderStatus.closed

    def test_price_gap_uses_seller_price(self):
        """Buy at 1200 matches ask at 1000 → trade at 1000 (seller's price)."""
        book = OrderBook()
        ask = _make_order(OrderSide.ask, price=1000)
        book.insert(ask)

        bid = _make_order(OrderSide.bid, price=1200)
        trades, _ = match_order(bid, book)

        assert len(trades) == 1
        assert trades[0].price == 1000

    def test_sell_price_gap_uses_seller_price(self):
        """Sell at 800 matches bid at 1000 → trade at 800 (seller's price)."""
        book = OrderBook()
        bid = _make_order(OrderSide.bid, price=1000)
        book.insert(bid)

        ask = _make_order(OrderSide.ask, price=800)
        trades, _ = match_order(ask, book)

        assert len(trades) == 1
        assert trades[0].price == 800


class TestNoMatch:
    def test_no_match_buy_too_low(self):
        """Buy at 900 doesn't match ask at 1000."""
        book = OrderBook()
        ask = _make_order(OrderSide.ask, price=1000)
        book.insert(ask)

        bid = _make_order(OrderSide.bid, price=900)
        trades, _ = match_order(bid, book)

        assert len(trades) == 0
        assert bid.status == OrderStatus.open
        # Bid should rest in book
        assert book.get_best_bid("PETR4") is not None

    def test_no_match_sell_too_high(self):
        """Sell at 1100 doesn't match bid at 1000."""
        book = OrderBook()
        bid = _make_order(OrderSide.bid, price=1000)
        book.insert(bid)

        ask = _make_order(OrderSide.ask, price=1100)
        trades, _ = match_order(ask, book)

        assert len(trades) == 0
        assert ask.status == OrderStatus.open
        assert book.get_best_ask("PETR4") is not None

    def test_no_match_empty_book(self):
        """Order rests in empty book."""
        book = OrderBook()
        bid = _make_order(OrderSide.bid, price=1000)
        trades, _ = match_order(bid, book)

        assert len(trades) == 0
        assert bid.status == OrderStatus.open
        best = book.get_best_bid("PETR4")
        assert best is not None
        assert best[0] == 1000


class TestPartialFill:
    def test_partial_fill_buyer_larger(self):
        """Buy 200 matches ask of 100 → partial fill, buyer rests with 100."""
        book = OrderBook()
        ask = _make_order(OrderSide.ask, price=1000, quantity=100)
        book.insert(ask)

        bid = _make_order(OrderSide.bid, price=1000, quantity=200)
        trades, _ = match_order(bid, book)

        assert len(trades) == 1
        assert trades[0].quantity == 100
        assert bid.remaining_quantity == 100
        assert bid.status == OrderStatus.open
        assert ask.remaining_quantity == 0
        assert ask.status == OrderStatus.closed
        # Bid should rest in book
        assert book.get_best_bid("PETR4") is not None

    def test_partial_fill_seller_larger(self):
        """Sell 200 matches bid of 100 → partial fill, seller rests with 100."""
        book = OrderBook()
        bid = _make_order(OrderSide.bid, price=1000, quantity=100)
        book.insert(bid)

        ask = _make_order(OrderSide.ask, price=1000, quantity=200)
        trades, _ = match_order(ask, book)

        assert len(trades) == 1
        assert trades[0].quantity == 100
        assert ask.remaining_quantity == 100
        assert ask.status == OrderStatus.open
        assert bid.remaining_quantity == 0
        assert bid.status == OrderStatus.closed


class TestMultipleFills:
    def test_buy_matches_multiple_asks(self):
        """Buy 500 at 1100 matches asks at 1000 (200) and 1050 (300)."""
        book = OrderBook()
        ask1 = _make_order(OrderSide.ask, price=1000, quantity=200)
        ask2 = _make_order(OrderSide.ask, price=1050, quantity=300)
        ask3 = _make_order(OrderSide.ask, price=1200, quantity=100)  # won't match
        book.insert(ask1)
        book.insert(ask2)
        book.insert(ask3)

        bid = _make_order(OrderSide.bid, price=1100, quantity=500)
        trades, _ = match_order(bid, book)

        assert len(trades) == 2
        assert trades[0].price == 1000
        assert trades[0].quantity == 200
        assert trades[1].price == 1050
        assert trades[1].quantity == 300
        assert bid.remaining_quantity == 0
        assert bid.status == OrderStatus.closed
        # ask3 at 1200 should remain
        best = book.get_best_ask("PETR4")
        assert best is not None
        assert best[0] == 1200

    def test_sell_matches_multiple_bids(self):
        """Sell 300 at 900 matches bids at 1100 (200) and 1000 (200) → partial on second."""
        book = OrderBook()
        bid1 = _make_order(OrderSide.bid, price=1100, quantity=200)
        bid2 = _make_order(OrderSide.bid, price=1000, quantity=200)
        book.insert(bid1)
        book.insert(bid2)

        ask = _make_order(OrderSide.ask, price=900, quantity=300)
        trades, _ = match_order(ask, book)

        assert len(trades) == 2
        # First match: highest bid (1100)
        assert trades[0].price == 900  # seller's price
        assert trades[0].quantity == 200
        # Second match: next bid (1000), partial
        assert trades[1].price == 900
        assert trades[1].quantity == 100
        assert ask.remaining_quantity == 0
        assert ask.status == OrderStatus.closed
        assert bid2.remaining_quantity == 100  # partial fill


class TestMarketOrders:
    def test_market_buy_matches_any_ask(self):
        """Market buy matches any ask, execution price = ask price."""
        book = OrderBook()
        ask = _make_order(OrderSide.ask, price=1500, quantity=100)
        book.insert(ask)

        bid = _make_order(OrderSide.bid, price=None, quantity=100, order_type=OrderType.market)
        trades, _ = match_order(bid, book)

        assert len(trades) == 1
        assert trades[0].price == 1500  # seller's price
        assert bid.status == OrderStatus.closed
        assert bid.remaining_quantity == 0

    def test_market_sell_matches_any_bid(self):
        """Market sell matches any bid, execution price = counterparty's price."""
        book = OrderBook()
        bid = _make_order(OrderSide.bid, price=1500, quantity=100)
        book.insert(bid)

        ask = _make_order(OrderSide.ask, price=None, quantity=100, order_type=OrderType.market)
        trades, _ = match_order(ask, book)

        assert len(trades) == 1
        assert trades[0].price == 1500  # market seller has no price, use buyer's
        assert ask.status == OrderStatus.closed

    def test_market_buy_ioc_cancel_remainder(self):
        """Market buy with insufficient book → unfilled portion cancelled."""
        book = OrderBook()
        ask = _make_order(OrderSide.ask, price=1000, quantity=50)
        book.insert(ask)

        bid = _make_order(OrderSide.bid, price=None, quantity=100, order_type=OrderType.market)
        trades, _ = match_order(bid, book)

        assert len(trades) == 1
        assert trades[0].quantity == 50
        assert bid.remaining_quantity == 50
        assert bid.status == OrderStatus.closed  # IOC cancel
        # Should NOT be in the book
        assert book.get_best_bid("PETR4") is None

    def test_market_sell_ioc_cancel_empty_book(self):
        """Market sell on empty book → immediately cancelled."""
        book = OrderBook()
        ask = _make_order(OrderSide.ask, price=None, quantity=100, order_type=OrderType.market)
        trades, _ = match_order(ask, book)

        assert len(trades) == 0
        assert ask.remaining_quantity == 100
        assert ask.status == OrderStatus.closed
        assert book.get_best_ask("PETR4") is None

    def test_market_buy_matches_multiple_price_levels(self):
        """Market buy sweeps through multiple ask levels."""
        book = OrderBook()
        book.insert(_make_order(OrderSide.ask, price=1000, quantity=50))
        book.insert(_make_order(OrderSide.ask, price=1100, quantity=50))
        book.insert(_make_order(OrderSide.ask, price=1200, quantity=50))

        bid = _make_order(OrderSide.bid, price=None, quantity=120, order_type=OrderType.market)
        trades, _ = match_order(bid, book)

        assert len(trades) == 3
        assert trades[0].price == 1000
        assert trades[1].price == 1100
        assert trades[2].price == 1200
        assert trades[2].quantity == 20  # partial on third
        assert bid.remaining_quantity == 0
        assert bid.status == OrderStatus.closed


class TestExpiredOrders:
    def test_expired_counterparty_skipped(self):
        """Expired ask is skipped, matching continues to next."""
        book = OrderBook()
        expired_ask = _make_order(
            OrderSide.ask, price=1000, quantity=100,
            valid_until=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        good_ask = _make_order(OrderSide.ask, price=1050, quantity=100)
        book.insert(expired_ask)
        book.insert(good_ask)

        bid = _make_order(OrderSide.bid, price=1100, quantity=100)
        trades, expired = match_order(bid, book)

        assert len(trades) == 1
        assert trades[0].price == 1050
        assert len(expired) == 1
        assert expired[0] is expired_ask
        assert expired_ask.status == OrderStatus.closed

    def test_all_counterparties_expired(self):
        """All asks expired → order rests in book."""
        book = OrderBook()
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        book.insert(_make_order(OrderSide.ask, price=1000, valid_until=past))
        book.insert(_make_order(OrderSide.ask, price=1050, valid_until=past))

        bid = _make_order(OrderSide.bid, price=1100, quantity=100)
        trades, expired = match_order(bid, book)

        assert len(trades) == 0
        assert len(expired) == 2
        assert bid.status == OrderStatus.open
        assert book.get_best_bid("PETR4") is not None


class TestFIFOPriority:
    def test_fifo_within_same_price(self):
        """Two asks at same price — first inserted matches first."""
        book = OrderBook()
        ask1 = _make_order(OrderSide.ask, price=1000, quantity=100, broker_id=BROKER_A)
        ask2 = _make_order(OrderSide.ask, price=1000, quantity=100, broker_id=BROKER_B)
        book.insert(ask1)
        book.insert(ask2)

        bid = _make_order(OrderSide.bid, price=1000, quantity=100)
        trades, _ = match_order(bid, book)

        assert len(trades) == 1
        assert trades[0].sell_order_id == ask1.id
        assert trades[0].seller_broker_id == BROKER_A
        # ask2 should still be in book
        best = book.get_best_ask("PETR4")
        assert best is not None
        assert best[1][0] is ask2

    def test_price_priority_over_fifo(self):
        """Ask at 900 matches before ask at 1000, even if 1000 was inserted first."""
        book = OrderBook()
        ask_1000 = _make_order(OrderSide.ask, price=1000, quantity=100)
        ask_900 = _make_order(OrderSide.ask, price=900, quantity=100)
        book.insert(ask_1000)
        book.insert(ask_900)

        bid = _make_order(OrderSide.bid, price=1000, quantity=100)
        trades, _ = match_order(bid, book)

        assert len(trades) == 1
        assert trades[0].price == 900
        assert trades[0].sell_order_id == ask_900.id
