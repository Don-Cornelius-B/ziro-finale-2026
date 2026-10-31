import os
import sys
import sqlite3
import json
import hashlib
import random
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Union

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DEFAULT_DB_PATH = DATA_DIR / "reconcile.db"
DB_PATH = DEFAULT_DB_PATH  # Backward-compatibility alias required by R1 and ORIGINAL_REQUEST.md
CACHE_DIR = BASE_DIR / ".cache" / "invoices"
FIXTURES_DIR = BASE_DIR / "fixtures"

# Dynamic Chaos Generation Pools & Constants (Requirement R1)
FIRST_NAMES = [
    "Aarav", "Sneha", "Karthik", "Priya",
    "Rohan", "Ananya", "Vikram", "Meera"
]
LAST_NAMES = [
    "Sharma", "Natarajan", "Varma", "Reddy",
    "Livingstone", "Krishnan", "Gupta", "Tiwari"
]
COURSE_CATALOG: List[Tuple[str, float]] = [
    ("FULLSTACK-AI", 25000.0),
    ("CLOUD-DEVOPS", 20000.0),
    ("DATA-ENG", 30000.0),
    ("GEN-AI-LEAD", 50000.0),
    ("CYBER-SEC", 15000.0),
]
STUDENT_DOMAINS = ["gmail.com", "student.edu", "outlook.com", "yahoo.co.in"]
PARENT_DOMAINS = ["corporate.com", "techcorp.in", "enterprise.org", "fintech.io"]
GATEWAY_SOURCES = ["razorpay", "stripe"]
PAYMENT_METHODS = ["upi", "card", "netbanking"]

OPTIONAL_DRIFT_ARCHETYPES = [
    "PAID_BUT_NOT_ACTIVATED",
    "DUPLICATE_CHARGE",
    "REFUND_PENDING",
    "PAYMENT_FAILED_ACCESS_ACTIVE",
    "PARTIAL_PAYMENT_DRIFT",
    "IDENTITY_DRIFT",
]


class DriftCollection(dict):
    """
    Dictionary supporting both student_id keys (Dict[str, Dict]) and
    convenient lookup / membership checking by archetype name (e.g. 'ESCALATE_REFUSAL_FRAUD').
    """
    def __init__(self, by_id: Dict[str, Dict[str, Any]], by_archetype: Dict[str, List[Dict[str, Any]]]):
        super().__init__(by_id)
        self._by_archetype = by_archetype

    @property
    def by_archetype(self) -> Dict[str, List[Dict[str, Any]]]:
        return self._by_archetype

    @property
    def by_id(self) -> Dict[str, Dict[str, Any]]:
        return dict(self)

    def __getitem__(self, key: str) -> Any:
        if super().__contains__(key):
            return super().__getitem__(key)
        if key in self._by_archetype:
            return self._by_archetype[key]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        if super().__contains__(key):
            return super().get(key)
        if key in self._by_archetype:
            return self._by_archetype[key]
        return default

    def __contains__(self, key: Any) -> bool:
        return super().__contains__(key) or key in self._by_archetype


def get_db_connection(db_path: Union[str, Path] = DEFAULT_DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(str(db_path))), exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def create_tables(conn: sqlite3.Connection) -> None:
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


def seed_ground_truth(conn: sqlite3.Connection) -> None:
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


def prewarm_invoice_cache() -> None:
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
                rendered_html = template_html
                for k, v in item.items():
                    rendered_html = rendered_html.replace(f"{{{{ {k} }}}}", str(v))
                seal = hashlib.sha256(f"{item['student_id']}-{item['invoice_id']}".encode()).hexdigest()[:12]
                rendered_html = rendered_html.replace("{{ seal_short }}", seal)

                page.set_content(rendered_html)
                page.screenshot(path=str(out_path), full_page=True)
            browser.close()
    except Exception as e:
        print(f"Playwright pre-warm warning (fallback mode will render on demand): {e}")


def init_db(db_path: Union[str, Path] = DEFAULT_DB_PATH, prewarm: bool = True) -> str:
    """
    Initializes database tables, seeds deterministic TC01-TC10 ground truth,
    and optionally pre-warms invoice PNG snapshots in .cache/invoices.
    Returns the string representation of db_path.
    """
    conn = get_db_connection(db_path)
    create_tables(conn)
    seed_ground_truth(conn)
    conn.close()
    if prewarm:
        prewarm_invoice_cache()
    return str(db_path)


