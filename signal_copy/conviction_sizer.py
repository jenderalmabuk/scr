"""Dynamic Conviction-Based Position Sizing.

Determines how much to risk per trade dynamically based on:
- Adversarial Committee decision (specialists consensus, warnings, vetos)
- Real-time Market Data (CVD flow, open interest, regime)
- Channel historical performance (win rate, reputation via Fractional Kelly)
- Multi-timeframe alignment (MTF trend following)
- Signal quality & geometry (RR, clarity)
"""

import os
import logging
from typing import Dict, Any, Optional, Tuple
from .channel_performance import get_tracker

logger = logging.getLogger(__name__)


class ConvictionSizer:
    """Calculate risk dynamically based on signal conviction and adversarial consensus."""

    BASE_RISK_PCT = 0.005  # 0.5%
    MAX_RISK_PCT = 0.025   # up to 2.5%

    def __init__(self):
        self.ch_perf = get_tracker()

    def calc(self, signal, metrics: Dict[str, Any]) -> float:
        """Return risk_pct based on multiple conviction factors (legacy fraction of equity)."""
        risk = self.BASE_RISK_PCT

        # 1. Channel reputation
        src_id = getattr(signal, "source_chat_id", None)
        if src_id is not None:
            rep = self.ch_perf.get_reputation_score(src_id)
            risk += (rep - 50.0) / 100.0 * 0.005
            risk += max(0, (self.ch_perf.get_channel_stats(src_id).get("signals", 0) - 10)) * 0.0002

        # 2. MTF alignment
        mtf = metrics.get("mtf_alignment")
        if mtf and isinstance(mtf, dict):
            score = float(mtf.get("score", 50.0))
            risk += (score - 50.0) / 50.0 * 0.004

        # 3. Adversarial vote adjustment
        committee = metrics.get("adversarial_committee") or {}
        adv_vote = str(committee.get("final_vote", "")).upper()
        if adv_vote == "YES":
            risk += 0.004
        elif adv_vote == "WARN":
            risk -= 0.002
        elif adv_vote == "NO":
            risk -= 0.004

        # 4. Whale / orderflow pressure
        flow = str(metrics.get("flow_direction", "")).upper()
        cvd_z = float(metrics.get("cvd_zscore", 0.0) or 0.0)
        is_long = getattr(signal, "is_long", True)
        if (is_long and cvd_z > 0.8) or (not is_long and cvd_z < -0.8):
            risk += 0.003
        elif (is_long and cvd_z < -0.8) or (not is_long and cvd_z > 0.8):
            risk -= 0.003

        # Clamp
        return max(0.0025, min(risk, self.MAX_RISK_PCT))

    def calc_risk_usd(
        self,
        signal,
        metrics: Dict[str, Any],
        max_risk_usd: float = 2.0,
        validation_score: float = 65.0,
    ) -> Tuple[float, str, Dict[str, Any]]:
        """
        Calculate exact dynamic risk in USD capped at max_risk_usd ($2.00).

        Returns: (target_risk_usd, sizing_tier, details_dict)
        - If vetoed (toxic fakeout): target_risk_usd = 0.0
        - Tier 1 (High Conviction): $1.70 - $2.00
        - Tier 2 (Moderate Conviction): $1.00 - $1.50
        - Tier 3 (Low Conviction / Probe): $0.50 - $0.75
        """
        enabled = os.getenv("ADVERSARIAL_DYNAMIC_SIZING_ENABLED", "true").lower() == "true"
        if not enabled:
            return max_risk_usd, "TIER_1_FLAT", {"multiplier": 1.0, "reason": "DYNAMIC_SIZING_DISABLED"}

        min_risk_usd = float(os.getenv("ADVERSARIAL_SIZING_MIN_RISK_USD", "0.50"))
        veto_enabled = os.getenv("ADVERSARIAL_SIZING_VETO_ENABLED", "true").lower() == "true"

        # 1. Parse Adversarial Committee
        committee = metrics.get("adversarial_committee") or {}
        final_vote = str(committee.get("final_vote", "NONE")).upper()
        no_votes = int(committee.get("no_votes", 0))
        warn_votes = int(committee.get("warn_votes", 0))
        adv_score = float(committee.get("score", 0.0))
        top_reasons = committee.get("top_reasons", [])

        # 2. Parse Orderflow & Market Data
        flow = str(metrics.get("flow_direction", "")).upper().replace(" ", "_")
        cvd_z = float(metrics.get("cvd_zscore", 0.0) or 0.0)
        is_long = getattr(signal, "is_long", True)

        # Opposing flow (e.g. LONG while CVD is heavily negative)
        flow_opposing = (is_long and cvd_z < -0.8) or (not is_long and cvd_z > 0.8)
        flow_toxic = (is_long and cvd_z < -1.4) or (not is_long and cvd_z > 1.4)

        # 3. Check Veto Conditions
        if veto_enabled and (
            (final_vote == "NO" and (flow_toxic or (flow == "NO_TRADE" and flow_opposing)))
            or (no_votes >= 2 and flow_toxic)
        ):
            details = {
                "multiplier": 0.0,
                "reason": f"ADVERSARIAL_VETO_TOXIC_FLOW: cvd={cvd_z:+.2f}, flow={flow}",
                "final_vote": final_vote,
                "top_reasons": top_reasons,
            }
            return 0.0, "VETO", details

        # 4. Compute Adversarial Multiplier (M_adv)
        if final_vote == "YES":
            if flow_opposing:
                m_adv = 0.85
            else:
                m_adv = 1.00
        elif final_vote == "WARN":
            if warn_votes <= 1 and not flow_opposing:
                m_adv = 0.75
            elif flow_opposing or flow == "NO_TRADE":
                m_adv = 0.50
            else:
                m_adv = 0.60
        elif final_vote == "NO":
            # Allowed as probe if not toxic
            m_adv = 0.30
        else:
            # Fallback if committee didn't run
            m_adv = 0.80

        # 5. Compute Validation Score Multiplier (M_score)
        score = float(validation_score or metrics.get("validation_score", 65.0) or 65.0)
        if score >= 80:
            m_score = 1.05
        elif score >= 70:
            m_score = 1.00
        elif score >= 65:
            m_score = 0.90
        else:
            m_score = 0.80

        # 6. Compute Channel Multiplier (Fractional Kelly track record)
        src_id = getattr(signal, "source_chat_id", None)
        m_channel = 1.0
        if src_id is not None:
            stats = self.ch_perf.get_channel_stats(src_id)
            sig_count = stats.get("signals", 0)
            win_rate = stats.get("win_rate", 0.5)
            if sig_count >= 5:
                if win_rate >= 0.65:
                    m_channel = 1.10
                elif win_rate >= 0.50:
                    m_channel = 1.00
                else:
                    m_channel = 0.80
            else:
                m_channel = 0.95  # new channel buffer

        # 7. Combined Multiplier & Clamp
        combined_m = m_adv * m_score * m_channel
        raw_risk = max_risk_usd * combined_m

        target_risk = round(max(min_risk_usd, min(max_risk_usd, raw_risk)), 2)

        # Classify Tier
        if target_risk >= 1.70:
            tier = "TIER_1_HIGH"
        elif target_risk >= 1.00:
            tier = "TIER_2_MODERATE"
        else:
            tier = "TIER_3_PROBE"

        details = {
            "multiplier": round(combined_m, 3),
            "m_adv": round(m_adv, 2),
            "m_score": round(m_score, 2),
            "m_channel": round(m_channel, 2),
            "final_vote": final_vote,
            "validation_score": score,
            "target_risk_usd": target_risk,
            "max_risk_usd": max_risk_usd,
            "reasons": top_reasons[:2],
        }

        return target_risk, tier, details
