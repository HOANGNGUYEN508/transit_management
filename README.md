# Inter-Transit Module

## Overview

The Inter-Transit Module automates and secures inter-company stock transfers between parent and child companies in Odoo. It solves the data integrity and operational challenges that arise when managing inventory in multi-company environments.

---

## Problem Statement

Standard Odoo inter-company transfers require manual handling with no enforcement of organizational hierarchy:
- **Human Error**: No built-in automation for parent-child relationships; high risk of incorrect quantities and missing records
- **Stock Disappearance**: Inventory vanishes in transit between companies that lack formal hierarchical relationships, breaking logical transit workflows
- **Scope Ambiguity**: No mechanism to restrict transit to related companies only, risking broken transfers between unrelated entities

---

## Solution Architecture

### 1. Hierarchical Transit Enforcement

Transit is **limited to direct parent-child relationships only**, enforced via Lowest Common Ancestor (LCA) computation. The ordering company must exactly equal the LCA of the source and destination companies — no more, no less. Grandchild transfers must be split manually into intermediate steps, keeping each hop auditable and atomic.

The three valid cases are:

| Case | Ordering | Source | Destination |
|---|---|---|---|
| Child A → Child B | Parent | Child A | Child B |
| Self → Child | Parent | Parent | Child |
| Child → Self | Parent | Child | Parent |

### 2. Virtual Transit Warehouse System

```
Company (Parent)
└── Transit Warehouse: {CompanyName}.TRANSIT  (company_id = parent)
        └── View Location: {CompanyName}.TRANSIT  (company_id = NULL)
                └── Stock Location: Stock  (type=transit, company_id=NULL)
```

Transit stock locations use `company_id=NULL` for cross-company access per Odoo's multi-company rules, while the warehouse retains the parent's `company_id` for accurate stock reporting. The transit location acts as a virtual waypoint — goods at this location are "in transit", not physically present at any company site.

### 3. Two-Phase Picking System

Each transit order produces exactly **one pair of pickings per route**, treated as a single logical operation:

```
Mother A → Child A1:

┌──────────┐    OUT    ┌──────────┐    IN     ┌──────────┐
│  A Stock │ ────────> │ A TRANSIT│ ────────> │ A1 Stock │
└──────────┘           └──────────┘           └──────────┘

Multi-hop (A → A1 → A1a) requires two separate transit orders:
  Step 1: A → A1  (using A's transit warehouse)
  Step 2: A1 → A1a  (using A1's transit warehouse)
```

- **Source (OUT) picking**: warehouse staff validates actual products, quantities, and lot/serial numbers at departure
- **Destination (IN) picking**: automatically pre-populated with exactly what the source validated — no manual re-entry
- **Mismatch detection**: when the destination is validated, the system compares product presence, lot/serial sets, and quantities at three priority levels, flagging any discrepancy

#### Product Matching Strategy

Moves between src and dest pickings are matched by `product_id`. No persistent link field on `stock.move` is required, making the transit resilient to move deletion and recreation. Duplicate-product lines within the same order are automatically merged (with quantity summed in the base UOM) before picking creation, so each picking always contains at most one move per product.

### 4. Backorder Support

Backorders can be initiated from **either the source or destination side**:

```
Original order: 100 units

After partial SOURCE validation:
├── Main src picking:      80 units validated  → dest synced immediately (Rule 4+5)
└── Src backorder:         20 units pending    → matching dest backorder created automatically (Rule 5)

After partial DESTINATION validation:
├── Main dest picking:     80 units received   → transit quantities updated (Rule 7)
└── Dest backorder:        20 units pending    → new src picking created; full transit pair formed (Rule 6)
```

Each backorder pair maintains full traceability through both the backorder chain (`backorder_id`) and the transit picking tree (`transit.picking`).

### 5. Delegation Feature

A parent company can delegate the **source (OUT) picking** of a transit order to a child company for execution. Delegation comes in two forms:

