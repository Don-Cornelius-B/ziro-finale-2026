import os
import pytest
import sqlite3
from pathlib import Path

from data.db_setup import init_db, DEFAULT_DB_PATH
from core.schemas import DetectedClass
from core.engine import (
    reconcile_drift_engine,
    execute_mutation,
    execute_rejection,
    check_idempotency,
    get_db,
)
from tools.ops_tools import GuardrailViolationError, compute_canonical_audit_hash

@pytest.fixture(autouse=True)
def fresh_database(tmp_path):
    """
    Initializes a fresh isolated SQLite database and pre-warms cache for each test.
    """
    test_db = tmp_path / "test_reconcile.db"
    init_db(db_path=test_db, prewarm=False)
    return test_db


def test_tc01_paid_but_not_activated(fresh_database):
    """
    TC01: Student paid ₹25,000, but CRM is inactive and dunning is firing.
    Asserts proposal activates CRM and silences false dunning reminders.
    """
    res = reconcile_drift_engine(db_path=fresh_database, matching_strategy="composite_student_id")
    tc01_prop = next((p for p in res["proposals"] if p["student_id"] == "STU-001"), None)

    assert tc01_prop is not None
    assert tc01_prop["detected_class"] == DetectedClass.PAID_BUT_NOT_ACTIVATED.value
    assert tc01_prop["confidence_score"] >= 0.95

    # Execute approved mutation
    mutation_res = execute_mutation(tc01_prop, db_path=fresh_database)
    assert mutation_res["status"] == "COMMITTED"
    assert mutation_res["status_code"] == 200
    assert mutation_res["webhook_response"]["delivered"] is True
    assert mutation_res["webhook_response"]["action"] == "CANCEL_DUNNING_SMS"

    # Verify CRM updated in DB
    conn = get_db(fresh_database)
    cur = conn.cursor()
    cur.execute("SELECT course_access_status, dunning_status FROM crm_records WHERE student_id = 'STU-001'")
    crm = cur.fetchone()
    assert crm["course_access_status"] == "active"
    assert crm["dunning_status"] == "none"

    # Verify Invoice updated in DB
    cur.execute("SELECT status, amount_paid FROM invoices WHERE student_id = 'STU-001'")
    inv = cur.fetchone()
    assert inv["status"] == "paid"
    assert inv["amount_paid"] == 25000.0
    conn.close()


def test_tc02_duplicate_charge(fresh_database):
    """
    TC02: Double captured payments (₹15,000 x 2) for single ₹15,000 invoice.
    Asserts duplicate capture detected and refund ticket staged.
    """
    res = reconcile_drift_engine(db_path=fresh_database, matching_strategy="composite_student_id")
    tc02_prop = next((p for p in res["proposals"] if p["student_id"] == "STU-002"), None)

    assert tc02_prop is not None
    assert tc02_prop["detected_class"] == DetectedClass.DUPLICATE_CHARGE.value
    assert "TXN-202" in tc02_prop["proposed_fix"] or "refund" in tc02_prop["proposed_fix"].lower()

    # Verify student course access remains active
    conn = get_db(fresh_database)
    cur = conn.cursor()
    cur.execute("SELECT course_access_status FROM crm_records WHERE student_id = 'STU-002'")
    row = cur.fetchone()
    assert row["course_access_status"] == "active"
    conn.close()


def test_tc03_refund_pending(fresh_database):
    """
    TC03: Gateway transaction status is refund_pending.
    Asserts grace period staged without immediate billing reminder.
    """
    res = reconcile_drift_engine(db_path=fresh_database, matching_strategy="composite_student_id")
    tc03_prop = next((p for p in res["proposals"] if p["student_id"] == "STU-003"), None)

    assert tc03_prop is not None
    assert tc03_prop["detected_class"] == DetectedClass.REFUND_PENDING.value
    assert "grace period" in tc03_prop["proposed_fix"].lower()

    mutation_res = execute_mutation(tc03_prop, db_path=fresh_database)
    assert mutation_res["status"] == "COMMITTED"

    conn = get_db(fresh_database)
    cur = conn.cursor()
    cur.execute("SELECT course_access_status, dunning_status FROM crm_records WHERE student_id = 'STU-003'")
    row = cur.fetchone()
    assert row["course_access_status"] == "grace_period"
    assert row["dunning_status"] == "suppressed"
    conn.close()


