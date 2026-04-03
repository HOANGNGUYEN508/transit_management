from odoo import api, fields, models # type: ignore
from odoo.exceptions import UserError # type: ignore
from odoo.tools import float_compare, float_is_zero, float_round # type: ignore
from markupsafe import Markup # type: ignore
import logging

_logger = logging.getLogger(__name__)


class TransitPickingDelegation(models.TransientModel):
    """
    Inter-Company Transit Delegation Wizard:
    
    Architecture:
    - This transient model serves as a way for user to specify how to delegate a transit picking to a child company.
    - As long as the picking is not in state 'done', 'cancel' or 'waiting', they can be delegated.
    - If one of the child company is the destination company of the transit, delegation to that child company is not allowed to avoid circular delegation.
    - The delegation can be done in two ways: whole or partial break.

    Note: 
    - In whole break, the entire picking credibility will be transferred to child company.
    - In partial break, they can specify the quantity to delegate for each move line. 
    The remaining quantity will stay with the parent company.
    - If user chooses whole break but the delegation lines add up to 100%, the system will treat it as a whole break but show a different confirmation message to make it clear to user. 
    However, if user chooses partial break but the delegation lines add up to 0%, the system will not allow the delegation to proceed and prompt user to enter some quantity to delegate.
    - Can only delegate the source picking, not the destination picking, because the receiving company must be the one to confirm receipt of goods and we want to avoid a scenario 
    where both src and dest are delegated to different child companies.
    - Regardless of delegated quantity, if the break type is whole, the entire picking will be reassigned to the child company.
    - When delegate a backorder picking, if the break type is whole, the backorder link will be severed and the delegated picking will no longer be recognised as a backorder of the original picking.
    - When delegate any picking that its state is not draft, the picking will be unreserved to avoid reservation issues after delegation.
    """
    _name = 'transit.picking.delegation'
    _description = 'Multi-Company Transit Delegation Wizard'

    name = fields.Char(string='Transit Route', readonly=True)

    transit_picking_id = fields.Many2one(
        'transit.picking',
        string='Transit Picking Pair',
        required=True,
    )

    src_picking_id = fields.Many2one(
        'stock.picking',
        string='Source Picking',
        compute='_compute_src_picking_id',
        store=False,
    )

    src_company_id = fields.Many2one(
        'res.company',
        string='Source Company',
        readonly=True,
        help="Populated explicitly in default_get so the child_company_id domain resolves correctly.",
    )

    dest_company_id = fields.Many2one(
        'res.company',
        string='Destination Company',
        readonly=True,
        help="Populated explicitly in default_get so it can be used in child_company_id domain.",
    )

    child_company_id = fields.Many2one(
        'res.company',
        string='Delegate To',
        required=True,
        domain="[('parent_id', '=', src_company_id), ('id', '!=', dest_company_id)]",
        help="Direct child of the source company that will handle the delegated quantities.",
    )

    break_type = fields.Selection(
        [('whole', 'Whole'), ('partial', 'Partial')],
        string='Break Type',
        default='whole',
    )

    delegation_wizard_line_ids = fields.One2many(
        'transit.picking.delegation.line',
        'delegation_wizard_id',
        string='Split Lines',
    )

    delegation_id_display = fields.Char(
        string='Delegated From',
        readonly=True,
    )

    delegation_ids_display = fields.Html(
        string='Delegated Routes',
        readonly=True,
        sanitize=False,
    )

    @api.depends('transit_picking_id')
    def _compute_src_picking_id(self):
        for rec in self:
            rec.src_picking_id = (
                rec.transit_picking_id.sudo().src_picking_id
                if rec.transit_picking_id else False
            )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)

        tp_id = (
            self.env.context.get('default_transit_picking_id')
            or self.env.context.get('active_id')
        )
        if not tp_id:
            return res

        tp = self.env['transit.picking'].sudo().browse(tp_id)
        if not tp.exists():
            return res

        src = tp.src_picking_id.sudo()
        if src.state in ('done', 'cancel'):
            raise UserError(
                f"Cannot delegate picking '{src.name}': "
                f"already in state '{src.state}'."
            )

        res['transit_picking_id'] = tp.id

        res['name'] = (
            f"{src.company_id.name}"
            f" → "
            f"{tp.dest_picking_id.sudo().company_id.name}"
        )

        res['src_company_id']  = src.company_id.id
        res['dest_company_id'] = tp.dest_company_id.id

        if tp.delegation_id:
            res['delegation_id_display'] = tp.delegation_id.sudo().display_name

        if tp.delegation_ids:
            rows = Markup('')
            for child in tp.delegation_ids.sudo():
                src_p  = child.src_picking_id
                dest_p = child.dest_picking_id
                rows += Markup('''
                    <div class="o_transit_banner_row">
                        <span class="o_transit_banner_label text-muted"
                              style="background:#f8f9fa; border:1px solid #dee2e6; border-right:none;">
                            <i class="fa fa-share-alt me-1"></i> DELEGATED
                        </span>
                        <div class="o_transit_banner_content"
                            style="background:#f8f9fa; border:1px solid #dee2e6; border-left:none;">
                            <i class="fa fa-sign-out text-muted"></i>
                            <span class="text-muted small fw-bold text-uppercase me-1">Delivery</span>
                            <span class="fw-semibold">{src_name}</span>
                            <i class="fa fa-long-arrow-right text-muted mx-1"></i>
                            <i class="fa fa-sign-in text-muted"></i>
                            <span class="text-muted small fw-bold text-uppercase mx-1">Receipt</span>
                            <span class="fw-semibold">{dest_name}</span>
                        </div>
                    </div>
                ''').format(
                    src_name=src_p.name or '',
                    dest_name=dest_p.name or '',
                )

            res['delegation_ids_display'] = Markup('''
                <div class="o_transit_banner_table"
                    style="display:grid; grid-template-columns:max-content 1fr; gap:6px 0;">
                    {rows}
                </div>
            ''').format(rows=rows)

        if 'delegation_wizard_line_ids' in fields_list:
            res['delegation_wizard_line_ids'] = [
                (0, 0, {
                    'move_id':       move.id,
                    'delegated_qty': 0.0,
                })
                for move in src.move_ids.filtered(lambda m: m.state != 'cancel')
            ]

        return res

    def _get_child_transit_config(self, child_company):
        tp = self.transit_picking_id.sudo()
        config = self.env['transit.picking.type'].sudo().search([
            ('company_id',          '=', child_company.id),
            ('transit_location_id', '=', tp.transit_location_id.id),
        ], order='create_date ASC', limit=1)

        if not config:
            raise UserError(
                f"Child company '{child_company.name}' has no transit picking type "
                f"configured for transit location "
                f"'{tp.transit_location_id.complete_name}'. "
                f"Run the setup wizard for '{child_company.name}' first."
            )
        return config

    def action_confirm(self):
        """
        Lightweight pre-check only. Returns {message, resolved_break_type}
        so the JS widget can show a ConfirmationDialog without a second popup.
        """
        self.ensure_one()

        tp = self.transit_picking_id.sudo()

        if self.break_type == 'whole':
            resolved_break_type = 'whole'
            message = (
                f"This will reassign the <strong>entire</strong> transfer to "
                f"<strong>{self.child_company_id.name}</strong>. "
                f"Do you want to proceed?"
            )
        else:
            all_whole = all(
                float_compare(
                    line.delegated_qty,
                    line.move_id.product_uom_qty,
                    precision_rounding=(
                        line.move_id.product_uom.rounding
                        if line.move_id.product_uom else 0.001
                    ),
                ) == 0
                for line in self.delegation_wizard_line_ids
                if line.move_id
            )
            if all_whole:
                resolved_break_type = 'whole'
                message = (
                    f"You have entered 100% of every quantity — this will hand the "
                    f"<strong>entire</strong> shipment to "
                    f"<strong>{self.child_company_id.name}</strong>, "
                    f"same as a Whole delegation. "
                    f"Do you want to proceed?"
                )
            else:
                resolved_break_type = 'partial'
                message = (
                    f"Delegate the selected quantities to "
                    f"<strong>{self.child_company_id.name}</strong>. "
                    f"The remainder stays with this company. "
                    f"Do you want to proceed?"
                )

        return {
            'message': message,
            'resolved_break_type': resolved_break_type,
        }

    def action_execute(self, resolved_break_type):
        """
        Full re-validation at the moment the user clicks Confirm in the JS dialog.
        """
        self.ensure_one()

        src = self.src_picking_id.sudo()

        STATE_CANNOT_DELEGATE = ('done', 'cancel', 'waiting')
        TYPE_CANNOT_DELEGATE  = ('incoming', 'internal')

        if src.state in STATE_CANNOT_DELEGATE:
            raise UserError(
                f"Cannot delegate transfer '{src.name}' in state '{src.state}'. "
                f"Delegation is only allowed on Draft, Confirmed, or Ready transfers."
            )

        if src.picking_type_id.code in TYPE_CANNOT_DELEGATE:
            raise UserError(
                f"Cannot delegate transfer '{src.name}' of type "
                f"'{src.picking_type_id.code}'. "
                f"Delegation is only allowed on Outgoing transfers."
            )

        active_moves = src.move_ids.filtered(lambda m: m.state != 'cancel')
        if not active_moves:
            raise UserError(f"No active moves on transfer '{src.name}'.")

        tp = self.transit_picking_id.sudo()
        if not tp.exists():
            raise UserError(
                "The transit picking pair no longer exists. "
                "Please close this wizard and reload."
            )

        transit_order = tp.transit_order_id.sudo()
        if not transit_order.exists():
            raise UserError(
                "The transit order no longer exists. "
                "Please close this wizard and reload."
            )

        if transit_order.state in ('done', 'cancel'):
            raise UserError(
                f"Transit order '{transit_order.name}' is now in state "
                f"'{transit_order.state}' — delegation is no longer possible."
            )

        if tp.src_picking_id != src:
            raise UserError(
                f"The source picking of transit pair '{tp.display_name}' has changed "
                f"since this wizard was opened. Please close and reopen."
            )

        if src.company_id.id != self.src_company_id.id:
            raise UserError(
                f"The company of picking '{src.name}' has changed "
                f"since this wizard was opened. Please close and reopen."
            )

        if self.child_company_id.id == self.dest_company_id.id:
            raise UserError(
                f"Cannot delegate to '{self.child_company_id.name}': "
                f"this is the destination company of the transit order. "
                f"Choose a different child company."
            )

        current_move_ids = set(active_moves.ids)
        wizard_move_ids  = set(
            line.move_id.id
            for line in self.delegation_wizard_line_ids
            if line.move_id
        )
        stale = wizard_move_ids - current_move_ids
        if stale:
            raise UserError(
                "Some moves on this wizard no longer exist on the source picking "
                "(they may have been cancelled or deleted). "
                "Please close and reopen the wizard to get fresh lines."
            )

        has_any_delegated = False
        for line in self.delegation_wizard_line_ids:
            move         = line.move_id
            rounding     = move.product_uom.rounding if move.product_uom else 0.001
            original_qty = move.product_uom_qty
            label        = move.product_id.display_name if move.product_id else f"line {line.id}"

            if float_compare(line.delegated_qty, 0.0, precision_rounding=rounding) < 0:
                raise UserError(f"'{label}': Delegated quantity cannot be negative.")

            if float_compare(line.delegated_qty, original_qty, precision_rounding=rounding) > 0:
                raise UserError(
                    f"'{label}': Delegated ({line.delegated_qty}) exceeds "
                    f"Demand ({original_qty})."
                )

            if not float_is_zero(line.delegated_qty, precision_rounding=rounding):
                has_any_delegated = True

        for line in self.delegation_wizard_line_ids:
            if not line.move_id:
                continue
            uom      = line.move_id.product_uom
            rounding = uom.rounding if uom else 0.001
            if float_is_zero(line.delegated_qty, precision_rounding=rounding):
                continue
            rounded = float_round(line.delegated_qty, precision_rounding=rounding)
            label   = line.move_id.product_id.display_name if line.move_id.product_id else f"line {line.id}"
            if float_compare(rounded, line.delegated_qty, precision_rounding=rounding / 10) != 0:
                raise UserError(
                    f"'{label}': {line.delegated_qty} is not a valid quantity for "
                    f"unit of measure '{uom.name}' (smallest unit: {rounding}). "
                    f"Did you mean {rounded}?"
                )

        child_config = self._get_child_transit_config(self.child_company_id)
        if src.state != 'draft':
            src.do_unreserve()
            src.mapped('move_ids').write({'state': 'draft'})

        if resolved_break_type == 'whole':
            if tp.delegation_ids:
                raise UserError(
                    f"Shipment '{tp.display_name}' has already split off "
                    f"{len(tp.delegation_ids)} child shipment(s) via partial delegation. "
                    f"Handing the entire picking to another company would abandon those splits. "
                    f"Use Partial delegation and enter only the quantities not yet delegated."
                )
            new_tp = self._do_whole_break(child_config)
        else:
            if not has_any_delegated:
                raise UserError("All delegated quantities are zero — nothing to delegate.")
            new_tp = self._do_partial_break(child_config)

        # ── Post chatter message to transit order ─────────────────────────────────
        if new_tp:
            if resolved_break_type == 'whole':
                lines = src.move_ids.filtered(lambda m: m.state != 'cancel')
                product_rows = Markup('').join(
                    Markup('<li>{product}: {delegated} / {total} {uom}</li>').format(
                        product=m.product_id.display_name,
                        delegated=m.product_uom_qty,
                        total=m.product_uom_qty,
                        uom=m.product_uom.name if m.product_uom else '',
                    )
                    for m in lines
                )
            else:
                product_rows = Markup('').join(
                    Markup('<li>{product}: {delegated} / {total} {uom}</li>').format(
                        product=line.move_id.product_id.display_name,
                        delegated=line.delegated_qty,
                        total=line.move_id.product_uom_qty,
                        uom=line.move_id.product_uom.name if line.move_id.product_uom else '',
                    )
                    for line in self.delegation_wizard_line_ids
                    if line.move_id
                    and not float_is_zero(
                        line.delegated_qty,
                        precision_rounding=line.move_id.product_uom.rounding if line.move_id.product_uom else 0.001
                    )
                )

            body = Markup('''
                <b>📦 Transit Delegation ({break_type})</b><br/>
                <b>By:</b> {user}<br/>
                <b>Delegated To:</b> {company}<br/>
                <b>New Route:</b> {route}<br/>
                <b>Products:</b>
                <ul>{product_rows}</ul>
            ''').format(
                break_type='Whole' if resolved_break_type == 'whole' else 'Partial',
                user=self.env.user.name,
                company=self.child_company_id.name,
                route=new_tp.display_name,
                product_rows=product_rows,
            )

            tp.sudo().transit_order_id.sudo().message_post(body=body)

    def _do_whole_break(self, child_config):
        tp          = self.transit_picking_id.sudo()
        src_picking = tp.src_picking_id.sudo()
        child       = self.child_company_id
        child_type  = child_config.src_picking_type_id

        # ── Backorder chain severance ─────────────────────────────────────────────
        # If the src picking being delegated is itself a backorder (i.e. it was
        # created by Odoo's backorder split), we must break the backorder link on
        # both sides before reassigning company/type. Leaving backorder_id intact
        # when execute a whole-break would be impossible since odoo apply check_company()
        # on backorder_id inside the logic of _create_backorder()
        #
        # Steps:
        #   1. Detect the backorder parent transit pair (via src_picking.backorder_id).
        #   2. Clear backorder_id on both src and dest pickings so no automation
        #      rule (Rule 5, Rule 6) fires on them again after reassignment.
        #   3. Promote the current transit pair from the backorder chain into the
        #      delegation chain by writing delegation_id = tp_parent.id.
        if src_picking.backorder_id:
            orig_src = src_picking.backorder_id

            # Find the parent transit pair that owns orig_src as its src_picking_id
            tp_parent = self.env['transit.picking'].sudo().search([
                ('src_picking_id', '=', orig_src.id),
            ], limit=1)

            # Sever the backorder link on the src side
            src_picking.write({'backorder_id': False})

            # Sever the backorder link on the dest side so dest is no longer
            # recognised as a backorder of the original dest picking
            dest_bo = tp.dest_picking_id
            if dest_bo and dest_bo.backorder_id:
                dest_bo.write({'backorder_id': False})

            # Promote this transit pair into the delegation chain
            if tp_parent:
                tp.write({'delegation_id': tp_parent.id})
                _logger.info(
                    "Whole break (backorder): severed backorder chain for src '%s' / dest '%s', "
                    "transit pair '%s' promoted to delegation child of '%s'",
                    src_picking.name,
                    dest_bo.name if dest_bo else '—',
                    tp.display_name,
                    tp_parent.display_name,
                )
            else:
                _logger.warning(
                    "Whole break (backorder): src '%s' has backorder_id set but no parent "
                    "transit pair was found for orig_src '%s' — backorder_id cleared but "
                    "no delegation link established",
                    src_picking.name,
                    orig_src.name,
                )

        # ── Standard whole-break reassignment (unchanged) ─────────────────────────
        src_picking.write({
            'company_id':      child.id,
            'picking_type_id': child_type.id,
            'partner_id':      tp.dest_company_id.partner_id.id,
        })
        src_picking.move_ids.sudo().write({'company_id': child.id})

        _logger.info(
            "Whole break: reassigned '%s' from '%s' → child '%s'",
            src_picking.name, self.src_company_id.name, child.name,
        )

        return tp

    def _do_partial_break(self, child_config):
        tp           = self.transit_picking_id.sudo()
        src_picking  = tp.src_picking_id.sudo()
        dest_picking = tp.dest_picking_id.sudo()
        child        = self.child_company_id
        child_type   = child_config.src_picking_type_id

        dest_config = self.env['transit.picking.type'].sudo().search([
            ('company_id',          '=', tp.dest_company_id.id),
            ('transit_location_id', '=', tp.transit_location_id.id),
        ], order='create_date ASC', limit=1)

        if not dest_config:
            raise UserError(
                f"Destination company '{tp.dest_company_id.name}' has no transit "
                f"picking type configured for transit location "
                f"'{tp.transit_location_id.complete_name}'."
            )

        delegated_by_move = {
            line.move_id.id: line.delegated_qty
            for line in self.delegation_wizard_line_ids
            if line.move_id
        }
        dest_moves_by_product = {
            m.product_id.id: m
            for m in dest_picking.move_ids.filtered(lambda m: m.state != 'cancel')
        }

        child_src_picking = self.env['stock.picking'].sudo().create({
            'partner_id':       tp.dest_company_id.partner_id.id,
            'picking_type_id':  child_type.id,
            'scheduled_date':   src_picking.scheduled_date,
            'company_id':       child.id,
            'origin':           src_picking.origin,
            'location_id':      child_type.default_location_src_id.id,
            'location_dest_id': src_picking.location_dest_id.id,
        })

        new_dest_picking = self.env['stock.picking'].sudo().create({
            'partner_id':       tp.src_company_id.partner_id.id,
            'picking_type_id':  dest_config.dest_picking_type_id.id,
            'scheduled_date':   dest_picking.scheduled_date,
            'company_id':       tp.dest_company_id.id,
            'origin':           dest_picking.origin,
            'location_id':      dest_picking.location_id.id,
            'location_dest_id': dest_picking.location_dest_id.id,
        })

        child_src_move_vals    = []
        new_dest_move_vals     = []
        moves_to_reassign      = self.env['stock.move'].sudo()
        dest_moves_to_reassign = self.env['stock.move'].sudo()

        for move in src_picking.move_ids.filtered(lambda m: m.state != 'cancel'):
            rounding     = move.product_uom.rounding if move.product_uom else 0.001
            original_qty = move.product_uom_qty
            delegated    = delegated_by_move.get(move.id, 0.0)
            remaining    = original_qty - delegated

            if float_is_zero(delegated, precision_rounding=rounding):
                continue

            dest_move = dest_moves_by_product.get(move.product_id.id)

            if float_is_zero(remaining, precision_rounding=rounding):
                moves_to_reassign |= move
                if dest_move:
                    dest_moves_to_reassign |= dest_move
            else:
                move.write({'product_uom_qty': remaining})
                child_src_move_vals.append({
                    'name':            move.name or move.product_id.display_name,
                    'product_id':      move.product_id.id,
                    'product_uom_qty': delegated,
                    'product_uom':     move.product_uom.id,
                    'company_id':      child.id,
                    'picking_id':      child_src_picking.id,
                    'state':           'draft',
                })
                if dest_move:
                    dest_move.write({'product_uom_qty': remaining})
                    new_dest_move_vals.append({
                        'name':            dest_move.name or move.product_id.display_name,
                        'product_id':      move.product_id.id,
                        'product_uom_qty': delegated,
                        'product_uom':     dest_move.product_uom.id,
                        'company_id':      tp.dest_company_id.id,
                        'picking_id':      new_dest_picking.id,
                        'state':           'draft',
                    })

        if moves_to_reassign:
            moves_to_reassign.write({
                'picking_id': child_src_picking.id,
                'company_id': child.id,
            })

        if dest_moves_to_reassign:
            dest_moves_to_reassign.write({
                'picking_id': new_dest_picking.id,
                'company_id': tp.dest_company_id.id,
            })

        if not moves_to_reassign and not child_src_move_vals:
            raise UserError("No quantities to delegate — delegated qty is zero for all products.")

        if child_src_move_vals:
            self.env['stock.move'].sudo().create(child_src_move_vals)

        if new_dest_move_vals:
            self.env['stock.move'].sudo().create(new_dest_move_vals)

        new_transit = self.env['transit.picking'].sudo().create({
            'transit_order_id': tp.transit_order_id.id,
            'src_picking_id':         child_src_picking.id,
            'dest_picking_id':        new_dest_picking.id,
            'delegation_id':    tp.id,
        })

        _logger.info(
            "Partial break on transit '%s': child src '%s', dest '%s' for child '%s'.",
            tp.transit_order_id.name,
            child_src_picking.name, new_dest_picking.name, child.name,
        )

        return new_transit