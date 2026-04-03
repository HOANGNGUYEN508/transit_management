from odoo import api, fields, models # type: ignore
from odoo.exceptions import UserError, ValidationError # type: ignore
from odoo.tools import float_compare, float_is_zero, float_round # type: ignore
import logging

_logger = logging.getLogger(__name__)


class TransitOrderLine(models.Model):
    """
    Transit Order Lines:
    - This model stores the user-defined line specifications for inter-company transits.
    - These serve as templates that are used to create real stock.move records in the 
    src and dest pickings.
    - Its structure is similar to sale.order - sale.order.line.
    - Due to its requirement of true product and quantity of 2 src and dest pickings, 
    we need to merge lines and handle UOM conversions.
    """
    _name = 'transit.order.line'
    _description = 'Transit Move Line'
    _rec_name = 'name'
    
    name = fields.Char(
        'Description',
        help="Description of the move (defaults to product name if empty)"
    )
    
    transit_id = fields.Many2one(
        'transit.order',
        'Transit Order',
        required=True,
        ondelete='cascade',
        index=True
    )
    
    company_id = fields.Many2one(
        'res.company',
        'Company',
        related='transit_id.company_id',
        store=True,
        readonly=True
    )
    
    state = fields.Selection(
        related='transit_id.state',
        string='Transit State',
        store=True,
        readonly=True
    )
    
    product_id = fields.Many2one(
        'product.product',
        'Product',
        required=True,
        check_company=True,
        domain="[('type', '=', 'consu')]",
        index=True
    )
    
    product_tmpl_id = fields.Many2one(
        'product.template',
        'Product Template',
        related='product_id.product_tmpl_id',
        store=True,
        readonly=True
    )
    
    product_uom_qty = fields.Float(
        'Demand',
        digits='Product Unit of Measure',
        required=True,
        default=1.0,
        help="Quantity to transfer in the selected unit of measure"
    )
    
    quantity = fields.Float(
        'Actual Quantity',
        digits='Product Unit of Measure',
        readonly=True,
        help="True quantity that arrived (populated after reception)"
    )
    
    product_uom = fields.Many2one(
        'uom.uom',
        'Unit of Measure',
        domain="[('category_id', '=', product_uom_category_id)]"
    )
    
    product_uom_category_id = fields.Many2one(
        related='product_id.uom_id.category_id',
        readonly=True
    )
    
    origin = fields.Char(
        related='transit_id.name',
        string='Source Document',
        readonly=True,
        store=True
    )
    
    note = fields.Text('Notes')
    
    transit_location_id = fields.Many2one(
        'stock.location',
        'Transit Location',
        related='transit_id.transit_location_id',
      )

    def _check_order_state(self, operation='write'):
        """
        Three-lane state guard for all CRUD operations.

        Lane 1 — pass:      state not in hard-blocked or soft-warn → silent proceed.
        Lane 2 — assigned:  state is 'assigned' → caller decides via context flag.
                            Returns the soft-warn transits so the caller can warn.
        Lane 3 — hard block: state is done/cancel → raise UserError immediately.
        """
        _STATE_CHECK_SKIP_KEYS = {
            'create': 'skip_transit_order_line_create_state_check',
            'write':  'skip_transit_order_line_write_state_check',
            'unlink': 'skip_transit_order_line_unlink_state_check',
        }
        if self.env.context.get(_STATE_CHECK_SKIP_KEYS[operation]):
            return self.env['transit.order'].browse()
        hard_blocked = self.mapped('transit_id').filtered(
            lambda t: t.state in ('done', 'cancel')
        )
        if hard_blocked:
            raise UserError(
                "Cannot modify lines: one or more transit orders are already done or cancelled:\n• "
                + '\n• '.join(hard_blocked.mapped('name'))
                + "\n\nPlease create a new transit order for any additional movements."
            )

        # Return soft-warn transits so callers can decide whether to post a warning
        return self.mapped('transit_id').filtered(
            lambda t: t.state in ('assigned')
        )


    @api.model_create_multi
    def create(self, vals_list):
        warn_transits = self._check_order_state(operation='create')          # hard block or pass

        for vals in vals_list:
            if 'product_id' in vals and 'product_uom' not in vals:
                product = self.env['product.product'].browse(vals['product_id'])
                if product.uom_id:
                    vals['product_uom'] = product.uom_id.id
            if 'product_id' in vals and not vals.get('name'):
                product = self.env['product.product'].browse(vals['product_id'])
                vals['name'] = product.display_name

        result = super().create(vals_list)

        # Warn if assigned-state transits exist AND no skip flag in context
        if (
            warn_transits.exists()
            and not self.env.context.get('skip_transit_order_line_create_warning')
        ):
            warn_transits._warn_pickings_of_change(reason='transit_line_change')

        return result


    def write(self, vals):
        warn_transits = self._check_order_state(operation='write')

        if 'product_id' in vals and 'product_uom' not in vals:
            product = self.env['product.product'].browse(vals['product_id'])
            if product.uom_id:
                vals['product_uom'] = product.uom_id.id
        if 'product_id' in vals and not vals.get('name'):
            product = self.env['product.product'].browse(vals['product_id'])
            vals['name'] = product.display_name

        result = super().write(vals)

        if (
            warn_transits.exists()
            and not self.env.context.get('skip_transit_order_line_write_warning')
        ):
            warn_transits._warn_pickings_of_change(reason='transit_line_change')

        return result


    def unlink(self):
        warn_transits = self._check_order_state(operation='unlink')
        # Capture before deletion — recordset will be empty after super()
        # all_transits = self.mapped('transit_id')

        result = super().unlink()

        if (
            warn_transits.exists()
            and not self.env.context.get('skip_transit_order_line_unlink_warning')
        ):
            warn_transits._warn_pickings_of_change(reason='transit_line_change')

        return result

    _sql_constraints = [
        ('check_qty_positive', 
         'CHECK(product_uom_qty > 0)', 
         'The quantity must be positive!'),
    ]
                
    @api.constrains('product_uom', 'product_id')
    def _check_uom_category(self):
        """
        Validate UOM is from same category as product
        
        CRITICAL: Ensures UOM conversions are possible
        """
        for move in self:
            if move.product_id and move.product_uom:
                if move.product_uom.category_id != move.product_id.uom_id.category_id:
                    raise ValidationError(
                        f"Product '{move.product_id.display_name}': "
                        f"UOM '{move.product_uom.name}' is not compatible with "
                        f"product UOM category '{move.product_id.uom_id.category_id.name}'. "
                        f"Please select a UOM from the same category."
                    )

    @api.constrains('product_uom', 'product_id')
    def _check_uom(self):
        """Validate UOM is compatible with product UOM"""
        for move in self:
            if move.product_id and move.product_uom:
                if move.product_uom.category_id != move.product_id.uom_id.category_id:
                    raise ValidationError(
                        f'The unit of measure "{move.product_uom.name}" is not compatible with '
                        f'the product "{move.product_id.display_name}" ({move.product_id.uom_id.name}).'
                    )

    @api.constrains('product_uom_qty')
    def _check_qty_positive(self):
        """Ensure quantity is positive"""
        for move in self:
            if move.product_uom_qty <= 0:
                raise ValidationError(
                    f"Transit move for '{move.product_id.display_name}': "
                    f"Quantity must be positive (current: {move.product_uom_qty})"
                )

    @api.onchange('product_id')
    def _onchange_product_id(self):
        """Set default UOM when product changes"""
        for move in self:
            if move.product_id:
                move.product_uom = move.product_id.uom_id
                move.name = move.product_id.display_name
            else:
                move.product_uom = False