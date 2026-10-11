from decimal import Decimal

from agent.models import Portfolio, ProposedOrder, Side
from agent.risk import RiskContext, check_account, check_order
from tests.fakes import make_limits

D = Decimal


def ctx(cash="10000", positions=None, prices=None, **kw):
    pf = Portfolio(D(cash), positions or {}, prices or {"SPY": D("100"), "VTI": D("100")})
    base = dict(portfolio=pf, limits=make_limits(), allowlist=frozenset({"SPY", "VTI"}),
                orders_today=0, day_start_equity=None, peak_equity=None, paused=False)
    base.update(kw)
    return RiskContext(**base)


def order(qty="5", price="100", side=Side.BUY, symbol="SPY"):
    return ProposedOrder("test", symbol, side, D(qty), D(price), D(price), 0.8, "r")


def test_normal_buy_passes():
    assert check_order(order(), ctx()) is None


def test_not_allowlisted():
    assert "allowlist" in check_order(order(symbol="GME"), ctx())


def test_invalid_qty():
    assert "invalid" in check_order(order(qty="0"), ctx())


def test_order_too_large():
    assert "exceeds" in check_order(order(qty="11"), ctx())


def test_cash_reserve_uses_free_cash():
    # 1500 cash but 500 is held by an open buy: a 501 buy leaves 499 free, under the 1000 reserve.
    pf = Portfolio(D("1500"), {"VTI": D("85")}, {"SPY": D("100"), "VTI": D("100")},
                   spendable=D("1000"))
    c = ctx(portfolio=pf)
    assert check_order(order(qty="5"), ctx(cash="1500", positions={"VTI": D("85")})) is None
    assert "cash reserve" in check_order(order(qty="5"), c)


def test_position_cap():
    c = ctx(cash="8500", positions={"SPY": D("15")})
    assert "position" in check_order(order(qty="6"), c)


def test_cash_reserve():
    c = ctx(cash="1200", positions={"VTI": D("88")})
    assert "reserve" in check_order(order(qty="5"), c)


def test_daily_order_limit():
    assert "daily order limit" in check_order(order(), ctx(orders_today=5))


def test_sell_more_than_held():
    c = ctx(cash="9000", positions={"SPY": D("3")})
    assert "shorting" in check_order(order(qty="4", side=Side.SELL), c)
    assert check_order(order(qty="3", side=Side.SELL), c) is None


def test_sell_larger_than_order_cap_passes():
    # 20 SPY @ 100 is 20% of equity, above the 10% order cap; a full exit must still pass.
    c = ctx(cash="8000", positions={"SPY": D("20")})
    assert check_order(order(qty="20", side=Side.SELL), c) is None


def test_sell_still_subject_to_account_checks():
    c = ctx(cash="8000", positions={"SPY": D("20")}, paused=True)
    assert "paused" in check_order(order(qty="20", side=Side.SELL), c)
    c = ctx(cash="8000", positions={"SPY": D("20")}, orders_today=5)
    assert "daily order limit" in check_order(order(qty="20", side=Side.SELL), c)


def test_paused():
    assert "paused" in check_order(order(), ctx(paused=True))


def test_daily_loss_halt():
    halt = check_account(ctx(day_start_equity=D("10400")))
    assert halt is not None and halt.kill_switch is False and "daily loss" in halt.reason


def test_drawdown_kill_switch_takes_precedence():
    halt = check_account(ctx(day_start_equity=D("10400"), peak_equity=D("12000")))
    assert halt.kill_switch is True and "drawdown" in halt.reason


def test_zero_equity_halts():
    assert check_account(ctx(cash="0", prices={})) is not None


def test_cash_reserve_includes_the_fee():
    # 10000 equity, 1500 cash, 1000 reserve: a 500 buy fits exactly, but not with a 1% fee on top.
    assert check_order(order(qty="5"), ctx(cash="1500", positions={"VTI": D("85")})) is None
    c = ctx(cash="1500", positions={"VTI": D("85")}, fee_pct=D("1"))
    assert "cash reserve" in check_order(order(qty="5"), c)
