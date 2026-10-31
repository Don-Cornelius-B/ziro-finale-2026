import os
import json
import sqlite3
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

from core.schemas import DetectedClass, MismatchProposal
from tools.ops_tools import (
    DEFAULT_DB_PATH,
    GuardrailViolationError,
    OfflineWebhookDispatcher,
    compute_canonical_audit_hash,
    verify_float_epsilon_balance,
    generate_invoice_screenshot,
)

webhook_dispatcher = OfflineWebhookDispatcher()

def get_db(db_path: Optional[Path] = None):
    target = Path(db_path or DEFAULT_DB_PATH).resolve()
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    return conn


def load_raw_data(db_path: Optional[Path] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Reads the tri-source data from SQLite: gateway_txns, crm_records, and invoices.
    """
    conn = get_db(db_path)
    cur = conn.cursor()

    cur.execute("SELECT * FROM gateway_txns ORDER BY timestamp ASC")
    txns = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM crm_records ORDER BY student_id ASC")
    crm = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM invoices ORDER BY invoice_id ASC")
    invoices = [dict(r) for r in cur.fetchall()]

    conn.close()
    return txns, crm, invoices


def reconcile_drift_engine(
    db_path: Optional[Path] = None,
    matching_strategy: str = "primary_email",
    replan_count: int = 0
) -> Dict[str, Any]:
    """
    Executes the multi-source reconciliation join across Gateway, CRM, and Invoices.
    Supports dynamic re-planning fallback (e.g. from email join to composite student_id join).
    """
    txns, crm_list, invoices_list = load_raw_data(db_path)

    crm_by_id = {c["student_id"]: c for c in crm_list}
    crm_by_email = {c["student_email"].lower().strip(): c for c in crm_list}
    inv_by_student = {i["student_id"]: i for i in invoices_list}

    # Group transactions by student_id or email
    txns_by_student: Dict[str, List[Dict[str, Any]]] = {}
    unmatched_txns = []

    for t in txns:
        s_id = t.get("student_id")
        t_email = t.get("email", "").lower().strip()

        # Check if metadata specifies student_id
        meta = {}
        if t.get("metadata_json"):
            try:
                meta = json.loads(t["metadata_json"])
            except Exception:
                meta = {}

        if matching_strategy == "primary_email":
            # Attempt primary match by email first
            matched_crm = crm_by_email.get(t_email)
            if matched_crm:
                sid = matched_crm["student_id"]
                txns_by_student.setdefault(sid, []).append(t)
            else:
                # If email match failed, mark as unmatched drift to trigger observe/replan
                unmatched_txns.append(t)
        else:
            # Fallback strategy: Composite key (student_id directly or via metadata)
            sid = s_id or meta.get("student_id")
            if sid and sid in crm_by_id:
                txns_by_student.setdefault(sid, []).append(t)
            else:
                unmatched_txns.append(t)

    replan_needed = (len(unmatched_txns) > 0 and replan_count == 0)

    # Now evaluate drift patterns across all students
    proposals: List[Dict[str, Any]] = []

    for sid, crm_entry in crm_by_id.items():
        inv_entry = inv_by_student.get(sid, {})
        student_txns = txns_by_student.get(sid, [])
        student_name = crm_entry.get("name", "Student")
        crm_status = crm_entry.get("course_access_status", "inactive")
        dunning_status = crm_entry.get("dunning_status", "none")
        invoice_id = inv_entry.get("invoice_id", "INV-N/A")
        amount_due = float(inv_entry.get("amount_due", 0.0))
        amount_paid_inv = float(inv_entry.get("amount_paid", 0.0))

        # Check for Fraud / Disputed transactions first (TC05)
        fraud_txns = [t for t in student_txns if t.get("status") == "disputed" or (
            t.get("metadata_json") and "chargeback" in t["metadata_json"]
        )]
        if fraud_txns:
            t = fraud_txns[0]
            prop_id = f"PROP-{sid}-FRAUD"
            audit_hash = compute_canonical_audit_hash(sid, t["txn_id"], prop_id)
            preview_meta = {
                "student_id": sid,
                "student_name": student_name,
                "invoice_id": invoice_id,
                "txn_id": t["txn_id"],
                "email": crm_entry.get("student_email"),
                "amount_due": amount_due,
                "amount_paid": t.get("amount", 0.0),
                "balance_due": "DISPUTED",
                "status_label": "CHARGEBACK DISPUTE",
                "badge_class": "badge-disputed",
                "balance_color": "#b91c1c"
            }
            proposals.append({
                "proposal_id": prop_id,
                "student_id": sid,
                "student_name": student_name,
                "txn_id": t["txn_id"],
                "invoice_id": invoice_id,
                "detected_class": DetectedClass.ESCALATE_REFUSAL_FRAUD.value,
                "confidence_score": 1.0,
                "evidence_quote": f"Transaction {t['txn_id']} status='disputed', chargeback=True detected in Gateway metadata.",
                "proposed_fix": "REFUSE AUTOMATED OVERRIDE: Hard freeze account, halt automated mutations, escalate to Anti-Fraud Compliance.",
                "requires_human_approval": True,
                "invoice_preview_path": generate_invoice_screenshot(preview_meta, db_path=db_path),
                "audit_hash": audit_hash,
                "delta": {"block_action": True, "reason": "AML_CHARGEBACK_RISK"}
            })
            continue

        # Check for Refund Pending (TC03)
        refund_txns = [t for t in student_txns if t.get("status") == "refund_pending"]
        if refund_txns:
            t = refund_txns[0]
            prop_id = f"PROP-{sid}-REFUND"
            audit_hash = compute_canonical_audit_hash(sid, t["txn_id"], prop_id)
            preview_meta = {
                "student_id": sid,
                "student_name": student_name,
                "invoice_id": invoice_id,
                "txn_id": t["txn_id"],
                "email": crm_entry.get("student_email"),
                "amount_due": amount_due,
                "amount_paid": t.get("amount", 0.0),
                "balance_due": "0.00",
                "status_label": "REFUND PENDING",
                "badge_class": "badge-pending",
                "balance_color": "#b45309"
            }
            proposals.append({
                "proposal_id": prop_id,
                "student_id": sid,
                "student_name": student_name,
                "txn_id": t["txn_id"],
                "invoice_id": invoice_id,
                "detected_class": DetectedClass.REFUND_PENDING.value,
                "confidence_score": 0.98,
                "evidence_quote": f"Gateway TXN {t['txn_id']} is refund_pending. CRM active access needs grace period hold.",
                "proposed_fix": "Stage 7-day grace period hold on CRM access; suppress automated overdue reminders.",
                "requires_human_approval": True,
                "invoice_preview_path": generate_invoice_screenshot(preview_meta, db_path=db_path),
                "audit_hash": audit_hash,
                "delta": {"crm_status": "grace_period", "dunning_status": "suppressed"}
            })
            continue

        # Check for Payment Failed but CRM Access Active (TC04)
        failed_txns = [t for t in student_txns if t.get("status") == "failed"]
        if failed_txns and crm_status == "active":
            t = failed_txns[0]
            prop_id = f"PROP-{sid}-REVOKE"
            audit_hash = compute_canonical_audit_hash(sid, t["txn_id"], prop_id)
            preview_meta = {
                "student_id": sid,
                "student_name": student_name,
                "invoice_id": invoice_id,
                "txn_id": t["txn_id"],
                "email": crm_entry.get("student_email"),
                "amount_due": amount_due,
                "amount_paid": 0.0,
                "balance_due": amount_due,
                "status_label": "PAYMENT FAILED",
                "badge_class": "badge-disputed",
                "balance_color": "#b91c1c"
            }
            proposals.append({
                "proposal_id": prop_id,
                "student_id": sid,
                "student_name": student_name,
                "txn_id": t["txn_id"],
                "invoice_id": invoice_id,
                "detected_class": DetectedClass.PAYMENT_FAILED_ACCESS_ACTIVE.value,
                "confidence_score": 0.99,
                "evidence_quote": f"Gateway TXN {t['txn_id']} failed, but CRM course_access_status is '{crm_status}' (Free-riding).",
                "proposed_fix": "Revoke course access immediately; update CRM to 'inactive' and notify finance team.",
                "requires_human_approval": True,
                "invoice_preview_path": generate_invoice_screenshot(preview_meta, db_path=db_path),
                "audit_hash": audit_hash,
                "delta": {"crm_status": "inactive", "invoice_status": "unpaid", "dunning_status": "payment_failed_alert"}
            })
            continue

        # Check for Duplicate Charge (TC02)
        captured_txns = [t for t in student_txns if t.get("status") == "captured"]
        if len(captured_txns) > 1 and sum(t["amount"] for t in captured_txns) > amount_due:
            dup_txn = captured_txns[1]
            prop_id = f"PROP-{sid}-DUP"
            audit_hash = compute_canonical_audit_hash(sid, dup_txn["txn_id"], prop_id)
            total_paid = sum(t["amount"] for t in captured_txns)
            preview_meta = {
                "student_id": sid,
                "student_name": student_name,
                "invoice_id": invoice_id,
                "txn_id": f"{captured_txns[0]['txn_id']}, {dup_txn['txn_id']}",
                "email": crm_entry.get("student_email"),
                "amount_due": amount_due,
                "amount_paid": total_paid,
                "balance_due": f"-{total_paid - amount_due:,.2f} (CREDIT)",
                "status_label": "DUPLICATE OVERPAYMENT",
                "badge_class": "badge-pending",
                "balance_color": "#4338ca"
            }
            proposals.append({
                "proposal_id": prop_id,
                "student_id": sid,
                "student_name": student_name,
                "txn_id": f"{captured_txns[0]['txn_id']}, {dup_txn['txn_id']}",
                "invoice_id": invoice_id,
                "detected_class": DetectedClass.DUPLICATE_CHARGE.value,
                "confidence_score": 1.0,
                "evidence_quote": f"Multiple captured payments ({len(captured_txns)}) totaling ₹{sum(t['amount'] for t in captured_txns):,.2f} for single invoice {invoice_id} of ₹{amount_due:,.2f}.",
                "proposed_fix": f"Auto-stage refund ticket for duplicate capture {dup_txn['txn_id']} (₹{dup_txn['amount']:,.2f}); preserve active student entitlement.",
                "requires_human_approval": True,
                "invoice_preview_path": generate_invoice_screenshot(preview_meta, db_path=db_path),
                "audit_hash": audit_hash,
                "delta": {"refund_txn": dup_txn["txn_id"], "refund_amount": dup_txn["amount"]}
            })
            continue

        # Check for Single Captured Payment
        if len(captured_txns) == 1:
            t = captured_txns[0]
            captured_amt = float(t["amount"])
            bal_check = verify_float_epsilon_balance(captured_amt, amount_due)

            # Check for Partial Payment (TC06)
            if captured_amt < (amount_due - 1.0):
                prop_id = f"PROP-{sid}-PARTIAL"
                audit_hash = compute_canonical_audit_hash(sid, t["txn_id"], prop_id)
                rem = bal_check["remaining_due"]
                preview_meta = {
                    "student_id": sid,
                    "student_name": student_name,
                    "invoice_id": invoice_id,
                    "txn_id": t["txn_id"],
                    "email": crm_entry.get("student_email"),
                    "amount_due": amount_due,
                    "amount_paid": captured_amt,
                    "balance_due": rem,
                    "status_label": "PARTIALLY PAID",
                    "badge_class": "badge-partial",
                    "balance_color": "#4338ca"
                }
                proposals.append({
                    "proposal_id": prop_id,
                    "student_id": sid,
                    "student_name": student_name,
                    "txn_id": t["txn_id"],
                    "invoice_id": invoice_id,
                    "detected_class": DetectedClass.PARTIAL_PAYMENT_DRIFT.value,
                    "confidence_score": 0.96,
                    "evidence_quote": f"Captured payment ₹{captured_amt:,.2f} is less than fee ₹{amount_due:,.2f}. Outstanding balance ₹{rem:,.2f}.",
                    "proposed_fix": f"Update invoice amount_paid to ₹{captured_amt:,.2f}, set invoice to 'partially_paid', adjust CRM dunning for ₹{rem:,.2f}.",
                    "requires_human_approval": True,
                    "invoice_preview_path": generate_invoice_screenshot(preview_meta, db_path=db_path),
                    "audit_hash": audit_hash,
                    "delta": {"invoice_paid": captured_amt, "invoice_status": "partially_paid", "dunning_status": "partial_overdue"}
                })
                continue

            # Check for Paid But Not Activated (TC01 or TC08)
            if bal_check["is_balanced"] and (crm_status != "active" or dunning_status == "daily_overdue_active"):
                detected_type = DetectedClass.PAID_BUT_NOT_ACTIVATED.value
                is_parent_payer = False
                # If matching strategy resolved through student_id fallback (parent email)
                if t.get("metadata_json") and "parent" in t["metadata_json"]:
                    detected_type = DetectedClass.IDENTITY_DRIFT_RESOLVED.value
                    is_parent_payer = True

                prop_id = f"PROP-{sid}-ACTIVATE"
                audit_hash = compute_canonical_audit_hash(sid, t["txn_id"], prop_id)
                preview_meta = {
                    "student_id": sid,
                    "student_name": student_name,
                    "invoice_id": invoice_id,
                    "txn_id": t["txn_id"],
                    "email": crm_entry.get("student_email"),
                    "amount_due": amount_due,
                    "amount_paid": captured_amt,
                    "balance_due": "0.00",
                    "status_label": "RESOLVED (PARENT PAYER)" if is_parent_payer else "PAID - ACTIVATION PENDING",
                    "badge_class": "badge-paid",
                    "balance_color": "#16a34a"
                }
                proposals.append({
                    "proposal_id": prop_id,
                    "student_id": sid,
                    "student_name": student_name,
                    "txn_id": t["txn_id"],
                    "invoice_id": invoice_id,
                    "detected_class": detected_type,
                    "confidence_score": 0.99,
                    "evidence_quote": f"Full payment ₹{captured_amt:,.2f} verified in Gateway, but CRM status is '{crm_status}' and dunning is '{dunning_status}'.",
                    "proposed_fix": "Activate CRM course access, mark invoice as 'paid', dispatch webhook to silence dunning reminders immediately.",
                    "requires_human_approval": True,
                    "invoice_preview_path": generate_invoice_screenshot(preview_meta, db_path=db_path),
                    "audit_hash": audit_hash,
                    "delta": {"crm_status": "active", "invoice_status": "paid", "dunning_status": "none", "invoice_paid": captured_amt}
                })
                continue

            # TC09: Clean record
            if bal_check["is_balanced"] and crm_status == "active" and dunning_status == "none" and inv_entry.get("status") == "paid":
                # Clean, no proposal needed
                pass

        # Check for Pending Gateway Sync (TC07)
        if len(student_txns) == 0 and crm_status == "inactive" and inv_entry.get("status") == "unpaid":
            # Normal awaiting payment state, no drift proposal
            pass

    return {
        "proposals": proposals,
        "unmatched_txns": unmatched_txns,
        "replan_needed": replan_needed,
        "strategy": matching_strategy
    }


def check_idempotency(db_path: Optional[Path], student_id: str, txn_id: Optional[str], proposal_id: str) -> bool:
    """
    Checks if a proposal has already been committed to the audit_ledger.
    Returns True if already processed.
    """
    conn = get_db(db_path)
    cur = conn.cursor()
    h = compute_canonical_audit_hash(student_id, txn_id, proposal_id)
    cur.execute("SELECT 1 FROM audit_ledger WHERE idempotency_hash = ? AND status = 'COMMITTED'", (h,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def execute_mutation(
    proposal: Dict[str, Any],
    operator_signature: str = "OPS-ADMIN-SIG",
    db_path: Optional[Path] = None
) -> Dict[str, Any]:
    """
    Executes the approved proposal:
    1. Checks idempotency.
    2. Validates Refusal Guardrail (raises GuardrailViolationError if ESCALATE_REFUSAL_FRAUD).
    3. Mutates SQLite CRM and Invoice tables.
    4. Silences dunning reminders via webhook.
    5. Appends record to audit_ledger.
    """
    sid = proposal["student_id"]
    tid = proposal.get("txn_id")
    pid = proposal["proposal_id"]
    det_class = proposal.get("detected_class")
    delta = proposal.get("delta", {})

    # 1. Refusal Guardrail Check (Physical Tool-Layer Enforcement)
    if det_class == DetectedClass.ESCALATE_REFUSAL_FRAUD.value or det_class == "ESCALATE_REFUSAL_FRAUD":
        # Log refusal to audit ledger
        _record_ledger_entry(
            db_path=db_path,
            student_id=sid,
            txn_id=tid,
            proposal_id=pid,
            action_executed="PHYSICAL_TOOL_REFUSAL_TRIGGERED",
            status="REFUSED_GUARDRAIL",
            operator_sig=operator_signature,
            delta={"refusal_reason": "Hard compliance block on AML/Disputed chargeback", "fraud_prevented": True}
        )
        raise GuardrailViolationError(
            f"Execution Physically Refused by Compliance Guardrail: Student {sid} Transaction {tid} has active chargeback/dispute.",
            detected_class="ESCALATE_REFUSAL_FRAUD"
        )

    # 2. Idempotency Check
    if check_idempotency(db_path, sid, tid, pid):
        return {
            "status_code": 409,
            "status": "SKIPPED_ALREADY_PROCESSED",
            "message": f"Proposal {pid} for student {sid} has already been reconciled.",
            "student_id": sid,
            "proposal_id": pid
        }

    # 3. Apply Mutations to SQLite
    conn = get_db(db_path)
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

    crm_mutated = False
    inv_mutated = False

    if "crm_status" in delta:
        new_crm = delta["crm_status"]
        new_dunning = delta.get("dunning_status", "none")
        cur.execute("""
            UPDATE crm_records
            SET course_access_status = ?, dunning_status = ?, last_synced = ?
            WHERE student_id = ?
        """, (new_crm, new_dunning, now, sid))
        crm_mutated = True

    if "invoice_status" in delta:
        new_inv_status = delta["invoice_status"]
        cur.execute("""
            UPDATE invoices
            SET status = ?, amount_paid = COALESCE(?, amount_paid)
            WHERE student_id = ?
        """, (new_inv_status, delta.get("invoice_paid"), sid))
        inv_mutated = True

    conn.commit()
    conn.close()

    # 4. Silence Dunning Webhook
    webhook_res = webhook_dispatcher.silence_dunning_reminder(sid, reason=det_class)

    # 5. Commit to Audit Ledger
    entry_id = _record_ledger_entry(
        db_path=db_path,
        student_id=sid,
        txn_id=tid,
        proposal_id=pid,
        action_executed=proposal.get("proposed_fix", "CRM_AND_INVOICE_SYNC"),
        status="COMMITTED",
        operator_sig=operator_signature,
        delta=delta
    )

    return {
        "status_code": 200,
        "status": "COMMITTED",
        "entry_id": entry_id,
        "student_id": sid,
        "proposal_id": pid,
        "crm_updated": crm_mutated,
        "invoice_updated": inv_mutated,
        "webhook_response": webhook_res
    }


def execute_rejection(
    proposal: Dict[str, Any],
    operator_signature: str = "OPS-ADMIN-SIG",
    rejection_reason: str = "Operator manually declined suggested mutation",
    db_path: Optional[Path] = None
) -> Dict[str, Any]:
    """
    Aborts the proposed database writes cleanly and logs the rejection in the audit ledger.
    """
    sid = proposal["student_id"]
    tid = proposal.get("txn_id")
    pid = proposal["proposal_id"]

    entry_id = _record_ledger_entry(
        db_path=db_path,
        student_id=sid,
        txn_id=tid,
        proposal_id=pid,
        action_executed="OPERATOR_REJECTION_RECORDED",
        status="REJECTED_BY_OPERATOR",
        operator_sig=operator_signature,
        delta={"rejection_reason": rejection_reason, "mutations_aborted": True}
    )

    return {
        "status_code": 200,
        "status": "REJECTED_BY_OPERATOR",
        "entry_id": entry_id,
        "student_id": sid,
        "proposal_id": pid,
        "message": "Proposal rejected. Database state preserved intact."
    }


def _record_ledger_entry(
    db_path: Optional[Path],
    student_id: str,
    txn_id: Optional[str],
    proposal_id: str,
    action_executed: str,
    status: str,
    operator_sig: str,
    delta: Dict[str, Any]
) -> str:
    conn = get_db(db_path)
    cur = conn.cursor()

    h = compute_canonical_audit_hash(student_id, txn_id, proposal_id)
    entry_id = f"AUD-{hashlib.sha256(f'{h}_{datetime.now().timestamp()}'.encode()).hexdigest()[:10]}"
    timestamp = datetime.now(timezone.utc).isoformat()
    delta_str = json.dumps(delta)
    state_seal = hashlib.sha256(f"{entry_id}_{h}_{status}_{timestamp}".encode()).hexdigest()

    cur.execute("""
        INSERT OR REPLACE INTO audit_ledger 
        (entry_id, idempotency_hash, timestamp, student_id, txn_id, proposal_id, action_executed, status, operator_signature, delta_payload_json, state_seal_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (entry_id, h, timestamp, student_id, txn_id, proposal_id, action_executed, status, operator_sig, delta_str, state_seal))

    conn.commit()
    conn.close()
    return entry_id

