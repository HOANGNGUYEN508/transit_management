from odoo import api, fields, models # type: ignore
from odoo.exceptions import UserError # type: ignore
from markupsafe import Markup, escape # type: ignore
import logging

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    needs_review = fields.Boolean(
        string='Needs Review',
        default=False,
        copy=False,
        tracking=True,
        help=(
            "Set to True when the linked transit order has been modified after confirmation "
            "(lines changed or order re-confirmed after cancellation). "
            "Cleared manually or via an automation rule once the transfer has been reviewed."
        ),
    )

    date_changed = fields.Boolean(
        string='Scheduled Date Changed',
        default=False,
        copy=False,
        help=(
            "Set to True when the scheduled date of the picking has been changed. "
            "Used to trigger review of the transfer if the date is changed after the transit order has been confirmed."
        ),
    )

    company_has_children = fields.Boolean(
        string='Company Has Children',
        compute='_compute_transit_fields',
        store=False,
        help="True if the picking's company has at least one direct child company.",
    )

    transit_order_id = fields.Many2one(
        'transit.order',
        string='Transit Order',
        compute='_compute_transit_fields',
        store=False,
        help="Transit order this picking belongs to, if any.",
    )

    delegation_id_display = fields.Char(
        string='Delegated From',
        compute='_compute_transit_fields',
        store=False,
    )

    delegation_ids_display = fields.Html(
        string='Delegated Routes',
        compute='_compute_transit_fields',
        store=False,
        sanitize=False,
    )

    @api.depends('company_id', 'company_id.child_ids')
    def _compute_transit_fields(self):

        _PICK_BADGE = {
            'draft': 'info', 'waiting': 'warning', 'confirmed': 'warning',
            'assigned': 'warning', 'done': 'success', 'cancel': 'secondary',
        }
        _PICK_LABEL = {
            'draft': 'Draft', 'waiting': 'Waiting', 'confirmed': 'Confirmed',
            'assigned': 'Ready', 'done': 'Done', 'cancel': 'Cancelled',
        }

        def _pick_html(pick):
            if not pick:
                return Markup('<span class="fw-semibold text-muted">—</span>')
            colour = _PICK_BADGE.get(pick.state, 'secondary')
            plabel = _PICK_LABEL.get(pick.state, pick.state or '—')
            badge  = Markup(f'<span class="badge text-bg-{colour} ms-1">{escape(plabel)}</span>')
            return Markup(f'<span class="fw-semibold">{escape(pick.name)}</span>{badge}')

        picking_ids = self.ids
        transit_pairs = self.env['transit.picking'].sudo().search([
            '|',
            ('src_picking_id',  'in', picking_ids),
            ('dest_picking_id', 'in', picking_ids),
        ])

        order_by_picking = {}
        tp_by_picking = {}  # picking_id → transit pair
        for tp in transit_pairs:
            if tp.src_picking_id.id:
                order_by_picking[tp.src_picking_id.id] = tp.transit_order_id
                tp_by_picking[tp.src_picking_id.id]    = tp
            if tp.dest_picking_id.id:
                order_by_picking[tp.dest_picking_id.id] = tp.transit_order_id
                tp_by_picking[tp.dest_picking_id.id]    = tp

        for picking in self:
            picking.company_has_children   = bool(picking.company_id.child_ids)
            picking.transit_order_id = order_by_picking.get(picking.id, False)

            tp = tp_by_picking.get(picking.id)

            # ── delegation_id_display ───────────────────────────────────────
            if tp and tp.delegation_id:
                picking.delegation_id_display = tp.delegation_id.sudo().display_name
            else:
                picking.delegation_id_display = False

            # ── delegation_ids_display ──────────────────────────────────────
            if tp and tp.delegation_ids:
                rows = Markup('')
                for child in tp.delegation_ids.sudo():
                    src  = child.src_picking_id
                    dest = child.dest_picking_id
                    rows += Markup(
                        '<span style="'
                        'display:flex; align-items:center;'
                        'font-weight:700; font-size:0.75rem; text-transform:uppercase;'
                        'letter-spacing:0.05em; white-space:nowrap;'
                        'padding:8px 16px 8px 12px;'
                        'border-radius:4px 0 0 4px;'
                        'background:#f8f9fa; border:1px solid #dee2e6; border-right:none;">'
                        '<i class="fa fa-share-alt me-1"></i> DELEGATED'
                        '</span>'
                        '<div style="'
                        'display:flex; align-items:center; gap:4px;'
                        'padding:8px 12px;'
                        'border-radius:0 4px 4px 0;'
                        'background:#f8f9fa; border:1px solid #dee2e6; border-left:none;">'
                        '<i class="fa fa-sign-out text-muted"></i>'
                        '<span class="text-muted small fw-bold text-uppercase me-1">Delivery</span>'
                        '{src_pick}'
                        '<i class="fa fa-long-arrow-right text-muted mx-2"></i>'
                        '<i class="fa fa-sign-in text-muted"></i>'
                        '<span class="text-muted small fw-bold text-uppercase mx-1">Receipt</span>'
                        '{dest_pick}'
                        '</div>'
                    ).format(
                        src_pick=_pick_html(src),
                        dest_pick=_pick_html(dest),
                    )
                picking.delegation_ids_display = Markup(
                    '<div style="display:grid; grid-template-columns:max-content 1fr; gap:6px 0;">'
                    '{rows}'
                    '</div>'
                ).format(rows=rows)
            else:
                picking.delegation_ids_display = False

    def action_delegate(self):
        self.ensure_one()

        if self.state in ('done', 'cancel'):
            raise UserError(f"Cannot delegate picking '{self.name}': already in state '{self.state}'.")

        if not self.company_has_children:
            raise UserError(f"Company '{self.company_id.name}' has no child companies to delegate to.")

        transit_pair = self.env['transit.picking'].sudo().search([
            ('src_picking_id', '=', self.id),
        ], limit=1)

        if not transit_pair:
            raise UserError(f"Picking '{self.name}' is not the source picking of any transit order.")

        line_defaults = [
            (0, 0, {
                'move_id':       move.id,
                'delegated_qty': 0.0,
            })
            for move in self.move_ids.filtered(lambda m: m.state != 'cancel')
        ]

        ctx = {
            'default_transit_picking_id': transit_pair.id,
            'default_delegation_wizard_line_ids':           line_defaults,
        }

        # Push delegation chain context through so default_get can populate
        # delegation_id (parent) and delegation_ids (existing children)
        if transit_pair.delegation_id:
            ctx['default_delegation_id'] = transit_pair.delegation_id.id

        if transit_pair.delegation_ids:
            ctx['default_delegation_ids'] = [(6, 0, transit_pair.delegation_ids.ids)]

        return {
            'type':      'ir.actions.act_window',
            'name':      'Delegate to Child Company',
            'res_model': 'transit.picking.delegation',
            'view_mode': 'form',
            'target':    'new',
            'context':   ctx,
        }

    def action_dismiss_review(self):
        self.ensure_one()
        vals = {}
        if self.needs_review:
            vals['needs_review'] = False
        if self.date_changed:
            vals['date_changed'] = False
        if vals:
            self.sudo().write(vals)
            
    def automation_handle_dest_backorder_init_revert(self):
        """
        Called from Rule 12 action on all triggered records.
        Reverts dest backorder pickings to draft if their own transit pair exists
        and src is still draft.

        Only processes incoming backorder pickings whose parent belongs to a transit pair.

        Picking already has its own transit pair and src is draft:
            → revert to draft directly (no _do_unreserve to avoid triggering Rule 10 again)
        """
        # ── Pre-filter: only incoming backorders ──────────────────────────────
        backorder_candidates = self.filtered(
            lambda p: p.backorder_id and p.picking_type_id.code == 'incoming'
        )
        if not backorder_candidates:
            return

        # ── Pre-filter: only whose parent is a known transit dest ─────────────
        parent_ids = backorder_candidates.mapped('backorder_id').ids
        transit_parent_dest_ids = set(
            self.env['transit.picking'].sudo().search([
                ('dest_picking_id', 'in', parent_ids)
            ]).mapped('dest_picking_id').ids
        )
        backorder_candidates = backorder_candidates.filtered(
            lambda p: p.backorder_id.id in transit_parent_dest_ids
        )
        if not backorder_candidates:
            return

        to_revert = self.env['stock.picking']

        # ── Case 1: picking already has its own transit pair, src is draft ────
        own_pairs = self.env['transit.picking'].sudo().search([
            ('dest_picking_id', 'in', backorder_candidates.ids)
        ])
        own_by_dest_id = {tp.dest_picking_id.id: tp for tp in own_pairs}

        for picking in backorder_candidates:
            tp = own_by_dest_id.get(picking.id)
            if tp and tp.src_picking_id and tp.src_picking_state == 'draft':
                to_revert |= picking

        if to_revert:
            for picking in to_revert:
                # Direct write — avoids _do_unreserve() which sets moves to
                # 'confirmed', triggering picking state recompute → Rule 10 re-fires
                picking.move_ids.sudo().write({'state': 'draft'})
                picking.sudo().write({'state': 'draft'})
                _logger.info(
                    "Reverted dest backorder '%s' to draft "
                    "(own transit pair exists, src '%s' is draft)",
                    picking.name,
                    own_by_dest_id[picking.id].src_picking_id.name,
                )