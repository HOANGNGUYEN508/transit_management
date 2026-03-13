from odoo import models, fields, api # type: ignore
from odoo.exceptions import UserError, ValidationError # type: ignore
from odoo.tools import float_compare, float_is_zero # type: ignore
from markupsafe import Markup, escape # type: ignore
import logging

_logger = logging.getLogger(__name__)


class T4tekTransitPicking(models.Model):
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
      product_id, only the first is matched. Consolidate duplicates before use.
    """
    _name = 't4tek.transit.picking'
    _description = 'T4tek Transit Picking Pair'
    _rec_name = 'display_name'
    _order = 'create_date desc, id desc'
    
    # =========================================================================
    # FIELDS
    # =========================================================================
    display_name = fields.Char(compute="_compute_display_name", string="Display name", store=False, readonly=True)

    t4tek_transit_order_id = fields.Many2one(
        't4tek.transit.order',
        string='Transit Order',
        required=True,
        ondelete='cascade',
        index=True,
    )

    transit_date_done = fields.Datetime(
        'Transit Effective Date', 
        related="t4tek_transit_order_id.date_done",
        help="Date at which the transit order have been processed or canceled",
    )
    
    src_picking_id = fields.Many2one(
        'stock.picking',
        string="Source Picking",
        required=True,
        ondelete='cascade',
        index=True,
    )

    src_date_done = fields.Datetime(
        'Source Effective Date', 
        related="src_picking_id.date_done",
        help="Date at which the source picking have been processed or canceled",
    )
    
    dest_picking_id = fields.Many2one(
        'stock.picking',
        string="Destination Picking",
        required=True,
        ondelete='cascade',
        index=True,
    )

    dest_date_done = fields.Datetime(
        'Destination Effective Date', 
        related="dest_picking_id.date_done",
        help="Date at which the destination picking have been processed or canceled",
    )
    
    state = fields.Selection(
        related='t4tek_transit_order_id.state',
        string='Transit State',
        store=True,
        readonly=True
    )

    scheduled_date = fields.Datetime(
        'Scheduled Date', 
        related='t4tek_transit_order_id.scheduled_date', 
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
        related='t4tek_transit_order_id.company_id',
        store=True,
        readonly=True
    )
    
    src_company_id = fields.Many2one(
        'res.company',
        string='Source Company',
        related='t4tek_transit_order_id.src_company_id',
        store=True,
        readonly=True
    )
    
    dest_company_id = fields.Many2one(
        'res.company',
        string='Destination Company',
        related='t4tek_transit_order_id.dest_company_id',
        store=True,
        readonly=True
    )
    
    transit_location_id = fields.Many2one(
        'stock.location',
        string='Transit Location',
        related='t4tek_transit_order_id.transit_location_id',
        store=True,
        readonly=True
    )

    is_late = fields.Boolean(compute='_compute_picking_state', store=False)
    is_today = fields.Boolean(compute='_compute_picking_state', store=False)
    is_very_late = fields.Boolean(compute='_compute_picking_state', store=False)
    has_mismatch = fields.Boolean(
        string='Has Mismatch',
        compute='_compute_picking_state',
        store=False,
    )
    comparison_data = fields.Json(compute='_compute_picking_state', store=False)

    # =========================================================================
    # WARNINGS
    # =========================================================================
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
        't4tek_transit_order_id.line_ids.product_id',
        't4tek_transit_order_id.line_ids.product_uom_qty',
        't4tek_transit_order_id.line_ids.product_uom',
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

        dest_done           = dest.state == "done"
        order_lines_by_prod = {l.product_id.id: l for l in self.t4tek_transit_order_id.line_ids}
        src_by_prod         = {m.product_id.id: m for m in src.move_ids}
        dest_by_prod        = {m.product_id.id: m for m in dest.move_ids}

        lines = []
        for prod_id in src_by_prod.keys() | dest_by_prod.keys():
            src_move   = src_by_prod.get(prod_id)
            dest_move  = dest_by_prod.get(prod_id)
            order_line = order_lines_by_prod.get(prod_id)
            ref_move   = src_move or dest_move

            # ── Expected quantity ─────────────────────────────────────────────
            if order_line and order_line.product_uom:
                expected_qty = order_line.product_uom_qty
                expected_uom = order_line.product_uom.name
            elif ref_move and ref_move.product_uom:
                expected_qty = ref_move.product_uom_qty
                expected_uom = ref_move.product_uom.name
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
        # If the sets of lot/SN names differ in any way, report lot_mismatch and
        # stop. The per-lot breakdown in the dialog and mismatch summary will
        # explain exactly which lots are missing or unexpected — a separate
        # qty_mismatch badge adds no extra information and creates visual noise.
        src_lot_names  = {entry["lot"] for entry in src_lines}
        dest_lot_names = {entry["lot"] for entry in dest_lines}
        if src_lot_names != dest_lot_names:
            return ["lot_mismatch"]

        # ── Level 3: Quantity accuracy ────────────────────────────────────────
        # Lots are identical on both sides — now check if quantities agree.
        # Check both per-lot AND overall total (UoM-converted) for robustness.
        dest_uom = dest_move.product_uom
        src_uom  = src_move.product_uom

        # Per-lot qty check
        src_map  = {e["lot"]: e["qty"] for e in src_lines}
        dest_map = {e["lot"]: e["qty"] for e in dest_lines}
        rounding = dest_uom.rounding if dest_uom else 0.01

        per_lot_ok = all(
            float_compare(src_map[lot], dest_map[lot], precision_rounding=rounding) == 0
            for lot in src_lot_names
        )

        # Overall qty check (handles products with no lot tracking)
        if dest_uom and src_uom:
            src_qty_converted = src_uom._compute_quantity(
                src_move.quantity, dest_uom, rounding_method='HALF-UP'
            )
            overall_ok = float_compare(
                src_qty_converted, dest_move.quantity,
                precision_rounding=rounding
            ) == 0
        else:
            overall_ok = False  # can't compare without UoM — flag it

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

    # =========================================================================
    # CONSTRAINTS
    # =========================================================================
    
    @api.constrains('src_picking_id', 'dest_picking_id', 'transit_location_id')
    def _check_location_consistency(self):
        """Validate location consistency between pickings"""
        for record in self:
            if not record.transit_location_id:
                raise ValidationError(
                    f"Transit Pair '{record.display_name}': No transit location found"
                )
            
            if record.src_picking_id.location_dest_id != record.transit_location_id:
                raise ValidationError(
                    f"Transit Pair '{record.display_name}': "
                    f"Source picking destination must be transit location '{record.transit_location_id.name}' "
                    f"(currently: '{record.src_picking_id.location_dest_id.name}')"
                )
            
            if record.dest_picking_id.location_id != record.transit_location_id:
                raise ValidationError(
                    f"Transit Pair '{record.display_name}': "
                    f"Destination picking source must be transit location '{record.transit_location_id.name}' "
                    f"(currently: '{record.dest_picking_id.location_id.name}')"
                )
    
    @api.constrains('src_picking_id', 'dest_picking_id')
    def _check_picking_types(self):
        """Validate picking types"""
        for record in self:
            if record.src_picking_id.picking_type_id.code != 'outgoing':
                raise ValidationError(
                    f"Transit Pair '{record.display_name}': "
                    f"Source picking must use 'outgoing' type"
                )
            
            if record.dest_picking_id.picking_type_id.code != 'incoming':
                raise ValidationError(
                    f"Transit Pair '{record.display_name}': "
                    f"Destination picking must use 'incoming' type"
                )
    
    # =========================================================================
    # AUTOMATION WRAPPER FUNCTIONS
    # =========================================================================

    def automation_handle_src_picking_done(self):
        """
        Batch-aware: handle SRC picking validation for one or more transit picking pairs.

          1. Sync SRC move quantities → DEST moves        (batch call)
          2. Propagate SRC move lines → DEST pickings     (batch call)
          3. Set transit order state  → in_progress       (batched write)
          4. Create + sync DEST backorders for any SRC backorders  (batch call)
        """
        invalid = self.filtered(lambda tp: tp.state not in ('assigned', 'in_progress'))
        if invalid:
            msgs = [
                f"Transit '{tp.t4tek_transit_order_id.name}' in state '{tp.state}' "
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
            valid._propagate_src_move_lines_to_dest()
        except Exception as e:
            raise UserError(f"Failed to propagate SRC move lines: {str(e)}")

        valid.mapped('t4tek_transit_order_id').sudo().write({'state': 'in_progress'})

        _logger.info(
            "SRC pickings %s validated → %d transit order(s) set to in_progress",
            valid.mapped('src_picking_id.name'),
            len(valid.mapped('t4tek_transit_order_id')),
        )

        parent_tp_ids = []
        src_bo_ids    = []

        for tp in valid:
            open_backorders = tp.src_picking_id.sudo().backorder_ids.filtered(
                lambda p: p.state not in ('done', 'cancel')
            )
            for src_backorder in open_backorders:
                already_exists = self.env['t4tek.transit.picking'].search([
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
            parent_tps = self.env['t4tek.transit.picking'].browse(parent_tp_ids)
            src_bos    = self.env['stock.picking'].browse(src_bo_ids)
            try:
                parent_tps._create_dest_backorders_from_src(src_bos)
            except Exception as e:
                raise UserError(f"SRC backorder creation failed: {str(e)}")

            new_transit_pickings = self.env['t4tek.transit.picking'].search([
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

    def automation_handle_src_backorder_ready(self, new_src_backorders):
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

            existing = self.env['t4tek.transit.picking'].search([
                ('src_picking_id', '=', new_backorder.id)
            ], limit=1)
            if existing:
                _logger.info(
                    "SRC backorder '%s' already has a transit picking pair, skipping",
                    new_backorder.name,
                )
                continue

            parent_tp_ids.append(parent_transit.id)
            src_bo_ids.append(new_backorder.id)

        if not parent_tp_ids:
            return

        parent_tps = self.env['t4tek.transit.picking'].browse(parent_tp_ids)
        src_bos    = self.env['stock.picking'].browse(src_bo_ids)

        try:
            parent_tps._create_dest_backorders_from_src(src_bos)
        except Exception as e:
            raise UserError(f"Failed to create dest backorders: {str(e)}")

        new_transit_pickings = self.env['t4tek.transit.picking'].search([
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

    def automation_handle_dest_picking_done(self):
        """
        Batch-aware: handle DEST picking validation for one or more transit picking pairs.

          1. Guard: SRC picking must already be done
          2. Populate transit line quantities from DEST move actuals  (batch call)
          3. If the DEST picking has no backorders, mark transit order done (batched write)
        """
        src_not_done = self.filtered(lambda tp: tp.src_picking_state != 'done')
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

        orders_to_done = self.env['t4tek.transit.order']
        orders_still_in_progress = self.env['t4tek.transit.order']

        for tp in self:
            has_backorders = bool(tp.dest_picking_id.backorder_ids)
            transit_order = tp.t4tek_transit_order_id
            if has_backorders:
                orders_still_in_progress |= transit_order
                _logger.info(
                    "Intermediate DEST picking '%s' validated, "
                    "transit '%s' remains in_progress (has backorders)",
                    tp.dest_picking_id.name, transit_order.name,
                )
            else:
                orders_to_done |= transit_order
                _logger.info(
                    "Final DEST picking '%s' validated, transit '%s' → done",
                    tp.dest_picking_id.name, transit_order.name,
                )

        truly_done = orders_to_done - orders_still_in_progress

        if truly_done:
            truly_done.sudo().write({
                'state': 'done',
                'date_done': fields.Datetime.now(),
            })

    # =========================================================================
    # CORE SYNCHRONIZATION METHODS
    # =========================================================================

    def _sync_src_moves_to_dest_moves(self):
        """
        Sync SRC picking moves → DEST picking moves, matching by product_id.

        For each SRC move, find the DEST move with the same product_id and update
        its quantity. Creates new DEST moves for unmatched SRC products and deletes
        DEST moves whose product no longer exists in SRC.
        """
        if not self:
            return

        all_moves_to_delete = self.env['stock.move']
        all_create_vals     = []
        all_update_buckets  = {}

        for transit_picking in self:
            src_picking  = transit_picking.src_picking_id
            dest_picking = transit_picking.dest_picking_id.sudo()
            is_backorder = bool(src_picking.backorder_id)
            new_state    = 'draft' if is_backorder else 'assigned'

            # Match dest moves by product_id (first occurrence wins)
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
                        'name': src_move.name or src_move.product_id.display_name,
                        'product_id': src_move.product_id.id,
                        'product_uom': src_move.product_uom.id,
                        'picking_id': dest_picking.id,
                        'company_id': dest_picking.company_id.id,
                        'state': new_state,
                        'product_uom_qty': planned_qty if is_backorder else actual_qty,
                        '_is_backorder': is_backorder,
                    }
                    if not is_backorder:
                        vals['quantity'] = actual_qty
                    all_create_vals.append(vals)

            # Delete dest moves whose product no longer exists in src
            for dest_move in dest_picking.move_ids:
                if dest_move.id not in processed_dest_ids:
                    all_moves_to_delete |= dest_move

        if all_moves_to_delete:
            all_moves_to_delete.unlink()
            _logger.info(f"Deleted {len(all_moves_to_delete)} unmatched dest moves")

        if all_update_buckets:
            total = 0
            for (is_backorder, vals_frozen), move_ids in all_update_buckets.items():
                write_ctx = {'skip_auto_assign': True} if is_backorder else {}
                self.env['stock.move'].sudo().with_context(**write_ctx).browse(move_ids).write(
                    dict(vals_frozen)
                )
                total += len(move_ids)
            _logger.info(f"Updated {total} dest moves in {len(all_update_buckets)} batch(es)")

        if all_create_vals:
            backorder_creates     = []
            non_backorder_creates = []
            for vals in all_create_vals:
                is_bo = vals.pop('_is_backorder')
                (backorder_creates if is_bo else non_backorder_creates).append(vals)

            if non_backorder_creates:
                self.env['stock.move'].sudo().create(non_backorder_creates)
                _logger.info(f"Created {len(non_backorder_creates)} dest moves (state=assigned)")

            if backorder_creates:
                self.env['stock.move'].sudo().with_context(skip_auto_assign=True).create(
                    backorder_creates
                )
                _logger.info(f"Created {len(backorder_creates)} dest moves (state=draft)")
    
    def _propagate_src_move_lines_to_dest(self):
        """
        Batch-aware: copy SRC move lines into DEST pickings, resolving/creating lots cross-company.

        Move lines are matched to their DEST move by product_id.
        """
        if not self:
            return

        # ── 1. Validate all location pairs up front ────────────────────────
        errors = []
        for tp in self:
            src  = tp.src_picking_id
            dest = tp.dest_picking_id.sudo()
            if src.location_dest_id.id != dest.location_id.id:
                errors.append(
                    f"Transit '{tp.t4tek_transit_order_id.name}': location mismatch — "
                    f"SRC.location_dest_id ({src.location_dest_id.name}) != "
                    f"DEST.location_id ({dest.location_id.name})"
                )
        if errors:
            raise ValidationError('\n'.join(errors))

        # ── 2. Batch-delete all existing dest move lines ───────────────────
        all_dest_move_lines = self.env['stock.move.line']
        for tp in self:
            all_dest_move_lines |= tp.dest_picking_id.sudo().move_line_ids
        if all_dest_move_lines:
            all_dest_move_lines.unlink()

        # ── 3. Collect all lots needed across every pair ───────────────────
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

        # ── 4. Build all move line create-vals in one pass ─────────────────
        _STRIP_FIELDS = frozenset([
            'location_id', 'location_dest_id', 'result_package_id', 'package_id',
            'id', 'create_date', 'create_uid', 'write_date', 'write_uid', '__last_update',
        ])
        create_vals_list = []
        total_src_lines  = 0

        for tp in self:
            src_picking  = tp.src_picking_id
            dest_picking = tp.dest_picking_id.sudo()

            # Match DEST moves by product_id
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

        # ── 5. Single create for every move line across all pairs ──────────
        if create_vals_list:
            self.env['stock.move.line'].sudo().create(create_vals_list)

        _logger.info(
            "Propagated %d move line(s) across %d transit picking pair(s)",
            total_src_lines, len(self),
        )
    
    def _create_dest_backorders_from_src(self, src_backorders):
        """
        Batch: create DEST backorder pickings and transit picking pairs.
        self[i] is the parent transit picking for src_backorders[i].
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
                't4tek_transit_order_id': parent_tp.t4tek_transit_order_id.id,
                'src_picking_id':         src_bo.id,
                'dest_picking_id':        dest_bo.id,
            })

        self.env['t4tek.transit.picking'].sudo().create(transit_picking_vals_list)

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

        Lines are matched to DEST moves by product_id.
        Each line accumulates quantities from all transit picking pairs for
        the same transit order (to handle backorder chains correctly).
        """
        # Accumulate deltas: line_id → (line_record, total_qty_to_add)
        line_deltas = {}

        for transit_picking in self:
            dest_picking = transit_picking.dest_picking_id
            if dest_picking.state != 'done':
                continue

            # Build a product→line map from the transit order
            order_lines_by_product = {
                line.product_id.id: line
                for line in transit_picking.t4tek_transit_order_id.line_ids
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
            line.write({'quantity': new_total})
            _logger.info(
                "Transit line %d (%s): +%.4f → new total %.4f",
                line_id, line.product_id.name, delta, new_total,
            )