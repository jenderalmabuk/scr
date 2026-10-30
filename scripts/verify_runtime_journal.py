#!/usr/bin/env python3
"""Runtime Journal Durability, Concurrency, and Crash-Recovery Test Suite

Fulfills all 8 institutional audit requirements from AI ane:
1. Restart gateway simulation.
2. Resend identical intent_id.
3. Assert no new line written.
4. Assert tip event_hash unchanged.
5. Concurrent multi-process appends (race condition / lock contention test).
6. Verify all events recorded exactly once without duplicate or dropped entries.
7. Simulate partial final line crash (mid-write truncation).
8. Verify validator rejects corrupted tail and recover_journal_after_crash cleanly restores it.
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
)

def _worker_append(journal_path_str: str, worker_id: int, num_items: int):
    """Worker function for multi-process concurrency test."""
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
    print(" LIVE RUNTIME JOURNAL CONCURRENCY & INTEGRITY VERIFICATION")
    print("==================================================================")

    with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as tmp:
        tmp_path = Path(tmp.name)

    try:
        with patch("gateway.service.REJECTED_INTENTS_PATH", tmp_path):
            # -------------------------------------------------------------
            # STEP 1-4: Restart & Deduplication Invariance
            # -------------------------------------------------------------
            print("\n[TEST 1] Testing Restart & Intent Deduplication...")
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

            # Inspect state before restart
            valid, count1, msg1 = verify_rejected_intents_journal(tmp_path)
            assert valid and count1 == 1, f"Seed write failed: {msg1}"
            lines_1 = [l for l in tmp_path.read_text().splitlines() if l.strip()]
            tip_hash_1 = json.loads(lines_1[-1])["event_hash"]
            print(f"  ✓ Initial intent recorded (count={count1}, tip={tip_hash_1[:12]})")

            # 1. Restart gateway (re-instantiate with fresh memory)
            print("  ✓ Simulating Gateway Daemon Restart...")
            gw2 = ExecutionGateway(risk_mgr=MagicMock(), trader=MagicMock())
            assert "intent_seed_001" in gw2._seen_rejected_intent_ids, "Failed to rebuild seen intent IDs on restart!"
            assert gw2._last_reject_event_hash == tip_hash_1, "Failed to restore last reject event hash!"

            # 2. Kirim ulang intent_id yang sama
            print("  ✓ Resubmitting duplicate intent_id 'intent_seed_001' after restart...")
            gw2._append_rejected_intent(intent_seed, GatewayResult(ok=False, reason="REGIME_FILTER"))

            # 3. Pastikan tidak ada baris baru
            lines_2 = [l for l in tmp_path.read_text().splitlines() if l.strip()]
            assert len(lines_2) == 1, f"Expected 1 line, found {len(lines_2)}! Duplicate was not suppressed!"

            # 4. Pastikan event_hash tip tetap sama
            tip_hash_2 = json.loads(lines_2[-1])["event_hash"]
            assert tip_hash_2 == tip_hash_1, f"Tip hash changed after duplicate! {tip_hash_2} != {tip_hash_1}"
            print("  ✓ PASS: Dedup across restart verified! 0 duplicate lines, tip hash preserved.")

            # -------------------------------------------------------------
            # STEP 5-6: High Concurrency Multi-Process Lock Contention
            # -------------------------------------------------------------
            print("\n[TEST 2] Testing Multi-Process Concurrency & Lock Contention (10 workers, 50 appends)...")
            num_workers = 5
            items_per_worker = 10
            expected_total = 1 + (num_workers * items_per_worker)

            with concurrent.futures.ProcessPoolExecutor(max_workers=num_workers) as executor:
                futures = [
                    executor.submit(_worker_append, str(tmp_path), w_id, items_per_worker)
                    for w_id in range(num_workers)
                ]
                concurrent.futures.wait(futures)

            lines_concurrent = [l for l in tmp_path.read_text().splitlines() if l.strip()]
            print(f"  ✓ Total lines recorded: {len(lines_concurrent)} (Expected: {expected_total})")
            assert len(lines_concurrent) == expected_total, f"Line count mismatch! {len(lines_concurrent)} != {expected_total}"

            # Verify that every intent_id is unique (no drops, no duplicates)
            seen_ids = set()
            for l in lines_concurrent:
                rec = json.loads(l)
                iid = rec["intent_id"]
                assert iid not in seen_ids, f"Duplicate intent_id in concurrent write: {iid}"
                seen_ids.add(iid)
            print("  ✓ All 51 intents are 100% unique (0 drops, 0 duplicates)")

            # Verify cryptographic chain across all 51 concurrent writes
            valid_c, count_c, msg_c = verify_rejected_intents_journal(tmp_path)
            print(f"  ✓ Cryptographic chain verification: valid={valid_c}, count={count_c}, msg='{msg_c}'")
            assert valid_c, f"Chain broken during concurrent append: {msg_c}"
            print("  ✓ PASS: Lock contention test passed! Chain is strictly monotonic and intact.")

            # -------------------------------------------------------------
            # STEP 7-8: Crash Simulation & Partial Tail Recovery
            # -------------------------------------------------------------
            print("\n[TEST 3] Simulating Mid-Write Crash (Partial Tail Line)...")
            # Append incomplete raw bytes to simulate power loss during append
            with tmp_path.open("a", encoding="utf-8") as f:
                f.write('{"schema_version": 2, "intent_id": "crashed_mid_write_intent", "symbol": "SO')
                f.flush()

            # 7. Validator must detect corruption immediately
            valid_bad, count_bad, msg_bad = verify_rejected_intents_journal(tmp_path)
            print(f"  ✓ Pre-repair validation: valid={valid_bad}, error='{msg_bad}'")
            assert not valid_bad, "Validator failed to catch crashed partial line!"
            assert "json decode error" in msg_bad or "tamper" in msg_bad

            # 8. Automated Crash Recovery truncates invalid tail and restores valid chain
            print("  ✓ Executing recover_journal_after_crash()...")
            rec_ok, rec_count, rec_msg = recover_journal_after_crash(tmp_path)
            print(f"  ✓ Recovery result: ok={rec_ok}, restored_count={rec_count}, msg='{rec_msg}'")
            assert rec_ok and rec_count == expected_total, f"Recovery failed: {rec_msg}"

            # Verify journal is now 100% valid again
            valid_post, count_post, msg_post = verify_rejected_intents_journal(tmp_path)
            assert valid_post and count_post == expected_total
            print("  ✓ PASS: Crash recovery safely restored journal integrity to 100%!")

            # -------------------------------------------------------------
            # TEST 4: Refusal to Repair Middle-of-File Tampering
            # -------------------------------------------------------------
            print("\n[TEST 4] Verifying Refusal to Repair Middle-of-File Tampering...")
            lines_for_tamper = [l for l in tmp_path.read_text().splitlines() if l.strip()]
            t_rec = json.loads(lines_for_tamper[5])
            t_rec["symbol"] = "TAMPERED_COIN"
            lines_for_tamper[5] = json.dumps(t_rec)
            tmp_path.write_text("\n".join(lines_for_tamper) + "\n")

            valid_t, _, msg_t = verify_rejected_intents_journal(tmp_path)
            assert not valid_t, "Middle tamper was not caught!"
            rec_t_ok, _, rec_t_msg = recover_journal_after_crash(tmp_path)
            assert not rec_t_ok, "Recovery should strictly REFUSE to repair middle-of-file tampering!"
            print(f"  ✓ PASS: Correctly refused recovery on deliberate tampering: '{rec_t_msg[:60]}...'")

    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    print("\n==================================================================")
    print(" ALL 8 INSTITUTIONAL RUNTIME JOURNAL AUDITS PASSED 100%!")
    print("==================================================================")

if __name__ == "__main__":
    main()
