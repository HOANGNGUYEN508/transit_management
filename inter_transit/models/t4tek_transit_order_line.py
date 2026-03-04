from odoo import api, fields, models # type: ignore
from odoo.exceptions import UserError, ValidationError # type: ignore
from odoo.tools import float_compare, float_is_zero, float_round # type: ignore
import logging

_logger = logging.getLogger(__name__)


class T4tekTransitOrderLine(models.Model):
    """
    Transit Order Lines:
    - This model stores the user-defined line specifications for inter-company transits.
    - These serve as templates that are used to create real stock.move records in the 
    src and dest pickings.
    - Its structure is similar to sale.order - sale.order.line.
    """
    _name = 't4tek.transit.order.line'
    _description = 'Transit Move Line'
    _rec_name = 'name'
    
    name = fields.Char(
        'Description',
        help="Description of the move (defaults to product name if empty)"
    )
    
    t4tek_transit_id = fields.Many2one(
        't4tek.transit.order',
        'Transit Order',
        required=True,
        ondelete='cascade',
        index=True
    )
    
    company_id = fields.Many2one(
        'res.company',
        'Company',
        related='t4tek_transit_id.company_id',
        store=True,
        readonly=True
    )
    
    state = fields.Selection(
        related='t4tek_transit_id.state',
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
        related='t4tek_transit_id.name',
        string='Source Document',
        readonly=True,
        store=True
    )
    
    note = fields.Text('Notes')
    
    transit_location_id = fields.Many2one(
				'stock.location',
				'Transit Location',
				related='t4tek_transit_id.transit_location_id',
			)
    
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
                                                
    @api.onchange('product_id')
    def _onchange_product_id(self):
        """Set default UOM when product changes"""
        for move in self:
            if move.product_id:
                move.product_uom = move.product_id.uom_id
                move.name = move.product_id.display_name
            else:
                move.product_uom = False
    
    @api.model_create_multi
    def create(self, vals_list):
        """
        Create transit lines freely with batch support
        
        Auto-sync behavior:
        - IF parent transit state == 'assigned' THEN trigger _sync_lines_to_pickings()
        - This propagates changes to actual stock.move records
        - Sync uses batch operations (see t4tek.transit.order)
        """
        # Set name from product if not provided
        for vals in vals_list:
            if 'product_id' in vals and 'product_uom' not in vals:
                product = self.env['product.product'].browse(vals['product_id'])
                if product.uom_id:
                    vals['product_uom'] = product.uom_id.id

            if 'product_id' in vals and not vals.get('name'):
                product = self.env['product.product'].browse(vals['product_id'])
                vals['name'] = product.display_name
        
        # Create the records
        result = super().create(vals_list)
        
        # Auto-sync to pickings if any parent transit is in 'assigned' state
        assigned_transits = result.mapped('t4tek_transit_id').filtered(
            lambda t: t.state == 'assigned'
        )
        
        if assigned_transits.exists() and not self.env.context.get('transit_pickings_sync'):
            try:
                assigned_transits._sync_lines_to_pickings(assigned_transits)
            except Exception as e:
                _logger.warning(
                    f"Failed to auto-sync new transit lines to pickings: {str(e)}"
                )
                # Don't block creation - just log the warning
        
        return result
    
    def write(self, vals):
        """
        Modify transit lines freely with auto-sync
        
        Auto-sync behavior:
        - Groups affected transits by state
        - Only transits in 'assigned' state trigger sync
        - Sync errors logged as warnings, don't block modification
        """
        if 'product_id' in vals and 'product_uom' not in vals:
            product = self.env['product.product'].browse(vals['product_id'])
            if product.uom_id:
                vals['product_uom'] = product.uom_id.id
                
        # Update name if product changes but name wasn't explicitly set
        if 'product_id' in vals and not vals.get('name'):
            product = self.env['product.product'].browse(vals['product_id'])
            vals['name'] = product.display_name

        # Perform the write
        result = super().write(vals)
        
        # Auto-sync to pickings if any parent transit is in 'assigned' state
        assigned_transits = self.mapped('t4tek_transit_id').filtered(
            lambda t: t.state == 'assigned'
        )
        
        if assigned_transits.exists() and not self.env.context.get('transit_pickings_sync'):
            try:
                assigned_transits._sync_lines_to_pickings(assigned_transits)
            except Exception as e:
                _logger.warning(
                    f"Failed to auto-sync transit move changes to pickings: {str(e)}"
                )
                # Don't block modification - just log the warning
        
        return result

    def unlink(self):
        """
        Delete transit lines freely with cascade cleanup
        
        TRANSACTIONAL SAFETY:
        - Parent transits stored before deletion
        - Deletion completes successfully before sync
        - Sync errors logged, don't block deletion
        - Related stock.move records cleaned up in batch
        
        Cascade behavior:
        - Deleting transit lines triggers sync on parent transit
        - Sync removes corresponding stock.move records
        """
        # Store parent transits before deletion
        parent_transits = self.mapped('t4tek_transit_id')
        assigned_transits = parent_transits.filtered(lambda t: t.state == 'assigned')
        
        # Perform the deletion
        result = super().unlink()
        
        # Auto-sync to pickings if any parent transit was in 'assigned' state
        if assigned_transits.exists() and not self.env.context.get('transit_pickings_sync'):
            try:
                assigned_transits._sync_lines_to_pickings(assigned_transits)
            except Exception as e:
                _logger.warning(
                    f"Failed to auto-sync after transit move deletion: {str(e)}"
                )
                # Don't block deletion - just log the warning
        
        return result
    
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