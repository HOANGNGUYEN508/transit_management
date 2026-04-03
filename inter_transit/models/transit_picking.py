from odoo import models, fields, api # type: ignore
from odoo.exceptions import UserError, ValidationError # type: ignore
from odoo.tools import float_compare, float_is_zero # type: ignore
from markupsafe import Markup, escape # type: ignore
from collections import defaultdict
import logging

_logger = logging.getLogger(__name__)


class TransitPicking(models.Model):
    """
    Transit Picking Pair Management
    
    Note: One transit order can have multiple transit picking records in case of backorder.

    Move Matching Strategy (product-based):
    - All sync and comparison operations match src/dest moves by product_id.
    - No persistent link on stock.move is required — moves can be deleted and
      recreated without breaking the transit flow.
    - The transit order lines are matched to moves by product_id as well, using
      the order's line_ids as the source of truth for expected quantities.
    - Limitation: if two lines (or moves) in the same picking share the same
      product_id, the total quantity is used. Consolidate duplicates before use.
    """
    _name = 'transit.picking'
    _description = 'Transit Picking Pair'
    _rec_name = 'display_name'
    _order = 'create_date desc, id desc'

    display_name = fields.Char(compute="_compute_display_name", string="Display name", store=False, readonly=True)

    cancel = fields.Boolean(default=False, help="Technical field used to mark transit pickings that were cancelled and should be ignored in the UI. Use for automation rules since its env cannot receive context flags.")

    transit_order_id = fields.Many2one(
        'transit.order',
        string='Transit Order',
        required=True,
        ondelete='cascade',
        index=True,
    )

    transit_order_id_display = fields.Char(
        string='Transit Order Display Name',
        compute='_compute_display_name',
        store=False,
        readonly=True,
    )

    transit_date_done = fields.Datetime(
        'Transit Effective Date', 
        related="transit_order_id.date_done",
        help="Date at which the transit order have been processed or canceled",
    )
    
    # Note: While technically both src and dest pickings can be null, this is the intend design phylosophy 
    # to implement transit backorder flow from both sides (src/dest). In this design, a transit picking record 
    # will be the place to determind which side init the backroder flow by checking which picking (src/dest) is null. 
    # This will be used in the automation rules to properly create the backorder picking and transit picking records.
    # So one side can be null while the backorder logic still resolve, transit.picking record at the end will always hold 2 
    # picking, one for dest and one for src, any other situation will be classifid as bug.
    src_picking_id = fields.Many2one(
        'stock.picking',
        string="Source Picking",
        ondelete='cascade',
        index=True,
    )
    
    dest_picking_id = fields.Many2one(
        'stock.picking',
        string="Destination Picking",
        ondelete='cascade',
        index=True,
    )
    
    state = fields.Selection(
        related='transit_order_id.state',
        string='Transit State',
        store=True,
        readonly=True
    )

    scheduled_date = fields.Datetime(
        'Scheduled Date', 
        related='transit_order_id.scheduled_date', 
        store=False, 
        readonly=True
    )
    
    src_picking_state = fields.Selection(
        related='src_picking_id.state',
        string='Source State',
        store=True,
        readonly=True
    )

    src_picking_move_ids = fields.One2many(
        related='src_picking_id.move_ids',
        string='Source Moves',
        store=False,
        readonly=True
    )

    dest_picking_state = fields.Selection(
        related='dest_picking_id.state',
        string='Destination State',
        store=True,
        readonly=True
    )

    dest_picking_move_ids = fields.One2many(
        related='dest_picking_id.move_ids',
        string='Destination Moves',
        store=False,
        readonly=True
    )
    
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='transit_order_id.company_id',
        store=True,
        readonly=True
    )
    
    src_company_id = fields.Many2one(
        'res.company',
        string='Source Company',
        related='transit_order_id.src_company_id',
        store=True,
        readonly=True
    )
    
    dest_company_id = fields.Many2one(
        'res.company',
        string='Destination Company',
        related='transit_order_id.dest_company_id',
        store=True,
        readonly=True
    )
    
    transit_location_id = fields.Many2one(
        'stock.location',
        string='Transit Location',
        related='transit_order_id.transit_location_id',
        store=True,
        readonly=True
    )

    # ── Delegation (stored relational fields) ─────────────────────────────────
    delegation_id = fields.Many2one(
        'transit.picking',
        string='Delegated From',
        ondelete='set null',
        index=True,
        readonly=True,
        help="The parent route that delegated part of its picking to create this one",
    )

    delegation_ids = fields.One2many(
        'transit.picking',
        'delegation_id',
        string='Delegated Routes',
        readonly=True,
        help="Child routes spawned from this one via delegation",
    )

    # ── Computed warning flags ────────────────────────────────────────────────
    is_late = fields.Boolean(compute='_compute_picking_state', store=False)
    is_today = fields.Boolean(compute='_compute_picking_state', store=False)
    is_very_late = fields.Boolean(compute='_compute_picking_state', store=False)
    has_mismatch = fields.Boolean(
        string='Has Mismatch',
        compute='_compute_picking_state',
        store=False,
    )
    comparison_data = fields.Json(compute='_compute_picking_state', store=False)

    # ── Backorder relationship (computed, store=False) ────────────────────────
    backorder_parent_id = fields.Many2one(
        'transit.picking',
        string='Backorder Of',
        compute='_compute_backorder_fields',
        store=False,
        readonly=True,
        help="The transit picking this one is a backorder of",
    )
    backorder_child_ids = fields.Many2many(
        'transit.picking',
        string='Backorders',
        compute='_compute_backorder_fields',
        store=False,
        readonly=True,
        help="Transit pickings that are backorders of this one",
    )
    backorder_parent_html = fields.Html(
        string='Backorder Of (Display)',
        compute='_compute_backorder_fields',
        sanitize=False,
        store=False,
    )
    backorder_child_ids_html = fields.Html(
        string='Backorders (Display)',
        compute='_compute_backorder_fields',
        sanitize=False,
        store=False,
    )

    # ── Delegation HTML display (computed, store=False) ───────────────────────
    delegation_id_html = fields.Html(
        string='Delegated From (Display)',
        compute='_compute_delegation_fields',
        sanitize=False,
        store=False,
        readonly=True,
    )
    delegation_ids_html = fields.Html(
        string='Delegated Routes (Display)',
        compute='_compute_delegation_fields',
        sanitize=False,
        store=False,
        readonly=True,
    )

    @api.depends(
        'state', 'scheduled_date',
        'src_picking_state', 'dest_picking_state',
        'src_picking_id.move_ids.product_id',
        'src_picking_id.move_ids.quantity',
        'src_picking_id.move_ids.product_uom_qty',
        'src_picking_id.move_ids.product_uom',
        'src_picking_id.move_line_ids.lot_id',
        'src_picking_id.move_line_ids.lot_name',
        'src_picking_id.move_line_ids.quantity',
        'dest_picking_id.move_ids.product_id',
        'dest_picking_id.move_ids.quantity',
        'dest_picking_id.move_ids.product_uom_qty',
        'dest_picking_id.move_ids.product_uom',
        'dest_picking_id.move_line_ids.lot_id',
        'dest_picking_id.move_line_ids.lot_name',
        'dest_picking_id.move_line_ids.quantity',
    )
    def _compute_picking_state(self):
        now = fields.Datetime.now()
        today = now.date()
        very_late_threshold = 3

        for rec in self:
            # ── Date warnings ─────────────────────────────────────────────────
            terminal = rec.state in ('done', 'cancel')
            sched = rec.scheduled_date

            rec.is_today     = bool(sched and sched.date() == today and not terminal)
            rec.is_late      = bool(sched and sched < now and not terminal)
            rec.is_very_late = bool(sched and (now - sched).days >= very_late_threshold and not terminal)

            # ── Comparison data + mismatch ────────────────────────────────────
            data = rec._build_comparison_data()
            rec.comparison_data = data
            rec.has_mismatch = any(
                s not in ('ok', 'pending')
                for line in data['lines']
                for s in line['status']
            )

    def _build_comparison_data(self):
        self.ensure_one()

        src  = self.src_picking_id
        dest = self.dest_picking_id
        if not src or not dest:
            return {"lines": [], "dest_state": ""}

        dest_done    = dest.state == "done"
        src_by_prod  = {m.product_id.id: m for m in src.move_ids}
        dest_by_prod = {m.product_id.id: m for m in dest.move_ids}

        lines = []
        for prod_id in src_by_prod.keys() | dest_by_prod.keys():
            src_move  = src_by_prod.get(prod_id)
            dest_move = dest_by_prod.get(prod_id)
            ref_move  = src_move or dest_move

            # ── Expected quantity ─────────────────────────────────────────────
            if src_move and src_move.product_uom:
                expected_qty = src_move.product_uom_qty
                expected_uom = src_move.product_uom.name
            elif dest_move and dest_move.product_uom:
                expected_qty = dest_move.product_uom_qty
                expected_uom = dest_move.product_uom.name
            else:
                expected_qty = 0.0
                expected_uom = ''

            # ── Actuals ───────────────────────────────────────────────────────
            src_qty  = src_move.quantity          if src_move  else None
            src_uom  = src_move.product_uom.name  if (src_move  and src_move.product_uom)  else expected_uom
            dest_qty = dest_move.quantity         if dest_move else None
            dest_uom = dest_move.product_uom.name if (dest_move and dest_move.product_uom) else expected_uom

            src_lines  = self._extract_move_lines(src_move)  if src_move  else []
            dest_lines = self._extract_move_lines(dest_move) if dest_move else []

            lines.append({
                "product_id":   prod_id,
                "product_name": ref_move.product_id.display_name if ref_move and ref_move.product_id else "Unknown Product",
                "expected_qty": expected_qty,
                "expected_uom": expected_uom,
                "src_qty":      src_qty,
                "src_uom":      src_uom,
                "dest_qty":     dest_qty,
                "dest_uom":     dest_uom,
                "src_lines":    src_lines,
                "dest_lines":   dest_lines,
                "status":       self._compute_line_status(
                                    src_move, dest_move,
                                    src_lines, dest_lines,
                                    dest_done,
                                ),
            })

        lines.sort(key=lambda l: l["product_name"])
        return {"lines": lines, "dest_state": dest.state}


    def _compute_line_status(self, src_move, dest_move, src_lines, dest_lines, dest_done):
        """
        Priority-based status check — each level only runs if the previous passed.

        Level 1 — Product presence
            Is the product present on both sides?
            → missing / src_only / dest_only / pending

        Level 2 — Lot / SN integrity
            Do the lot/SN sets match exactly?
            → lot_mismatch  (stops here — qty delta is a consequence, not a separate issue)

        Level 3 — Quantity accuracy
            Does every matched lot and the overall total match?
            → qty_mismatch

        If all pass → ok
        """
        # ── Level 1: Product presence ─────────────────────────────────────────
        if not src_move and not dest_move:
            return ["missing"]
        if not src_move:
            return ["dest_only"]
        if not dest_move:
            return ["src_only"] if dest_done else ["pending"]
        if not dest_done:
            return ["pending"]

        # ── Level 2: Lot / SN integrity ───────────────────────────────────────
        src_lot_names  = {entry["lot"] for entry in src_lines}
        dest_lot_names = {entry["lot"] for entry in dest_lines}
        if src_lot_names != dest_lot_names:
            return ["lot_mismatch"]

        # ── Level 3: Quantity accuracy ────────────────────────────────────────
        dest_uom = dest_move.product_uom
        src_uom  = src_move.product_uom

        src_map  = {e["lot"]: e["qty"] for e in src_lines}
        dest_map = {e["lot"]: e["qty"] for e in dest_lines}
        rounding = dest_uom.rounding if dest_uom else 0.01

        per_lot_ok = all(
            float_compare(src_map[lot], dest_map[lot], precision_rounding=rounding) == 0
            for lot in src_lot_names
        )

        if dest_uom and src_uom:
            src_qty_converted = src_uom._compute_quantity(
                src_move.quantity, dest_uom, rounding_method='HALF-UP'
            )
            overall_ok = float_compare(
                src_qty_converted, dest_move.quantity,
                precision_rounding=rounding
            ) == 0
        else:
            overall_ok = False

        if not per_lot_ok or not overall_ok:
            return ["qty_mismatch"]

        return ["ok"]

    def _extract_move_lines(self, move):
        """Return [{"lot": str, "qty": float}] for a stock.move."""
        result = []
        for ml in move.move_line_ids:
            lot = ml.lot_id.name if ml.lot_id else (ml.lot_name or "")
            result.append({"lot": lot, "qty": ml.quantity})
        return result

    def _render_tp_row_html(self, tp, label, label_class, label_style, content_style):
        """
        Render a single transit picking pair as one two-column banner row.
        The two returned elements (label span + content div) participate directly
        in the caller's CSS grid (grid-template-columns: max-content 1fr).
        """
        _PICK_BADGE = {
            'draft': 'info', 'waiting': 'warning', 'confirmed': 'warning',
            'assigned': 'warning', 'done': 'success', 'cancel': 'secondary',
        }
        _PICK_LABEL = {
            'draft': 'Draft', 'waiting': 'Waiting', 'confirmed': 'Confirmed',
            'assigned': 'Ready', 'done': 'Done', 'cancel': 'Cancelled',
        }
        # Note: not used, but keep for potential future use if we want to badge the transit itself
        # _TP_BADGE = {
        #     'draft': 'info', 'assigned': 'warning', 'in_progress': 'primary',
        #     'done': 'success', 'cancel': 'secondary',
        # }
        # _TP_LABEL = {
        #     'draft': 'Draft', 'assigned': 'Assigned', 'in_progress': 'In Progress',
        #     'done': 'Done', 'cancel': 'Cancelled',
        # }

        src  = tp.src_picking_id.sudo()
        dest = tp.dest_picking_id.sudo()

        def _pick_html(pick):
            if not pick:
                return Markup('<span class="fw-semibold text-muted">—</span>')
            colour = _PICK_BADGE.get(pick.state, 'secondary')
            plabel = _PICK_LABEL.get(pick.state, pick.state or '—')
            badge  = Markup(f'<span class="badge text-bg-{colour} ms-1">{escape(plabel)}</span>')
            return Markup(f'<span class="fw-semibold">{escape(pick.name)}</span>{badge}')

        return Markup(f'''<span class="{label_class}" style="
                display:flex; align-items:center;
                font-weight:700; font-size:0.75rem; text-transform:uppercase;
                letter-spacing:0.05em; white-space:nowrap;
                padding:8px 16px 8px 12px;
                border-radius:4px 0 0 4px;
                {label_style}
            "><i class="fa fa-exchange me-1"></i> {escape(label)}</span>
            <div style="
                display:flex; align-items:center; gap:4px;
                padding:8px 12px;
                border-radius:0 4px 4px 0;
                {content_style}
            ">
                <i class="fa fa-sign-out text-muted"></i>
                <span class="text-muted small fw-bold text-uppercase me-1">Delivery</span>
                {_pick_html(src)}
                <i class="fa fa-long-arrow-right text-muted mx-2"></i>
                <i class="fa fa-sign-in text-muted"></i>
                <span class="text-muted small fw-bold text-uppercase mx-1">Receipt</span>
                {_pick_html(dest)}
            </div>''')

    def _render_tp_list_html(self, tps, label, label_class, label_style, content_style):
        """
        Render one or more transit picking pairs as a self-contained grid of banner rows.
        Each tp in tps produces one row via _render_tp_row_html.
        """
        _GRID = 'display:grid; grid-template-columns:max-content 1fr; gap:6px 0;'
        rows = Markup('').join(
            self._render_tp_row_html(tp, label, label_class, label_style, content_style)
            for tp in tps
        )
        return Markup(f'<div style="padding:8px 0; {_GRID}">{rows}</div>')

    # =========================================================================
    # BACKORDER FIELDS COMPUTE
    # =========================================================================

    @api.depends(
        'src_picking_id',
        'src_picking_id.backorder_id',
        'dest_picking_id',
        'dest_picking_id.backorder_id',
    )
    def _compute_backorder_fields(self):
        """
        Compute all four backorder fields in one pass:
          [1] backorder_parent_id      — the transit picking this one is a backorder of
          [2] backorder_child_ids      — transit pickings that are backorders of this one
          [3] backorder_parent_html    — cross-company-safe HTML display of [1]
          [4] backorder_child_ids_html — cross-company-safe HTML display of [2]

        Two chains are supported:
          - Full-pair (src_picking_id set): traverse via src_picking_id.backorder_id (original logic).
          - Dest-only (src_picking_id null): traverse via dest_picking_id.backorder_id among
            dest-only transit pickings.
        """
        _LABEL_STYLE   = 'background:#fff8e1; border:1px solid #ffe082; border-right:none;'
        _CONTENT_STYLE = 'background:#fff8e1; border:1px solid #ffe082; border-left:none;'

        # ── Full-pair chain (src-based) ───────────────────────────────────────
        parent_src_ids = self.sudo().mapped('src_picking_id.backorder_id').ids
        tp_by_src_id: dict[int, 'TransitPicking'] = {}
        if parent_src_ids:
            parent_tps = self.env['transit.picking'].sudo().search([
                ('src_picking_id', 'in', parent_src_ids)
            ])
            tp_by_src_id = {tp.src_picking_id.id: tp for tp in parent_tps}

        own_src_ids = self.sudo().mapped('src_picking_id').ids
        children_by_parent_src_id: dict[int, list] = {}
        if own_src_ids:
            child_tps = self.env['transit.picking'].sudo().search([
                ('src_picking_id.backorder_id', 'in', own_src_ids)
            ])
            for child_tp in child_tps:
                pid = child_tp.src_picking_id.backorder_id.id
                children_by_parent_src_id.setdefault(pid, []).append(child_tp)

        # ── Dest-only chain (dest-based, src_picking_id = False) ─────────────
        # parent lookup: dest_picking_id.backorder_id → dest-only transit picking
        parent_dest_ids = [
            rec.sudo().dest_picking_id.backorder_id.id
            for rec in self
            if not rec.src_picking_id and rec.dest_picking_id and rec.dest_picking_id.backorder_id
        ]
        tp_by_dest_id: dict[int, 'TransitPicking'] = {}
        if parent_dest_ids:
            parent_dest_tps = self.env['transit.picking'].sudo().search([
                ('dest_picking_id', 'in', parent_dest_ids),
                ('src_picking_id', '=', False),
            ])
            tp_by_dest_id = {tp.dest_picking_id.id: tp for tp in parent_dest_tps}

        own_dest_ids = [
            rec.dest_picking_id.id
            for rec in self
            if not rec.src_picking_id and rec.dest_picking_id
        ]
        children_by_parent_dest_id: dict[int, list] = {}
        if own_dest_ids:
            child_dest_tps = self.env['transit.picking'].sudo().search([
                ('dest_picking_id.backorder_id', 'in', own_dest_ids),
                ('src_picking_id', '=', False),
            ])
            for child_tp in child_dest_tps:
                pid = child_tp.dest_picking_id.backorder_id.id
                children_by_parent_dest_id.setdefault(pid, []).append(child_tp)

        for rec in self:
            if rec.src_picking_id:
                # ── Full-pair: use src chain ───────────────────────────────────
                parent_src = rec.sudo().src_picking_id.backorder_id
                parent_tp  = tp_by_src_id.get(parent_src.id) if parent_src else False

                rec.backorder_parent_id   = parent_tp or False
                rec.backorder_parent_html = (
                    rec._render_tp_list_html(
                        [parent_tp], 'BACKORDER OF',
                        label_class='text-warning',
                        label_style=_LABEL_STYLE,
                        content_style=_CONTENT_STYLE,
                    )
                    if parent_tp else Markup('')
                )

                children = children_by_parent_src_id.get(rec.src_picking_id.id, [])
                rec.backorder_child_ids = (
                    self.env['transit.picking'].browse([c.id for c in children])
                    if children else self.env['transit.picking']
                )
                rec.backorder_child_ids_html = (
                    rec._render_tp_list_html(
                        children, 'BACKORDER',
                        label_class='text-warning',
                        label_style=_LABEL_STYLE,
                        content_style=_CONTENT_STYLE,
                    )
                    if children else Markup('')
                )
            else:
                # ── Dest-only: use dest chain ─────────────────────────────────
                parent_dest = rec.sudo().dest_picking_id.backorder_id if rec.dest_picking_id else False
                parent_tp   = tp_by_dest_id.get(parent_dest.id) if parent_dest else False

                rec.backorder_parent_id   = parent_tp or False
                rec.backorder_parent_html = (
                    rec._render_tp_list_html(
                        [parent_tp], 'BACKORDER OF',
                        label_class='text-warning',
                        label_style=_LABEL_STYLE,
                        content_style=_CONTENT_STYLE,
                    )
                    if parent_tp else Markup('')
                )

                children = children_by_parent_dest_id.get(
                    rec.dest_picking_id.id if rec.dest_picking_id else 0, []
                )
                rec.backorder_child_ids = (
                    self.env['transit.picking'].browse([c.id for c in children])
                    if children else self.env['transit.picking']
                )
                rec.backorder_child_ids_html = (
                    rec._render_tp_list_html(
                        children, 'BACKORDER',
                        label_class='text-warning',
                        label_style=_LABEL_STYLE,
                        content_style=_CONTENT_STYLE,
                    )
                    if children else Markup('')
                )

    # =========================================================================
    # DELEGATION FIELDS COMPUTE
    # =========================================================================

    @api.depends(
        'delegation_id',
        'delegation_id.src_picking_id',
        'delegation_id.dest_picking_id',
        'delegation_id.state',
        'delegation_ids',
        'delegation_ids.src_picking_id',
        'delegation_ids.dest_picking_id',
        'delegation_ids.state',
    )
    def _compute_delegation_fields(self):
        """
        Compute all four delegation fields in one pass:
          [1] delegation_id      — stored: the transit picking this was delegated from
          [2] delegation_ids     — stored: transit pickings delegated from this one
          [3] delegation_id_html  — cross-company-safe HTML display of [1]
          [4] delegation_ids_html — cross-company-safe HTML display of [2]
        """
        _LABEL_STYLE   = 'background:#e8f4fd; border:1px solid #90caf9; border-right:none;'
        _CONTENT_STYLE = 'background:#e8f4fd; border:1px solid #90caf9; border-left:none;'

        for rec in self:
            # ── [3]: parent delegation HTML ───────────────────────────────────
            parent = rec.sudo().delegation_id
            rec.delegation_id_html = (
                rec._render_tp_list_html(
                    [parent], 'DELEGATED FROM',
                    label_class='text-primary',
                    label_style=_LABEL_STYLE,
                    content_style=_CONTENT_STYLE,
                )
                if parent else Markup('')
            )

            # ── [4]: child delegations HTML ───────────────────────────────────
            children = rec.sudo().delegation_ids
            rec.delegation_ids_html = (
                rec._render_tp_list_html(
                    children, 'ROUTE',
                    label_class='text-primary',
                    label_style=_LABEL_STYLE,
                    content_style=_CONTENT_STYLE,
                )
                if children else Markup('')
            )

    @api.depends('src_picking_id.name', 'dest_picking_id.name', 'transit_order_id.name')
    def _compute_display_name(self):
        for record in self:
            if record.src_picking_id and record.dest_picking_id:
                record.display_name = f"{record.src_picking_id.sudo().company_id.name}: {record.src_picking_id.sudo().name} → {record.dest_picking_id.sudo().company_id.name}: {record.dest_picking_id.sudo().name}"
            else:
                record.display_name = "Incomplete Transit Pair"

            if record.transit_order_id:
                record.transit_order_id_display = record.transit_order_id.sudo().name

    def _merge_duplicate_src_moves(self, src_picking):
        """
        Consolidate duplicate-product moves on a src picking in-place.
 
        For each product that appears more than once:
          - Sum all product_uom_qty and quantity values into the first move (lowest id).
          - Delete the redundant moves.
 
        UOM: all moves for the same product are assumed to share the same UOM
        (Odoo enforces this at the picking level via the picking type's UOM).
        If they somehow differ, each qty is converted to the first move's UOM
        before summing.
 
        Called at the top of _sync_src_moves_to_dest_moves, before any dict
        is built, so the rest of the sync sees at most one move per product.
 
        Returns the number of moves deleted (0 means no duplicates existed).
        """
 
        groups = defaultdict(list)
        for move in src_picking.move_ids.sorted('id'):
            groups[move.product_id.id].append(move)
 
        moves_to_delete = self.env['stock.move']
        update_vals     = {}   # {move_id: {'product_uom_qty': x, 'quantity': y}}
 
        for product_id, moves in groups.items():
            if len(moves) == 1:
                continue
 
            survivor   = moves[0]
            base_uom   = survivor.product_uom
            total_planned = 0.0
            total_actual  = 0.0
 
            for move in moves:
                uom = move.product_uom
                if uom and base_uom and uom.id != base_uom.id:
                    total_planned += uom._compute_quantity(
                        move.product_uom_qty, base_uom, rounding_method='HALF-UP'
                    )
                    total_actual  += uom._compute_quantity(
                        move.quantity, base_uom, rounding_method='HALF-UP'
                    )
                else:
                    total_planned += move.product_uom_qty
                    total_actual  += move.quantity
 
            update_vals[survivor.id] = {
                'product_uom_qty': total_planned,
                'quantity':        total_actual,
            }
 
            for move in moves[1:]:
                moves_to_delete |= move
 
        if moves_to_delete:
            moves_to_delete.sudo().unlink()
            _logger.info(
                "Merged %d duplicate-product src move(s) on '%s'",
                len(moves_to_delete), src_picking.name,
            )
 
        for move_id, vals in update_vals.items():
            self.env['stock.move'].sudo().browse(move_id).write(vals)
 
        return len(moves_to_delete)

    def _sync_src_moves_to_dest_moves(self):
        """
        Sync SRC picking moves → DEST picking moves, matching by product_id.
 
        Duplicate-product moves on SRC are merged first so that the product-keyed
        dict always has at most one entry per product — preserving the invariant
        that this transit tracks (product, total qty) rather than individual lines.
 
        For each SRC move, find the DEST move with the same product_id and update
        its quantity. Creates new DEST moves for unmatched SRC products and deletes
        DEST moves whose product no longer exists in SRC.
        """
        if not self:
            return
 
        all_moves_to_delete = self.env['stock.move']
        all_create_vals     = []
        all_update_buckets  = {}
 
        for transit_picking in self.filtered(lambda tp: tp.src_picking_id):
            src_picking  = transit_picking.src_picking_id
            dest_picking = transit_picking.dest_picking_id.sudo()
            is_backorder = bool(src_picking.backorder_id)
            new_state    = 'draft' if is_backorder else 'assigned'
 
            # ── Merge duplicates on SRC before building the product dict ──────
            self._merge_duplicate_src_moves(src_picking)
 
            dest_by_product = {m.product_id.id: m for m in dest_picking.move_ids}
            processed_dest_ids = set()
 
            for src_move in src_picking.move_ids:
                prod_id   = src_move.product_id.id
                dest_move = dest_by_product.get(prod_id)
 
                planned_qty = src_move.product_uom_qty
                actual_qty  = src_move.quantity
 
                if dest_move:
                    processed_dest_ids.add(dest_move.id)
 
                    def convert(qty, src_uom=src_move.product_uom, dest_uom=dest_move.product_uom):
                        if src_uom.id != dest_uom.id:
                            return src_uom._compute_quantity(
                                qty, dest_uom, rounding_method='HALF-UP'
                            )
                        return qty
 
                    update_vals = {}
 
                    if is_backorder:
                        dest_planned = convert(planned_qty)
                        if dest_planned != dest_move.product_uom_qty:
                            update_vals['product_uom_qty'] = dest_planned
                    else:
                        dest_actual = convert(actual_qty)
                        if dest_actual != dest_move.product_uom_qty:
                            update_vals['product_uom_qty'] = dest_actual
                        if dest_actual != dest_move.quantity:
                            update_vals['quantity'] = dest_actual
 
                    if update_vals:
                        bucket_key = (is_backorder, frozenset(update_vals.items()))
                        all_update_buckets.setdefault(bucket_key, []).append(dest_move.id)
 
                else:
                    vals = {
                        'name':            src_move.name or src_move.product_id.display_name,
                        'product_id':      src_move.product_id.id,
                        'product_uom':     src_move.product_uom.id,
                        'picking_id':      dest_picking.id,
                        'company_id':      dest_picking.company_id.id,
                        'state':           new_state,
                        'product_uom_qty': planned_qty if is_backorder else actual_qty,
                        '_is_backorder':   is_backorder,
                    }
                    if not is_backorder:
                        vals['quantity'] = actual_qty
                    all_create_vals.append(vals)
 
            for dest_move in dest_picking.move_ids:
                if dest_move.id not in processed_dest_ids:
                    all_moves_to_delete |= dest_move
 
        if all_moves_to_delete:
            all_moves_to_delete.unlink()
            _logger.info("Deleted %d unmatched dest moves", len(all_moves_to_delete))
 
        if all_update_buckets:
            total = 0
            for (is_backorder, vals_frozen), move_ids in all_update_buckets.items():
                write_ctx = {'skip_auto_assign': True} if is_backorder else {}
                self.env['stock.move'].sudo().with_context(**write_ctx).browse(move_ids).write(
                    dict(vals_frozen)
                )
                total += len(move_ids)
            _logger.info("Updated %d dest moves in %d batch(es)", total, len(all_update_buckets))
 
        if all_create_vals:
            backorder_creates     = []
            non_backorder_creates = []
            for vals in all_create_vals:
                is_bo = vals.pop('_is_backorder')
                (backorder_creates if is_bo else non_backorder_creates).append(vals)
 
            if non_backorder_creates:
                self.env['stock.move'].sudo().create(non_backorder_creates)
                _logger.info("Created %d dest moves (state=assigned)", len(non_backorder_creates))
 
            if backorder_creates:
                self.env['stock.move'].sudo().with_context(skip_auto_assign=True).create(
                    backorder_creates
                )
                _logger.info("Created %d dest moves (state=draft)", len(backorder_creates))

    def _sync_src_valuation_to_dest_moves(self):
        """
        Batch: read the true unit cost from stock.valuation.layer for every
        done SRC move and write it to the matching DEST move's price_unit.
        """
        src_done = self.filtered(lambda tp: tp.src_picking_id.state == 'done')
        if not src_done:
            return

        all_src_moves = src_done.mapped('src_picking_id.move_ids')
        if not all_src_moves:
            return

        svls = self.env['stock.valuation.layer'].sudo().search([
            ('stock_move_id', 'in', all_src_moves.ids),
            ('stock_valuation_layer_id', '=', False),
        ])

        if not svls:
            _logger.warning(
                "_sync_src_valuation_to_dest_moves: no SVL rows found for src moves %s — "
                "dest price_unit will not be set",
                all_src_moves.mapped('name'),
            )
            return

        agg: dict[int, dict] = {}
        for svl in svls:
            mid = svl.stock_move_id.id
            if mid not in agg:
                agg[mid] = {'value': 0.0, 'quantity': 0.0}
            agg[mid]['value']    += svl.value
            agg[mid]['quantity'] += svl.quantity

        updates: dict[int, float] = {}

        for tp in src_done:
            dest_by_product = {
                m.product_id.id: m
                for m in tp.dest_picking_id.sudo().move_ids
            }

            for src_move in tp.src_picking_id.move_ids:
                bucket = agg.get(src_move.id)
                if not bucket:
                    _logger.warning(
                        "No SVL found for src move %d (%s) in '%s' — skipping price_unit sync",
                        src_move.id, src_move.product_id.display_name, tp.src_picking_id.name,
                    )
                    continue

                qty = bucket['quantity']
                if float_is_zero(qty, precision_rounding=0.00001):
                    _logger.warning(
                        "SVL for src move %d (%s) has zero quantity — skipping",
                        src_move.id, src_move.product_id.display_name,
                    )
                    continue

                unit_cost = bucket['value'] / qty

                dest_move = dest_by_product.get(src_move.product_id.id)
                if not dest_move:
                    _logger.warning(
                        "No dest move for product '%s' in '%s' — skipping price_unit sync",
                        src_move.product_id.display_name, tp.dest_picking_id.name,
                    )
                    continue

                updates[dest_move.id] = unit_cost
                _logger.info(
                    "price_unit sync: '%s' product='%s' unit_cost=%.6f (SVL value=%.4f qty=%.4f)",
                    tp.display_name, src_move.product_id.display_name,
                    unit_cost, bucket['value'], qty,
                )

        if updates:
            for dest_move_id, unit_cost in updates.items():
                self.env['stock.move'].sudo().browse(dest_move_id).write(
                    {'price_unit': unit_cost}
                )
            _logger.info(
                "_sync_src_valuation_to_dest_moves: wrote price_unit on %d dest move(s)",
                len(updates),
            )
    
    def _propagate_src_move_lines_to_dest(self):
        """
        Batch-aware: copy SRC move lines into DEST pickings, resolving/creating lots cross-company.
        """
        if not self:
            return

        errors = []
        for tp in self.filtered(lambda tp: tp.src_picking_id):
            src  = tp.src_picking_id
            dest = tp.dest_picking_id.sudo()
            if src.location_dest_id.id != dest.location_id.id:
                errors.append(
                    f"Transit '{tp.transit_order_id.name}': location mismatch — "
                    f"SRC.location_dest_id ({src.location_dest_id.name}) != "
                    f"DEST.location_id ({dest.location_id.name})"
                )
        if errors:
            raise ValidationError('\n'.join(errors))

        all_dest_move_lines = self.env['stock.move.line']
        for tp in self:
            all_dest_move_lines |= tp.dest_picking_id.sudo().move_line_ids
        if all_dest_move_lines:
            all_dest_move_lines.unlink()

        all_lot_data = []
        for tp in self:
            for src_ml in tp.src_picking_id.move_line_ids:
                lot_name = src_ml.lot_id.name if src_ml.lot_id else src_ml.lot_name
                if lot_name:
                    all_lot_data.append({
                        'lot_name': lot_name,
                        'product_id': src_ml.product_id.id,
                    })

        lot_map = self._batch_find_or_create_lots(all_lot_data)

        _STRIP_FIELDS = frozenset([
            'location_id', 'location_dest_id', 'result_package_id', 'package_id',
            'id', 'create_date', 'create_uid', 'write_date', 'write_uid', '__last_update',
        ])
        create_vals_list = []
        total_src_lines  = 0

        for tp in self.filtered(lambda tp: tp.src_picking_id):
            src_picking  = tp.src_picking_id
            dest_picking = tp.dest_picking_id.sudo()

            in_moves_by_product = {
                move.product_id.id: move
                for move in dest_picking.move_ids
            }

            for src_ml in src_picking.move_line_ids:
                total_src_lines += 1
                product_id = src_ml.product_id.id

                in_move = in_moves_by_product.get(product_id)
                if not in_move:
                    _logger.warning(
                        "No dest move for product_id=%s ('%s') in '%s' — skipping",
                        product_id, src_ml.product_id.name, src_picking.name,
                    )
                    continue

                lot_name  = src_ml.lot_id.name if src_ml.lot_id else src_ml.lot_name
                copy_vals = src_ml.copy_data()[0]
                copy_vals.update({
                    'picking_id':           dest_picking.id,
                    'move_id':              in_move.id,
                    'company_id':           dest_picking.company_id.id,
                    'state':                'assigned',
                    'quantity':             src_ml.quantity,
                    'quantity_product_uom': src_ml.quantity_product_uom,
                })

                if lot_name:
                    shared_lot = lot_map.get((lot_name, product_id))
                    if shared_lot and shared_lot.exists():
                        copy_vals['lot_id']   = shared_lot.id
                        copy_vals['lot_name'] = shared_lot.name
                    else:
                        copy_vals['lot_id']   = False
                        copy_vals['lot_name'] = False
                else:
                    copy_vals['lot_id']   = False
                    copy_vals['lot_name'] = False

                for field in _STRIP_FIELDS:
                    copy_vals.pop(field, None)

                create_vals_list.append(copy_vals)

        if create_vals_list:
            self.env['stock.move.line'].sudo().create(create_vals_list)

        _logger.info(
            "Propagated %d move line(s) across %d transit picking pair(s)",
            total_src_lines, len(self),
        )
    
    def _create_dest_backorders_from_src(self, src_backorders):
        """
        Batch: create DEST backorder pickings and transit picking pairs.
        """
        if not self or not src_backorders:
            return self.env['stock.picking']

        if len(self) != len(src_backorders):
            raise UserError(
                f"_create_dest_backorders_from_src: "
                f"{len(self)} parent transit picking(s) vs {len(src_backorders)} src backorder(s)"
            )

        dest_backorder_vals_list = []
        for parent_tp, src_bo in zip(self, src_backorders):
            dest = parent_tp.dest_picking_id.sudo()
            dest_backorder_vals_list.append({
                'partner_id':      dest.partner_id.id,
                'picking_type_id': dest.picking_type_id.id,
                'scheduled_date':  dest.scheduled_date,
                'company_id':      dest.company_id.id,
                'origin':          dest.origin,
                'backorder_id':    dest.id,
            })

        dest_backorders = self.env['stock.picking'].sudo().create(dest_backorder_vals_list)

        transit_picking_vals_list = []
        for parent_tp, src_bo, dest_bo in zip(self, src_backorders, dest_backorders):
            transit_picking_vals_list.append({
                'transit_order_id': parent_tp.transit_order_id.id,
                'src_picking_id':         src_bo.id,
                'dest_picking_id':        dest_bo.id,
            })

        self.env['transit.picking'].sudo().create(transit_picking_vals_list)

        _logger.info(
            "Created %d dest backorder(s) %s from src backorders %s",
            len(dest_backorders),
            dest_backorders.mapped('name'),
            src_backorders.mapped('name'),
        )

        return dest_backorders
    
    def _batch_find_or_create_lots(self, lot_data_list):
        if not lot_data_list:
            return {}
        
        StockLot = self.env['stock.lot'].sudo()
        result = {}
        
        lot_keys = set()
        for data in lot_data_list:
            lot_name = data.get('lot_name')
            product_id = data.get('product_id')
            if lot_name and product_id:
                lot_keys.add((lot_name, product_id))
        
        if not lot_keys:
            return {}
        
        if len(lot_keys) == 1:
            lot_name, product_id = list(lot_keys)[0]
            domain = [
                ('name', '=', lot_name),
                ('product_id', '=', product_id),
            ]
        else:
            domain = []
            lot_keys_list = list(lot_keys)
            
            for _ in range(len(lot_keys_list) - 1):
                domain.append('|')
            
            for lot_name, product_id in lot_keys_list:
                domain.extend([
                    '&',
                    ('name', '=', lot_name),
                    ('product_id', '=', product_id),
                ])
        
        existing_lots = StockLot.search(domain)
        
        existing_map = {}
        lots_to_convert = self.env['stock.lot'].sudo()

        for lot in existing_lots:
            key = (lot.name, lot.product_id.id)
            if key not in existing_map:
                existing_map[key] = lot
                if lot.company_id:
                    lots_to_convert |= lot

        if lots_to_convert:
            try:
                lots_to_convert.write({'company_id': False})
                _logger.info("Converted %d lot(s) to cross-company", len(lots_to_convert))
            except Exception as e:
                _logger.warning("Failed to convert lots to cross-company: %s", str(e))
        
        missing_keys = lot_keys - set(existing_map.keys())
        
        if missing_keys:
            create_vals_list = [
                {
                    'name': lot_name,
                    'product_id': product_id,
                    'company_id': False,
                }
                for lot_name, product_id in missing_keys
            ]
            
            try:
                new_lots = StockLot.create(create_vals_list)
                for lot in new_lots:
                    key = (lot.name, lot.product_id.id)
                    existing_map[key] = lot
                    
            except Exception as e:
                _logger.warning(f"Batch lot creation failed: {str(e)}")
                
                for vals in create_vals_list:
                    lot_name = vals['name']
                    product_id = vals['product_id']
                    key = (lot_name, product_id)
                    
                    try:
                        new_lot = StockLot.create(vals)
                        existing_map[key] = new_lot
                    except Exception as individual_error:
                        existing = StockLot.search([
                            ('name', '=', lot_name),
                            ('product_id', '=', product_id),
                        ], limit=1)
                        
                        if existing:
                            if existing.company_id:
                                try:
                                    existing.write({'company_id': False})
                                except:
                                    pass
                            existing_map[key] = existing
        
        for lot_name, product_id in lot_keys:
            result[(lot_name, product_id)] = existing_map.get((lot_name, product_id), False)
        
        return result
    
    def _populate_transit_quantities(self):
        """
        Batch-aware: accumulate DEST move actuals back into transit order lines.
        """
        line_deltas = {}

        for transit_picking in self:
            dest_picking = transit_picking.dest_picking_id
            if dest_picking.state != 'done':
                continue

            order_lines_by_product = {
                line.product_id.id: line
                for line in transit_picking.transit_order_id.line_ids
            }

            for dest_move in dest_picking.move_ids:
                line = order_lines_by_product.get(dest_move.product_id.id)
                if not line:
                    _logger.warning(
                        "Dest move %d (%s) has no matching transit order line by product — skipping",
                        dest_move.id, dest_move.product_id.name,
                    )
                    continue

                actual_qty = dest_move.quantity
                if dest_move.product_uom != line.product_uom:
                    actual_qty = dest_move.product_uom._compute_quantity(
                        actual_qty, line.product_uom, rounding_method='HALF-UP'
                    )

                if line.id not in line_deltas:
                    line_deltas[line.id] = [line, 0.0]
                line_deltas[line.id][1] += actual_qty

        for line_id, (line, delta) in line_deltas.items():
            new_total = line.quantity + delta
            line.sudo().with_context(skip_transit_order_line_write_warning=True, skip_transit_order_line_write_state_check=True).write({'quantity': new_total})
            _logger.info(
                "Transit line %d (%s): +%.4f → new total %.4f",
                line_id, line.product_id.name, delta, new_total,
            )

    def _do_cancel(self):
        # Only operate on records that have a src picking —
        # dest-only pairs have no src to cancel.
        has_src = self.filtered(lambda tp: tp.src_picking_id)
        if not has_src:
            return

        src_picking_ids   = has_src.mapped('src_picking_id').ids
        transit_order_ids = has_src.mapped('transit_order_id').ids

        try:
            if src_picking_ids:
                self.env.cr.execute(
                    'SELECT id FROM stock_picking WHERE id IN %s FOR UPDATE NOWAIT',
                    (tuple(src_picking_ids),)
                )
            if transit_order_ids:
                self.env.cr.execute(
                    'SELECT id FROM transit_order WHERE id IN %s FOR UPDATE NOWAIT',
                    (tuple(transit_order_ids),)
                )
        except Exception:
            raise UserError(
                "One or more transit orders are currently being modified by another "
                "operation. Please try again."
            )
        has_src.mapped('src_picking_id').invalidate_recordset(['state'])
        has_src.mapped('transit_order_id').invalidate_recordset(['state'])

        src_pickings = has_src.mapped('src_picking_id').filtered(
            lambda p: p.state not in ('done', 'cancel')
        )
        if not src_pickings:
            return
        try:
            has_src.sudo().write({'cancel': True})
            src_pickings.sudo().action_cancel()
        finally:
            has_src.sudo().write({'cancel': False})


    def action_cancel(self):
        self.ensure_one()

        if not self.src_picking_id:
            raise UserError(
                f"Cannot cancel transit picking of '{self.transit_order_id.name}': "
                f"this is a dest-only backorder pair. Cancel the destination picking directly."
            )

        if self.src_picking_state in ('done', 'cancel'):
            raise UserError(
                f"Cannot cancel transfers of '{self.transit_order_id.name}': "
                f"'{self.src_picking_id.name}' is already '{self.src_picking_state}'."
            )

        if self.sudo().transit_order_id.state in ('cancel', 'done', 'in_progress'):
            raise UserError(
                f"Cannot cancel '{self.src_picking_id.name}': transit order "
                f"'{self.transit_order_id.name}' is in state "
                f"'{self.transit_order_id.state}'."
            )

        self._do_cancel()
        return True


    def action_batch_cancel(self):
        blocked_order_state = self.sudo().filtered(
            lambda tp: tp.transit_order_id.state in ('cancel', 'done', 'in_progress')
        )
        non_cancellable = self.filtered(
            lambda tp: tp.src_picking_id and tp.src_picking_state in ('done', 'cancel')
        )
        cancellable = self - blocked_order_state - non_cancellable

        if cancellable:
            cancellable._do_cancel()

        errors = [
            f"Transfer of '{tp.transit_order_id.name}' blocked: order is "
            f"'{tp.sudo().transit_order_id.state}'"
            for tp in blocked_order_state
        ] + [
            f"Transfer of '{tp.transit_order_id.name}' "
            f"('{tp.src_picking_id.name}' is already '{tp.src_picking_state}')"
            for tp in non_cancellable
        ]

        if errors:
            raise UserError(
                f"{len(cancellable)} record(s) cancelled.\n\n"
                f"The following could not be cancelled:\n• " + '\n• '.join(errors)
            )

        return True

    def action_stop(self):
        self.ensure_one()

        if not self.src_picking_id:
            raise UserError(
                f"Cannot stop transit picking of '{self.transit_order_id.name}': "
                f"this is a dest-only backorder pair. Cancel the destination picking directly."
            )

        if self.sudo().state != 'in_progress':
            raise UserError(
                f"Cannot stop transit picking of '{self.transit_order_id.name}': "
                f"order is in state '{self.transit_order_id.state}'."
            )

        if self.src_picking_state in ('done', 'cancel'):
            raise UserError(
                f"Cannot stop '{self.src_picking_id.name}': "
                f"already in state '{self.src_picking_state}'."
            )

        self._do_cancel()
        return True


    def action_batch_stop(self):
        if not self:
            return True

        cancellable = self.filtered(
            lambda tp: tp.src_picking_id
            and tp.src_picking_state not in ('done', 'cancel')
            and tp.sudo().transit_order_id.state == 'in_progress'
        )

        if cancellable:
            cancellable._do_cancel()

        return True
    
    @api.constrains('src_picking_id', 'dest_picking_id', 'transit_location_id')
    def _check_location_consistency(self):
        """Validate location consistency between pickings"""
        for record in self:
            if not record.transit_location_id:
                raise ValidationError(
                    f"Transit Pair '{record.display_name}': No transit location found"
                )

            if record.src_picking_id and record.src_picking_id.location_dest_id != record.transit_location_id:
                raise ValidationError(
                    f"Transit Pair '{record.display_name}': "
                    f"Source picking destination must be transit location '{record.transit_location_id.name}' "
                    f"(currently: '{record.src_picking_id.location_dest_id.name}')"
                )

            if record.dest_picking_id and record.dest_picking_id.location_id != record.transit_location_id:
                raise ValidationError(
                    f"Transit Pair '{record.display_name}': "
                    f"Destination picking source must be transit location '{record.transit_location_id.name}' "
                    f"(currently: '{record.dest_picking_id.location_id.name}')"
                )
    
    @api.constrains('src_picking_id', 'dest_picking_id')
    def _check_picking_types(self):
        """Validate picking types"""
        for record in self:
            if record.src_picking_id and record.src_picking_id.picking_type_id.code != 'outgoing':
                raise ValidationError(
                    f"Transit Pair '{record.display_name}': "
                    f"Source picking must use 'outgoing' type"
                )

            if record.dest_picking_id and record.dest_picking_id.picking_type_id.code != 'incoming':
                raise ValidationError(
                    f"Transit Pair '{record.display_name}': "
                    f"Destination picking must use 'incoming' type"
                )

    def automation_handle_src_picking_done(self):
        """
        Batch-aware: handle SRC picking validation for one or more transit picking pairs.

          1. Sync SRC move quantities → DEST moves        (batch call)
          2. Propagate SRC move lines → DEST pickings     (batch call)
          3. Set transit order state  → in_progress       (batched write)
          4. Create + sync DEST backorders for any SRC backorders  (batch call)
          5. Clear needs_review on the validated SRC pickings
        """

        # Lock first, before any read or state check
        self.env.cr.execute(
            'SELECT id FROM stock_picking WHERE id IN %s FOR UPDATE',
            (tuple(self.mapped('src_picking_id').ids),)
        )
        self.env.cr.execute(
            'SELECT id FROM transit_order WHERE id IN %s FOR UPDATE',
            (tuple(self.mapped('transit_order_id').ids),)
        )
        self.mapped('src_picking_id').invalidate_recordset(['state'])
        self.mapped('transit_order_id').invalidate_recordset(['state'])

        invalid = self.filtered(lambda tp: tp.state not in ('assigned', 'in_progress'))
        if invalid:
            msgs = [
                f"Transit '{tp.transit_order_id.name}' in state '{tp.state}' "
                f"cannot process SRC picking validation."
                for tp in invalid
            ]
            raise ValidationError('\n'.join(msgs))

        valid = self - invalid

        try:
            valid._sync_src_moves_to_dest_moves()
        except Exception as e:
            raise UserError(f"Failed to sync SRC→DEST moves: {str(e)}")

        try:
            valid._sync_src_valuation_to_dest_moves()
        except Exception as e:
            raise UserError(f"Failed to sync SRC valuation to DEST moves: {str(e)}")

        try:
            valid._propagate_src_move_lines_to_dest()
        except Exception as e:
            raise UserError(f"Failed to propagate SRC move lines: {str(e)}")

        valid.mapped('transit_order_id').sudo().write({'state': 'in_progress'})

        parent_tp_ids = []
        src_bo_ids    = []

        for tp in valid:
            open_backorders = tp.src_picking_id.sudo().backorder_ids.filtered(
                lambda p: p.state not in ('done', 'cancel')
            )
            for src_backorder in open_backorders:
                already_exists = self.env['transit.picking'].search([
                    ('src_picking_id', '=', src_backorder.id)
                ], limit=1)
                if already_exists:
                    _logger.info(
                        "SRC backorder '%s' already has a transit pair (Rule 2), skipping",
                        src_backorder.name,
                    )
                    continue
                parent_tp_ids.append(tp.id)
                src_bo_ids.append(src_backorder.id)

        if parent_tp_ids:
            parent_tps = self.env['transit.picking'].browse(parent_tp_ids)
            src_bos    = self.env['stock.picking'].browse(src_bo_ids)
            try:
                parent_tps._create_dest_backorders_from_src(src_bos)
            except Exception as e:
                raise UserError(f"SRC backorder creation failed: {str(e)}")

            new_transit_pickings = self.env['transit.picking'].search([
                ('src_picking_id', 'in', src_bo_ids)
            ])
            if new_transit_pickings:
                try:
                    new_transit_pickings._sync_src_moves_to_dest_moves()
                except Exception as e:
                    raise UserError(f"SRC backorder sync failed: {str(e)}")
                _logger.info(
                    "Rule 1: created and synced %d transit pair(s) for src backorders %s",
                    len(new_transit_pickings),
                    src_bos.mapped('name'),
                )

        # ── 5. Clear review flag on validated SRC pickings ────────────────────
        # The picking has just been processed by the transit automation — there is
        # nothing left to review on the src side. Clear the flag so the banner
        # does not linger on a picking that is already done.
        src_pickings_to_clear = valid.mapped('src_picking_id').sudo().filtered(
            lambda p: p.needs_review
        )
        if src_pickings_to_clear:
            src_pickings_to_clear.write({'needs_review': False})

    def automation_handle_dest_picking_done(self):
        """
        Batch-aware: handle DEST picking validation for one or more transit picking pairs.

          1. Guard: SRC picking must already be done (skipped for dest-only pairs —
            src_picking_id is null, meaning goods were already shipped in the original transit)
          2. Guard: a backorder picking already exists for this dest picking — Rule 7 fires
            INSIDE stock.picking._create_backorder() after the split moves are moved away,
            so by this point the backorder picking exists in DB with backorder_id set.
            Checking pending moves on the original picking is useless (they're already gone).
            Check stock.picking.backorder_id directly instead.
          3. Populate transit line quantities from DEST move actuals  (batch call)
          4. If all pairs settled: done if any pair fully done, cancel if all cancelled

        Null-src (dest-only) pairs are treated as "src already done" throughout.
        """
        # Only enforce src-done guard for full-pair transit pickings (src_picking_id set)
        src_not_done = self.filtered(lambda tp: tp.src_picking_id and tp.src_picking_state != 'done')
        if src_not_done:
            msgs = [
                f"Cannot validate Destination picking '{tp.dest_picking_id.name}'. "
                f"Source picking '{tp.src_picking_id.name}' must be validated first "
                f"(current state: {tp.src_picking_state})."
                for tp in src_not_done
            ]
            raise ValidationError('\n'.join(msgs))

        try:
            self._populate_transit_quantities()
        except Exception as e:
            raise UserError(f"Failed to populate transit quantities: {str(e)}")

        orders_to_done           = self.env['transit.order']
        orders_to_cancel         = self.env['transit.order']
        orders_still_in_progress = self.env['transit.order']

        for tp in self:
            transit_order = tp.transit_order_id
            all_pairs = transit_order.transit_picking_ids

            # ── Guard: backorder picking already created for this dest picking ────
            # Rule 7 fires INSIDE stock.picking._create_backorder(), triggered when
            # split moves are moved away from the original picking to the new backorder
            # picking. At that moment the pending moves are already gone from the
            # original picking — checking move state is useless. However, the backorder
            # picking ALREADY EXISTS in DB with backorder_id = original picking id.
            # Use search_count (not ORM cache) to detect it reliably.
            pending_backorder_count = self.env['stock.picking'].search_count([
                ('backorder_id', '=', tp.dest_picking_id.id),
                ('state', 'not in', ['done', 'cancel']),
            ])
            if pending_backorder_count:
                orders_still_in_progress |= transit_order
                _logger.info(
                    "DEST picking '%s' validated but has %d backorder picking(s) — "
                    "transit '%s' stays in_progress until backorder is processed",
                    tp.dest_picking_id.name,
                    pending_backorder_count,
                    transit_order.name,
                )
                continue

            # Dest-only pairs (src_picking_id = False) have no src to wait for —
            # the goods were already shipped in the original transit. Never block them.
            unsettled = all_pairs.filtered(
                lambda p: (p.src_picking_id and p.src_picking_state not in ('done', 'cancel'))
                          or p.dest_picking_state not in ('done', 'cancel')
            )

            if unsettled:
                orders_still_in_progress |= transit_order
                _logger.info(
                    "DEST picking '%s' validated, transit '%s' remains in_progress "
                    "(%d pair(s) still unsettled: %s)",
                    tp.dest_picking_id.name, transit_order.name,
                    len(unsettled), unsettled.mapped('display_name'),
                )
            else:
                # Dest-only pair: treat as "src done" when checking if any pair fully done
                any_done = any(
                    (not p.src_picking_id or p.src_picking_state == 'done')
                    and p.dest_picking_state == 'done'
                    for p in all_pairs
                )
                if any_done:
                    orders_to_done |= transit_order
                    _logger.info(
                        "DEST picking '%s' validated, all pairs settled, "
                        "transit '%s' → done (at least one pair fully done)",
                        tp.dest_picking_id.name, transit_order.name,
                    )
                else:
                    orders_to_cancel |= transit_order
                    _logger.info(
                        "DEST picking '%s' validated, all pairs settled, "
                        "transit '%s' → cancel (all pairs cancelled)",
                        tp.dest_picking_id.name, transit_order.name,
                    )

        truly_done   = orders_to_done   - orders_still_in_progress
        truly_cancel = orders_to_cancel - orders_still_in_progress

        if truly_done:
            truly_done.sudo().write({
                'state': 'done',
                'date_done': fields.Datetime.now(),
            })

        if truly_cancel:
            truly_cancel.sudo().write({
                'state': 'cancel',
                'date_done': fields.Datetime.now(),
            })

    def automation_handle_src_backorder_created(self, new_src_backorders):
        """
        Batch: for each new SRC backorder, create a matching DEST backorder and sync moves.
        """
        parent_by_src_id = {tp.src_picking_id.id: tp for tp in self}

        parent_tp_ids = []
        src_bo_ids    = []

        for new_backorder in new_src_backorders:
            parent_transit = parent_by_src_id.get(new_backorder.backorder_id.id)
            if not parent_transit:
                continue

            existing = self.env['transit.picking'].search([
                ('src_picking_id', '=', new_backorder.id)
            ], limit=1)
            if existing:
                continue

            parent_tp_ids.append(parent_transit.id)
            src_bo_ids.append(new_backorder.id)

        if not parent_tp_ids:
            return

        parent_tps = self.env['transit.picking'].browse(parent_tp_ids)
        src_bos    = self.env['stock.picking'].browse(src_bo_ids)

        try:
            parent_tps._create_dest_backorders_from_src(src_bos)
        except Exception as e:
            raise UserError(f"Failed to create dest backorders: {str(e)}")

        new_transit_pickings = self.env['transit.picking'].search([
            ('src_picking_id', 'in', src_bo_ids)
        ])
        if new_transit_pickings:
            try:
                new_transit_pickings._sync_src_moves_to_dest_moves()
            except Exception as e:
                raise UserError(f"Failed to sync backorder transit pairs: {str(e)}")
            _logger.info(
                "Rule 2: created and synced %d backorder transit pair(s) for src backorders %s",
                len(new_transit_pickings),
                src_bos.mapped('name'),
            )

    def automation_handle_dest_backorder_created(self, new_dest_backorders):
        """
        Batch: for each new DEST backorder, create a matching fresh SRC picking
        and a fully-populated transit picking pair (both sides set from the start).

        Design philosophy: null src/dest on a transit.picking is a TEMPORARY signal only.
        This method always resolves to a full pair — transit.picking at the end always
        holds both src and dest. Any persisted null-src record is a bug.

        Backorder structure: new_src.backorder_id → orig_src, forming a net/tree
        alongside any Rule 5 src backorders. This is intentional — the combination
        structure is supported and preferred over a strict linear chain.

        Loop prevention (idempotency-based):
        - Rule 5 fires on outgoing pickings when state → assigned AND backorder_id != False.
        - The new src picking created here has backorder_id = orig_src.id, so Rule 5
          will fire on it. The idempotency check in automation_handle_src_backorder_created
          (searches for an existing transit pair with src_picking_id = new_src.id) finds
          the pair already created here and skips — no duplicate, no infinite loop.
        """
        _logger.info("===================== automation_handle_dest_backorder_created =====================")
        parent_by_dest_id = {tp.dest_picking_id.id: tp for tp in self}

        pairing_data      = []
        src_picking_vals  = []

        for dest_bo in new_dest_backorders:
            parent_transit = parent_by_dest_id.get(dest_bo.backorder_id.id)
            if not parent_transit:
                continue

            existing = self.env['transit.picking'].search([
                ('dest_picking_id', '=', dest_bo.id)
            ], limit=1)
            if existing:
                _logger.info(
                    "DEST backorder '%s' already has a transit picking pair, skipping",
                    dest_bo.name,
                )
                continue

            orig_src = parent_transit.src_picking_id.sudo()
            src_picking_vals.append({
                'partner_id':       orig_src.partner_id.id,
                'picking_type_id':  orig_src.picking_type_id.id,
                'location_id':      orig_src.location_id.id,
                'location_dest_id': orig_src.location_dest_id.id,
                'scheduled_date':   dest_bo.scheduled_date,
                'company_id':       orig_src.company_id.id,
                'origin':           orig_src.origin,
                'backorder_id':     orig_src.id,
            })
            pairing_data.append((parent_transit, dest_bo))

        if not pairing_data:
            return

        new_src_pickings = self.env['stock.picking'].sudo().create(src_picking_vals)

        move_vals_list = []
        for (parent_transit, dest_bo), new_src in zip(pairing_data, new_src_pickings):
            for dest_move in dest_bo.move_ids.filtered(lambda m: m.state != 'cancel'):
                move_vals_list.append({
                    'name':            dest_move.name or dest_move.product_id.display_name,
                    'product_id':      dest_move.product_id.id,
                    'product_uom':     dest_move.product_uom.id,
                    'product_uom_qty': dest_move.product_uom_qty,
                    'picking_id':      new_src.id,
                    'company_id':      new_src.company_id.id,
                    'state':           'draft',
                })

        if move_vals_list:
            self.env['stock.move'].sudo().with_context(skip_auto_assign=True).create(
                move_vals_list
            )

        transit_picking_vals = []
        for (parent_transit, dest_bo), new_src in zip(pairing_data, new_src_pickings):
            transit_picking_vals.append({
                'transit_order_id': parent_transit.transit_order_id.id,
                'src_picking_id':         new_src.id,
                'dest_picking_id':        dest_bo.id,
            })

        try:
            created = self.env['transit.picking'].sudo().create(transit_picking_vals)

            _logger.info(
                "Rule 6: created %d full transit picking pair(s) for dest backorders, "
                "new src pickings: %s",
                len(created),
                new_src_pickings.mapped('name'),
            )
        except Exception as e:
            raise UserError(
                f"Failed to create transit picking pairs for dest backorders: {str(e)}"
            )

    def automation_handle_picking_cancelled(self, cancelled_picking_ids):
        """
        Batch-aware: handles state=cancel transition on any picking linked to a transit pair.

        Two sub-cases resolved by checking which side was cancelled:

        SRC (outgoing) cancelled:
            → cancel dest picking (Rule 9 re-fires on dest, hits valid_dest path below)

        DEST (incoming) cancelled:
            → if src already cancelled (system cascade from above): update transit order state
            → if src NOT cancelled (user bypass): block with error
            → if dest-only pair (src_picking_id = False): allow cancel directly

        Dest-only transit pickings (src_picking_id = False) can always have their
        dest cancelled — there is no src to guard against.
        """
        # Block direct stock.picking cancellation — but only for full-pair pickings.
        # Dest-only transit pickings have no src to protect, so their dest can be
        # cancelled freely.
        blocked_direct = self.filtered(lambda tp: not tp.cancel and tp.src_picking_id)
        if blocked_direct:
            raise UserError(
                "Cannot cancel this transfer directly. "
                "Can only be cancelled with the rights to the transit order or transit picking:\n• "
                + '\n• '.join(
                    f"'{tp.src_picking_id.name}' (Transit: '{tp.transit_order_id.name}')"
                    for tp in blocked_direct
                )
            )

        cancelled_ids = set(cancelled_picking_ids)

        dest_cancelled_tps = self.filtered(lambda tp: tp.dest_picking_id.id in cancelled_ids)
        src_cancelled_tps  = self.filtered(lambda tp: tp.src_picking_id and tp.src_picking_id.id in cancelled_ids)

        # ── Guard: dest cancelled while src is still active (user bypass) ─────
        # Skip dest-only pairs (src_picking_id = False) — no src to check.
        blocked = dest_cancelled_tps.filtered(
            lambda tp: tp.src_picking_id and tp.src_picking_id.state != 'cancel'
        )
        if blocked:
            raise UserError(
                "Cannot cancel a destination (incoming) picking of a transit order directly. "
                "Cancel the source picking or use the transit order cancel action instead:\n• "
                + '\n• '.join(
                    f"'{tp.dest_picking_id.name}' (Transit: '{tp.transit_order_id.name}')"
                    for tp in blocked
                )
            )

        # ── Guard: prevent structural mismatch ─────────
        src_cancel_but_dest_done = src_cancelled_tps.filtered(
            lambda tp: tp.src_picking_id and tp.dest_picking_id.state == 'done'
        )
        if src_cancel_but_dest_done:
            raise UserError(
                "Cannot cancel source picking when destination is already done:\n• "
                + '\n• '.join(
                    f"'{tp.src_picking_id.name}' (Transit: '{tp.transit_order_id.name}')"
                    for tp in src_cancel_but_dest_done
                )
            )

        # ── SRC cancelled: cascade cancel dest pickings ───────────────────────
        # Rule 9 will fire again on the dest pickings → hits valid_dest_tps path
        dest_to_cancel = src_cancelled_tps.mapped('dest_picking_id').filtered(
            lambda p: p.state not in ('done', 'cancel')
        )
        if dest_to_cancel:
            dest_to_cancel.sudo().action_cancel()

        # ── Update transit order state for all affected orders ────────────────
        valid_dest_tps  = dest_cancelled_tps - blocked
        affected_orders = (src_cancelled_tps | valid_dest_tps).mapped('transit_order_id')

        for order in affected_orders:
            all_pairs = order.transit_picking_ids
            # Dest-only pairs count as "src done/cancelled" — only check dest side for them
            unsettled = all_pairs.filtered(
                lambda p: (p.src_picking_id and p.src_picking_state not in ('done', 'cancel'))
                          or p.dest_picking_state not in ('done', 'cancel')
            )
            if unsettled:
                continue

            any_done = any(
                (not p.src_picking_id or p.src_picking_state == 'done')
                and p.dest_picking_state == 'done'
                for p in all_pairs
            )
            order.sudo().write({
                'state':     'done' if any_done else 'cancel',
                'date_done': fields.Datetime.now(),
            })

    def automation_handle_picking_deleted(self, deleted_picking_ids):
        """
        Batch-aware: handles on_unlink of any picking linked to a transit pair.
        Fires BEFORE the actual deletion, so all references are still valid.

        Rules:
        - DEST (incoming) being deleted → always block
        - SRC (outgoing) not in cancel state → block (cancel first)
        - SRC (outgoing) in cancel state → post chatter, unlink dest picking
          (transit.picking cascade-deletes itself via src_picking_id FK)
        """
        deleted_ids = set(deleted_picking_ids)

        dest_deleted_tps = self.filtered(lambda tp: tp.dest_picking_id.id in deleted_ids)
        src_deleted_tps  = self.filtered(lambda tp: tp.src_picking_id and tp.src_picking_id.id in deleted_ids)

        # ── Dest-only transit pickings: deleting their dest is permitted ───────
        # (the user is discarding a dest backorder they chose not to process)
        # Simply unlink the orphaned transit.picking — no cascade via FK since src is null.
        dest_deleted_dest_only = dest_deleted_tps.filtered(lambda tp: not tp.src_picking_id)
        dest_deleted_full      = dest_deleted_tps.filtered(lambda tp: tp.src_picking_id)

        if dest_deleted_dest_only:
            dest_deleted_dest_only.sudo().unlink()

        # ── Block: incoming picking of a full-pair deleted directly ───────────
        if dest_deleted_full:
            raise UserError(
                "Cannot delete a destination (incoming) picking of a transit order directly:\n• "
                + '\n• '.join(
                    f"'{tp.dest_picking_id.name}' (Transit: '{tp.transit_order_id.name}')"
                    for tp in dest_deleted_full
                )
            )

        # ── Block: src picking not yet cancelled ──────────────────────────────
        not_cancelled = src_deleted_tps.filtered(
            lambda tp: tp.src_picking_id.state != 'cancel'
        )
        if not_cancelled:
            raise UserError(
                "Cannot delete a picking that is not in 'cancel' state. Cancel it first:\n• "
                + '\n• '.join(
                    f"'{tp.src_picking_id.name}' "
                    f"(state: '{tp.src_picking_id.state}', "
                    f"Transit: '{tp.transit_order_id.name}')"
                    for tp in not_cancelled
                )
            )

        # ── Guard: prevent structural mismatch ─────────
        dest_done_tps = src_deleted_tps.filtered(
            lambda tp: tp.dest_picking_id.state == 'done'
        )
        if dest_done_tps:
            raise UserError(
                "Cannot delete source picking when destination is already done:\n• "
                + '\n• '.join(
                    f"'{tp.src_picking_id.name}' (Transit: '{tp.transit_order_id.name}')"
                    for tp in dest_done_tps
                )
            )

        # ── Valid: post chatter then unlink dest picking ──────────────────────
        # transit.picking itself is NOT manually unlinked here —
        # the cascade on src_picking_id FK handles it automatically.
        user    = self.env.user
        now_str = fields.Datetime.to_string(fields.Datetime.now())

        for tp in src_deleted_tps:
            order     = tp.transit_order_id
            src_name  = tp.src_picking_id.name
            dest_name = tp.dest_picking_id.name

            msg = Markup(
                "<b>🗑️ Transfer Pair Deleted</b><br/>"
                "<b>Deleted by:</b> {user_name}<br/>"
                "<b>Time:</b> {now_str}<br/>"
                "Source picking <b>{src_name}</b> was deleted. "
                "Destination picking <b>{dest_name}</b> has been removed as well."
            ).format(
                user_name=escape(user.name or ''),
                now_str=escape(now_str),
                src_name=escape(src_name or ''),
                dest_name=escape(dest_name or ''),
            )
            try:
                order.message_post(body=msg)
            except Exception as e:
                _logger.warning(
                    "Failed to post deletion message on transit '%s': %s",
                    order.name, str(e),
                )

        dest_pickings = src_deleted_tps.mapped('dest_picking_id').filtered(lambda p: p.exists())
        if dest_pickings:
            dest_pickings.sudo().unlink()
            
    def automation_handle_dest_picking_advance_guard(self, action):
        if action == 'assign':
            # Allow assignment if src is a Rule 6-created placeholder
            # (draft + has backorder_id = it was just created for this backorder pair)
            blocked = self.filtered(
                lambda tp: tp.src_picking_id
                and tp.src_picking_state not in ('done', 'cancel')
                and not (
                    tp.src_picking_id.state == 'draft'
                    and bool(tp.src_picking_id.sudo().backorder_id)
                )
            )
        else:  # 'validate'
            # Always strict — dest cannot be validated until src is done
            blocked = self.filtered(
                lambda tp: tp.src_picking_id
                and tp.src_picking_state not in ('done', 'cancel')
            )

        if not blocked:
            return

        action_label = "marked as ready" if action == 'assign' else "validated"
        error_lines = [
            f"'{tp.dest_picking_id.name if tp.dest_picking_id else '?'}' "
            f"— source '{tp.src_picking_id.name if tp.src_picking_id else '?'}' "
            f"is '{tp.src_picking_state}'"
            for tp in blocked
        ]
        raise UserError(
            f"Destination picking(s) of a transit order cannot be {action_label} "
            f"before the source picking has been validated:\n• " + '\n• '.join(error_lines)
        )