def test_tc04_failed_payment_access_active(fresh_database):
    """
    TC04: Gateway transaction failed, but student CRM has free-riding active access.
    Asserts access suspended immediately.
    """
    res = reconcile_drift_engine(db_path=fresh_database, matching_strategy="composite_student_id")
    tc04_prop = next((p for p in res["proposals"] if p["student_id"] == "STU-004"), None)

    assert tc04_prop is not None
    assert tc04_prop["detected_class"] == DetectedClass.PAYMENT_FAILED_ACCESS_ACTIVE.value

    mutation_res = execute_mutation(tc04_prop, db_path=fresh_database)
    assert mutation_res["status"] == "COMMITTED"

    conn = get_db(fresh_database)
    cur = conn.cursor()
    cur.execute("SELECT course_access_status FROM crm_records WHERE student_id = 'STU-004'")
    row = cur.fetchone()
    assert row["course_access_status"] == "inactive"
    conn.close()


def test_tc05_fraud_refusal_guardrail(fresh_database):
    """
    TC05: Disputed transaction with chargeback flag.
    Asserts ESCALATE_REFUSAL_FRAUD is flagged and execution refuses unconditionally with GuardrailViolationError.
    """
    res = reconcile_drift_engine(db_path=fresh_database, matching_strategy="composite_student_id")
    tc05_prop = next((p for p in res["proposals"] if p["student_id"] == "STU-005"), None)

    assert tc05_prop is not None
    assert tc05_prop["detected_class"] == DetectedClass.ESCALATE_REFUSAL_FRAUD.value

    # Direct execution attempt MUST be physically refused
    with pytest.raises(GuardrailViolationError) as exc_info:
        execute_mutation(tc05_prop, db_path=fresh_database)

    assert "Compliance Guardrail" in str(exc_info.value)

    # Verify audit ledger recorded the refusal event
    conn = get_db(fresh_database)
    cur = conn.cursor()
    cur.execute("SELECT status, action_executed FROM audit_ledger WHERE student_id = 'STU-005'")
    ledger_entry = cur.fetchone()
    assert ledger_entry is not None
    assert ledger_entry["status"] == "REFUSED_GUARDRAIL"
    conn.close()


def test_tc06_partial_payment(fresh_database):
    """
    TC06: ₹15,000 paid against ₹20,000 fee.
    Asserts float calculation leaves remaining balance correct (₹5,000).
    """
    res = reconcile_drift_engine(db_path=fresh_database, matching_strategy="composite_student_id")
    tc06_prop = next((p for p in res["proposals"] if p["student_id"] == "STU-006"), None)

    assert tc06_prop is not None
    assert tc06_prop["detected_class"] == DetectedClass.PARTIAL_PAYMENT_DRIFT.value
    assert "5,000" in tc06_prop["evidence_quote"] or "5000" in tc06_prop["evidence_quote"]

    mutation_res = execute_mutation(tc06_prop, db_path=fresh_database)
    assert mutation_res["status"] == "COMMITTED"

    conn = get_db(fresh_database)
    cur = conn.cursor()
    cur.execute("SELECT amount_due, amount_paid, status FROM invoices WHERE student_id = 'STU-006'")
    inv = cur.fetchone()
    assert inv["amount_paid"] == 15000.0
    assert inv["status"] == "partially_paid"
    conn.close()


def test_tc07_pending_gateway_sync(fresh_database):
    """
    TC07: CRM inactive, invoice unpaid, no gateway capture.
    Asserts uncaptured invoices remain unchanged (no false proposals).
    """
    res = reconcile_drift_engine(db_path=fresh_database, matching_strategy="composite_student_id")
    tc07_prop = next((p for p in res["proposals"] if p["student_id"] == "STU-007"), None)

    # Should have no drift proposal because pending sync is expected
    assert tc07_prop is None

    conn = get_db(fresh_database)
    cur = conn.cursor()
    cur.execute("SELECT status FROM invoices WHERE student_id = 'STU-007'")
    inv = cur.fetchone()
    assert inv["status"] == "unpaid"
    conn.close()


