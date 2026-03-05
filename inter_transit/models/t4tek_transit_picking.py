from odoo import models, fields, api # type: ignore
from odoo.exceptions import UserError, ValidationError # type: ignore
from odoo.tools import float_compare, float_is_zero # type: ignore
from markupsafe import Markup, escape # type: ignore
import logging

_logger = logging.getLogger(__name__)


class T4tekTransitPicking(models.Model):
    """
    Transit Picking Pair Management
    
    Note: One transit order can have multiple transit pickings record incase of there is backorder.
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
    
    # Related fields
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

    is_late = fields.Boolean(compute='_compute_picking_warnings', store=False)
    is_today = fields.Boolean(compute='_compute_picking_warnings', store=False)
    is_very_late = fields.Boolean(compute='_compute_picking_warnings', store=False)
    has_mismatch = fields.Boolean(
        string='Has Mismatch',
        compute='_compute_mismatch_data',
        store=False,
    )
    comparison_html = fields.Html(
        string='Move Comparison',
        compute='_compute_mismatch_data',
        store=False,
        sanitize=False,
    )

    @api.depends(
        'src_picking_id.move_ids.quantity',
        'src_picking_id.move_ids.product_uom_qty',
        'src_picking_id.move_ids.product_uom',
        'src_picking_id.move_ids.t4tek_transit_line_id',
        'src_picking_id.move_ids.product_id',
        'src_picking_id.move_line_ids.quantity',
        'src_picking_id.move_line_ids.lot_id',
        'src_picking_id.move_line_ids.lot_name',
        'dest_picking_id.move_ids.quantity',
        'dest_picking_id.move_ids.product_uom_qty',
        'dest_picking_id.move_ids.product_uom',
        'dest_picking_id.move_ids.t4tek_transit_line_id',
        'dest_picking_id.move_ids.product_id',
        'dest_picking_id.move_line_ids.quantity',
        'dest_picking_id.move_line_ids.lot_id',
        'dest_picking_id.move_line_ids.lot_name',
    )
    def _compute_mismatch_data(self):
        """Compute has_mismatch and generate unified comparison_html always."""
        for record in self:
            src = record.src_picking_id
            dest = record.dest_picking_id

            if not src or not dest:
                record.has_mismatch = False
                record.comparison_html = Markup(
                    '<p class="text-muted fst-italic p-3">Missing source or destination picking.</p>'
                )
                continue

            # ── Build data structures ────────────────────────────────────────────
            lines_data = {}
            src_unlinked = []
            dest_unlinked = []

            for move in src.move_ids:
                line = move.t4tek_transit_line_id
                if line:
                    lines_data.setdefault(line.id, {
                        'line': line,
                        'src_move': move,
                        'dest_move': None,
                        'src_lots': {},
                        'dest_lots': {},
                    })
                else:
                    src_unlinked.append(move)

            for move in dest.move_ids:
                line = move.t4tek_transit_line_id
                if line:
                    data = lines_data.setdefault(line.id, {
                        'line': line,
                        'src_move': None,
                        'dest_move': move,
                        'src_lots': {},
                        'dest_lots': {},
                    })
                    data['dest_move'] = move
                else:
                    dest_unlinked.append(move)

            for data in lines_data.values():
                if data['src_move']:
                    data['src_lots'] = self._get_lot_map(data['src_move'])
                if data['dest_move']:
                    data['dest_lots'] = self._get_lot_map(data['dest_move'])

            # ── Compute mismatch flag ─────────────────────────────────────────────
            mismatch_found = self._check_mismatch_from_data(
                lines_data, src_unlinked, dest_unlinked, dest_state=dest.state
            )
            record.has_mismatch = mismatch_found

            # ── Always generate HTML ──────────────────────────────────────────────
            html = self._build_comparison_html(
                lines_data, src_unlinked, dest_unlinked, src, dest
            )
            record.comparison_html = Markup(html)

    def _get_lot_map(self, move):
        """Return {(product_id, lot_name): quantity} for a stock.move."""
        lot_map = {}
        for ml in move.move_line_ids:
            lot = ml.lot_id.name if ml.lot_id else (ml.lot_name or '')
            key = (ml.product_id.id, lot)
            lot_map[key] = lot_map.get(key, 0.0) + ml.quantity
        return lot_map

    def _lot_names_differ(self, src_lots, dest_lots):
        """
        Compare only the lot NAME keys, ignoring quantities.

        Returns True only when the set of (product_id, lot_name) tuples differs.
        A pure quantity difference on the same lots is NOT a lot mismatch —
        it is already captured by the quantity comparison.
        """
        return set(src_lots.keys()) != set(dest_lots.keys())

    def _check_mismatch_from_data(self, lines_data, src_unlinked, dest_unlinked, dest_state):
        """Determine if any mismatch exists."""
        # 1. Unlinked moves on either side
        if src_unlinked or dest_unlinked:
            return True

        # 2. Missing move in a pair
        for data in lines_data.values():
            if not data['src_move'] or not data['dest_move']:
                return True

        # 3. Product / quantity / lot-name mismatches (only meaningful when dest is done)
        if dest_state == 'done':
            for data in lines_data.values():
                src_m = data['src_move']
                dest_m = data['dest_move']
                if not src_m or not dest_m:
                    continue

                if src_m.product_id != dest_m.product_id:
                    return True

                rounding = dest_m.product_uom.rounding
                src_done_in_dest_uom = src_m.product_uom._compute_quantity(
                    src_m.quantity, dest_m.product_uom, rounding_method='HALF-UP'
                )
                if float_compare(src_done_in_dest_uom, dest_m.quantity, precision_rounding=rounding) != 0:
                    return True

                # ── FIX: compare lot NAMES only, not quantities ──────────────────
                if self._lot_names_differ(data['src_lots'], data['dest_lots']):
                    return True

        return False

    # ── Styling constants ──────────────────────────────────────────────────────
    _TH_STYLE  = "padding:10px 12px; font-weight:600; color:#212529; border-bottom:2px solid #dee2e6; background:#f8f9fa; white-space:nowrap;"
    _TD_STYLE  = "padding:8px 12px; border-bottom:1px solid #dee2e6; vertical-align:top;"
    _TD_ERR    = "padding:8px 12px; border-bottom:1px solid #dee2e6; vertical-align:top; background:#fff1f0;"
    _TD_WARN   = "padding:8px 12px; border-bottom:1px solid #dee2e6; vertical-align:top; background:#fff8e1;"

    def _badge(self, label, color):
        """Render a small Bootstrap-like badge."""
        colors = {
            'success': ('#198754', '#d1e7dd'),
            'danger':  ('#dc3545', '#f8d7da'),
            'warning': ('#856404', '#fff3cd'),
            'muted':   ('#6c757d', '#e9ecef'),
            'info':    ('#0c63e4', '#cfe2ff'),
        }
        fg, bg = colors.get(color, ('#333', '#eee'))
        return (
            f'<span style="display:inline-block; padding:2px 8px; border-radius:10px; '
            f'font-size:0.78em; font-weight:600; color:{fg}; background:{bg};">'
            f'{label}</span>'
        )

    def _build_comparison_html(self, lines_data, src_unlinked, dest_unlinked, src_picking, dest_picking):
        """
        Generate a unified comparison table styled like Odoo's list view.

        Always rendered — content adapts to whether mismatches exist.
        """
        dest_done = dest_picking.state == 'done'

        html = [
            '<div style="border:1px solid #dee2e6; border-radius:6px; overflow:hidden;">',
            '<table style="width:100%; border-collapse:collapse; font-size:0.875rem;">',
            '<thead><tr>',
        ]

        headers = ['Product', 'Expected', 'Source Done', 'Destination Done', 'Source Lots', 'Destination Lots', 'Status']
        for h in headers:
            html.append(f'<th style="{self._TH_STYLE}">{h}</th>')
        html.append('</tr></thead><tbody>')

        # ── Rows for transit-line-linked pairs ─────────────────────────────────
        for data in lines_data.values():
            line      = data['line']
            src_move  = data['src_move']
            dest_move = data['dest_move']
            src_lots  = data['src_lots']
            dest_lots = data['dest_lots']

            product      = line.product_id
            product_name = escape(product.display_name) if product else '—'

            expected_qty = line.product_uom_qty or (src_move.product_uom_qty if src_move else 0)
            expected_uom = (line.product_uom.name if line.product_uom else
                            (src_move.product_uom.name if src_move else ''))

            src_qty_str  = (f"{src_move.quantity:.2f} {escape(src_move.product_uom.name)}"
                            if src_move else '—')
            dest_qty_str = (f"{dest_move.quantity:.2f} {escape(dest_move.product_uom.name)}"
                            if dest_move else '—')

            # ── Determine per-row issues ───────────────────────────────────────
            issues      = []
            qty_err     = False
            lot_err     = False
            missing_err = False

            if not src_move or not dest_move:
                missing_err = True
                issues.append(self._badge('Missing move', 'warning'))

            elif src_move.product_id != dest_move.product_id:
                issues.append(self._badge('Product mismatch', 'danger'))

            else:
                if dest_done:
                    rounding = dest_move.product_uom.rounding
                    src_done_in_dest_uom = src_move.product_uom._compute_quantity(
                        src_move.quantity, dest_move.product_uom, rounding_method='HALF-UP'
                    )
                    if float_compare(src_done_in_dest_uom, dest_move.quantity,
                                     precision_rounding=rounding) != 0:
                        qty_err = True
                        issues.append(self._badge('Qty mismatch', 'danger'))

                    # ── FIX: only flag lot mismatch when lot NAMES differ ──────
                    if self._lot_names_differ(src_lots, dest_lots):
                        lot_err = True
                        issues.append(self._badge('Lot mismatch', 'danger'))

                else:
                    issues.append(self._badge('Pending receipt', 'muted'))

            if not issues:
                issues.append(self._badge('✓ Match', 'success'))

            # ── Cell styles ───────────────────────────────────────────────────
            base   = self._TD_ERR if (qty_err or lot_err or missing_err) else self._TD_STYLE
            qty_td = self._TD_ERR if qty_err else base
            lot_td = self._TD_ERR if lot_err else base

            src_lots_html  = self._format_lots(src_lots)
            dest_lots_html = self._format_lots(dest_lots)

            html.append(
                f'<tr>'
                f'<td style="{base}">{product_name}</td>'
                f'<td style="{base}">{expected_qty:.2f}&nbsp;{escape(expected_uom)}</td>'
                f'<td style="{qty_td}">{src_qty_str}</td>'
                f'<td style="{qty_td}">{dest_qty_str}</td>'
                f'<td style="{lot_td}">{src_lots_html}</td>'
                f'<td style="{lot_td}">{dest_lots_html}</td>'
                f'<td style="{base}">{"&nbsp;".join(issues)}</td>'
                f'</tr>'
            )

        # ── Unlinked source moves ──────────────────────────────────────────────
        for move in src_unlinked:
            product_name = escape(move.product_id.display_name)
            src_lots_html = self._format_lots(self._get_lot_map(move))
            html.append(
                f'<tr>'
                f'<td style="{self._TD_WARN}">{product_name}</td>'
                f'<td style="{self._TD_WARN}">{move.product_uom_qty:.2f}&nbsp;{escape(move.product_uom.name)}</td>'
                f'<td style="{self._TD_WARN}">{move.quantity:.2f}&nbsp;{escape(move.product_uom.name)}</td>'
                f'<td style="{self._TD_WARN}">—</td>'
                f'<td style="{self._TD_WARN}">{src_lots_html}</td>'
                f'<td style="{self._TD_WARN}">—</td>'
                f'<td style="{self._TD_WARN}">{self._badge("Src only", "warning")}</td>'
                f'</tr>'
            )

        # ── Unlinked destination moves ─────────────────────────────────────────
        for move in dest_unlinked:
            product_name  = escape(move.product_id.display_name)
            dest_lots_html = self._format_lots(self._get_lot_map(move))
            html.append(
                f'<tr>'
                f'<td style="{self._TD_WARN}">{product_name}</td>'
                f'<td style="{self._TD_WARN}">{move.product_uom_qty:.2f}&nbsp;{escape(move.product_uom.name)}</td>'
                f'<td style="{self._TD_WARN}">—</td>'
                f'<td style="{self._TD_WARN}">{move.quantity:.2f}&nbsp;{escape(move.product_uom.name)}</td>'
                f'<td style="{self._TD_WARN}">—</td>'
                f'<td style="{self._TD_WARN}">{dest_lots_html}</td>'
                f'<td style="{self._TD_WARN}">{self._badge("Dest only", "warning")}</td>'
                f'</tr>'
            )

        # ── Empty state ────────────────────────────────────────────────────────
        if not lines_data and not src_unlinked and not dest_unlinked:
            html.append(
                f'<tr><td colspan="7" style="{self._TD_STYLE} text-align:center; color:#6c757d; font-style:italic;">'
                f'No moves found.</td></tr>'
            )

        html.append('</tbody></table></div>')
        return ''.join(html)

    def _format_lots(self, lot_map):
        """Convert lot_map to an HTML string."""
        if not lot_map:
            return '<span style="color:#adb5bd;">—</span>'
        items = []
        for (product_id, lot_name), qty in lot_map.items():
            lot_display = escape(lot_name) if lot_name else '<em>No lot</em>'
            items.append(f'<span>{lot_display}:&nbsp;<strong>{qty:.2f}</strong></span>')
        return '<br/>'.join(items)

    # =========================================================================
    # DATE WARNINGS
    # =========================================================================
    @api.depends(
        'state', 'scheduled_date',
        'src_picking_state', 'dest_picking_state',
    )
    def _compute_picking_warnings(self):
        now = fields.Datetime.now()
        today = now.date()
        very_late_threshold = 3

        for rec in self:
            terminal = rec.state in ('done', 'cancel')
            sched = rec.scheduled_date

            rec.is_today     = bool(sched and sched.date() == today and not terminal)
            rec.is_late      = bool(sched and sched < now and not terminal)
            rec.is_very_late = bool(sched and (now - sched).days >= very_late_threshold and not terminal)

    @api.depends('t4tek_transit_order_id')
    def _compute_display_name(self):
        for record in self:
            if record.t4tek_transit_order_id:
                record.display_name = f"Pickings of {record.t4tek_transit_order_id.name}"
    
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
        self.ensure_one()

        if self.state not in ['assigned', 'in_progress']:
            raise ValidationError(
                f"Transit '{self.t4tek_transit_order_id.name}' in state '{self.state}' "
                f"cannot process SRC picking validation."
            )

        try:
            self._sync_src_moves_to_dest_moves()
            self._propagate_src_move_lines_to_dest()

            self.t4tek_transit_order_id.sudo().write({'state': 'in_progress'})

            _logger.info(
                f"SRC picking '{self.src_picking_id.name}' validated, "
                f"transit '{self.t4tek_transit_order_id.name}' → in_progress"
            )

            src_backorders = self.src_picking_id.sudo().backorder_ids.filtered(
                lambda p: p.state not in ('done', 'cancel')
            )
            for src_backorder in src_backorders:
                existing = self.env['t4tek.transit.picking'].search([
                    ('src_picking_id', '=', src_backorder.id)
                ], limit=1)
                if existing:
                    _logger.info(
                        f"SRC backorder '{src_backorder.name}' already has a transit pair "
                        f"(handled by Rule 2), skipping"
                    )
                    continue

                try:
                    dest_backorder = self._create_dest_backorder_from_src(src_backorder)
                    if dest_backorder:
                        new_tp = self.env['t4tek.transit.picking'].search([
                            ('src_picking_id', '=', src_backorder.id),
                            ('dest_picking_id', '=', dest_backorder.id)
                        ], limit=1)
                        if new_tp:
                            new_tp._sync_src_moves_to_dest_moves()
                            _logger.info(
                                f"SRC backorder '{src_backorder.name}' handled from Rule 1 "
                                f"(state={src_backorder.state}), "
                                f"dest backorder '{dest_backorder.name}' created and synced"
                            )
                        else:
                            _logger.error(
                                f"Transit pair not found after creating dest backorder "
                                f"for src backorder '{src_backorder.name}'"
                            )
                except Exception as e:
                    _logger.error(
                        f"Failed to handle src backorder '{src_backorder.name}': {str(e)}",
                        exc_info=True
                    )
                    raise

        except Exception as e:
            _logger.error(
                f"Error handling SRC picking validation for {self.src_picking_id.name}: {str(e)}",
                exc_info=True
            )
            raise UserError(
                f"Failed to process Source picking validation for "
                f"transit '{self.t4tek_transit_order_id.name}': {str(e)}"
            )
        
    def automation_handle_src_backorder_ready(self, new_src_backorders):
        parent_by_src_id = {tp.src_picking_id.id: tp for tp in self}
        errors = []

        for new_backorder in new_src_backorders:
            parent_transit = parent_by_src_id.get(new_backorder.backorder_id.id)
            if not parent_transit:
                continue

            existing = self.env['t4tek.transit.picking'].search([
                ('src_picking_id', '=', new_backorder.id)
            ], limit=1)
            if existing:
                _logger.info(
                    f"SRC backorder '{new_backorder.name}' already has a transit picking pair, skipping"
                )
                continue

            try:
                dest_backorder = parent_transit._create_dest_backorder_from_src(new_backorder)

                if dest_backorder:
                    backorder_transit_picking = self.env['t4tek.transit.picking'].search([
                        ('src_picking_id', '=', new_backorder.id),
                        ('dest_picking_id', '=', dest_backorder.id)
                    ], limit=1)

                    if backorder_transit_picking:
                        backorder_transit_picking._sync_src_moves_to_dest_moves()
                        _logger.info(
                            f"SRC backorder '{new_backorder.name}' ready, "
                            f"dest backorder '{dest_backorder.name}' created and synced"
                        )
                    else:
                        errors.append(
                            f"Backorder picking '{new_backorder.name}': "
                            f"transit picking pair not found after creation"
                        )

            except Exception as e:
                _logger.error(
                    f"Error handling SRC backorder ready for '{new_backorder.name}': {str(e)}",
                    exc_info=True
                )
                errors.append(f"Picking '{new_backorder.name}': {str(e)}")

        if errors:
            raise UserError(
                "Failed to process SRC backorder(s):\n• " + "\n• ".join(errors)
            )
        
    def automation_handle_dest_picking_done(self):
        if self.src_picking_state != 'done':
            raise ValidationError(
                f"Cannot validate Destination picking '{self.dest_picking_id.name}'. "
                f"Source picking '{self.src_picking_id.name}' must be validated first "
                f"(current state: {self.src_picking_state})"
            )
        
        try:
            self._populate_transit_quantities()
            
            has_backorders = bool(self.dest_picking_id.backorder_ids)
            
            if has_backorders:
                _logger.info(
                    f"Intermediate DEST picking '{self.dest_picking_id.name}' validated, "
                    f"transit '{self.t4tek_transit_order_id.name}' remains in_progress (has backorders)"
                )
            else:
                self.t4tek_transit_order_id.sudo().write({
                    'state': 'done',
                    'date_done': fields.Datetime.now()
                })
                
                _logger.info(
                    f"Final DEST picking '{self.dest_picking_id.name}' validated, "
                    f"transit '{self.t4tek_transit_order_id.name}' → done"
                )
            
        except Exception as e:
            _logger.error(
                f"Error handling DEST picking validation for {self.dest_picking_id.name}: {str(e)}",
                exc_info=True
            )
            raise UserError(
                f"Failed to process Destination picking validation for "
                f"transit '{self.t4tek_transit_order_id.name}': {str(e)}"
            )

    # =========================================================================
    # CORE SYNCHRONIZATION METHODS
    # =========================================================================
    def _sync_src_moves_to_dest_moves(self):
        if not self:
            return

        all_moves_to_delete  = self.env['stock.move']
        all_create_vals      = []
        all_update_buckets   = {}

        for transit_picking in self:
            src_picking  = transit_picking.src_picking_id
            dest_picking = transit_picking.dest_picking_id.sudo()
            is_backorder = bool(src_picking.backorder_id)
            new_state    = 'draft' if is_backorder else 'assigned'

            dest_by_line = {
                m.t4tek_transit_line_id.id: m
                for m in dest_picking.move_ids
                if m.t4tek_transit_line_id
            }

            processed_dest_ids = set()

            for src_move in src_picking.move_ids:
                line_id   = src_move.t4tek_transit_line_id.id if src_move.t4tek_transit_line_id else None
                dest_move = dest_by_line.get(line_id) if line_id else None

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

                    if line_id and not dest_move.t4tek_transit_line_id:
                        update_vals['t4tek_transit_line_id'] = line_id

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
                        't4tek_transit_line_id': line_id or False,
                        'product_uom_qty': planned_qty if is_backorder else actual_qty,
                        '_is_backorder': is_backorder,
                    }
                    if not is_backorder:
                        vals['quantity'] = actual_qty
                    all_create_vals.append(vals)

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
            backorder_creates    = []
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
        self.ensure_one()
        
        src_picking = self.src_picking_id
        dest_picking = self.dest_picking_id.sudo()
        
        if src_picking.location_dest_id.id != dest_picking.location_id.id:
            raise ValidationError(
                f"Location mismatch! "
                f"Source.location_dest_id ({src_picking.location_dest_id.name}) != "
                f"Dest.location_id ({dest_picking.location_id.name})"
            )
        
        if dest_picking.move_line_ids:
            dest_picking.move_line_ids.unlink()
        
        in_moves_by_line = {
            move.t4tek_transit_line_id.id: move
            for move in dest_picking.move_ids
            if move.t4tek_transit_line_id
        }
        
        lot_data_list = []
        for src_ml in src_picking.move_line_ids:
            lot_name = src_ml.lot_id.name if src_ml.lot_id else src_ml.lot_name
            if lot_name:
                lot_data_list.append({
                    'lot_name': lot_name,
                    'product_id': src_ml.product_id.id,
                })
        
        lot_map = self._batch_find_or_create_lots(lot_data_list)
        
        for src_ml in src_picking.move_line_ids:
            product_id = src_ml.product_id.id
            
            transit_line_id = (
                src_ml.move_id.t4tek_transit_line_id.id
                if src_ml.move_id and src_ml.move_id.t4tek_transit_line_id
                else None
            )
            
            in_move = in_moves_by_line.get(transit_line_id)
            if not in_move:
                _logger.warning(
                    f"No dest move for transit_line_id={transit_line_id} "
                    f"(product '{src_ml.product_id.name}') — skipping"
                )
                continue
            
            lot_name = src_ml.lot_id.name if src_ml.lot_id else src_ml.lot_name
            
            copy_vals = src_ml.copy_data()[0]
            copy_vals.update({
                'picking_id': dest_picking.id,
                'move_id': in_move.id,
                'company_id': dest_picking.company_id.id,
                'state': 'assigned',
                'quantity': src_ml.quantity,
                'quantity_product_uom': src_ml.quantity_product_uom,
            })
            
            if lot_name:
                shared_lot = lot_map.get((lot_name, product_id))
                if shared_lot and shared_lot.exists():
                    copy_vals['lot_id'] = shared_lot.id
                    copy_vals['lot_name'] = shared_lot.name
                else:
                    copy_vals['lot_id'] = False
                    copy_vals['lot_name'] = False
            else:
                copy_vals['lot_id'] = False
                copy_vals['lot_name'] = False
            
            copy_vals.pop('location_id', None)
            copy_vals.pop('location_dest_id', None)
            copy_vals.pop('result_package_id', None)
            copy_vals.pop('package_id', None)
            for field in ['id', 'create_date', 'create_uid', 'write_date', 'write_uid', '__last_update']:
                copy_vals.pop(field, None)
            
            self.env['stock.move.line'].sudo().create(copy_vals)
        
        _logger.info(
            f"Propagated {len(src_picking.move_line_ids)} move_lines "
            f"from {src_picking.name} to {dest_picking.name}"
        )
    
    def _create_dest_backorder_from_src(self, src_backorder):
        self.ensure_one()
        
        dest_picking = self.dest_picking_id.sudo()
        
        if not src_backorder or not dest_picking:
            return False
        
        dest_backorder_vals = {
            'partner_id': dest_picking.partner_id.id,
            'picking_type_id': dest_picking.picking_type_id.id,
            'scheduled_date': dest_picking.scheduled_date,
            'company_id': dest_picking.company_id.id,
            'origin': dest_picking.origin,
            'backorder_id': dest_picking.id,
        }
        
        dest_backorder = self.env['stock.picking'].sudo().create(dest_backorder_vals)
        
        transit_picking_vals = {
            't4tek_transit_order_id': self.t4tek_transit_order_id.id,
            'src_picking_id': src_backorder.id,
            'dest_picking_id': dest_backorder.id,
        }
        
        self.env['t4tek.transit.picking'].sudo().create(transit_picking_vals)
        
        _logger.info(
            f"Created dest backorder '{dest_backorder.name}' "
            f"from src backorder '{src_backorder.name}'"
        )
        
        return dest_backorder
    
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
        
        for lot in existing_lots:
            key = (lot.name, lot.product_id.id)
            
            if key not in existing_map:
                existing_map[key] = lot
                
                if lot.company_id:
                    try:
                        lot.write({'company_id': False})
                        _logger.info(f"Converted lot '{lot.name}' to cross-company")
                    except Exception as e:
                        _logger.warning(f"Failed to convert lot '{lot.name}': {str(e)}")
        
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
        self.ensure_one()

        transit = self.t4tek_transit_order_id.sudo()
        dest_picking = self.dest_picking_id

        if dest_picking.state != 'done':
            return

        for dest_move in dest_picking.move_ids:
            line = dest_move.t4tek_transit_line_id
            if not line:
                _logger.warning(
                    f"Dest move {dest_move.id} ({dest_move.product_id.name}) "
                    f"has no t4tek_transit_line_id — skipping"
                )
                continue

            actual_qty = dest_move.quantity
            if dest_move.product_uom != line.product_uom:
                actual_qty = dest_move.product_uom._compute_quantity(
                    actual_qty, line.product_uom, rounding_method='HALF-UP'
                )

            new_total = line.quantity + actual_qty
            line.write({'quantity': new_total})

            _logger.info(
                f"Transit line {line.id} ({line.product_id.name}): "
                f"+{actual_qty}, new total: {new_total}"
            )