- **Whole break**: the entire picking is reassigned to the child company. If the picking is itself a backorder, the backorder chain link is severed before reassignment to satisfy Odoo's `check_company()` constraint, and the transit pair is promoted into the delegation chain instead.
- **Partial break**: specified quantities per move are split off into a new child picking + a new destination picking. The remaining quantities stay on the original picking. The two-step JS confirmation dialog (pre-check → `action_confirm()` → execute → `action_execute()`) prevents stale wizard data from being committed.

Delegation to the destination company of the transit order is explicitly blocked to prevent circular routing. Delegating the destination (IN) picking is not supported — the receiving company must confirm receipt.

### 6. Lot / Serial Number Cross-Company Sharing

When the source picking is validated and its move lines are propagated to the destination picking, lots are resolved cross-company via `_batch_find_or_create_lots()`. Existing lots belonging to a specific company are converted to `company_id=False` (shared) before being referenced on the destination picking. Missing lots are created as cross-company from the start.

### 7. In-Transit Quantity Reporting

`product.product` is extended with a computed `transit_qty` field that surfaces in Odoo's inventory quantity views:

- **Active transit qty**: goods whose source picking is done but destination picking is still pending (reserved at the transit location by the pending dest move).
- **Stuck transit qty** (all-warehouse view only): unreserved quants at transit locations — goods stranded after a mismatch where the destination picking was validated with fewer units than the source shipped. Stuck qty is also subtracted from `free_qty` so the product is not reported as freely available.

Transit warehouse scopes are automatically excluded from per-warehouse/location views.

### 8. Automated Setup

