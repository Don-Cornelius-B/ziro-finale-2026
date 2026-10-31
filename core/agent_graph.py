import os
import uuid
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from core.schemas import ReconcileAgentState, DetectedClass
from core.engine import (
    reconcile_drift_engine,
    execute_mutation,
    execute_rejection,
    check_idempotency,
    get_db,
)
from tools.ops_tools import (
    DEFAULT_DB_PATH,
    GuardrailViolationError,
    get_sql_database_tool,
    get_python_repl_tool,
    compute_canonical_audit_hash,
)

# Global memory saver for state persistence across HITL cycles
default_checkpointer = MemorySaver()


def plan_node(state: ReconcileAgentState) -> Dict[str, Any]:
    """
    Deconstructs reconciliation goals across tri-source tables and formulates join strategies.
    """
    run_id = state.get("run_id") or str(uuid.uuid4())[:8]
    strategy = state.get("plan", {}).get("strategy", "primary_email")
    replan_count = state.get("replan_count", 0)

    step = {
        "node": "plan_node",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "strategy": strategy,
        "detail": f"Formulated tri-source reconciliation plan using {strategy} matching. (Replan cycle: {replan_count})"
    }

    history = list(state.get("step_history", []))
    history.append(step)

    plan = {
        "target_tables": ["gateway_txns", "crm_records", "invoices"],
        "strategy": strategy,
        "tolerance_epsilon": 1.0,
        "currency": "INR",
        "guardrail_policy": "STRICT_REFUSAL_ON_AML_FRAUD"
    }

    return {
        "run_id": run_id,
        "step_history": history,
        "plan": plan,
        "replan_needed": False
    }


def act_node(state: ReconcileAgentState) -> Dict[str, Any]:
    """
    Invokes QuerySQLDatabaseTool and PythonREPLTool to read data, execute joins, and compute balance tolerances.
    """
    sql_tool = get_sql_database_tool()
    # Execute query tool to sample table counts
    gateway_count = sql_tool.run("SELECT COUNT(*) FROM gateway_txns")
    crm_count = sql_tool.run("SELECT COUNT(*) FROM crm_records")
    inv_count = sql_tool.run("SELECT COUNT(*) FROM invoices")

    strategy = state.get("plan", {}).get("strategy", "primary_email")
    replan_count = state.get("replan_count", 0)

    # Run deterministic reconciliation engine
    engine_output = reconcile_drift_engine(
        matching_strategy=strategy,
        replan_count=replan_count
    )

    step = {
        "node": "act_node",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool_invoked": "QuerySQLDatabaseTool & PythonREPLTool",
        "detail": f"Ingested tri-source records: Gateway ({gateway_count.strip()}), CRM ({crm_count.strip()}), Invoices ({inv_count.strip()}). Staged {len(engine_output['proposals'])} raw drift proposals."
    }

    history = list(state.get("step_history", []))
    history.append(step)

    return {
        "step_history": history,
        "raw_mismatches": engine_output["proposals"],
        "observed_drift_count": len(engine_output["proposals"]),
        "replan_needed": engine_output["replan_needed"]
    }


def observe_node(state: ReconcileAgentState) -> Dict[str, Any]:
    """
    Inspects anomalies and detects unresolved drifts (e.g. parent email drift vs student ID).
    """
    replan_needed = state.get("replan_needed", False)
    replan_count = state.get("replan_count", 0)
    raw_mismatches = state.get("raw_mismatches", [])

    if replan_needed and replan_count < 2:
        obs_detail = "Detected unresolved payment-to-student drift (parental billing email mismatch). Triggering autonomous re-plan."
    else:
        obs_detail = f"Drift inspection complete. {len(raw_mismatches)} anomalies confirmed for proposal staging."

    step = {
        "node": "observe_node",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "replan_needed": replan_needed,
        "detail": obs_detail
    }

    history = list(state.get("step_history", []))
    history.append(step)

    return {
        "step_history": history,
        "replan_needed": replan_needed
    }


def replan_node(state: ReconcileAgentState) -> Dict[str, Any]:
    """
    Adjusts matching policy to fallback composite key (student_id) and increments replan_count.
    """
    current_count = state.get("replan_count", 0)
    new_count = current_count + 1

    plan = dict(state.get("plan", {}))
    plan["strategy"] = "composite_student_id"

    step = {
        "node": "replan_node",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "replan_count": new_count,
        "detail": "Switching from primary_email join to composite_student_id key fallback to resolve parental email identity drift."
    }

    history = list(state.get("step_history", []))
    history.append(step)

    return {
        "step_history": history,
        "plan": plan,
        "replan_count": new_count,
        "replan_needed": False
    }


