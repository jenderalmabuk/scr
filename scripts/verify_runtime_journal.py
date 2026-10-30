#!/usr/bin/env python3
"""Comprehensive Runtime Journal Concurrency, Durability, and Crash-Recovery Audit Suite

Formal verification covering:
1. Isolated Scratch Path (test_path != prod_path)
2. Gateway Restart Intent Deduplication
3. Tip Event-Hash Invariance on Duplicate Intent Resubmission
4. Multi-Process Lock Contention & Concurrency (5 workers x 10 appends)
5. Monotonic Cryptographic Chain Continuity across Concurrent Writers
6. Partial Tail Mid-Write Crash Detection (incomplete JSON / EOF)
7. Automated Crash Tail Recovery & Valid Record Restoration
8. Recovery Idempotence: recover(recover(journal)) == recover(journal)
9. Strict Refusal to Repair Deliberate Middle-of-File Tampering
10. Production Journal Contamination Invariance Proof
"""
import concurrent.futures
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, "/opt/signalcopyreal")
from gateway.order_intent import OrderIntent
from gateway.service import (
    ExecutionGateway,
    GatewayResult,
    verify_rejected_intents_journal,
    recover_journal_after_crash,
    REJECTED_INTENTS_PATH,
)

def _worker_append(journal_path_str: str, worker_id: int, num_items: int):
    p = Path(journal_path_str)
    with patch("gateway.service.REJECTED_INTENTS_PATH", p):
        gw = ExecutionGateway(risk_mgr=MagicMock(), trader=MagicMock())
        for i in range(num_items):
            intent = OrderIntent(
                intent_id=f"worker_{worker_id}_intent_{i}",
                source="AUTONOMOUS_SCANNER",
                symbol="BTCUSDT",
                side="LONG",
                entry_price=50000.0 + (worker_id * 10) + i,
                sl_price=49000.0,
            )
            gw._append_rejected_intent(intent, GatewayResult(ok=False, reason="CONCURRENT_RACE_TEST"))