Automation rules (using Odoo's `base.automation` framework) handle the full infrastructure lifecycle. **12 rules total** — 3 for setup, 9 for the transit flow:

| # | Model | Trigger | Action |
|---|---|---|---|
| 1 | `res.company` | `child_ids` changes (gains first child) | Create transit warehouse for parent company |
| 2 | `res.company` | `name` changes (company has children) | Update transit warehouse name, view location name, and all sequence prefixes |
| 3 | `stock.warehouse` | New normal warehouse created | Generate transit picking type pairs for the company's full ancestor chain |
| 4 | `stock.picking` | `date_done` set (any picking) | SRC done: sync moves → dest; propagate move lines; advance transit order to `in_progress`; create dest backorders for any src backorders |
| 5 | `stock.picking` | State → `assigned`/`confirmed`/`waiting` + `backorder_id` set (outgoing) | SRC backorder created: create matching dest backorder + new transit pair; sync moves |
| 6 | `stock.picking` | State → `assigned`/`confirmed`/`waiting` + `backorder_id` set (incoming) | DEST backorder created: create new src picking + full transit pair; mirror dest moves onto new src |
| 7 | `stock.picking` | `date_done` set (incoming only) | DEST done: populate transit order line quantities from dest actuals; finalize order state (done/cancel) when all pairs settle |
| 8 | `stock.picking` | State → `cancel` | Cascade cancel: block direct cancellation of transit pickings; cascade src-cancel to dest; update order state |
| 9 | `stock.picking` | `on_unlink` | Picking deleted: block deletion of full-pair dest pickings; block src deletion when dest is done; clean up orphaned transit pair records |
| 10 | `stock.picking` | State → `assigned`/`confirmed`/`waiting` (incoming) | **Guard**: block dest picking from being marked ready while src is not done (unless src is a Rule 6 draft placeholder) |
| 11 | `stock.picking` | `date_done` set (incoming only) | **Guard**: block dest picking validation while src is not done |
| 12 | `stock.picking` | State → `assigned`/`confirmed`/`waiting` + `backorder_id` set (incoming) | **Guard**: revert dest backorder to draft if its own transit pair's src is still draft (race condition between Rule 6 and Rule 10) |

On module installation, a post-install hook scans all existing parent-child company relationships and creates transit infrastructure retroactively.

### 9. Protection System

All transit-managed records are write/unlink protected to prevent accidental or unauthorised modification:

| Protected Model | Protected Fields / Operations |
|---|---|
| `base.automation` | write, unlink (all 12 transit automation rules) |
| `ir.actions.server` | write, unlink (all 12 paired server actions) |
| `ir.sequence` | `active`, `code`, `company_id`, `name`; unlink — for transit order and transit picking sequences |
| `stock.warehouse` | `lot_stock_id`, `view_location_id`, `company_id`, `active`; unlink — for transit warehouses |
| `stock.location` | `location_id`, `usage`, `company_id`, `active`; unlink — for locations inside transit warehouses |
| `stock.picking.type` | `default_location_src_id`, `default_location_dest_id`, `code`, `warehouse_id`, `company_id`, `active`; unlink — for transit-managed operation types |

Each protection can be bypassed via a specific context key (e.g. `skip_stock_warehouse_write_protection=True`) for internal system operations.

---

## Four-Level Engine Architecture

| Level | Model | Responsibility |
|---|---|---|
| 1 | `transit.order` | Main order definition, validation, orchestration, state management |
| 2 | `transit.order.line` | User-defined line items: product, quantity, UOM |
| 3 | `transit.picking.type` | Config record: maps (warehouse, company, transit location) → OUT/IN picking type pair |
| 4 | `transit.picking` | Maps and synchronises src/dest picking pairs; handles all automation rule callbacks |

---

## Work Flow

```
**TRANSIT ORDER** (transit.order):
    ┌─────────────┐  [action_confirm]  ┌──────────────┐  [1st src _action_done] ┌──────────────┐ [last dest _action_done]┌──────────────┐
    │    draft    │ ─────────────────> │   assigned   │ ──────────────────────> │  in_progress │ ──────────────────────> │     done     │
    └─────┬───────┘                    └──────┬───────┘                         └───┬──────────┘                         └──────────────┘
          |           [action_cancel]         |                                     |   ↑                                         ↑
          └─────────────────┬─────────────────┘                                     |   | ≥1 transit picking(s) not done/cancel   │ all transit picking(s)
                            |                                                       |   └─────────────────────────────────────────◇     done/cancel
                     ┌──────┴──────┐                                                |                                             │
                     │    cancel   │                                                └─────────────────────────────────────────────┘
                     └─────────────┘                                                                   [action_stop]

**TRANSIT PICKING** (transit.picking): 
- state mirror transit order, considered done when both sides are done, considered cancelled when both sides are cancelled.
- can execute action_cancel/action_stop independently from the transit order, dictated by the state of transit order 
and the scope is for the transit picking itself (not the entire transit).
                     
**STOCK PICKING** (stock.picking):
- outgoing (src side):
       ┌─────────────────────────────────────────────────────────────────────────────────────┐
       |                                                                                     |
       |                    whole              [delegate]            partial                 |
       |   ┌───────────────────────────────────────◇────────────────────────────────────────┴────────────────────────────────────────┐
       |   |     (only if no partial before)       ↑                                                                                  |
       |   |                                       |                                                                                  |
       |   |   ┌───────────────────────────────────┤                                                                                  |
       ↓   ↓   |                                   |                                                                                  |
    ┌──────────┴──┐     [action_confirm]    ┌──────┴───────┐     [_action_done]      ┌──────────────┐   [_create_backorder]           ↓
    │    draft    │ ──────────────────────> │   assigned   │ ──────────────────────> │     done     │ ──────────────────────> New transit route
    └─────┬───────┘                         └──────┬───────┘                         └──────────────┘                         
          |    ┌transit.picking/transit.order┐     |    
          |    └  action_cancel/action_stop  ┘     |      - Delegation:
          └───────────────────┬────────────────────┘      + Whole: transfer the responsibility of the entire picking to the direct child company.
                              |                           + Partial: transfer the responsibility of part of the picking by creating a new route,
                       ┌──────┴──────┐                    by creating a new picking and moves for the delegated lines in the child company,
                       │    cancel   │                    and link it to the original picking, then set the state of the original picking to 'draft'.
                       └─────────────┘
                       
- incoming (dest side):
    ┌─────────────┐   [propgate from src]   ┌──────────────┐     [_action_done]      ┌──────────────┐   [_create_backorder]
    │    draft    │ ──────────────────────> │   assigned   │ ──────────────────────> │     done     │ ──────────────────────> New transit route
    └─────┬───────┘                         └──────┬───────┘                         └──────────────┘                         
          |    ┌transit.picking/transit.order┐     |
          |    └  action_cancel/action_stop  ┘     |
          └───────────────────┬────────────────────┘
                              |
                       ┌──────┴──────┐
                       │    cancel   │
                       └─────────────┘

**Note**:
- action_confirm: Mark as todo
- action_cancel: Cancel
- action_stop: Stop
- delegate: Delegation Wizard
- _create_backorder: Validate with backorder
- New transit route: a transit.picking record with both source and destination picking
---

## Technical Components

### Core Models

**`TransitOrder`** (`transit.order`) — Main transit order

Key fields:
- `company_id` — ordering company (must be the LCA of src and dest)
- `src_company_id`, `dest_company_id` — source and destination companies
- `transit_location_id` — resolved and written by `action_confirm()` from the LCA's transit warehouse `lot_stock_id`
- `transit_picking_ids` — one-to-many of transit picking pairs (grows with backorders)
- `line_ids` — transit order lines
- `state` — `draft / assigned / in_progress / done / cancel`
- `is_reviewed` — set manually when a quantity/lot mismatch is intentional; clears the danger decoration
- `has_mismatch` — computed; true if any non-cancelled pair has a move-level mismatch, or if the order is done but some pairs were cancelled (structural mismatch)
- `is_fully_stopped` — computed; true when all src pickings are done or cancelled (nothing left for `action_stop()`)
- `is_late`, `is_today`, `is_very_late` — scheduling warning flags (very late threshold: 3 days)

Key methods:
- `_compute_lca()` (on `res.company`) — walks ancestor chains of both companies to find their Lowest Common Ancestor
- `_validate_transit_authorization()` — enforces the direct-parent-only rule
- `_validate_and_get_companies()` — resolves transit location from LCA's transit warehouse; looks up `transit.picking.type` configs for both sides
- `_merge_duplicate_lines()` — merges same-product lines before confirm; normalises to base UOM
- `_create_transfer_pickings()` — batch creates OUT + IN pickings
- `_create_moves_for_transit()` — creates stock moves on both pickings
- `_sync_lines_to_pickings()` — syncs line changes to existing moves on assigned-state transits
- `_sync_scheduled_date_to_pickings()` — pushes date changes to all non-terminal pickings
- `_warn_pickings_of_change()` — posts a chatter warning and sets `needs_review=True` on src pickings when lines are modified post-confirmation

---

**`TransitOrderLine`** (`transit.order.line`) — Transfer line items

- Fields: `product_id` (consumable only), `product_uom_qty`, `product_uom`, `quantity` (actual, populated after reception), `name`, `note`
- State guard (`_check_order_state()`): hard-blocks writes/unlinks on done/cancel transits; returns soft-warn transits in `assigned` state so callers can post a review warning
- UOM validation: unit of measure must belong to the same category as the product's UOM

---

**`TransitPickingType`** (`transit.picking.type`) — Picking operation configuration

- Identity key: `(warehouse_id, company_id, transit_location_id)` — unique constraint enforced in SQL
- `transit_company_id` — computed from `transit_location_id` by resolving which transit warehouse owns that location
- `src_picking_type_id` — outgoing operation type: `warehouse.Stock → transit_location`
- `dest_picking_type_id` — incoming operation type: `transit_location → warehouse.Stock`
- Constraint `_check_transit_location_is_ancestor_or_self()`: transit location must belong to an ancestor company or own company (cross-tree configs rejected)
- Constraint `_check_picking_types_consistency()`: both operation types must belong to the declared warehouse and company, with correct location symmetry

---

**`TransitPicking`** (`transit.picking`) — Source/destination picking pair mapping

Key fields:
- `src_picking_id`, `dest_picking_id` — the paired OUT/IN pickings
- `src_picking_state`, `dest_picking_state` — stored related fields for fast domain filtering
- `delegation_id` / `delegation_ids` — stored delegation chain links
- `backorder_parent_id` / `backorder_child_ids` — computed from `src_picking_id.backorder_id` (or `dest_picking_id.backorder_id` for dest-only pairs)
- `comparison_data` — computed JSON payload consumed by the frontend detail widget; contains per-product line status, src/dest quantities, and lot-level breakdown
- `has_mismatch` — true when any line status is not `ok` or `pending`
- `cancel` — technical boolean set during `_do_cancel()` to allow Rule 8 to distinguish system-initiated cancellations from direct user cancellations

Key automation callbacks (called by `ir.actions.server`):
- `automation_handle_src_picking_done()` — Rules 4+5: sync moves, propagate lines, advance order, create backorders
- `automation_handle_dest_picking_done()` — Rule 7: populate quantities, finalize order
- `automation_handle_src_backorder_created()` — Rule 5: create dest backorder and new transit pair
- `automation_handle_dest_backorder_created()` — Rule 6: create new src picking and full transit pair
- `automation_handle_picking_cancelled()` — Rule 8: cascade cancel logic with guards
- `automation_handle_picking_deleted()` — Rule 9: cleanup with guards
- `automation_handle_dest_picking_advance_guard()` — Rules 10/11: block premature dest advancement
- `automation_handle_dest_backorder_init_revert()` — Rule 12: revert dest backorder to draft

Mismatch detection (`_compute_line_status()`) uses a three-level priority check:
1. **Product presence**: missing / src_only / dest_only / pending
2. **Lot/SN integrity**: lot sets must match exactly → `lot_mismatch`
3. **Quantity accuracy**: per-lot qty and overall total must match → `qty_mismatch`

---

**`TransitPickingDelegation`** (`transit.picking.delegation`) — Delegation wizard (TransientModel)

- Two-phase execute: `action_confirm()` returns a pre-check result (message + resolved break type) consumed by the frontend JS dialog; `action_execute()` performs full re-validation and the actual delegation at the moment the user clicks Confirm
- `_do_whole_break()`: reassigns the src picking's `company_id` and `picking_type_id` to the child company; severs backorder chain links if the src is itself a backorder, then promotes the transit pair into the delegation chain
- `_do_partial_break()`: creates new child src + new dest pickings; redistributes moves between original and child pickings based on wizard line quantities; creates a new `transit.picking` record with `delegation_id` set

---

**`ResCompany`** (extended) — New methods:

- `_compute_lca(company_a, company_b)` — static; walks ancestor chains to find the LCA; returns empty recordset if companies are in different trees
- `_get_all_descendants()` — BFS traversal returning the full subtree (excluding self); used to propagate new transit configs down to all existing descendants
- `_create_transit_warehouse()` — creates view location (company_id=NULL), transit stock location (type=transit, company_id=NULL), transit warehouse, transit order sequence, then archives Odoo's auto-generated default picking types
- `_get_transit_targets_for_company(company)` — returns ordered list of `{transit_location, transit_company, transit_wh_code}` dicts: ancestor transit locations (nearest-first), then own transit location if company has children
- `_create_warehouse_transit_picking_types(warehouse=None)` — for each normal warehouse × each transit target, creates OUT/IN picking type pair and `transit.picking.type` config record; idempotent
- `_find_or_create_transit_picking_type(...)` — creates (or returns existing) picking type + sequence for a given warehouse/location combination
- `_update_transit_sequences_for_warehouse(company, warehouse, warehouse_code)` — updates OUT/IN sequence prefixes when company or warehouse is renamed
- `automation_handle_company_name_change()` — Rule 2 callback: updates transit warehouse name, view location name, transit order sequence prefix, and all picking type sequences
- `_archive_transit_warehouse_defaults()` — archives the default picking types and sequences auto-created by Odoo when a new warehouse is created

---

**`StockPicking`** (extended) — New fields:

- `needs_review` — set to `True` when the linked transit order is modified after confirmation (lines changed, date changed). Cleared via `action_dismiss_review()` or automatically when the picking is validated by a transit automation rule.
- `date_changed` — set to `True` when the picking's scheduled date is changed post-confirmation
- `transit_order_id` — computed many2one to the transit order this picking belongs to
- `delegation_id_display` / `delegation_ids_display` — computed HTML banners for delegation chain display
- `company_has_children` — computed; used to control visibility of the Delegate button

---

**`ProductProduct`** (extended) — New field:

- `transit_qty` — computed float; active transit qty (src done, dest pending) + stuck transit qty (unreserved quants at transit locations in all-warehouse view). Stuck qty is also subtracted from `free_qty`.

---

### Design Principles

- Transit locations use `company_id=NULL`; warehouses retain company ownership
- All transit infrastructure extends, never modifies, core Odoo models
- Move matching is product-based (no persistent link on `stock.move`), making the transit resilient to move deletion/recreation
- `ir.model.access` + `ir.rule` for role-based security and company data isolation
- `mail.thread` integration on `transit.order` for chatter notifications on all state changes, modifications, and delegation events
- Lot/serial numbers are automatically shared cross-company (`company_id=False`) when propagating from src to dest pickings
- All automation callbacks are batch-aware: they operate on recordsets, not individual records

---

## Frontend Components (OWL / Odoo 18)

| Component | Type | Purpose |
|---|---|---|
| `TransitPickingCancelButton` | View widget | Custom cancel button on the transit picking form; calls `action_cancel()` server-side and reloads the view |
| `TransitPickingStopButton` | View widget | Custom stop button on the transit order form; calls `action_stop()` and reloads |
| `TransitPickingDelegationButton` | View widget | Two-step delegation confirm: saves wizard → calls `action_confirm()` for a pre-check message → shows `ConfirmationDialog` → calls `action_execute()` with the resolved break type |
| `TransitPickingDetail` | Field widget (`transit_picking_detail`) | Renders the src/dest comparison table inline on the transit picking form; per-product status badges (ok / pending / qty_mismatch / lot_mismatch / missing / src_only / dest_only); row highlighting; mismatch summary with per-lot detail |
| `TransitLotDialog` | Dialog (used by detail widget) | Lot/serial detail popup; zips src and dest lot rows side-by-side; highlights mismatched lots |

All custom buttons evaluate `invisible` and `readonly` expressions against the OWL record's `evalContext` using `evaluateBooleanExpr`.

---

## Usage Workflow

1. **Create Transit Order**: Select ordering company, source company, destination company, and add product lines (consumable products only)
2. **Confirm**: System merges duplicate-product lines, validates the company hierarchy (LCA check), creates the OUT and IN picking pair with appropriate operation types, and creates stock moves on both pickings. Order moves to `Assigned`.
3. **Optional Delegation**: From the source picking form, click Delegate to assign the outgoing transfer (whole or partial) to a child company. The transit pair is updated with delegation chain links.
4. **Validate Source**: Warehouse staff validates the OUT picking with actual quantities and lot/serial numbers. System automatically syncs src move lines to dest, writes unit cost from stock valuation layers to dest moves, advances the transit order to `In Progress`, and creates backorder pairs if needed.
5. **Validate Destination**: Receiving warehouse validates the IN picking. The comparison widget shows per-product and per-lot status. System writes actual quantities back to transit order lines. When all pairs settle, the order moves to `Done` (or `Cancel` if all pairs were cancelled).
6. **Mismatch Acknowledgement**: If the order is done with a mismatch flag, the responsible user clicks Acknowledge Mismatch (`action_review()`) to set `is_reviewed=True`, clearing the danger decoration.
7. **Backorders** (if applicable): Undelivered quantities automatically generate a new picking pair from either side. The process resumes from step 4.

---

## Configuration

**Adding a child company triggers full automation:**

```python
company_a.write({'child_ids': [(4, company_b.id)]})
# → Transit warehouse created for Company A (if not exists)
# → Picking types created for all normal warehouses in both companies
# → Transit sequences configured
```

**Adding a new warehouse triggers automatic config generation:**

```python
# Rule 3 fires on warehouse creation
# → OUT/IN picking types created for all transit targets of the warehouse's company
# → transit.picking.type config records created
```

**Picking type config lookup key:**
The system always resolves the correct picking type pair by searching `transit.picking.type` for `(company_id=X, transit_location_id=LCA.transit_warehouse.lot_stock_id)`. Each warehouse can have multiple config records — one per transit location it routes through (own transit + all ancestor transits).

**Multiple transit routes:** If a company has multiple warehouses, each warehouse gets its own OUT/IN picking type pair per transit target. The transit order uses the first config (by `create_date ASC`) for each side when there are multiple matching records.

---

## Sequence Naming

| Sequence | Code | Prefix format |
|---|---|---|
| Transit order | `transit.order` | `{CompanyName}/TRANSIT/` |
| Transit picking (OUT) | `transit.picking` | `{WH_CODE}/TRANSIT/OUT/` |
| Transit picking (IN) | `transit.picking` | `{WH_CODE}/TRANSIT/IN/` |

When a company is renamed, Rule 2 automatically updates the transit order sequence prefix and all picking type sequence prefixes for that company's warehouses.

---

## Troubleshooting

| Issue | Cause | Solution |
|---|---|---|
| "No transit warehouse" error on confirm | LCA company has no transit warehouse (child was added before module install, or post-install hook failed) | Add/re-add the child company relationship; Rule 1 will create the warehouse automatically. Or call `company._create_transit_warehouse()` directly. |
| "No transit picking type configuration" error on confirm | Warehouse exists but transit picking types were not generated for it | Call `company._create_warehouse_transit_picking_types()` manually, or trigger Rule 3 by touching the warehouse record. |
| Dest picking blocked from being marked ready | Rule 10 guard: src picking not yet done | Validate the source picking first. If the src picking is a Rule 6 draft placeholder (backorder_id set, state=draft), the guard allows readying the dest. |
| Backorder quantities out of sync | Race condition between Rule 5 and Rule 6 | Rule 12 reverts the dest backorder to draft until its own transit pair's src is confirmed. Wait for Rule 6 to create the src, then process normally. |
| `transit_qty` shows unexpected values | Stuck quants at transit location (mismatch during dest validation left goods unreserved) | Investigate the transit order for mismatch. Manually adjust or create a corrective transit order. |
| Cannot cancel picking directly | Rule 8 blocks direct cancellation of transit-managed pickings | Use the Cancel button on the transit picking form, or cancel the transit order. |
| Delegation blocked: "already split off child shipments" | Whole-break attempted on a transit pair that already has partial delegations | Use Partial delegation and enter only the quantities not yet delegated. |

---

## Future Considerations

- Support for grandchild direct transfers with automated intermediate step creation
- Real-time transit tracking dashboard
- Integration with landed costs for cross-border transfers

---

**Version**: 1.2.0 | **License**: Proprietary | **Author**: Nguyen Cao Hoang | **Status**: Active development

© 2026 Nguyen Cao Hoang. All rights reserved.