import unittest
from unittest.mock import patch, MagicMock
from signal_copy.signal_schema import ParsedSignal, SignalSide
from signal_copy.validation_engine import validate_signal, Verdict
import signal_copy.validation_config as vc

class TestShadowCVDAndWhaleShield(unittest.TestCase):
    def setUp(self):
        self.long_signal = ParsedSignal(
            symbol="SOLUSDT",
            side=SignalSide.LONG,
            entry_low=100.0,
            entry_high=100.0,
            stop_loss=95.0,
            take_profits=[105.0, 110.0],
            raw_text="BUY SOLUSDT 100",
            source_chat_id=-100123456789
        )
        self.short_signal = ParsedSignal(
            symbol="SOLUSDT",
            side=SignalSide.SHORT,
            entry_low=100.0,
            entry_high=100.0,
            stop_loss=105.0,
            take_profits=[95.0, 90.0],
            raw_text="SELL SOLUSDT 100",
            source_chat_id=-100123456789
        )
        self.base_metrics = {
            "price": 100.0,
            "cvd_zscore": 1.2,
            "rsi": 50.0,
            "imbalance": 0.1,
            "price_change_15m": 0.5,
            "mtf_alignment": {"score": 75.0, "tf4h_trend": "BULLISH", "d1_trend": "BULLISH"},
            "tradingview": {"score": 75.0},
            "btc_gate": {"blocked": False, "btc_diff_pct": 0.1, "btc_ret_15m": 0.05},
        }

    def test_fakeout_penalty_on_distribution_long(self):
        """LONG signal with 30d DISTRIBUTION must receive -15 confluence penalty and fakeout flag."""
        metrics = dict(self.base_metrics)
        # Without shadow cvd:
        res_baseline = validate_signal(self.long_signal, dict(metrics))
        self.assertGreaterEqual(res_baseline.score, 50.0)

        # With 30d DISTRIBUTION:
        metrics["shadow_cvd"] = {
            "status": "ACTIVE",
            "verdict": "DISTRIBUTION",
            "cvd_7d": -0.45,
            "cvd_30d": -0.65,
            "mom_30d": -0.15
        }
        res_penalized = validate_signal(self.long_signal, metrics)
        expected_score = max(0.0, res_baseline.score - getattr(vc, "SHADOW_CVD_PENALTY", 15.0))
        self.assertAlmostEqual(res_penalized.score, expected_score, places=1)
        self.assertIsNotNone(res_penalized.metrics_snapshot.get("fakeout_risk_flag"))
        self.assertEqual(res_penalized.metrics_snapshot.get("shadow_cvd_penalty_applied"), 15.0)

    def test_fakeout_penalty_on_accumulation_short(self):
        """SHORT signal with 30d ACCUMULATION must receive -15 confluence penalty."""
        metrics = dict(self.base_metrics)
        metrics["mtf_alignment"] = {"score": 75.0, "tf4h_trend": "BEARISH", "d1_trend": "BEARISH"}
        res_baseline = validate_signal(self.short_signal, dict(metrics))

        metrics["shadow_cvd"] = {
            "status": "ACTIVE",
            "verdict": "ACCUMULATION",
            "cvd_7d": 0.40,
            "cvd_30d": 0.55,
            "mom_30d": 0.20
        }
        res_penalized = validate_signal(self.short_signal, metrics)
        expected_score = max(0.0, res_baseline.score - getattr(vc, "SHADOW_CVD_PENALTY", 15.0))
        self.assertAlmostEqual(res_penalized.score, expected_score, places=1)
        self.assertIsNotNone(res_penalized.metrics_snapshot.get("fakeout_risk_flag"))

    def test_whale_accumulation_bonus_long(self):
        """LONG signal with 30d ACCUMULATION must receive +5 confluence bonus."""
        metrics = dict(self.base_metrics)
        res_baseline = validate_signal(self.long_signal, dict(metrics))

        metrics["shadow_cvd"] = {
            "status": "ACTIVE",
            "verdict": "ACCUMULATION",
            "cvd_7d": 0.35,
            "cvd_30d": 0.45,
            "mom_30d": 0.10
        }
        res_boosted = validate_signal(self.long_signal, metrics)
        expected_score = min(100.0, res_baseline.score + getattr(vc, "SHADOW_CVD_BONUS", 5.0))
        self.assertAlmostEqual(res_boosted.score, expected_score, places=1)
        self.assertIsNotNone(res_boosted.metrics_snapshot.get("whale_flow_backed"))

    def test_whale_flush_cascade_hard_block(self):
        """Massive opposing liquidation cascade triggers instant HARD_VETO_WHALE_FLUSH."""
        metrics = dict(self.base_metrics)
        metrics["whale_flush"] = {
            "blocked": True,
            "reason": "WHALE_FLUSH_CASCADE: $2,500,000 long liquidation cascade"
        }
        res = validate_signal(self.long_signal, metrics)
        self.assertEqual(res.verdict, Verdict.REJECT)
        self.assertTrue(any("HARD_VETO_WHALE_FLUSH" in b for b in res.hard_blocks))

    def test_exempt_symbol_no_penalty(self):
        """Bybit only / unlisted symbol with status EXEMPT receives no penalty or bonus."""
        metrics = dict(self.base_metrics)
        res_baseline = validate_signal(self.long_signal, dict(metrics))

        metrics["shadow_cvd"] = {
            "status": "EXEMPT",
            "verdict": "EXEMPT_BYBIT_ONLY",
            "cvd_7d": None,
            "cvd_30d": None,
            "mom_30d": None
        }
        res_exempt = validate_signal(self.long_signal, metrics)
        self.assertAlmostEqual(res_exempt.score, res_baseline.score, places=1)
        self.assertIsNone(res_exempt.metrics_snapshot.get("fakeout_risk_flag"))
        self.assertIsNone(res_exempt.metrics_snapshot.get("shadow_cvd_penalty_applied"))
        self.assertIsNone(res_exempt.metrics_snapshot.get("shadow_cvd_bonus_applied"))

if __name__ == "__main__":
    unittest.main()
