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


FRACTIONAL = OrderConstraints(D("0.0001"), D("0.0001"), D("0.01"), min_notional=D("1"))


def test_small_account_buys_a_fraction_of_an_expensive_etf():
    # $2,000 account, SPY at $600: a 0.9 BUY is worth $180, less than one share.
    pf = Portfolio(D("2000"), {}, {"SPY": D("600")})
    assert run([sig("SPY", "BUY", 0.9)], pf) == []  # whole shares: nothing
    [o] = run([sig("SPY", "BUY", 0.9)], pf, {"SPY": FRACTIONAL})
    assert o.limit_price == D("601.20") and o.qty == D("0.2994")
    assert o.value <= D("180")


def test_orders_below_min_notional_are_skipped():
    # 1000 equity, 0.6 BUY: 1000 * 10% * 0.6 = 60 of SPY.
    pf = Portfolio(D("1000"), {}, {"SPY": D("100")})
    ok = OrderConstraints(D("0.0001"), D("0.0001"), D("0.01"), min_notional=D("50"))
    too_small = OrderConstraints(D("0.0001"), D("0.0001"), D("0.01"), min_notional=D("100"))
    assert len(run([sig("SPY", "BUY", 0.6)], pf, {"SPY": ok})) == 1
    assert run([sig("SPY", "BUY", 0.6)], pf, {"SPY": too_small}) == []


def test_fee_reduces_spend_to_stay_above_the_cash_reserve():
    eq = D("10000")
    # 1300 cash, 1000 reserve: 300 available including a 1% fee → 297.03 of order value.
    assert target_buy_value(0.8, eq, D("0"), D("1300"), LIMITS, D("1")) == D("300") / D("1.01")
    # The fee doesn't change the order or position caps.
    assert target_buy_value(0.8, eq, D("0"), D("10000"), LIMITS, D("1")) == D("800")


def test_fees_count_against_cash_across_buys():
    # 10000 equity, 1700 cash, 1000 reserve: 700 to spend in total, fees included.
    pf = Portfolio(D("1700"), {"VTI": D("83")}, {"SPY": D("100"), "QQQ": D("100"), "VTI": D("100")})
    signals = {s: sig(s, "BUY", 0.8) for s in ("QQQ", "SPY")}
    cons = {s: FRACTIONAL for s in pf.prices}
    no_fee = propose_orders("test", signals, pf, cons, LIMITS)
    with_fee = propose_orders("test", signals, pf, cons, LIMITS, D("1"))
    assert sum(o.value for o in no_fee) <= D("700")
    assert sum(o.value * D("1.01") for o in with_fee) <= D("700")
    assert sum(o.qty for o in with_fee) < sum(o.qty for o in no_fee)
