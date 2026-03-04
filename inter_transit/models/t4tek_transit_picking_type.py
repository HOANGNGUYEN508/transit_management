from odoo import api, fields, models  # type: ignore
from odoo.exceptions import ValidationError  # type: ignore
import logging

_logger = logging.getLogger(__name__)


class TransitPickingType(models.Model):
    """Maps transit picking type pairs by warehouse and transit location.
    
    Each record represents a pair of operation types (src + dest) for a specific:
    - Warehouse (who owns these operations)
    - Company (for organizational tracking)
    - Transit location (where goods transit through)
    
    A warehouse can have up to 2 records:
    1. Operations to/from parent company's transit location (if company has parent)
    2. Operations to/from own company's transit location (if company has children)
    """
    _name = 't4tek.transit.picking.type'
    _description = 'Transit Picking Type Pair'
    _rec_name = 'display_name'
    
    display_name = fields.Char(
        string='Display Name',
        compute='_compute_display_name',
    )
    
    warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Warehouse',
        required=True,
        ondelete='cascade',
        help='Warehouse that owns these transit operations'
    )
    
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        required=True,
        ondelete='cascade',
        help='Company that owns the warehouse and these transit operations'
    )
    
    transit_location_id = fields.Many2one(
        'stock.location',
        string='Transit Location',
        required=True,
        ondelete='cascade',
        help='Transit location these operations work with (either parent\'s or own)'
    )
    
    src_picking_type_id = fields.Many2one(
        'stock.picking.type',
        string='Delivery Operation Type',
        required=True,
        ondelete='cascade',
        domain="[('warehouse_id', '=', warehouse_id), ('company_id', '=', company_id), ('code', '=', 'Source')]",
        help='Operation type for sending goods TO transit location'
    )
    
    dest_picking_type_id = fields.Many2one(
        'stock.picking.type',
        string='Receipt Operation Type',
        required=True,
        ondelete='cascade',
        domain="[('warehouse_id', '=', warehouse_id), ('company_id', '=', company_id), ('code', '=', 'Destination')]",
        help='Operation type for receiving goods FROM transit location'
    )
    
    relation_type = fields.Selection(
        [
            ('child_to_parent', 'Child to Parent'),
            ('parent_to_child', 'Parent to Child'),
        ], 
        string='Relation Type',
        help='Type of relationship: child_to_parent (using parent\'s transit) or parent_to_child (using own transit)'
    )
    
    _sql_constraints = [
        ('unique_warehouse_transit_location',
         'UNIQUE(warehouse_id, transit_location_id)',
         'Each warehouse can only have one transit picking type pair per transit location!'),
    ]
    
    @api.depends('relation_type', 'company_id', 'warehouse_id')
    def _compute_display_name(self):
        """Compute a user-friendly display name for the transit picking type pair."""
        for record in self:
            if record.relation_type == 'child_to_parent':
                record.display_name = f"{record.warehouse_id.name}: Transit to Parent - {record.company_id.parent_id.name}"
            elif record.relation_type == 'parent_to_child':
                record.display_name = f"{record.warehouse_id.name}: Transit to Child - {record.company_id.name}"
            else:
                record.display_name = f"{record.warehouse_id.name}: Transit - {record.company_id.name}"

    @api.constrains('src_picking_type_id', 'dest_picking_type_id', 'warehouse_id', 'company_id', 'transit_location_id')
    def _check_picking_types_consistency(self):
        """Ensure picking types are consistent with warehouse, company and transit location."""
        for record in self:
            # Both picking types must belong to the same warehouse
            if record.src_picking_type_id.warehouse_id != record.warehouse_id:
                raise ValidationError(
                    f"Source operation type must belong to warehouse {record.warehouse_id.name}"
                )
            if record.dest_picking_type_id.warehouse_id != record.warehouse_id:
                raise ValidationError(
                    f"Destination operation type must belong to warehouse {record.warehouse_id.name}"
                )
            
            # Both picking types must belong to the same company
            if record.src_picking_type_id.company_id != record.company_id:
                raise ValidationError(
                    f"Source operation type must belong to company {record.company_id.name}"
                )
            if record.dest_picking_type_id.company_id != record.company_id:
                raise ValidationError(
                    f"Destination operation type must belong to company {record.company_id.name}"
                )
            
            # Verify warehouse belongs to company
            if record.warehouse_id.company_id != record.company_id:
                raise ValidationError(
                    f"Warehouse {record.warehouse_id.name} must belong to company {record.company_id.name}"
                )
            
            # Verify locations match transit location
            if record.src_picking_type_id.default_location_dest_id != record.transit_location_id:
                raise ValidationError(
                    f"Source operation type must send TO transit location {record.transit_location_id.name}"
                )
            if record.dest_picking_type_id.default_location_src_id != record.transit_location_id:
                raise ValidationError(
                    f"Destination operation type must receive FROM transit location {record.transit_location_id.name}"
                )