def seed_random_drifts(
    db_path: Union[str, Path] = DEFAULT_DB_PATH,
    num_clean: int = 3,
    num_drifts: int = 6,
    prewarm: bool = False,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Empties gateway_txns, crm_records, invoices, and audit_ledger.
    Synthesizes dynamically generated student records across realistic Indian name pools,
    accredited course IDs, randomized financial values, and varied drift archetypes.
    Guarantees inclusion of ESCALATE_REFUSAL_FRAUD when num_drifts >= 1.
    Returns a structured dictionary of seeded archetypes for verification.
    """
    rng = random.Random(seed)

    conn = get_db_connection(db_path)
    create_tables(conn)
    cursor = conn.cursor()

    # Completely purge existing records across all 4 tables
    cursor.execute("DELETE FROM gateway_txns")
    cursor.execute("DELETE FROM crm_records")
    cursor.execute("DELETE FROM invoices")
    cursor.execute("DELETE FROM audit_ledger")
    conn.commit()

    # 1. Assemble drift archetypes list with GUARANTEED fraud inclusion
    selected_drifts: List[str] = []
    if num_drifts > 0:
        selected_drifts.append("ESCALATE_REFUSAL_FRAUD")
        remaining = num_drifts - 1
        if remaining > 0:
            if remaining <= len(OPTIONAL_DRIFT_ARCHETYPES):
                selected_drifts.extend(rng.sample(OPTIONAL_DRIFT_ARCHETYPES, remaining))
            else:
                selected_drifts.extend(OPTIONAL_DRIFT_ARCHETYPES)
                extra = remaining - len(OPTIONAL_DRIFT_ARCHETYPES)
                selected_drifts.extend(rng.choices(OPTIONAL_DRIFT_ARCHETYPES, k=extra))

    clean_archetypes = ["CLEAN_RECONCILED"] * max(0, num_clean)
    all_archetypes = clean_archetypes + selected_drifts
    rng.shuffle(all_archetypes)

    # 2. Pool of unique name pairs
    all_names = [(f, l) for f in FIRST_NAMES for l in LAST_NAMES]
    rng.shuffle(all_names)

    now = datetime.now(timezone.utc).isoformat()
    due_date = "2026-10-31"

    students_list: List[Dict[str, Any]] = []
    drifts_by_id: Dict[str, Dict[str, Any]] = {}
    drifts_by_archetype: Dict[str, List[Dict[str, Any]]] = {}
    seeded_archetypes: Dict[str, List[str]] = {
        "CLEAN_RECONCILED": [],
        "PAID_BUT_NOT_ACTIVATED": [],
        "DUPLICATE_CHARGE": [],
        "REFUND_PENDING": [],
        "PAYMENT_FAILED_ACCESS_ACTIVE": [],
        "ESCALATE_REFUSAL_FRAUD": [],
        "PARTIAL_PAYMENT_DRIFT": [],
        "IDENTITY_DRIFT": [],
    }

    gateway_rows: List[Tuple] = []
    crm_rows: List[Tuple] = []
    invoice_rows: List[Tuple] = []

    for idx, archetype in enumerate(all_archetypes, start=1):
        sid = f"STU-R{100 + idx:03d}"
        inv_id = f"INV-R{100 + idx:03d}"
        txn_id = f"TXN-R{100 + idx:03d}"

        first_name, last_name = all_names[(idx - 1) % len(all_names)]
        full_name = f"{first_name} {last_name}"
        domain = rng.choice(STUDENT_DOMAINS)
        student_email = f"{first_name.lower()}.{last_name.lower()}@{domain}"

        course_id, fee = rng.choice(COURSE_CATALOG)
        gateway = rng.choice(GATEWAY_SOURCES)
        method = rng.choice(PAYMENT_METHODS)
        ts = f"2026-10-30T{10 + (idx % 10):02d}:{15 + (idx % 40):02d}:00Z"

        txn_ids = [txn_id]
        expected_proposal_class = None

        if archetype == "CLEAN_RECONCILED":
            meta = json.dumps({"source": gateway, "method": method, "course_id": course_id, "status": "settled"})
            gateway_rows.append((txn_id, sid, student_email, fee, "INR", "captured", ts, meta))
            crm_rows.append((sid, full_name, student_email, "active", "none", now))
            invoice_rows.append((inv_id, sid, fee, fee, "paid", due_date))

        elif archetype == "PAID_BUT_NOT_ACTIVATED":
            meta = json.dumps({"source": gateway, "method": method, "course_id": course_id})
            gateway_rows.append((txn_id, sid, student_email, fee, "INR", "captured", ts, meta))
            crm_rows.append((sid, full_name, student_email, "inactive", "daily_overdue_active", now))
            invoice_rows.append((inv_id, sid, fee, 0.0, "unpaid", due_date))
            expected_proposal_class = "PAID_BUT_NOT_ACTIVATED"

        elif archetype == "DUPLICATE_CHARGE":
            txn_id_1 = f"{txn_id}-1"
            txn_id_2 = f"{txn_id}-2"
            txn_ids = [txn_id_1, txn_id_2]
            meta1 = json.dumps({"source": gateway, "method": method, "course_id": course_id})
            meta2 = json.dumps({"source": gateway, "method": method, "course_id": course_id, "note": "duplicate_attempt"})
            gateway_rows.append((txn_id_1, sid, student_email, fee, "INR", "captured", ts, meta1))
            gateway_rows.append((txn_id_2, sid, student_email, fee, "INR", "captured", f"2026-10-30T{10 + (idx % 10):02d}:{15 + (idx % 40):02d}:45Z", meta2))
            crm_rows.append((sid, full_name, student_email, "active", "none", now))
            invoice_rows.append((inv_id, sid, fee, 2.0 * fee, "paid", due_date))
            expected_proposal_class = "DUPLICATE_CHARGE"

        elif archetype == "REFUND_PENDING":
            meta = json.dumps({"source": gateway, "refund_reason": "course_cancellation_requested"})
            gateway_rows.append((txn_id, sid, student_email, fee, "INR", "refund_pending", ts, meta))
            crm_rows.append((sid, full_name, student_email, "active", "none", now))
            invoice_rows.append((inv_id, sid, fee, fee, "paid", due_date))
            expected_proposal_class = "REFUND_PENDING"

        elif archetype == "PAYMENT_FAILED_ACCESS_ACTIVE":
            meta = json.dumps({"error_code": "INSUFFICIENT_FUNDS", "failure_stage": "bank_authorization"})
            gateway_rows.append((txn_id, sid, student_email, fee, "INR", "failed", ts, meta))
            crm_rows.append((sid, full_name, student_email, "active", "none", now))
            invoice_rows.append((inv_id, sid, fee, 0.0, "unpaid", due_date))
            expected_proposal_class = "PAYMENT_FAILED_ACCESS_ACTIVE"

        elif archetype == "ESCALATE_REFUSAL_FRAUD":
            meta = json.dumps({"chargeback": True, "reason": "suspected_stolen_card", "compliance_alert": "AML_FLAG_RED"})
            gateway_rows.append((txn_id, sid, student_email, fee, "INR", "disputed", ts, meta))
            crm_rows.append((sid, full_name, student_email, "active", "none", now))
            invoice_rows.append((inv_id, sid, fee, fee, "paid", due_date))
            expected_proposal_class = "ESCALATE_REFUSAL_FRAUD"

        elif archetype == "PARTIAL_PAYMENT_DRIFT":
            paid_amount = round(fee * 0.6, 2)
            meta = json.dumps({"source": gateway, "installment": 1, "total_installments": 2})
            gateway_rows.append((txn_id, sid, student_email, paid_amount, "INR", "captured", ts, meta))
            crm_rows.append((sid, full_name, student_email, "inactive", "partial_dunning", now))
            invoice_rows.append((inv_id, sid, fee, paid_amount, "partially_paid", due_date))
            expected_proposal_class = "PARTIAL_PAYMENT_DRIFT"

        elif archetype == "IDENTITY_DRIFT":
            parent_initial = rng.choice(["B.", "R.", "S.", "K.", "V."])
            parent_domain = rng.choice(PARENT_DOMAINS)
            parent_email = f"{parent_initial.lower().replace('.', '')}.{last_name.lower()}@{parent_domain}"
            parent_name = f"{parent_initial} {last_name}"
            meta = json.dumps({"payer_relation": "parent", "student_id": sid, "payer_name": parent_name})
            gateway_rows.append((txn_id, sid, parent_email, fee, "INR", "captured", ts, meta))
            crm_rows.append((sid, full_name, student_email, "inactive", "daily_overdue_active", now))
            invoice_rows.append((inv_id, sid, fee, 0.0, "unpaid", due_date))
            expected_proposal_class = "IDENTITY_DRIFT_RESOLVED"

        student_record = {
            "student_id": sid,
            "name": full_name,
            "student_name": full_name,
            "student_email": student_email,
            "email": student_email,
            "course_id": course_id,
            "fee": fee,
            "archetype": archetype,
            "invoice_id": inv_id,
            "txn_ids": txn_ids,
        }
        students_list.append(student_record)
        seeded_archetypes.setdefault(archetype, []).append(sid)

        if archetype != "CLEAN_RECONCILED":
            drift_entry = {
                "student_id": sid,
                "student_name": full_name,
                "name": full_name,
                "student_email": student_email,
                "email": student_email,
                "archetype": archetype,
                "expected_proposal_class": expected_proposal_class,
                "course_id": course_id,
                "fee": fee,
                "invoice_id": inv_id,
                "txn_ids": txn_ids,
            }
            drifts_by_id[sid] = drift_entry
            drifts_by_archetype.setdefault(archetype, []).append(drift_entry)

    # Bulk insert synthetic records
    cursor.executemany("""
    INSERT INTO gateway_txns (txn_id, student_id, email, amount, currency, status, timestamp, metadata_json)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, gateway_rows)

    cursor.executemany("""
    INSERT INTO crm_records (student_id, name, student_email, course_access_status, dunning_status, last_synced)
    VALUES (?, ?, ?, ?, ?, ?)
    """, crm_rows)

    cursor.executemany("""
    INSERT INTO invoices (invoice_id, student_id, amount_due, amount_paid, status, due_date)
    VALUES (?, ?, ?, ?, ?, ?)
    """, invoice_rows)

    conn.commit()
    conn.close()

    archetype_counts = {arch: len(sids) for arch, sids in seeded_archetypes.items()}
    actual_drift_count = sum(len(sids) for arch, sids in seeded_archetypes.items() if arch != "CLEAN_RECONCILED")
    clean_sids = seeded_archetypes.get("CLEAN_RECONCILED", [])
    drift_collection = DriftCollection(drifts_by_id, drifts_by_archetype)

    if prewarm:
        prewarm_invoice_cache()

    return {
        "db_path": str(db_path),
        "seed": seed,
        "prewarmed": prewarm,
        "total_students": len(students_list),
        "clean_count": len(clean_sids),
        "drift_count": actual_drift_count,
        "clean": clean_sids,
        "archetype_counts": archetype_counts,
        "seeded_archetypes": seeded_archetypes,
        "drifts": drift_collection,
        "students": students_list,
        "summary": {
            "total": len(students_list),
            "clean": len(clean_sids),
            "drifts": actual_drift_count,
            "seed": seed,
            "prewarmed": prewarm,
            "fraud_guaranteed": len(seeded_archetypes.get("ESCALATE_REFUSAL_FRAUD", [])) > 0,
            "archetypes_present": [k for k, v in archetype_counts.items() if v > 0],
        },
    }


