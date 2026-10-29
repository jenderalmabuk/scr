import unittest
from signal_copy.signal_schema import ParsedSignal, SignalSide
from signal_copy.validation_engine import validate_signal, Verdict


def _mk_sig(symbol="BTCUSDT", side=SignalSide.LONG, entry=100.0, sl=95.0, tp=110.0):
    return ParsedSignal(
        symbol=symbol,
        side=side,
        entry_low=entry,
        entry_high=entry,
        stop_loss=sl,
        take_profits=[tp],
    )


class TestResilienceFilterComponents(unittest.TestCase):
    def test_anti_average_trap_downgrades_when_component_collapsed(self):
        sig = _mk_sig()
        # Price is fresh (22/22), TradingView is max (15/15), but MTF is completely collapsed (score=0, misaligned -> 2.25/15 = 15% < 25%)
        metrics = {
            "price": 100.0,
            "data_valid": True,
            "rsi": 50.0,
            "cvd_zscore": 1.5,
            "funding_rate": 0.0001,
            "mtf_alignment": {"score": 0.0, "tf4h_trend": "SHORT", "d1_trend": "SHORT"}, # collapsed
            "tradingview": {"score": 100.0},
        }
        res = validate_signal(sig, metrics)
        # Even though raw score might be high, Anti-Average Trap must downgrade to WEAK!
        self.assertEqual(res.verdict, Verdict.WEAK)
        self.assertIsNotNone(res.metrics_snapshot.get("anti_average_trap_flag"))

    def test_moderate_signal_with_consistent_components_is_valid(self):
        sig = _mk_sig()
        # All components are consistent and healthy (none collapsed)
        metrics = {
            "price": 100.0,
            "data_valid": True,
            "rsi": 52.0,
            "cvd_zscore": 0.8,
            "funding_rate": 0.0001,
            "mtf_alignment": {"score": 60.0, "tf4h_trend": "LONG", "d1_trend": "FLAT"}, # aligned 4h (30%+)
            "tradingview": {"score": 60.0},
            "regime_label": "TRENDING",
        }
        res = validate_signal(sig, metrics)
        self.assertGreaterEqual(res.score, 50.0)
        self.assertEqual(res.verdict, Verdict.VALID)

    def test_moderate_macro_dump_applies_penalty(self):
        sig = _mk_sig()
        metrics = {
            "price": 100.0,
            "data_valid": True,
            "rsi": 55.0,
            "cvd_zscore": 1.2,
            "funding_rate": 0.0001,
            "mtf_alignment": {"score": 80.0, "tf4h_trend": "LONG", "d1_trend": "LONG"},
            "tradingview": {"score": 80.0},
            "btc_gate": {
                "blocked": True,
                "reason": "BTC Dump Risk on High-Beta Pair",
                "btc_diff_pct": -0.60, # moderate dump
                "btc_ret_15m": -0.40,
            },
        }
        res = validate_signal(sig, metrics)
        # It should NOT be hard-blocked by HARD_VETO_EXTREME_DUMP
        self.assertNotIn("HARD_VETO_EXTREME_DUMP", " ".join(res.hard_blocks))
        self.assertEqual(res.metrics_snapshot.get("macro_penalty_applied"), 18.0)

    def test_extreme_macro_dump_triggers_hard_veto(self):
        sig = _mk_sig()
        metrics = {
            "price": 100.0,
            "data_valid": True,
            "btc_gate": {
                "blocked": True,
                "reason": "BTC Dump Risk on High-Beta Pair",
                "btc_diff_pct": -2.20, # extreme cascade
                "btc_ret_15m": -1.80,
            },
        }
        res = validate_signal(sig, metrics)
        self.assertEqual(res.verdict, Verdict.REJECT)
        self.assertTrue(any("HARD_VETO_EXTREME_DUMP" in b for b in res.hard_blocks))


if __name__ == "__main__":
    unittest.main()
