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
Transit is **limited to direct parent-child relationships only**. Grandchild transfers must process through intermediate steps, preventing complex cascading transfers that are difficult to audit.

### 2. Virtual Transit Warehouse System

```
Company (Parent)
└── Transit Warehouse: {CompanyName}.TRANSIT  (company_id = parent)
        └── View Location: {CompanyName}.TRANSIT  (company_id = NULL)
                └── Stock Location: Stock  (type=transit, company_id=NULL)
```

Transit stock locations use `company_id=NULL` for cross-company access per Odoo's multi-company rules, while the warehouse retains the parent's `company_id` for accurate stock reporting. The transit location acts as a virtual waypoint (like a truck in transit), not a physical one.

### 3. Two-Phase Picking System

Each transit is two paired pickings treated as one logical operation:

```
Mother A → Child A1:

┌──────────┐    OUT    ┌──────────┐    IN     ┌──────────┐
│  A Stock │ ────────> │ A TRANSIT│ ────────> │ A1 Stock │
└──────────┘           └──────────┘           └──────────┘

Multi-hop (A → A1 → A1a) requires two separate transit orders:
  Step 1: A → A1  (using A's transit warehouse)
  Step 2: A1 → A1a  (using A1's transit warehouse)
```

- **Source (OUT) picking**: validates actual products, quantities, lots/serials at departure
- **Destination (IN) picking**: automatically receives exactly what source validated—no manual intervention
- **Mismatch detection**: on destination validation, the system flags any tampering with product, lot/serial, or quantity

### 4. Backorder Support

```
Original order: 100 units

After partial source validation:
├── Main picking:      80 units validated → destination synced immediately
└── Backorder picking: 20 units pending  → new source+destination pair created automatically
```

Each backorder pair maintains full traceability; no data loss between main and backorder quantities.

### 5. Automated Setup

Automation rules (using Odoo's base automation framework) handle infrastructure lifecycle:

| Trigger | Action |
|---|---|
| Child company added to parent | Create transit warehouse for parent |
| Company name changes | Update transit warehouse name and sequences |
| New warehouse created | Generate transit picking types for the company structure |
| Source picking validated | Propagate moves to destination picking |
| Source backorder created and ready | Create and sync corresponding destination backorder |
| Destination picking validated | Update transit order status; mark done if all pickings complete |

On module installation, a post-install hook scans all existing parent-child relationships and creates transit infrastructure retroactively.

---

## Four-Level Engine Architecture

| Level | Model | Responsibility |
|---|---|---|
| 1 | `t4tek.transit.order` | Main order definition and orchestration |
| 2 | `t4tek.transit.order.line` | Line items: product, quantity, UOM |
| 3 | `t4tek.transit.picking.type` | Picking operation behavior and location mappings |
| 4 | `t4tek.transit.picking` | Maps and synchronizes source/destination picking pairs |

---

## State Flow

```
DRAFT
  │ action_confirm()
  ▼
ASSIGNED  ◄─── (onchange: companies / moves)
  │ Source picking validated
  ▼
IN_PROGRESS
  │ Destination picking validated
  ▼
DONE

Cancel:  DRAFT/ASSIGNED → CANCEL (action_cancel())
Reopen:  CANCEL → ASSIGNED (action_confirm() after edits)
Delete:  DRAFT/CANCEL only (via unlink())
```

---

## Technical Components

### Core Models

**`T4tekTransitOrder`** — Main transit order
- Key fields: `company_id`, `src_company_id`, `dest_company_id`, `transit_location_id`, `t4tek_src_transit_picking_type_id`, `t4tek_dest_transit_picking_type_id`

**`T4tekTransitOrderLine`** — Transfer line items
- Fields: product, quantity, UOM

**`T4tekTransitPicking`** — Source/destination picking pair mapping
- Handles move-line synchronization and backorder linking

**`T4tekTransitPickingType`** — Picking operation configuration
- Fields: sequence, location mapping, operation constraints

**`ResCompany` (extended)** — New methods:
- `_get_transit_location()`: retrieves company's transit location
- `_create_transit_warehouse()`: creates complete transit infrastructure
- `_create_warehouse_transit_picking_types()`: generates picking types for warehouses
- `_archive_transit_warehouse_defaults()`: cleans up Odoo auto-generated defaults

### Design Principles
- Transit locations use `company_id=NULL`; warehouses retain company ownership
- All models extend, never modify, core Odoo models
- `ir.model.access` + `ir.rule` for role-based security and company data isolation
- `mail.thread` integration for notifications and activity logging

---

## Usage Workflow

1. **Create Transit Order**: Select source company, destination company, add product lines
2. **Confirm**: System creates the source (OUT) and destination (IN) picking pair
3. **Validate Source**: Warehouse staff validates source picking → system auto-syncs moves to destination and updates order to "In Progress"
4. **Validate Destination**: Receiving warehouse validates → system marks order "Done" and creates audit record
5. **Backorders** (if applicable): Undelivered quantities automatically generate a new picking pair linked to the original order; process resumes from step 3

---

## Configuration

**Adding a child company triggers full automation:**
```python
company_a.write({'child_ids': [(4, company_b.id)]})
# → Transit warehouse created for Company A (if not exists)
# → Picking types created for all warehouses in both companies
# → Sequences configured
```

**Multiple transit routes**: Create additional `t4tek.transit.picking.type` records mapped to different warehouse + transit location combinations. System selects the appropriate type based on the companies involved.

---

## Performance & Compatibility

| Metric | Value |
|---|---|
| Transit order + pickings creation | < 100ms |
| Source → Destination sync | < 50ms |
| DB queries per operation | 3–5 |
| Storage per transit record | ~2KB |

**Integrates with**: Stock, Stock Accounting, Base Automation
**Does not modify**: Core stock.picking, warehouse logic, or company model

---

## Troubleshooting

| Issue | Cause | Solution |
|---|---|---|
| Transit location not found | Parent has no children or warehouse not created | Add child company relationship; warehouse auto-creates |
| Backorder quantities incorrect | Stock reserved at source but not released | Verify source picking validation; check availability |
| Picking types not generated | Warehouse created before inter-company relationship | Manually run `_create_warehouse_transit_picking_types()` |

---

## Future Considerations
- Support for grandchild direct transfers (with automated intermediate step creation)
- Real-time transit tracking dashboard
- Integration with landed costs for cross-border transfers

---

**Version**: 1.0.0 | **License**: Proprietary | **Author**: Nguyen Cao Hoang | **Status**: Active development

© 2026 Nguyen Cao Hoang. All rights reserved.