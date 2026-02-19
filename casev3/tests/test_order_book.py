import uuid
from datetime import datetime, timezone

from app.engine import Order, Engine
from app.engine.order_book import OrderBook
from app.models import OrderSide, OrderType, OrderStatus


def _make_order(
    side: OrderSide = OrderSide.ask,
    symbol: str = "PETR4",
    price: int = 1000,
    quantity: int = 100,
) -> Order:
    now = datetime.now(timezone.utc)
    return Order(
        id=uuid.uuid4(),
        broker_id=uuid.uuid4(),
        symbol=symbol,
        side=side,
        order_type=OrderType.limit,
        price=price,
        quantity=quantity,
        remaining_quantity=quantity,
        status=OrderStatus.open,
        document_number="12345678901",
        valid_until=now,
        created_at=now,
    )


class TestOrderBookInsert:
    def test_insert_ask(self):
        book = OrderBook()
        order = _make_order(side=OrderSide.ask, price=1000)
        book.insert(order)

        assert "PETR4" in book.asks
        assert 1000 in book.asks["PETR4"]
        assert book.asks["PETR4"][1000][0] is order

    def test_insert_bid(self):
        book = OrderBook()
        order = _make_order(side=OrderSide.bid, price=1000)
        book.insert(order)

        assert "PETR4" in book.bids
        assert 1000 in book.bids["PETR4"]
        assert book.bids["PETR4"][1000][0] is order

    def test_insert_multiple_at_same_price_fifo(self):
        book = OrderBook()
        o1 = _make_order(side=OrderSide.ask, price=1000)
        o2 = _make_order(side=OrderSide.ask, price=1000)
        book.insert(o1)
        book.insert(o2)

        dq = book.asks["PETR4"][1000]
        assert len(dq) == 2
        assert dq[0] is o1
        assert dq[1] is o2

    def test_insert_multiple_prices_sorted(self):
        book = OrderBook()
        book.insert(_make_order(side=OrderSide.ask, price=1100))
        book.insert(_make_order(side=OrderSide.ask, price=900))
        book.insert(_make_order(side=OrderSide.ask, price=1000))

        keys = list(book.asks["PETR4"].keys())
        assert keys == [900, 1000, 1100]


class TestOrderBookRemoveFront:
    def test_remove_front_deletes_price_level_when_empty(self):
        book = OrderBook()
        book.insert(_make_order(side=OrderSide.ask, price=1000))
        book.remove_front("PETR4", OrderSide.ask, 1000)

        assert 1000 not in book.asks["PETR4"]

    def test_remove_front_keeps_remaining(self):
        book = OrderBook()
        o1 = _make_order(side=OrderSide.ask, price=1000)
        o2 = _make_order(side=OrderSide.ask, price=1000)
        book.insert(o1)
        book.insert(o2)

        book.remove_front("PETR4", OrderSide.ask, 1000)

        dq = book.asks["PETR4"][1000]
        assert len(dq) == 1
        assert dq[0] is o2


class TestOrderBookBestAskBid:
    def test_best_ask_returns_lowest(self):
        book = OrderBook()
        book.insert(_make_order(side=OrderSide.ask, price=1100))
        book.insert(_make_order(side=OrderSide.ask, price=900))

        price, dq = book.get_best_ask("PETR4")
        assert price == 900

    def test_best_bid_returns_highest(self):
        book = OrderBook()
        book.insert(_make_order(side=OrderSide.bid, price=900))
        book.insert(_make_order(side=OrderSide.bid, price=1100))

        price, dq = book.get_best_bid("PETR4")
        assert price == 1100

    def test_best_ask_empty_returns_none(self):
        book = OrderBook()
        assert book.get_best_ask("PETR4") is None

    def test_best_bid_empty_returns_none(self):
        book = OrderBook()
        assert book.get_best_bid("PETR4") is None

    def test_best_ask_empty_after_remove(self):
        book = OrderBook()
        book.insert(_make_order(side=OrderSide.ask, price=1000))
        book.remove_front("PETR4", OrderSide.ask, 1000)

        # SortedDict is empty but still in asks dict — should return None
        assert book.get_best_ask("PETR4") is None


class TestOrderBookClear:
    def test_clear_resets_all(self):
        book = OrderBook()
        book.insert(_make_order(side=OrderSide.ask, price=1000))
        book.insert(_make_order(side=OrderSide.bid, price=900))
        book.insert(_make_order(side=OrderSide.ask, price=1000, symbol="VALE3"))

        book.clear()

        assert len(book.asks) == 0
        assert len(book.bids) == 0


class TestEngine:
    def test_engine_clear(self):
        eng = Engine()
        order = _make_order()
        eng.orders[order.id] = order
        eng.book.insert(order)
        eng.brokers_by_key_hash["abc"] = uuid.uuid4()
        eng.queue.put_nowait("item")

        eng.clear()

        assert len(eng.orders) == 0
        assert len(eng.book.asks) == 0
        assert len(eng.brokers_by_key_hash) == 0
        assert eng.queue.empty()