def test_tc08_replan_identity_drift(fresh_database):
    """
    TC08: Payment made under parent email (b.livingstone@corporate.com) while student email is karthik.l@student.edu.
    Asserts parent email mismatch resolves via student_id during re-planning.
    """
    # Pass 1: Primary email join fails for parent email
    pass1 = reconcile_drift_engine(db_path=fresh_database, matching_strategy="primary_email", replan_count=0)
    assert pass1["replan_needed"] is True
    assert len(pass1["unmatched_txns"]) > 0

    # Pass 2: Autonomous Re-plan switches to composite key fallback
    pass2 = reconcile_drift_engine(db_path=fresh_database, matching_strategy="composite_student_id", replan_count=1)
    assert pass2["replan_needed"] is False
    assert len(pass2["unmatched_txns"]) == 0

    tc08_prop = next((p for p in pass2["proposals"] if p["student_id"] == "STU-008"), None)
    assert tc08_prop is not None
    assert tc08_prop["detected_class"] == DetectedClass.IDENTITY_DRIFT_RESOLVED.value

    # Execute fix
    mutation_res = execute_mutation(tc08_prop, db_path=fresh_database)
    assert mutation_res["status"] == "COMMITTED"

    conn = get_db(fresh_database)
    cur = conn.cursor()
    cur.execute("SELECT course_access_status, dunning_status FROM crm_records WHERE student_id = 'STU-008'")
    crm = cur.fetchone()
    assert crm["course_access_status"] == "active"
    assert crm["dunning_status"] == "none"
    conn.close()


def test_tc09_clean_record(fresh_database):
    """
    TC09: Clean record (₹25,000 paid, CRM active, invoice paid).
    Asserts 0 proposals generated for clean records.
    """
    res = reconcile_drift_engine(db_path=fresh_database, matching_strategy="composite_student_id")
    tc09_prop = next((p for p in res["proposals"] if p["student_id"] == "STU-009"), None)
    assert tc09_prop is None


def test_tc10_operator_rejection(fresh_database):
    """
    TC10: Operator rejects proposal in HITL.
    Asserts operator rejection aborts database write cleanly and logs rejection.
    """
    res = reconcile_drift_engine(db_path=fresh_database, matching_strategy="composite_student_id")
    tc10_prop = next((p for p in res["proposals"] if p["student_id"] == "STU-010"), None)
    assert tc10_prop is not None

    # Operator explicitly rejects
    rej_res = execute_rejection(tc10_prop, db_path=fresh_database, rejection_reason="Declined by audit officer")
    assert rej_res["status"] == "REJECTED_BY_OPERATOR"
    assert rej_res["status_code"] == 200

    # Ensure database state was NOT mutated
    conn = get_db(fresh_database)
    cur = conn.cursor()
    cur.execute("SELECT course_access_status, dunning_status FROM crm_records WHERE student_id = 'STU-010'")
    crm = cur.fetchone()
    assert crm["course_access_status"] == "inactive"
    assert crm["dunning_status"] == "daily_overdue_active"

    # Verify audit ledger recorded the rejection
    cur.execute("SELECT status, action_executed FROM audit_ledger WHERE student_id = 'STU-010'")
    ledger = cur.fetchone()
    assert ledger["status"] == "REJECTED_BY_OPERATOR"
    conn.close()


def test_idempotency_double_execution(fresh_database):
    """
    IDEMPOTENCY INVARIANT: Resubmission of identical (student_id + txn_id + proposal_id).
    Asserts duplicate execution is safely blocked with HTTP 409 / SKIPPED_ALREADY_PROCESSED.
    """
    res = reconcile_drift_engine(db_path=fresh_database, matching_strategy="composite_student_id")
    tc01_prop = next((p for p in res["proposals"] if p["student_id"] == "STU-001"), None)
    assert tc01_prop is not None

    # First execution succeeds
    res1 = execute_mutation(tc01_prop, db_path=fresh_database)
    assert res1["status"] == "COMMITTED"
    assert res1["status_code"] == 200

    # Second identical execution MUST be skipped idempotently
    res2 = execute_mutation(tc01_prop, db_path=fresh_database)
    assert res2["status_code"] == 409
    assert res2["status"] == "SKIPPED_ALREADY_PROCESSED"
    assert "already been reconciled" in res2["message"]
