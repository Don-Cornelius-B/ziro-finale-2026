import os
import sqlite3
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DEFAULT_DB_PATH = DATA_DIR / "reconcile.db"
CACHE_DIR = BASE_DIR / ".cache" / "invoices"
FIXTURES_DIR = BASE_DIR / "fixtures"

def get_db_connection(db_path=DEFAULT_DB_PATH):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn

def create_tables(conn):
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS gateway_txns (
        txn_id TEXT PRIMARY KEY,
        student_id TEXT,
        email TEXT,
        amount REAL,
        currency TEXT,
        status TEXT,
        timestamp TEXT,
        metadata_json TEXT
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS crm_records (
        student_id TEXT PRIMARY KEY,
        name TEXT,
        student_email TEXT,
        course_access_status TEXT,
        dunning_status TEXT,
        last_synced TEXT
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS invoices (
        invoice_id TEXT PRIMARY KEY,
        student_id TEXT,
        amount_due REAL,
        amount_paid REAL,
        status TEXT,
        due_date TEXT
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS audit_ledger (
        entry_id TEXT PRIMARY KEY,
        idempotency_hash TEXT UNIQUE,
        timestamp TEXT,
        student_id TEXT,
        txn_id TEXT,
        proposal_id TEXT,
        action_executed TEXT,
        status TEXT,
        operator_signature TEXT,
        delta_payload_json TEXT,
        state_seal_hash TEXT
    );
    """)
    conn.commit()

def seed_ground_truth(conn):
    cursor = conn.cursor()
    cursor.execute("DELETE FROM gateway_txns")
    cursor.execute("DELETE FROM crm_records")
    cursor.execute("DELETE FROM invoices")
    cursor.execute("DELETE FROM audit_ledger")

    now = datetime.now(timezone.utc).isoformat()

    # 1. Gateway Transactions
    gateway_data = [
        # TC01: PAID_BUT_NOT_ACTIVATED
        ("TXN-101", "STU-001", "arjun.sharma@gmail.com", 25000.0, "INR", "captured", "2026-10-30T10:15:00Z", json.dumps({"source": "razorpay", "method": "upi"})),
        # TC02: DUPLICATE_CHARGE (Two captures for single invoice)
        ("TXN-201", "STU-002", "priya.patel@gmail.com", 15000.0, "INR", "captured", "2026-10-30T11:00:00Z", json.dumps({"source": "stripe", "method": "card"})),
        ("TXN-202", "STU-002", "priya.patel@gmail.com", 15000.0, "INR", "captured", "2026-10-30T11:00:45Z", json.dumps({"source": "stripe", "method": "card", "note": "duplicate_attempt"})),
        # TC03: REFUND_PENDING
        ("TXN-301", "STU-003", "rohan.mehta@gmail.com", 20000.0, "INR", "refund_pending", "2026-10-30T12:30:00Z", json.dumps({"source": "razorpay", "refund_reason": "course_cancellation_requested"})),
        # TC04: PAYMENT_FAILED_ACCESS_ACTIVE
        ("TXN-401", "STU-004", "ananya.iyer@gmail.com", 30000.0, "INR", "failed", "2026-10-30T13:45:00Z", json.dumps({"error_code": "INSUFFICIENT_FUNDS"})),
        # TC05: ESCALATE_REFUSAL_FRAUD
        ("TXN-501", "STU-005", "vikram.malhotra@gmail.com", 50000.0, "INR", "disputed", "2026-10-30T14:10:00Z", json.dumps({"chargeback": True, "reason": "suspected_stolen_card", "compliance_alert": "AML_FLAG_RED"})),
        # TC06: PARTIAL_PAYMENT_DRIFT
        ("TXN-601", "STU-006", "sneha.reddy@gmail.com", 15000.0, "INR", "captured", "2026-10-30T15:20:00Z", json.dumps({"source": "razorpay", "installment": 1})),
        # TC07: PENDING_GATEWAY_SYNC (No gateway capture present for STU-007)
        # TC08: IDENTITY_DRIFT (Paid under parental email b.livingstone@corporate.com)
        ("TXN-801", "STU-008", "b.livingstone@corporate.com", 22000.0, "INR", "captured", "2026-10-30T16:05:00Z", json.dumps({"payer_relation": "parent", "student_id": "STU-008", "payer_name": "Barnaby Livingstone"})),
        # TC09: CLEAN_RECONCILED
        ("TXN-901", "STU-009", "neha.verma@gmail.com", 25000.0, "INR", "captured", "2026-10-30T16:30:00Z", json.dumps({"source": "razorpay", "status": "settled"})),
        # TC10: OPERATOR_REJECTED
        ("TXN-999", "STU-010", "amitabh.sen@gmail.com", 12000.0, "INR", "captured", "2026-10-30T17:15:00Z", json.dumps({"source": "stripe", "status": "captured"})),
    ]

    cursor.executemany("""
    INSERT INTO gateway_txns (txn_id, student_id, email, amount, currency, status, timestamp, metadata_json)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, gateway_data)

    # 2. CRM Records
    crm_data = [
        # TC01: Student paid, but CRM still inactive & daily overdue dunning is firing!
        ("STU-001", "Arjun Sharma", "arjun.sharma@gmail.com", "inactive", "daily_overdue_active", now),
        # TC02: Student paid twice, CRM is active, no dunning
        ("STU-002", "Priya Patel", "priya.patel@gmail.com", "active", "none", now),
        # TC03: Refund requested, CRM currently active
        ("STU-003", "Rohan Mehta", "rohan.mehta@gmail.com", "active", "none", now),
        # TC04: Payment failed, yet CRM is free-riding active!
        ("STU-004", "Ananya Iyer", "ananya.iyer@gmail.com", "active", "none", now),
        # TC05: Disputed / Fraudulent, CRM active
        ("STU-005", "Vikram Malhotra", "vikram.malhotra@gmail.com", "active", "none", now),
        # TC06: Partial payment, CRM inactive with partial dunning
        ("STU-006", "Sneha Reddy", "sneha.reddy@gmail.com", "inactive", "partial_dunning", now),
        # TC07: Unpaid, CRM inactive, pending sync
        ("STU-007", "Devendra Joshi", "devendra.j@gmail.com", "inactive", "none", now),
        # TC08: Student email in CRM differs from parent billing email in Gateway
        ("STU-008", "Karthik Livingstone", "karthik.l@student.edu", "inactive", "daily_overdue_active", now),
        # TC09: Clean and synced
        ("STU-009", "Neha Verma", "neha.verma@gmail.com", "active", "none", now),
        # TC10: Operator will reject staged proposal
        ("STU-010", "Amitabh Sen", "amitabh.sen@gmail.com", "inactive", "daily_overdue_active", now),
    ]

    cursor.executemany("""
    INSERT INTO crm_records (student_id, name, student_email, course_access_status, dunning_status, last_synced)
    VALUES (?, ?, ?, ?, ?, ?)
    """, crm_data)

    # 3. Invoices
    invoice_data = [
        # TC01
        ("INV-101", "STU-001", 25000.0, 0.0, "unpaid", "2026-10-31"),
        # TC02 (Single invoice for 15,000, but 30,000 was collected)
        ("INV-201", "STU-002", 15000.0, 30000.0, "paid", "2026-10-31"),
        # TC03
        ("INV-301", "STU-003", 20000.0, 20000.0, "paid", "2026-10-31"),
        # TC04
        ("INV-401", "STU-004", 30000.0, 0.0, "unpaid", "2026-10-31"),
        # TC05
        ("INV-501", "STU-005", 50000.0, 50000.0, "paid", "2026-10-31"),
        # TC06
        ("INV-601", "STU-006", 20000.0, 15000.0, "partially_paid", "2026-10-31"),
        # TC07
        ("INV-701", "STU-007", 18000.0, 0.0, "unpaid", "2026-10-31"),
        # TC08
        ("INV-801", "STU-008", 22000.0, 0.0, "unpaid", "2026-10-31"),
        # TC09
        ("INV-901", "STU-009", 25000.0, 25000.0, "paid", "2026-10-31"),
        # TC10
        ("INV-999", "STU-010", 12000.0, 0.0, "unpaid", "2026-10-31"),
    ]

    cursor.executemany("""
    INSERT INTO invoices (invoice_id, student_id, amount_due, amount_paid, status, due_date)
    VALUES (?, ?, ?, ?, ?, ?)
    """, invoice_data)

    conn.commit()
    print("Database tables created and seeded with 10 ground-truth evaluation cases.")

def prewarm_invoice_cache():
    """
    Pre-renders high-fidelity PNG invoice receipts for TC01-TC10 into .cache/invoices/
    using headless Playwright so runtime rendering on Streamlit is sub-100ms.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    template_path = FIXTURES_DIR / "invoice_template.html"
    if not template_path.exists():
        print(f"Warning: template {template_path} does not exist. Skipping prewarm.")
        return

    with open(template_path, "r", encoding="utf-8") as f:
        template_html = f.read()

    # Prewarm metadata for all 10 students
    test_cases_meta = [
        {"student_id": "STU-001", "student_name": "Arjun Sharma", "invoice_id": "INV-101", "txn_id": "TXN-101", "email": "arjun.sharma@gmail.com", "billing_date": "2026-10-30", "amount_due": "25,000", "amount_paid": "25,000", "balance_due": "0.00", "currency": "INR", "status_label": "PAID - DRIFT DETECTED", "badge_class": "badge-pending", "balance_color": "#16a34a"},
        {"student_id": "STU-002", "student_name": "Priya Patel", "invoice_id": "INV-201", "txn_id": "TXN-201, TXN-202", "email": "priya.patel@gmail.com", "billing_date": "2026-10-30", "amount_due": "15,000", "amount_paid": "30,000", "balance_due": "-15,000.00 (CREDIT)", "currency": "INR", "status_label": "DUPLICATE OVERPAYMENT", "badge_class": "badge-pending", "balance_color": "#4338ca"},
        {"student_id": "STU-003", "student_name": "Rohan Mehta", "invoice_id": "INV-301", "txn_id": "TXN-301", "email": "rohan.mehta@gmail.com", "billing_date": "2026-10-30", "amount_due": "20,000", "amount_paid": "20,000", "balance_due": "0.00", "currency": "INR", "status_label": "REFUND PENDING", "badge_class": "badge-pending", "balance_color": "#b45309"},
        {"student_id": "STU-004", "student_name": "Ananya Iyer", "invoice_id": "INV-401", "txn_id": "TXN-401", "email": "ananya.iyer@gmail.com", "billing_date": "2026-10-30", "amount_due": "30,000", "amount_paid": "0.00", "balance_due": "30,000.00", "currency": "INR", "status_label": "PAYMENT FAILED", "badge_class": "badge-disputed", "balance_color": "#b91c1c"},
        {"student_id": "STU-005", "student_name": "Vikram Malhotra", "invoice_id": "INV-501", "txn_id": "TXN-501", "email": "vikram.malhotra@gmail.com", "billing_date": "2026-10-30", "amount_due": "50,000", "amount_paid": "50,000", "balance_due": "DISPUTED", "currency": "INR", "status_label": "CHARGEBACK DISPUTE", "badge_class": "badge-disputed", "balance_color": "#b91c1c"},
        {"student_id": "STU-006", "student_name": "Sneha Reddy", "invoice_id": "INV-601", "txn_id": "TXN-601", "email": "sneha.reddy@gmail.com", "billing_date": "2026-10-30", "amount_due": "20,000", "amount_paid": "15,000", "balance_due": "5,000.00", "currency": "INR", "status_label": "PARTIALLY PAID", "badge_class": "badge-partial", "balance_color": "#4338ca"},
        {"student_id": "STU-007", "student_name": "Devendra Joshi", "invoice_id": "INV-701", "txn_id": "N/A", "email": "devendra.j@gmail.com", "billing_date": "2026-10-30", "amount_due": "18,000", "amount_paid": "0.00", "balance_due": "18,000.00", "currency": "INR", "status_label": "UNPAID - NO CAPTURE", "badge_class": "badge-inactive", "balance_color": "#64748b"},
        {"student_id": "STU-008", "student_name": "Karthik Livingstone", "invoice_id": "INV-801", "txn_id": "TXN-801", "email": "karthik.l@student.edu", "billing_date": "2026-10-30", "amount_due": "22,000", "amount_paid": "22,000", "balance_due": "0.00", "currency": "INR", "status_label": "RESOLVED (PARENT PAYER)", "badge_class": "badge-paid", "balance_color": "#16a34a"},
        {"student_id": "STU-009", "student_name": "Neha Verma", "invoice_id": "INV-901", "txn_id": "TXN-901", "email": "neha.verma@gmail.com", "billing_date": "2026-10-30", "amount_due": "25,000", "amount_paid": "25,000", "balance_due": "0.00", "currency": "INR", "status_label": "CLEAN RECONCILED", "badge_class": "badge-paid", "balance_color": "#16a34a"},
        {"student_id": "STU-010", "student_name": "Amitabh Sen", "invoice_id": "INV-999", "txn_id": "TXN-999", "email": "amitabh.sen@gmail.com", "billing_date": "2026-10-30", "amount_due": "12,000", "amount_paid": "12,000", "balance_due": "0.00", "currency": "INR", "status_label": "PENDING OPERATOR REVIEW", "badge_class": "badge-pending", "balance_color": "#b45309"},
    ]

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 640, "height": 620})
            for item in test_cases_meta:
                out_path = CACHE_DIR / f"{item['student_id']}.png"
                # Simple templating replacement
                rendered_html = template_html
                for k, v in item.items():
                    rendered_html = rendered_html.replace(f"{{{{ {k} }}}}", str(v))
                seal = hashlib.sha256(f"{item['student_id']}-{item['invoice_id']}".encode()).hexdigest()[:12]
                rendered_html = rendered_html.replace("{{ seal_short }}", seal)

                page.set_content(rendered_html)
                page.screenshot(path=str(out_path), full_page=True)
            browser.close()
            print(f"Pre-warmed {len(test_cases_meta)} invoice screenshots into {CACHE_DIR}")
    except Exception as e:
        print(f"Playwright pre-warm warning (fallback mode will render on demand): {e}")

def init_db(db_path=DEFAULT_DB_PATH, prewarm=True):
    conn = get_db_connection(db_path)
    create_tables(conn)
    seed_ground_truth(conn)
    conn.close()
    if prewarm:
        prewarm_invoice_cache()
    return str(db_path)

if __name__ == "__main__":
    init_db()