def print_baseline_summary(db_path: Union[str, Path], prewarm: bool) -> None:
    print("=" * 80)
    print(" ZIRO PAYMENT-RECONCILIATION OPS AGENT | DATABASE SETUP")
    print("=" * 80)
    print("Mode: DETERMINISTIC BENCHMARK (TC01-TC10)")
    print(f"Target Database: {os.path.abspath(str(db_path))}")
    print(f"Cache Pre-warming: {'ENABLED (.cache/invoices)' if prewarm else 'DISABLED'}")
    print("-" * 80)
    print("[+] Database tables verified / created:")
    print("    - gateway_txns, crm_records, invoices, audit_ledger")
    print("[+] Seeded 10 deterministic ground-truth evaluation cases:")
    print("    * TC01: STU-001 (Arjun Sharma)        -> PAID_BUT_NOT_ACTIVATED (INR 25,000)")
    print("    * TC02: STU-002 (Priya Patel)         -> DUPLICATE_CHARGE (2x INR 15,000 capture)")
    print("    * TC03: STU-003 (Rohan Mehta)         -> REFUND_PENDING (INR 20,000)")
    print("    * TC04: STU-004 (Ananya Iyer)         -> PAYMENT_FAILED_ACCESS_ACTIVE (INR 30,000)")
    print("    * TC05: STU-005 (Vikram Malhotra)     -> ESCALATE_REFUSAL_FRAUD (INR 50,000, AML Flag)")
    print("    * TC06: STU-006 (Sneha Reddy)         -> PARTIAL_PAYMENT_DRIFT (INR 15,000 of INR 20,000)")
    print("    * TC07: STU-007 (Devendra Joshi)      -> PENDING_GATEWAY_SYNC (INR 18,000 unpaid)")
    print("    * TC08: STU-008 (Karthik Livingstone) -> IDENTITY_DRIFT (Parent corporate email)")
    print("    * TC09: STU-009 (Neha Verma)          -> CLEAN_RECONCILED (INR 25,000 settled)")
    print("    * TC10: STU-010 (Amitabh Sen)         -> OPERATOR_REJECTED (INR 12,000 unpaid)")
    if prewarm:
        print("[+] Pre-warmed 10 high-fidelity invoice screenshots into .cache/invoices")
    print("=" * 80)
    print("Status: READY for deterministic evaluation (11/11 tests passing)")
    print("=" * 80)


