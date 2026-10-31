from enum import Enum
from typing import Optional, List, Dict, Any, TypedDict
from pydantic import BaseModel, Field

class DetectedClass(str, Enum):
    PAID_BUT_NOT_ACTIVATED = "PAID_BUT_NOT_ACTIVATED"
    DUPLICATE_CHARGE = "DUPLICATE_CHARGE"
    REFUND_PENDING = "REFUND_PENDING"
    PAYMENT_FAILED_ACCESS_ACTIVE = "PAYMENT_FAILED_ACCESS_ACTIVE"
    ESCALATE_REFUSAL_FRAUD = "ESCALATE_REFUSAL_FRAUD"
    PARTIAL_PAYMENT_DRIFT = "PARTIAL_PAYMENT_DRIFT"
    IDENTITY_DRIFT_RESOLVED = "IDENTITY_DRIFT_RESOLVED"
    PENDING_GATEWAY_SYNC = "PENDING_GATEWAY_SYNC"
    CLEAN_RECONCILED = "CLEAN_RECONCILED"
    OPERATOR_REJECTED = "OPERATOR_REJECTED"

class GatewayTxn(BaseModel):
    txn_id: str
    student_id: Optional[str] = None
    email: str
    amount: float
    currency: str = "INR"
    status: str
    timestamp: str
    metadata_json: Optional[str] = "{}"

class CRMRecord(BaseModel):
    student_id: str
    name: str
    student_email: str
    course_access_status: str  # "active" or "inactive"
    dunning_status: str        # "none", "daily_overdue_active", "partial_dunning"
    last_synced: str

class InvoiceRecord(BaseModel):
    invoice_id: str
    student_id: str
    amount_due: float
    amount_paid: float
    status: str                # "paid", "unpaid", "partially_paid"
    due_date: str

class MismatchProposal(BaseModel):
    proposal_id: str
    student_id: str
    student_name: str
    txn_id: Optional[str] = None
    invoice_id: Optional[str] = None
    detected_class: DetectedClass
    confidence_score: float = Field(ge=0.0, le=1.0)
    evidence_quote: str
    proposed_fix: str
    requires_human_approval: bool = True
    invoice_preview_path: Optional[str] = None
    audit_hash: Optional[str] = None

class AuditLedgerEntry(BaseModel):
    entry_id: str
    idempotency_hash: str
    timestamp: str
    student_id: str
    txn_id: Optional[str] = None
    proposal_id: str
    action_executed: str
    status: str  # "COMMITTED", "REFUSED_GUARDRAIL", "SKIPPED_ALREADY_PROCESSED", "REJECTED_BY_OPERATOR"
    operator_signature: str
    delta_payload_json: str
    state_seal_hash: str

class ReconcileAgentState(TypedDict):
    run_id: str
    step_history: List[Dict[str, Any]]
    plan: Dict[str, Any]
    observed_drift_count: int
    raw_mismatches: List[Dict[str, Any]]
    proposals: List[Dict[str, Any]]
    approved_proposal_ids: List[str]
    rejected_proposal_ids: List[str]
    executed_actions: List[Dict[str, Any]]
    refused_actions: List[Dict[str, Any]]
    replan_count: int
    replan_needed: bool
    audit_hash: str

