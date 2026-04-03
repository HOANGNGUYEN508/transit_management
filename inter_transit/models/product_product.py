from odoo import api, fields, models # type: ignore
from odoo.tools.float_utils import float_round # type: ignore
from odoo.osv import expression # type: ignore
from collections import defaultdict
import logging

_logger = logging.getLogger(__name__)


class ProductProduct(models.Model):
    _inherit = 'product.product'

    transit_qty = fields.Float(
        'In Transit',
        compute='_compute_quantities',
        digits='Product Unit of Measure',
        aggregator='sum',
        compute_sudo=False,
        help=(
            "Quantity physically sitting in a transit location right now.\n"
            "\n"
            "Two cases are captured:\n"
            "  • Active transit  — src picking DONE, dest picking still pending\n"
            "    (goods reserved at transit location by the pending dest move).\n"
            "  • Stuck transit   — transit order DONE but a mismatch left goods\n"
            "    unreserved at the transit location (all-warehouse view only).\n"
            "\n"
            "For stuck goods, the same amount is subtracted from free_qty so\n"
            "the product is not reported as freely available.\n"
            "Purely informational — does not modify incoming/outgoing/virtual qty."
        ),
    )
 
    @api.depends(
        'stock_move_ids.product_qty',
        'stock_move_ids.state',
        'stock_move_ids.quantity',
        # Quant-level depends so that stuck-qty recomputes when goods are
        # physically moved in/out of the transit location.
        'stock_quant_ids.quantity',
        'stock_quant_ids.reserved_quantity',
        'stock_quant_ids.location_id',
    )
    @api.depends_context(
        'lot_id', 'owner_id', 'package_id', 'from_date', 'to_date',
        'location', 'warehouse_id', 'allowed_company_ids', 'is_storable',
    )
    def _compute_quantities(self):
        # ── 1. Let Odoo compute all standard qty fields first ────────────────
        super()._compute_quantities()
 
        storable = self.filtered(lambda p: p.type != 'service')
        (self - storable).transit_qty = 0.0
 
        if not storable:
            return
 
        location_ctx = self.env.context.get('location')
        warehouse_ctx = self.env.context.get('warehouse_id')
        is_all_warehouses = not location_ctx and not warehouse_ctx
 
        # ── 2. Active in-transit qty (src done, dest pending) ───────────────
        transit_res = storable._compute_transit_quantities_dict()
 
        # ── 3. Stuck qty (transit done but goods still at transit location) ──
        #       Only meaningful in all-warehouse mode; per-warehouse/location
        #       views exclude transit warehouses by design (_is_transit_warehouse_scope).
        stuck_res = {}
        if is_all_warehouses:
            stuck_res = storable._compute_stuck_transit_quantities_dict()
 
        rounding_map = {p.id: p.uom_id.rounding for p in storable}
 
        for product in storable:
            rounding = rounding_map[product.id]
            active_transit = transit_res.get(product.id, 0.0)
            stuck = stuck_res.get(product.id, 0.0)
 
            # transit_qty = active (all modes) + stuck (all-WH mode only)
            product.transit_qty = float_round(
                active_transit + stuck,
                precision_rounding=rounding,
            )
 
            # Subtract stuck qty from free_qty so it is not reported as
            # freely available stock.
            if stuck > 0.0:
                product.free_qty = float_round(
                    product.free_qty - stuck,
                    precision_rounding=rounding,
                )
 
        # incoming_qty / outgoing_qty / virtual_available untouched.

    def _compute_transit_quantities_dict(self):
        """
        Returns {product_id: float}
 
        transit_qty = quantity whose src_picking is DONE but dest_picking is
                      still pending — goods physically sitting in the transit
                      location right now, reserved by the pending dest move.
 
        Source: pending dest_picking moves.
        Scope filter applied when in individual warehouse/location mode.
        """
        location_ctx = self.env.context.get('location')
        warehouse_ctx = self.env.context.get('warehouse_id')
        is_all_warehouses = not location_ctx and not warehouse_ctx
 
        result = {p.id: 0.0 for p in self}
 
        in_pairs = self.env['transit.picking'].search([
            ('src_picking_state', '=', 'done'),
            ('dest_picking_state', 'not in', ['done', 'cancel']),
            ('company_id', 'in', self.env.companies.ids),
        ])
 
        if not in_pairs:
            return result
 
        dest_picking_ids = in_pairs.mapped('dest_picking_id').ids
        pending_states = ('waiting', 'confirmed', 'assigned', 'partially_available')
 
        base_domain = [
            ('picking_id', 'in', dest_picking_ids),
            ('product_id', 'in', self.ids),
            ('state', 'in', pending_states),
        ]
 
        if not is_all_warehouses:
            if self._is_transit_warehouse_scope(warehouse_ctx):
                return result
 
            scope_loc_ids = self._get_transit_scope_location_ids(
                location_ctx, warehouse_ctx
            )
            if not scope_loc_ids:
                return result
            base_domain = expression.AND([
                base_domain,
                expression.OR([
                    [('location_id', 'in', scope_loc_ids)],
                    [('location_dest_id', 'in', scope_loc_ids)],
                ]),
            ])
 
        Move = self.env['stock.move'].with_context(active_test=False)
        for product, qty in Move._read_group(
            base_domain, ['product_id'], ['product_qty:sum']
        ):
            if product.id in result:
                result[product.id] = qty
 
        return result

    def _get_transit_scope_location_ids(self, location_ctx, warehouse_ctx):
        """
        Resolve context scope into a flat list of child stock.location ids.
        """
        Location = self.env['stock.location']
        Warehouse = self.env['stock.warehouse']
 
        if warehouse_ctx:
            wh_ids = (
                warehouse_ctx if isinstance(warehouse_ctx, list) else [warehouse_ctx]
            )
            scope_roots = Warehouse.browse(wh_ids).mapped('view_location_id')
        elif location_ctx:
            loc_ids = (
                location_ctx if isinstance(location_ctx, list) else [location_ctx]
            )
            scope_roots = Location.browse(loc_ids)
        else:
            return []
 
        if not scope_roots:
            return []
 
        path_domain = expression.OR([
            [('parent_path', '=like', loc.parent_path + '%')]
            for loc in scope_roots
        ])
        return Location.search(path_domain).ids

    def _compute_stuck_transit_quantities_dict(self):
        """
        Returns {product_id: float}
 
        Detects goods that ended up stranded at a transit location after a
        transit order is marked DONE — typically because the destination
        picking was validated with a mismatch (fewer units received than
        shipped, or an entire product line not received).
 
        Detection strategy
        ──────────────────
        When a src picking is validated, Odoo creates a quant at the transit
        location.  When the dest picking is pending, that quant is reserved
        (reserved_quantity > 0) by the dest move.  Once the transit order is
        DONE both pickings are closed, reservations are cleared, and any
        shortfall quantity stays as an unreserved quant at the transit
        location.
 
        Therefore:
            stuck_qty = SUM(quant.quantity - quant.reserved_quantity)
                        at transit locations for each product
 
        This is naturally zero for healthy transits:
          • Active transit  → quant is reserved → (qty - reserved) = 0
          • Completed, no mismatch → quant is zero after dest picking done
          • Stuck (mismatch) → quant > 0 and unreserved → captured here
 
        Only called in all-warehouse mode (transit warehouses are excluded
        from per-warehouse/location scopes by _is_transit_warehouse_scope).
        """
        result = {p.id: 0.0 for p in self}
 
        # ── Collect all known transit location IDs ───────────────────────────
        # sudo() is intentional: we only read location IDs, not qty data.
        # The qty read below respects the normal access rules via stock.quant.
        transit_location_ids = (
            self.env['transit.order']
            .sudo()
            .search([('company_id', 'in', self.env.companies.ids)])
            .mapped('transit_location_id')
            .ids
        )
 
        if not transit_location_ids:
            return result
 
        # ── Query quants: unreserved qty at transit locations ────────────────
        Quant = self.env['stock.quant'].with_context(active_test=False)
        quant_domain = [
            ('location_id', 'in', transit_location_ids),
            ('product_id', 'in', self.ids),
        ]
 
        for product, qty, reserved in Quant._read_group(
            quant_domain,
            groupby=['product_id'],
            aggregates=['quantity:sum', 'reserved_quantity:sum'],
        ):
            if product.id not in result:
                continue
            unreserved = qty - reserved
            if unreserved > 0.0:
                result[product.id] = unreserved
 
        return result
 
    def _is_transit_warehouse_scope(self, warehouse_ctx):
        """
        Returns True if the current warehouse scope is a transit warehouse.
        A warehouse is considered transit if its view_location_id has no
        company_id (company_id = null) — transit locations are company-neutral
        by design to allow cross-company stock movement.
        Only meaningful when a specific warehouse is selected (not All WH).
        """
        if not warehouse_ctx:
            return False
        wh_ids = (
            warehouse_ctx if isinstance(warehouse_ctx, list) else [warehouse_ctx]
        )
        view_locations = self.env['stock.warehouse'].browse(wh_ids).mapped(
            'view_location_id'
        )
        return any(not loc.company_id for loc in view_locations)