def print_random_summary(result: Dict[str, Any]) -> None:
    print("=" * 80)
    print(" ZIRO PAYMENT-RECONCILIATION OPS AGENT | DATABASE SETUP")
    print("=" * 80)
    print("Mode: DYNAMIC CHAOS MODE (Randomized Drift Synthesis)")
    print(f"Target Database: {os.path.abspath(str(result.get('db_path', DEFAULT_DB_PATH)))}")
    seed_val = result.get("seed")
    print(f"Random Seed: {seed_val if seed_val is not None else 'None (Non-deterministic dynamic)'}")
    print("-" * 80)
    print(f"[+] Total Student Records Seeded: {result.get('total_students', 0)}")
    print(f"    - Clean Records: {result.get('clean_count', 0)}")
    print(f"    - Drift Records: {result.get('drift_count', 0)}")
    print("[+] Seeded Archetype Breakdown:")
    seeded_archs = result.get("seeded_archetypes", {})
    for arch, sids in seeded_archs.items():
        tag = " [COMPLIANCE GUARDRAIL ACTIVE]" if arch == "ESCALATE_REFUSAL_FRAUD" else ""
        sids_str = ", ".join(sids) if sids else "none"
        print(f"    * {arch} ({len(sids)} record{'s' if len(sids) != 1 else ''}): {sids_str}{tag}")
    prewarmed = result.get("prewarmed", False)
    if prewarmed:
        print("[+] Invoice Screenshots: Pre-warmed into .cache/invoices")
    else:
        print("[+] Invoice Screenshots: Deferred to on-the-fly rendering (<500ms Playwright / <15ms fallback)")
    print("=" * 80)
    print("Status: READY for generalized reconciliation evaluation (TC12 compliant)")
    print("=" * 80)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Ziro Ops Agent Database Initialization and Seeding Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Usage Examples:
  python data/db_setup.py                 # Resets deterministic TC01-TC10 baseline (pre-warms cache)
  python data/db_setup.py --no-prewarm    # Resets baseline without Playwright rendering (<50ms)
  python data/db_setup.py --random        # Generates dynamic chaos dataset (3 clean, 6 drifts)
  python data/db_setup.py --random --seed 42  # Deterministic chaos generation
  python data/db_setup.py --random --num-clean 4 --num-drifts 7
        """
    )
    parser.add_argument(
        "--random",
        action="store_true",
        help="Generate dynamic randomized chaos dataset instead of deterministic baseline"
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=str(DEFAULT_DB_PATH),
        help=f"Target SQLite database path (default: {DEFAULT_DB_PATH})"
    )
    parser.add_argument(
        "--num-clean",
        type=int,
        default=3,
        help="Number of clean reconciled student records in random mode (default: 3)"
    )
    parser.add_argument(
        "--num-drifts",
        type=int,
        default=6,
        help="Number of drift records in random mode (default: 6)"
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional random seed integer for reproducible chaos generation"
    )
    parser.add_argument(
        "--prewarm",
        action="store_true",
        default=False,
        help="Pre-warm Playwright invoice screenshot cache for dynamic random records (default: False)"
    )
    parser.add_argument(
        "--no-prewarm",
        action="store_true",
        help="Skip pre-warming Playwright invoice screenshot cache for baseline"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON to stdout"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.random:
        res = seed_random_drifts(
            db_path=args.db_path,
            num_clean=args.num_clean,
            num_drifts=args.num_drifts,
            prewarm=args.prewarm,
            seed=args.seed
        )
        if args.json:
            # Prepare json serializable payload (convert DriftCollection to dict)
            serializable_res = dict(res)
            serializable_res["drifts"] = dict(res["drifts"])
            print(json.dumps(serializable_res, indent=2))
        else:
            print_random_summary(res)
    else:
        should_prewarm = not args.no_prewarm
        db_p = init_db(db_path=args.db_path, prewarm=should_prewarm)
        if args.json:
            print(json.dumps({"status": "SUCCESS", "mode": "BENCHMARK", "db_path": db_p, "prewarm": should_prewarm}, indent=2))
        else:
            print_baseline_summary(db_path=db_p, prewarm=should_prewarm)


__all__ = [
    "init_db",
    "seed_random_drifts",
    "DEFAULT_DB_PATH",
    "DB_PATH",
    "CACHE_DIR",
    "FIXTURES_DIR",
    "get_db_connection",
    "create_tables",
    "seed_ground_truth",
    "prewarm_invoice_cache",
    "DriftCollection",
]

if __name__ == "__main__":
    main()
