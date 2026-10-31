# Project: ziro-finale-2026 Payment-Reconciliation Ops Agent Upgrade

## Architecture
- **Data Layer (`data/db_setup.py`)**: SQLite database (`data/reconcile.db`) with 4 tables (`gateway_txns`, `crm_records`, `invoices`, `audit_ledger`). Supports deterministic benchmark mode (`init_db`) and dynamic chaos mode (`seed_random_drifts`).
- **Invoicing & Tool Layer (`tools/ops_tools.py`, `fixtures/invoice_template.html`)**: `PlaywrightInvoiceGenerator` provides sub-500ms on-the-fly rendering using headless Chromium with caching (<5ms) and instant SVG/PNG fallback for 100% resilience.
- **Reconciliation Engine & Graph Layer (`core/schemas.py`, `core/agent_graph.py`, `core/engine.py`)**: State graph with database path scoping (`db_path`), relational invariant matching without hardcoded IDs, refusal guardrails for fraud/disputes (`ESCALATE_REFUSAL_FRAUD`).
- **UI Layer (`app.py`)**: Streamlit application with sidebar mode badge (`[STANDARD BENCHMARK]` vs `[DYNAMIC CHAOS MODE]`), dual reset/chaos buttons, Tab 1 tri-source matrix & drift breakdown metric pills, Tab 2 HITL action center with generalized reminder suppression and dynamic invoice previews, Tab 3 real-time cryptographic audit trail.
- **Testing & Verification Layer (`tests/test_benchmark.py`)**: 12/12 passing tests including TC01–TC10, idempotency, and TC12 dynamic randomized reconciliation.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Baseline Seed & Snapshot Prewarm | `init_db(db_path=DB_PATH)` preserves TC01–TC10 baseline seed and pre-warms PNG snapshots | M1 | survey (R1) |
| 2 | Dynamic Drift Generator | `seed_random_drifts(db_path=DB_PATH, num_clean=3, num_drifts=6)` clears tables and generates random student records from Indian name pools & course IDs | M1 | survey (R1) |
| 3 | Drift Archetype Synthesis | Synthesizes 8 archetypes: PAID_BUT_NOT_ACTIVATED, DUPLICATE_CHARGE, REFUND_PENDING, PAYMENT_FAILED_ACCESS_ACTIVE, ESCALATE_REFUSAL_FRAUD, PARTIAL_PAYMENT_DRIFT, IDENTITY_DRIFT, CLEAN_RECONCILED | M1 | survey (R1) |
| 4 | Structured Return Dictionary | `seed_random_drifts` returns structured dict mapping archetype names to seeded record details | M1 | survey (R1) |
| 5 | CLI Random Flag | `python data/db_setup.py --random` triggers `seed_random_drifts()` | M1 | survey (R1) |
| 6 | Fast Invoice Cache Lookup | `PlaywrightInvoiceGenerator.render_invoice` returns cached file path in <5ms if `.cache/invoices/{student_id}.png` exists | M2 | survey (R2) |
| 7 | On-the-fly Chromium Rendering | Uncached invoices rendered via headless Chromium to `.cache/invoices/{student_id}.png` (600x400) in <500ms with uppercase placeholder substitution | M2 | survey (R2) |
| 8 | Instant Fallback Receipt | Instant (<15ms) SVG/PNG fallback receipt generator using Pillow if Chromium fails to spawn, preventing broken Streamlit previews | M2 | survey (R2) |
| 9 | Template Placeholder Alignment | `fixtures/invoice_template.html` supports both uppercase `{{STUDENT_NAME}}` tokens and lowercase tokens with 600x400 viewport styling | M2 | survey (R2) |
| 10 | Graph DB Path Scoping | `core/schemas.py` and `core/agent_graph.py` accept and propagate `db_path` through `act_node`, `execute_fix_node`, and SQL tools | M3 | survey (R4) |
| 11 | Relational Drift Invariants | `core/engine.py` reconciles arbitrary student IDs, transaction IDs, emails, or amounts without hardcoded strings | M3 | survey (R4) |
| 12 | Deterministic Baseline Invariant | `tests/test_benchmark.py` passes 11/11 baseline tests (`TC01`–`TC10` + `IDEM`) | M3 | survey (R4) |
| 13 | Dynamic Chaos Test TC12 | `test_tc12_dynamic_randomized_reconciliation` in `tests/test_benchmark.py` tests unseen dynamic seed, ground-truth invariants, refusal guardrails | M3 | survey (R4) |
| 14 | 12/12 Pytest Pass | Full test suite passes 12/12 with exit code 0 | M3 | survey (R4) |
| 15 | Sidebar Mode Controls & Badge | High-visibility mode badge (`[STANDARD BENCHMARK]` vs `[DYNAMIC CHAOS MODE]`) and reset/chaos trigger buttons | M4 | survey (R3) |
| 16 | Tab 1 Tri-Source Matrix Metrics | Displays live rows from `data/reconcile.db`, total row counts, and drift breakdown metric pills | M4 | survey (R3) |
| 17 | Tab 2 Dynamic HITL & Guardrails | Renders dynamic proposals, enforces Refusal Guardrail on `ESCALATE_REFUSAL_FRAUD`, renders on-demand invoice previews | M4 | survey (R3) |
| 18 | Tab 2 Generalized Reminder Suppression | Preserves green `Status: Reminder SUPPRESSED` badge upon approving any `PAID_BUT_NOT_ACTIVATED` case dynamically without hardcoding `STU-001` | M4 | survey (R3) |
| 19 | Tab 3 Live Cryptographic Audit Ledger | Displays real-time cryptographic audit trail hashes (`idempotency_hash` and `state_seal_hash`) | M4 | survey (R3) |
| 20 | Zero Placeholders & Invariants | Complete production-grade code, zero `# TODO` or stub functions | M5 | survey (R5) |
| 21 | Sequential Verification & Delivery | Execute sequential verification commands, git commit, and git push | M5 | survey (R5) |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Dynamic Data Generator | `data/db_setup.py` (R1) | none | IN_PROGRESS |
| M2 | Dynamic Playwright Invoicing & Fallback | `tools/ops_tools.py`, `fixtures/invoice_template.html` (R2) | none | PLANNED |
| M3 | Generalized Reconciliation & Test TC12 | `core/schemas.py`, `core/agent_graph.py`, `core/engine.py`, `tests/test_benchmark.py` (R4) | M1 | PLANNED |
| M4 | Dual-Mode Streamlit UI & HITL Controls | `app.py` (R3) | M1, M2, M3 | PLANNED |
| M5 | Final Verification & Git Release | End-to-end verification, git commit, git push (R5) | M1, M2, M3, M4 | PLANNED |