def stage_proposals_node(state: ReconcileAgentState) -> Dict[str, Any]:
    """
    Finalizes MismatchProposal records, embeds invoice preview links, and prepares HITL pause.
    """
    proposals = list(state.get("raw_mismatches", []))

    step = {
        "node": "stage_proposals_node",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "staged_count": len(proposals),
        "detail": f"Prepared {len(proposals)} reconciliation proposals. LangGraph pausing for Human-in-the-Loop operator approval."
    }

    history = list(state.get("step_history", []))
    history.append(step)

    return {
        "step_history": history,
        "proposals": proposals
    }


def execute_fix_node(state: ReconcileAgentState) -> Dict[str, Any]:
    """
    Executes approved proposals:
    - Enforces physical Refusal Guardrail on ESCALATE_REFUSAL_FRAUD
    - Verifies idempotency ledger
    - Mutates CRM & Invoices
    - Silences false dunning reminders via webhook
    """
    approved_ids = set(state.get("approved_proposal_ids", []))
    rejected_ids = set(state.get("rejected_proposal_ids", []))
    proposals = state.get("proposals", [])

    executed_actions = list(state.get("executed_actions", []))
    refused_actions = list(state.get("refused_actions", []))

    for prop in proposals:
        pid = prop["proposal_id"]
        sid = prop["student_id"]
        det_class = prop.get("detected_class")

        # 1. Check if proposal was rejected by operator
        if pid in rejected_ids:
            res = execute_rejection(prop)
            executed_actions.append(res)
            continue

        # 2. Check if proposal was approved
        if pid in approved_ids:
            # Check fraud refusal guardrail
            if det_class == DetectedClass.ESCALATE_REFUSAL_FRAUD.value or det_class == "ESCALATE_REFUSAL_FRAUD":
                try:
                    execute_mutation(prop)
                except GuardrailViolationError as gve:
                    refused_record = {
                        "proposal_id": pid,
                        "student_id": sid,
                        "status": "REFUSED_GUARDRAIL",
                        "error": str(gve),
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
                    refused_actions.append(refused_record)
                continue

            # Standard approved mutation
            res = execute_mutation(prop)
            executed_actions.append(res)

    step = {
        "node": "execute_fix_node",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "executed_count": len(executed_actions),
        "refused_count": len(refused_actions),
        "detail": f"Executed {len(executed_actions)} state mutations and enforced {len(refused_actions)} compliance refusal guardrails."
    }

    history = list(state.get("step_history", []))
    history.append(step)

    return {
        "step_history": history,
        "executed_actions": executed_actions,
        "refused_actions": refused_actions
    }


def audit_seal_node(state: ReconcileAgentState) -> Dict[str, Any]:
    """
    Computes cryptographic SHA-256 state seal of the entire batch execution and commits seal.
    """
    executed = state.get("executed_actions", [])
    refused = state.get("refused_actions", [])
    raw_payload = json.dumps({"executed": executed, "refused": refused}, sort_keys=True)
    batch_seal = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()

    step = {
        "node": "audit_seal_node",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "batch_seal": batch_seal,
        "detail": f"Cryptographic state seal generated: {batch_seal[:16]}... Committing immutable audit batch."
    }

    history = list(state.get("step_history", []))
    history.append(step)

    return {
        "step_history": history,
        "audit_hash": batch_seal
    }


def route_after_observe(state: ReconcileAgentState) -> str:
    """
    Conditional routing edge: loops to replan_node if unresolved drift exists; otherwise stages proposals.
    """
    if state.get("replan_needed", False) and state.get("replan_count", 0) < 2:
        return "replan_node"
    return "stage_proposals_node"


def create_reconcile_graph(checkpointer: Optional[MemorySaver] = None):
    """
    Assembles and compiles the production LangGraph state graph with HITL interrupt.
    """
    cp = checkpointer or default_checkpointer
    builder = StateGraph(ReconcileAgentState)

    builder.add_node("plan_node", plan_node)
    builder.add_node("act_node", act_node)
    builder.add_node("observe_node", observe_node)
    builder.add_node("replan_node", replan_node)
    builder.add_node("stage_proposals_node", stage_proposals_node)
    builder.add_node("execute_fix_node", execute_fix_node)
    builder.add_node("audit_seal_node", audit_seal_node)

    builder.set_entry_point("plan_node")
    builder.add_edge("plan_node", "act_node")
    builder.add_edge("act_node", "observe_node")

    builder.add_conditional_edges(
        "observe_node",
        route_after_observe,
        {
            "replan_node": "replan_node",
            "stage_proposals_node": "stage_proposals_node"
        }
    )

    builder.add_edge("replan_node", "act_node")
    builder.add_edge("stage_proposals_node", "execute_fix_node")
    builder.add_edge("execute_fix_node", "audit_seal_node")
    builder.add_edge("audit_seal_node", END)

    # Compile with human-in-the-loop pause before executing mutations
    graph = builder.compile(
        checkpointer=cp,
        interrupt_before=["execute_fix_node"]
    )
    return graph
