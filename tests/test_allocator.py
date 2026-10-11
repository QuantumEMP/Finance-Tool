from decimal import Decimal

from agent.allocator import propose_orders, round_down, round_up, target_buy_value
from agent.models import OrderConstraints, Portfolio, Side, Signal
from tests.fakes import WHOLE, make_limits

D = Decimal
LIMITS = make_limits()


def sig(symbol, action, conf):
    return Signal(symbol, action, conf, f"{action} {symbol}")


def run(signals, portfolio, constraints=None):
    cons = constraints if constraints is not None else {s: WHOLE for s in portfolio.prices}
    return propose_orders("test", {s.symbol: s for s in signals}, portfolio, cons, LIMITS)


def test_rounding():
    assert round_down(D("7.98"), D("1")) == D("7")
    assert round_down(D("100.2049"), D("0.01")) == D("100.20")
    assert round_up(D("99.801"), D("0.01")) == D("99.81")


def test_target_buy_value_takes_tightest_limit():
    eq = D("10000")
    assert target_buy_value(0.8, eq, D("0"), D("10000"), LIMITS) == D("800")       # order cap
    assert target_buy_value(0.8, eq, D("1500"), D("10000"), LIMITS) == D("500")    # position cap
    assert target_buy_value(0.8, eq, D("0"), D("1300"), LIMITS) == D("300")        # cash reserve
    assert target_buy_value(0.8, eq, D("0"), D("900"), LIMITS) == D("0")           # below reserve


def test_buy_sizing_uses_free_cash():
    # 1500 cash, 400 held by an open buy, 1000 reserve: only 100 left to spend, so no order.
    pf = Portfolio(D("1500"), {"QQQ": D("85")}, {"SPY": D("100"), "QQQ": D("100")},
                   spendable=D("1100"))
    assert run([sig("SPY", "BUY", 0.9)], pf) == []


def test_buy_sizing():
    orders = run([sig("SPY", "BUY", 0.8)], Portfolio(D("10000"), {}, {"SPY": D("100")}))
    assert len(orders) == 1
    o = orders[0]
    assert (o.side, o.qty, o.limit_price, o.ref_price) == (Side.BUY, D("7"), D("100.20"), D("100"))
    assert o.account == "test" and o.reasoning == "BUY SPY"


def test_cash_reserve_limits_buy():
    pf = Portfolio(D("1500"), {"QQQ": D("85")}, {"SPY": D("100"), "QQQ": D("100")})
    assert run([sig("SPY", "BUY", 0.8)], pf)[0].qty == D("4")


def test_position_cap_limits_buy():
    pf = Portfolio(D("8500"), {"SPY": D("15")}, {"SPY": D("100")})
    assert run([sig("SPY", "BUY", 0.8)], pf)[0].qty == D("4")


def test_two_buys_share_cash():
    pf = Portfolio(D("2000"), {"VTI": D("80")},
                   {"AAA": D("100"), "BBB": D("100"), "VTI": D("100")})
    orders = run([sig("AAA", "BUY", 1.0), sig("BBB", "BUY", 1.0)], pf)
    assert [(o.symbol, o.qty) for o in orders] == [("AAA", D("9"))]


def test_sell_portion_by_confidence():
    pf = Portfolio(D("9000"), {"SPY": D("10")}, {"SPY": D("100")})
    o = run([sig("SPY", "SELL", 0.6)], pf)[0]
    assert (o.side, o.qty, o.limit_price) == (Side.SELL, D("6"), D("99.80"))


def test_sell_does_not_leave_dust():
    cons = {"XBT": OrderConstraints(D("1"), D("0.1"), D("0.01"))}
    pf = Portfolio(D("0"), {"XBT": D("1.2")}, {"XBT": D("100")})
    assert run([sig("XBT", "SELL", 0.6)], pf, cons)[0].qty == D("1.2")


def test_no_order_cases():
    pf = Portfolio(D("10000"), {}, {"SPY": D("100")})
    assert run([sig("SPY", "SELL", 0.9)], pf) == []          # nothing to sell
    assert run([sig("SPY", "HOLD", 0.9)], pf) == []
    assert run([sig("SPY", "BUY", 0.5)], pf) == []           # below min_confidence
    assert run([sig("SPY", "BUY", 0.9)], pf, constraints={}) == []  # no constraints
    assert run([sig("SPY", "BUY", 0.9)], Portfolio(D("0"), {}, {"SPY": D("100")})) == []
    pf_hold = Portfolio(D("10000"), {"SPY": D("10")}, {"SPY": D("100")})
    assert run([sig("SPY", "SELL", 0.5)], pf_hold) == []  # SELL below min_confidence


def test_zero_price_skipped():
    pf = Portfolio(D("10000"), {"SPY": D("5")}, {"SPY": D("0")})
    assert run([sig("SPY", "BUY", 0.9)], pf) == []
    assert run([sig("SPY", "SELL", 0.9)], pf) == []
