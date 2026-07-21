"""Parser tests for the H0STASP0 order-book frame.

Feeds synthetic 59-field records through parse_data_frame and asserts the
full ask/bid ladders come out right, including truncation at the first empty
level (a valid ladder ends at the first hole). Deterministic, no network.
"""

from __future__ import annotations

from etf_arb.ws_client import ASP_RECORD_LEN, TR_ID_ASP, parse_data_frame


def build_asp_record(
    askp: list[int],
    askp_qty: list[int],
    bidp: list[int],
    bidp_qty: list[int],
    code: str = "233740",
    hour_cls: str = "0",
) -> str:
    """Build one 59-field H0STASP0 record payload (10 levels per side).

    Field layout (verified): code[0], BSOP_HOUR[1], HOUR_CLS_CODE[2],
    ASKP1..10[3..12], BIDP1..10[13..22], ASKP_RSQN1..10[23..32],
    BIDP_RSQN1..10[33..42], then 16 totals/expected filler fields [43..58].
    """
    assert len(askp) == len(askp_qty) == len(bidp) == len(bidp_qty) == 10
    fields = ["0"] * ASP_RECORD_LEN
    fields[0] = code
    fields[1] = "100000"
    fields[2] = hour_cls
    for i in range(10):
        fields[3 + i] = str(askp[i])
        fields[13 + i] = str(bidp[i])
        fields[23 + i] = str(askp_qty[i])
        fields[33 + i] = str(bidp_qty[i])
    return "^".join(fields)


def parse_one(payload: str, ts: float = 123.0):
    raw = f"0|{TR_ID_ASP}|001|{payload}"
    events, error = parse_data_frame(raw, ts)
    return events, error


class TestAspLadderParsing:
    def test_full_ladder_extracted(self):
        askp = [9940, 9945, 9950, 9955, 9960, 0, 0, 0, 0, 0]
        askp_qty = [100, 200, 300, 400, 500, 0, 0, 0, 0, 0]
        bidp = [9935, 9930, 9925, 0, 0, 0, 0, 0, 0, 0]
        bidp_qty = [150, 250, 350, 0, 0, 0, 0, 0, 0, 0]
        events, error = parse_one(
            build_asp_record(askp, askp_qty, bidp, bidp_qty)
        )
        assert error is None
        assert len(events) == 1
        ev = events[0]
        assert ev["type"] == "quote"
        assert ev["code"] == "233740"
        assert ev["hour_cls_code"] == "0"
        # backward-compat top-of-book keys == ladder[0]
        assert ev["ask1"] == 9940 and ev["ask1_qty"] == 100
        assert ev["bid1"] == 9935 and ev["bid1_qty"] == 150
        # ladders truncate at the first empty level
        assert ev["ask_ladder"] == [
            (9940, 100), (9945, 200), (9950, 300), (9955, 400), (9960, 500)
        ]
        assert ev["bid_ladder"] == [(9935, 150), (9930, 250), (9925, 350)]

    def test_zero_qty_mid_ladder_truncates(self):
        # A zero qty (even with a nonzero price) ends the ladder.
        askp = [9940, 9945, 9950, 0, 0, 0, 0, 0, 0, 0]
        askp_qty = [100, 0, 300, 0, 0, 0, 0, 0, 0, 0]
        bidp = [9935, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        bidp_qty = [150, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        events, error = parse_one(
            build_asp_record(askp, askp_qty, bidp, bidp_qty)
        )
        assert error is None
        ev = events[0]
        assert ev["ask_ladder"] == [(9940, 100)]
        assert ev["bid_ladder"] == [(9935, 150)]

    def test_all_ten_levels(self):
        askp = list(range(10000, 10100, 10))  # 10 ascending prices
        askp_qty = [(i + 1) * 10 for i in range(10)]
        bidp = list(range(9990, 9890, -10))    # 10 descending prices
        bidp_qty = [(i + 1) * 5 for i in range(10)]
        events, _ = parse_one(build_asp_record(askp, askp_qty, bidp, bidp_qty))
        ev = events[0]
        assert len(ev["ask_ladder"]) == 10
        assert len(ev["bid_ladder"]) == 10
        assert ev["ask_ladder"][0] == (10000, 10)
        assert ev["ask_ladder"][-1] == (10090, 100)
        assert ev["bid_ladder"][-1] == (9900, 50)
