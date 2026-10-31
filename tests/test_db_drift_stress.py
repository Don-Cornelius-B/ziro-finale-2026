import os
import sys
import json
import sqlite3
import subprocess
from pathlib import Path
import pytest

from data.db_setup import (
    init_db,
    seed_random_drifts,
    get_db_connection,
    COURSE_CATALOG,
    FIRST_NAMES,
    LAST_NAMES,
    STUDENT_DOMAINS,
    PARENT_DOMAINS,
    OPTIONAL_DRIFT_ARCHETYPES,
    DEFAULT_DB_PATH,
)


COURSE_FEE_MAP = dict(COURSE_CATALOG)


def get_table_counts(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM gateway_txns")
    gw_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM crm_records")
    crm_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM invoices")
    inv_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM audit_ledger")
    audit_count = cur.fetchone()[0]
    return {
        "gateway_txns": gw_count,
        "crm_records": crm_count,
        "invoices": inv_count,
        "audit_ledger": audit_count,
    }


def test_stress_100_seeds_and_counts(tmp_path):
    """
    Stress-tests seed_random_drifts across 100 different random seeds:
    - Asserts exact num_clean, num_drifts, and total counts.
    - Asserts ESCALATE_REFUSAL_FRAUD is present in every run where num_drifts >= 1.
    - Asserts database row counts match exactly for all tables.
    """
    test_db = tmp_path / "stress_100.db"

    for seed in range(100):
        # Vary num_clean (0 to 6) and num_drifts (1 to 12) across iterations
        num_clean = seed % 7
        num_drifts = 1 + (seed % 12)

        res = seed_random_drifts(
            db_path=test_db,
            num_clean=num_clean,
            num_drifts=num_drifts,
            prewarm=False,
            seed=seed,
        )

        # 1. Verification of return dictionary counts
        assert res["clean_count"] == num_clean, f"Seed {seed}: clean_count mismatch ({res['clean_count']} vs {num_clean})"
        assert res["drift_count"] == num_drifts, f"Seed {seed}: drift_count mismatch ({res['drift_count']} vs {num_drifts})"
        assert res["total_students"] == num_clean + num_drifts, f"Seed {seed}: total_students mismatch"
        assert len(res["students"]) == num_clean + num_drifts, f"Seed {seed}: students list length mismatch"
        assert len(res["clean"]) == num_clean, f"Seed {seed}: clean list length mismatch"
        assert len(res["drifts"]) == num_drifts, f"Seed {seed}: drifts dict length mismatch"

        # 2. Fraud refusal guardrail guarantee when num_drifts >= 1
        assert "ESCALATE_REFUSAL_FRAUD" in res["drifts"], f"Seed {seed}: ESCALATE_REFUSAL_FRAUD missing from drifts"
        fraud_sids = res["seeded_archetypes"].get("ESCALATE_REFUSAL_FRAUD", [])
        assert len(fraud_sids) >= 1, f"Seed {seed}: No student ID seeded for ESCALATE_REFUSAL_FRAUD"
        assert res["summary"]["fraud_guaranteed"] is True, f"Seed {seed}: fraud_guaranteed flag not True"

        # 3. Database direct verification
        conn = get_db_connection(test_db)
        counts = get_table_counts(conn)

        assert counts["crm_records"] == num_clean + num_drifts, (
            f"Seed {seed}: crm_records count {counts['crm_records']} != {num_clean + num_drifts}"
        )
        assert counts["invoices"] == num_clean + num_drifts, (
            f"Seed {seed}: invoices count {counts['invoices']} != {num_clean + num_drifts}"
        )
        assert counts["audit_ledger"] == 0, (
            f"Seed {seed}: audit_ledger count {counts['audit_ledger']} != 0"
        )

        # Gateway txns has 2 rows for DUPLICATE_CHARGE, 1 for all other archetypes
        duplicate_count = len(res["seeded_archetypes"].get("DUPLICATE_CHARGE", []))
        expected_gw_count = (num_clean + num_drifts) + duplicate_count
        assert counts["gateway_txns"] == expected_gw_count, (
            f"Seed {seed}: gateway_txns count {counts['gateway_txns']} != expected {expected_gw_count}"
        )

        conn.close()


def test_edge_cases(tmp_path):
    """
    Empirically stress-tests boundary conditions:
    - num_clean=0, num_drifts=1
    - num_clean=10, num_drifts=1
    - num_clean=0, num_drifts=15
    - num_clean=10, num_drifts=15
    - num_clean=0, num_drifts=0
    """
    edge_scenarios = [
        {"clean": 0, "drifts": 1},
        {"clean": 10, "drifts": 1},
        {"clean": 0, "drifts": 15},
        {"clean": 10, "drifts": 15},
        {"clean": 0, "drifts": 0},
    ]

    for scenario in edge_scenarios:
        num_clean = scenario["clean"]
        num_drifts = scenario["drifts"]
        test_db = tmp_path / f"edge_{num_clean}_{num_drifts}.db"

        res = seed_random_drifts(
            db_path=test_db,
            num_clean=num_clean,
            num_drifts=num_drifts,
            prewarm=False,
            seed=42,
        )

        assert res["clean_count"] == num_clean
        assert res["drift_count"] == num_drifts
        assert res["total_students"] == num_clean + num_drifts
        assert len(res["students"]) == num_clean + num_drifts

        if num_drifts >= 1:
            assert "ESCALATE_REFUSAL_FRAUD" in res["drifts"]
            assert len(res["seeded_archetypes"]["ESCALATE_REFUSAL_FRAUD"]) >= 1
            assert res["summary"]["fraud_guaranteed"] is True
        else:
            assert len(res["seeded_archetypes"].get("ESCALATE_REFUSAL_FRAUD", [])) == 0
            assert res["summary"]["fraud_guaranteed"] is False

        conn = get_db_connection(test_db)
        counts = get_table_counts(conn)
        assert counts["crm_records"] == num_clean + num_drifts
        assert counts["invoices"] == num_clean + num_drifts
        assert counts["audit_ledger"] == 0

        duplicate_count = len(res["seeded_archetypes"].get("DUPLICATE_CHARGE", []))
        assert counts["gateway_txns"] == (num_clean + num_drifts) + duplicate_count
        conn.close()


def test_table_purging_between_runs(tmp_path):
    """
    Asserts all 4 database tables are completely purged between runs (no leftover records):
    1. Pre-seeds deterministic baseline init_db (10 records) + inserts a dummy audit_ledger row.
    2. Runs seed_random_drifts(num_clean=2, num_drifts=3). Asserts 0 baseline/dummy records remain.
    3. Runs seed_random_drifts(num_clean=1, num_drifts=1). Asserts exactly 2 records remain.
    4. Runs seed_random_drifts(num_clean=0, num_drifts=0). Asserts all 4 tables are 100% empty.
    """
    test_db = tmp_path / "purge_test.db"

    # Step 1: Pre-populate with init_db baseline
    init_db(db_path=test_db, prewarm=False)
    conn = get_db_connection(test_db)
    cur = conn.cursor()

    # Insert a dummy audit ledger entry to test ledger purge
    cur.execute("""
    INSERT INTO audit_ledger (
        entry_id, idempotency_hash, timestamp, student_id, txn_id, proposal_id,
        action_executed, status, operator_signature, delta_payload_json, state_seal_hash
    ) VALUES (
        'ENTRY-DUMMY', 'HASH-DUMMY', '2026-10-30T00:00:00Z', 'STU-999', 'TXN-999',
        'PROP-999', 'TEST_ACTION', 'COMMITTED', 'OP-TEST', '{}', 'SEAL-DUMMY'
    )
    """)
    conn.commit()

    init_counts = get_table_counts(conn)
    assert init_counts["crm_records"] == 10
    assert init_counts["invoices"] == 10
    assert init_counts["gateway_txns"] == 10  # TC02 has 2 txns, but TC07 has 0 txns (pending sync)
    assert init_counts["audit_ledger"] == 1
    conn.close()

    # Step 2: Run seed_random_drifts with 2 clean, 3 drifts (total 5)
    res1 = seed_random_drifts(db_path=test_db, num_clean=2, num_drifts=3, prewarm=False, seed=10)
    conn = get_db_connection(test_db)
    cur = conn.cursor()

    # Assert no trace of baseline STU-001 or ENTRY-DUMMY
    cur.execute("SELECT student_id FROM crm_records WHERE student_id = 'STU-001'")
    assert cur.fetchone() is None, "Baseline record STU-001 was not purged!"
    cur.execute("SELECT entry_id FROM audit_ledger WHERE entry_id = 'ENTRY-DUMMY'")
    assert cur.fetchone() is None, "Audit ledger dummy record was not purged!"

    counts1 = get_table_counts(conn)
    assert counts1["crm_records"] == 5
    assert counts1["invoices"] == 5
    assert counts1["audit_ledger"] == 0
    conn.close()

    # Step 3: Run with 1 clean, 1 drift (total 2)
    seed_random_drifts(db_path=test_db, num_clean=1, num_drifts=1, prewarm=False, seed=20)
    conn = get_db_connection(test_db)
    counts2 = get_table_counts(conn)
    assert counts2["crm_records"] == 2, f"Leftover records in crm_records ({counts2['crm_records']} != 2)"
    assert counts2["invoices"] == 2
    assert counts2["audit_ledger"] == 0
    conn.close()

    # Step 4: Run with 0 clean, 0 drifts -> completely empty tables
    seed_random_drifts(db_path=test_db, num_clean=0, num_drifts=0, prewarm=False, seed=30)
    conn = get_db_connection(test_db)
    counts3 = get_table_counts(conn)
    assert counts3["crm_records"] == 0
    assert counts3["invoices"] == 0
    assert counts3["gateway_txns"] == 0
    assert counts3["audit_ledger"] == 0
    conn.close()


def test_relational_invariants(tmp_path):
    """
    Asserts all generated emails, student IDs, and amounts match relational invariants:
    - Primary key formats: STU-R1xx, INV-R1xx, TXN-R1xx.
    - Fee consistency across course catalog, gateway, and invoice.
    - Email formats & domain consistency.
    - Specific relational invariants for each archetype.
    """
    test_db = tmp_path / "invariants.db"
    res = seed_random_drifts(
        db_path=test_db,
        num_clean=5,
        num_drifts=12,
        prewarm=False,
        seed=1001,
    )

    conn = get_db_connection(test_db)
    cur = conn.cursor()

    # Extract all data
    cur.execute("SELECT * FROM crm_records")
    crm_rows = {row["student_id"]: dict(row) for row in cur.fetchall()}

    cur.execute("SELECT * FROM invoices")
    invoice_rows = {row["student_id"]: dict(row) for row in cur.fetchall()}

    cur.execute("SELECT * FROM gateway_txns")
    gw_rows = cur.fetchall()
    gw_by_student = {}
    for r in gw_rows:
        row_dict = dict(r)
        sid = row_dict["student_id"]
        gw_by_student.setdefault(sid, []).append(row_dict)

    assert len(crm_rows) == 17
    assert len(invoice_rows) == 17

    # Check each student
    for s in res["students"]:
        sid = s["student_id"]
        arch = s["archetype"]
        fee = s["fee"]
        course_id = s["course_id"]
        student_email = s["student_email"]

        # 1. ID Format Assertions
        assert sid.startswith("STU-R"), f"Invalid student_id format: {sid}"
        assert s["invoice_id"].startswith("INV-R"), f"Invalid invoice_id format: {s['invoice_id']}"

        # 2. Course Fee Invariant
        assert course_id in COURSE_FEE_MAP, f"Unknown course: {course_id}"
        assert fee == COURSE_FEE_MAP[course_id], f"Fee {fee} does not match catalog tuition {COURSE_FEE_MAP[course_id]}"

        # 3. CRM Assertions
        assert sid in crm_rows, f"Missing CRM row for {sid}"
        crm = crm_rows[sid]
        assert crm["student_email"] == student_email
        assert any(crm["student_email"].endswith(f"@{d}") for d in STUDENT_DOMAINS)

        # 4. Invoice Assertions
        assert sid in invoice_rows, f"Missing Invoice row for {sid}"
        inv = invoice_rows[sid]
        assert inv["invoice_id"] == s["invoice_id"]
        assert inv["amount_due"] == fee

        # 5. Gateway Assertions
        assert sid in gw_by_student, f"Missing gateway txns for {sid}"
        txns = gw_by_student[sid]

        # 6. Per-Archetype Relational Invariants
        if arch == "CLEAN_RECONCILED":
            assert len(txns) == 1
            assert txns[0]["status"] == "captured"
            assert txns[0]["amount"] == fee
            assert txns[0]["email"] == student_email
            assert crm["course_access_status"] == "active"
            assert crm["dunning_status"] == "none"
            assert inv["amount_paid"] == fee
            assert inv["status"] == "paid"

        elif arch == "PAID_BUT_NOT_ACTIVATED":
            assert len(txns) == 1
            assert txns[0]["status"] == "captured"
            assert txns[0]["amount"] == fee
            assert txns[0]["email"] == student_email
            assert crm["course_access_status"] == "inactive"
            assert crm["dunning_status"] == "daily_overdue_active"
            assert inv["amount_paid"] == 0.0
            assert inv["status"] == "unpaid"

        elif arch == "DUPLICATE_CHARGE":
            assert len(txns) == 2, f"Expected 2 duplicate transactions, got {len(txns)}"
            assert txns[0]["amount"] == fee
            assert txns[1]["amount"] == fee
            assert txns[0]["status"] == "captured"
            assert txns[1]["status"] == "captured"
            assert crm["course_access_status"] == "active"
            assert crm["dunning_status"] == "none"
            assert inv["amount_paid"] == 2.0 * fee
            assert inv["status"] == "paid"

        elif arch == "REFUND_PENDING":
            assert len(txns) == 1
            assert txns[0]["status"] == "refund_pending"
            assert txns[0]["amount"] == fee
            assert crm["course_access_status"] == "active"
            assert crm["dunning_status"] == "none"
            assert inv["amount_paid"] == fee
            assert inv["status"] == "paid"

        elif arch == "PAYMENT_FAILED_ACCESS_ACTIVE":
            assert len(txns) == 1
            assert txns[0]["status"] == "failed"
            assert txns[0]["amount"] == fee
            assert crm["course_access_status"] == "active"
            assert crm["dunning_status"] == "none"
            assert inv["amount_paid"] == 0.0
            assert inv["status"] == "unpaid"

        elif arch == "ESCALATE_REFUSAL_FRAUD":
            assert len(txns) == 1
            assert txns[0]["status"] == "disputed"
            assert txns[0]["amount"] == fee
            meta = json.loads(txns[0]["metadata_json"])
            assert meta.get("chargeback") is True
            assert meta.get("compliance_alert") == "AML_FLAG_RED"
            assert crm["course_access_status"] == "active"
            assert inv["amount_paid"] == fee
            assert inv["status"] == "paid"

        elif arch == "PARTIAL_PAYMENT_DRIFT":
            assert len(txns) == 1
            expected_partial = round(fee * 0.6, 2)
            assert txns[0]["amount"] == expected_partial
            assert txns[0]["status"] == "captured"
            assert crm["course_access_status"] == "inactive"
            assert crm["dunning_status"] == "partial_dunning"
            assert inv["amount_paid"] == expected_partial
            assert inv["status"] == "partially_paid"

        elif arch == "IDENTITY_DRIFT":
            assert len(txns) == 1
            assert txns[0]["amount"] == fee
            assert txns[0]["status"] == "captured"
            # Parent corporate email differs from student email
            assert txns[0]["email"] != student_email
            assert any(txns[0]["email"].endswith(f"@{d}") for d in PARENT_DOMAINS)
            meta = json.loads(txns[0]["metadata_json"])
            assert meta.get("payer_relation") == "parent"
            assert meta.get("student_id") == sid
            assert crm["course_access_status"] == "inactive"
            assert crm["dunning_status"] == "daily_overdue_active"
            assert inv["amount_paid"] == 0.0
            assert inv["status"] == "unpaid"

    conn.close()


def test_baseline_init_db_integrity(tmp_path):
    """
    Confirms baseline init_db() is not corrupted:
    - TC01-TC10 fixture integrity preserved.
    - Correct student IDs (STU-001 through STU-010).
    - Exactly 10 CRM records, 10 invoices, 11 gateway txns, 0 audit ledger records.
    """
    test_db = tmp_path / "baseline_check.db"
    returned_path = init_db(db_path=test_db, prewarm=False)

    assert returned_path == str(test_db)
    conn = get_db_connection(test_db)
    counts = get_table_counts(conn)

    assert counts["crm_records"] == 10
    assert counts["invoices"] == 10
    assert counts["gateway_txns"] == 10  # TC07 has no gateway transaction (unpaid, no capture)
    assert counts["audit_ledger"] == 0

    cur = conn.cursor()
    # Check specific deterministic baseline markers
    cur.execute("SELECT student_id, name, course_access_status, dunning_status FROM crm_records ORDER BY student_id")
    crm_list = [dict(r) for r in cur.fetchall()]
    assert len(crm_list) == 10
    assert crm_list[0]["student_id"] == "STU-001"
    assert crm_list[0]["name"] == "Arjun Sharma"
    assert crm_list[0]["course_access_status"] == "inactive"
    assert crm_list[0]["dunning_status"] == "daily_overdue_active"

    assert crm_list[4]["student_id"] == "STU-005"
    assert crm_list[4]["name"] == "Vikram Malhotra"

    # Check TC05 fraud disputed status in gateway_txns
    cur.execute("SELECT status, metadata_json FROM gateway_txns WHERE student_id = 'STU-005'")
    tc05_gw = cur.fetchone()
    assert tc05_gw["status"] == "disputed"
    tc05_meta = json.loads(tc05_gw["metadata_json"])
    assert tc05_meta.get("compliance_alert") == "AML_FLAG_RED"

    # Check TC02 double capture
    cur.execute("SELECT txn_id, amount FROM gateway_txns WHERE student_id = 'STU-002'")
    tc02_txns = cur.fetchall()
    assert len(tc02_txns) == 2
    assert tc02_txns[0]["amount"] == 15000.0
    assert tc02_txns[1]["amount"] == 15000.0

    conn.close()


def test_cli_flags(tmp_path):
    """
    Empirically validates CLI invocation for both baseline and random drift generation.
    """
    cli_db = tmp_path / "cli_test.db"

    # 1. Baseline CLI execution
    res1 = subprocess.run(
        [sys.executable, "data/db_setup.py", "--db-path", str(cli_db), "--no-prewarm", "--json"],
        capture_output=True,
        text=True,
    )
    assert res1.returncode == 0, f"CLI baseline failed: {res1.stderr}"
    data1 = json.loads(res1.stdout)
    assert data1["mode"] == "BENCHMARK"
    assert data1["status"] == "SUCCESS"

    # 2. Chaos CLI execution with json output
    res2 = subprocess.run(
        [
            sys.executable,
            "data/db_setup.py",
            "--random",
            "--db-path",
            str(cli_db),
            "--num-clean",
            "2",
            "--num-drifts",
            "4",
            "--seed",
            "99",
            "--json",
        ],
        capture_output=True,
        text=True,
    )
    assert res2.returncode == 0, f"CLI random failed: {res2.stderr}"
    data2 = json.loads(res2.stdout)
    assert data2["clean_count"] == 2
    assert data2["drift_count"] == 4
    assert data2["total_students"] == 6
    assert data2["summary"]["fraud_guaranteed"] is True
    fraud_sids = data2["seeded_archetypes"]["ESCALATE_REFUSAL_FRAUD"]
    assert len(fraud_sids) >= 1
    assert fraud_sids[0] in data2["drifts"]
    assert data2["drifts"][fraud_sids[0]]["archetype"] == "ESCALATE_REFUSAL_FRAUD"