## Interface Contracts
### `data/db_setup.py` -> Callers (`app.py`, `tests/test_benchmark.py`)
- `init_db(db_path: Union[str, Path] = DEFAULT_DB_PATH, prewarm: bool = True) -> str`: Preserves TC01–TC10 seed, returns db_path string.
- `seed_random_drifts(db_path: Union[str, Path] = DEFAULT_DB_PATH, num_clean: int = 3, num_drifts: int = 6, seed: Optional[int] = None) -> Dict[str, Any]`:
  - Returns dict with keys: `{"students": List[Dict], "drifts": Dict[str, Dict], "archetype_counts": Dict[str, int], "summary": Dict}`.
- CLI: `python data/db_setup.py` runs `init_db()`. `python data/db_setup.py --random` runs `seed_random_drifts()`.

### `tools/ops_tools.py` -> Callers (`app.py`, `data/db_setup.py`)
- `PlaywrightInvoiceGenerator.render_invoice(student_meta: Dict[str, Any], db_path: Optional[Union[str, Path]] = None) -> str`:
  - Returns absolute file path string to `.cache/invoices/{student_id}.png`.
  - Cache hit: returns in <5ms.
  - Cache miss: renders 600x400 screenshot via Chromium in <500ms; on exception, produces clean 600x400 Pillow fallback receipt in <15ms.
- `generate_invoice_screenshot(student_meta: Dict[str, Any]) -> str`: Backward-compatible alias.

### `core/agent_graph.py` -> Callers (`tests/test_benchmark.py`, `app.py`)
- `create_reconcile_graph()`: State graph accepting `state["db_path"]` to query the designated SQLite database.
- State schema `ReconcileAgentState`: includes `db_path: Optional[str]`.

## Code Layout
- `data/db_setup.py`: Database schema definition, baseline TC01–TC10 seeding, dynamic chaos generator, pre-warming logic, CLI parser.
- `fixtures/invoice_template.html`: Invoice template supporting uppercase placeholders and 600x400 viewport styling.
- `tools/ops_tools.py`: `PlaywrightInvoiceGenerator` class with caching, warm browser pool, and instant SVG/PNG fallback receipt renderer.
- `core/schemas.py`: `ReconcileAgentState` typed dict with `db_path`.
- `core/agent_graph.py`: State graph workflow propagating `db_path` to all tools and nodes.
- `core/engine.py`: Generalized relational invariant drift detection.
- `app.py`: Streamlit application with sidebar mode controls, Tab 1 live matrices & drift metric pills, Tab 2 HITL center with dynamic invoice preview and generalized reminder suppression badge, Tab 3 real-time audit hashes.
- `tests/test_benchmark.py`: Baseline tests TC01–TC10, idempotency test, and `test_tc12_dynamic_randomized_reconciliation`.