def main():
    print("==================================================================")
    print(" FORMAL RUNTIME JOURNAL AUDIT & CONCURRENCY VERIFICATION")
    print("==================================================================")

    prod_path = Path("/opt/signalcopyreal/journal/rejected_intents.jsonl")
    prod_bytes_before = prod_path.read_bytes() if prod_path.exists() else b""
    prod_lines_before = len(prod_bytes_before.splitlines())

    with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as tmp:
        tmp_path = Path(tmp.name)

    scenarios = []

    try:
        # SCENARIO 1: Scratch Isolation Assertion
        assert tmp_path != prod_path, "TEST CONTAMINATION HAZARD: tmp_path equals prod_path!"
        scenarios.append(("Scenario 1: Scratch Path Isolation", "test_path != prod_path", "PASS"))

        with patch("gateway.service.REJECTED_INTENTS_PATH", tmp_path):
            # Seed initial record
            gw1 = ExecutionGateway(risk_mgr=MagicMock(), trader=MagicMock())
            intent_seed = OrderIntent(
                intent_id="intent_seed_001",
                source="SIGNAL_COPY",
                symbol="ETHUSDT",
                side="LONG",
                entry_price=3000.0,
                sl_price=2900.0,
            )
            gw1._append_rejected_intent(intent_seed, GatewayResult(ok=False, reason="REGIME_FILTER"))
            lines_1 = [l for l in tmp_path.read_text().splitlines() if l.strip()]
            tip_hash_1 = json.loads(lines_1[-1])["event_hash"]

            # SCENARIO 2: Gateway Restart Intent Deduplication
            gw2 = ExecutionGateway(risk_mgr=MagicMock(), trader=MagicMock())
            gw2._append_rejected_intent(intent_seed, GatewayResult(ok=False, reason="REGIME_FILTER"))
            lines_2 = [l for l in tmp_path.read_text().splitlines() if l.strip()]
            assert len(lines_2) == 1, "Duplicate intent appended after restart!"
            assert gw2.duplicate_intents_suppressed == 1, "Duplicate counter not incremented!"
            scenarios.append(("Scenario 2: Gateway Restart Intent Deduplication", "0 new lines written on dup", "PASS"))

            # SCENARIO 3: Tip Event-Hash Invariance on Duplicate Resubmit
            tip_hash_2 = json.loads(lines_2[-1])["event_hash"]
            assert tip_hash_2 == tip_hash_1, f"Tip hash mutated! {tip_hash_2} != {tip_hash_1}"
            scenarios.append(("Scenario 3: Tip Hash Invariance on Resubmit", f"tip unchanged ({tip_hash_1[:12]})", "PASS"))

            # SCENARIO 4: Multi-Process Lock Contention (5 workers x 10 appends)
            num_workers = 5
            items_per_worker = 10
            expected_total = 1 + (num_workers * items_per_worker)
            with concurrent.futures.ProcessPoolExecutor(max_workers=num_workers) as executor:
                futures = [
                    executor.submit(_worker_append, str(tmp_path), w_id, items_per_worker)
                    for w_id in range(num_workers)
                ]
                concurrent.futures.wait(futures)

            lines_c = [l for l in tmp_path.read_text().splitlines() if l.strip()]
            assert len(lines_c) == expected_total, f"Line count mismatch! {len(lines_c)} != {expected_total}"
            scenarios.append(("Scenario 4: Multi-Process Concurrency Locking", f"50 concurrent appends recorded ({expected_total} total)", "PASS"))

            # SCENARIO 5: Monotonic Cryptographic Chain Continuity across Concurrent Writers
            seen_ids = set()
            for l in lines_c:
                iid = json.loads(l)["intent_id"]
                assert iid not in seen_ids, f"Duplicate intent in concurrent writes: {iid}"
                seen_ids.add(iid)
            valid_c, count_c, msg_c = verify_rejected_intents_journal(tmp_path)
            assert valid_c and count_c == expected_total, f"Hash chain break: {msg_c}"
            scenarios.append(("Scenario 5: Concurrent Chain Monotonicity", f"51/51 records verified ({msg_c})", "PASS"))

            # SCENARIO 6: Partial Tail Mid-Write Crash Detection
            with tmp_path.open("a", encoding="utf-8") as f:
                f.write('{"schema_version": 2, "intent_id": "crashed_intent_tail", "symbol": "SO')
                f.flush()
            valid_bad, _, msg_bad = verify_rejected_intents_journal(tmp_path)
            assert not valid_bad, "Validator failed to detect partial tail crash!"
            scenarios.append(("Scenario 6: Partial Tail Crash Detection", f"caught error: {msg_bad[:40]}...", "PASS"))

            # SCENARIO 7: Automated Crash Tail Recovery & Restoration
            rec1_ok, rec1_count, rec1_msg = recover_journal_after_crash(tmp_path)
            assert rec1_ok and rec1_count == expected_total, f"Recovery failed: {rec1_msg}"
            valid_repaired, count_repaired, _ = verify_rejected_intents_journal(tmp_path)
            assert valid_repaired and count_repaired == expected_total
            scenarios.append(("Scenario 7: Automated Crash Tail Recovery", f"restored {rec1_count} records cleanly", "PASS"))

            # SCENARIO 8: Recovery Idempotence: recover(recover(j)) == recover(j)
            bytes_after_rec1 = tmp_path.read_bytes()
            rec2_ok, rec2_count, rec2_msg = recover_journal_after_crash(tmp_path)
            bytes_after_rec2 = tmp_path.read_bytes()
            assert rec2_ok and rec2_count == expected_total
            assert bytes_after_rec1 == bytes_after_rec2, "Recovery is not idempotent (mutated clean journal)!"
            scenarios.append(("Scenario 8: Recovery Idempotence", "recover(recover(j)) == recover(j)", "PASS"))

            # SCENARIO 9: Strict Refusal to Repair Deliberate Middle-of-File Tampering
            lines_for_tamper = [l for l in tmp_path.read_text().splitlines() if l.strip()]
            t_rec = json.loads(lines_for_tamper[5])
            t_rec["symbol"] = "TAMPERED_IN_MIDDLE"
            lines_for_tamper[5] = json.dumps(t_rec)
            tmp_path.write_text("\n".join(lines_for_tamper) + "\n")

            valid_mid, _, _ = verify_rejected_intents_journal(tmp_path)
            assert not valid_mid, "Middle tamper was not caught!"
            rec_mid_ok, _, rec_mid_msg = recover_journal_after_crash(tmp_path)
            assert not rec_mid_ok, "Recovery must strictly REFUSE to repair middle-of-file tampering!"
            scenarios.append(("Scenario 9: Middle-File Tamper Refusal", f"strictly refused ({rec_mid_msg[:35]}...)", "PASS"))

        # SCENARIO 10: Production Journal Invariance Proof
        prod_bytes_after = prod_path.read_bytes() if prod_path.exists() else b""
        prod_lines_after = len(prod_bytes_after.splitlines())
        assert prod_bytes_before == prod_bytes_after, "FATAL: Production journal was modified during test run!"
        scenarios.append(("Scenario 10: Production Journal Contamination Invariance", f"prod untouched ({prod_lines_after} lines identical)", "PASS"))

    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    print("\n### Execution Results Matrix")
    print("| Scenario | Description / Actual Result | Verdict |")
    print("|---|---|:---:|")
    for name, desc, verdict in scenarios:
        print(f"| {name} | {desc} | **{verdict}** |")

    print(f"\n>>> VERDICT: ALL {len(scenarios)} RUNTIME INTEGRITY SCENARIOS PASSED 100%! <<<")

if __name__ == "__main__":
    main()
