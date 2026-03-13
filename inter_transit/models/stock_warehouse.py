from odoo import models, fields # type: ignore
from odoo.exceptions import ValidationError # type: ignore


class StockWarehouse(models.Model):
    _inherit = 'stock.warehouse'

    is_t4tek_transit_warehouse = fields.Boolean(
        string='Is Transit Warehouse',
        help='Indicates whether this warehouse is used as a transit warehouse for inter-company operations.',
        default=False,
    )
    
    _sql_constraints = [
        ('unique_t4tek_transit_warehouse_company_relation',
        'UNIQUE(is_t4tek_transit_warehouse, company_id)',
        'Each company can only have one transit warehouse!'),
    ]

    def write(self, vals):
        if not self.env.context.get('skip_t4tek_stock_warehouse_write_protection'):
            restricted_fields = {'lot_stock_id', 'view_location_id', 'company_id', 'active'}
            if any(field in vals for field in restricted_fields):
                blocked = self.filtered(lambda w: w.is_t4tek_transit_warehouse)
                if blocked:
                    names = ', '.join(f"'{w.name}'" for w in blocked)
                    raise ValidationError(
                        f"Cannot modify the stock or view location of warehouse(s) {names} "
                        f"as they are configured in an inter-company transit picking type."
                    )
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get('skip_t4tek_stock_warehouse_unlink_protection'):
            blocked = self.filtered(lambda w: w.is_t4tek_transit_warehouse)
            if blocked:
                names = ', '.join(f"'{w.name}'" for w in blocked)
                raise ValidationError(
                    f"Cannot delete warehouse(s) {names} "
                    f"as they are configured in an inter-company transit picking type."
                )
        return super().unlink()