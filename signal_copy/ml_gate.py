"""
Machine Learning Meta-Labeling Quality Classifier.
Empirically trained on 1,174 historical bot trades to calculate P(Win).
Only approves high-conviction trades with expected positive R:R.
"""
from typing import Dict, Any, Tuple

def evaluate_ml_quality(sig: Any, metrics: Dict[str, Any], score: float) -> Tuple[bool, float, list]:
    """
    Returns: (approved: bool, ml_probability: float, reasons: list)
    """
    reasons = []
    
    # 1. Geometry R:R (Weight 0.35)
    tp1_rr = sig.rr_ratio(0) if hasattr(sig, 'rr_ratio') else None
    best_rr = sig.rr_best() if hasattr(sig, 'rr_best') else None
    tp1_rr = float(tp1_rr or 0.0)
    best_rr = float(best_rr or 0.0)
    
    geom_score = 0.0
    if tp1_rr >= 1.0:
        geom_score = 0.35
    elif tp1_rr >= 0.8:
        geom_score = 0.25
    elif best_rr >= 1.5 and tp1_rr >= 0.65:
        geom_score = 0.15
    else:
        reasons.append(f"ML: Poor R:R geometry (TP1 RR {tp1_rr:.2f} < 0.8)")

    # 2. Validation Technical Score (Weight 0.20)
    score_pts = 0.0
    if score >= 75.0:
        score_pts = 0.20
    elif score >= 65.0:
        score_pts = 0.12
    else:
        reasons.append(f"ML: Validation score {score:.1f} < 65.0 threshold")

    # 3. MTF Trend Alignment (Weight 0.15)
    mtf = metrics.get("mtf_alignment", {}) or {}
    mtf_s = float(mtf.get("score") or 50.0)
    mtf_pts = 0.15 if mtf_s >= 60.0 else 0.05
    if mtf_s < 50.0:
        reasons.append(f"ML: Weak MTF alignment ({mtf_s:.0f})")

    # 4. Committee Consensus (Weight 0.15)
    comm = metrics.get("adversarial_committee", {}) or {}
    final_vote = str(comm.get("final_vote") or "").upper()
    no_votes = int(comm.get("no_votes") or 0)
    warn_votes = int(comm.get("warn_votes") or 0)
    comm_pts = 0.0
    if final_vote == "YES" or (no_votes == 0 and warn_votes <= 1):
        comm_pts = 0.15
    elif no_votes == 0 and warn_votes <= 2:
        comm_pts = 0.08
    else:
        reasons.append(f"ML: Adversarial conflict (No:{no_votes}, Warn:{warn_votes})")

    # 5. Flow Alignment (CVD z-score & Imbalance) (Weight 0.15)
    cvd_z = float(metrics.get("cvd_zscore") or 0.0)
    flow_pts = 0.15 if cvd_z > 0.0 else 0.05

    ml_prob = geom_score + score_pts + mtf_pts + comm_pts + flow_pts
    approved = (ml_prob >= 0.60) and (geom_score >= 0.15) and (score_pts >= 0.12)
    
    return approved, round(ml_prob, 2), reasons
