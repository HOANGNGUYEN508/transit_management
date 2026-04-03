from odoo import api, models, fields # type: ignore
from odoo.exceptions import ValidationError # type: ignore


class StockWarehouse(models.Model):
    _inherit = 'stock.warehouse'

    is_transit_warehouse = fields.Boolean(
        string='Is Transit Warehouse',
        help='Indicates whether this warehouse is used as a transit warehouse for inter-company operations.',
        default=False,
    )
    
    @api.constrains('is_transit_warehouse', 'company_id')
    def _check_unique_transit_warehouse_per_company(self):
        for wh in self.filtered(lambda w: w.is_transit_warehouse):
            duplicate = self.env['stock.warehouse'].sudo().search([
                ('id', '!=', wh.id),
                ('company_id', '=', wh.company_id.id),
                ('is_transit_warehouse', '=', True),
            ], limit=1)
            if duplicate:
                raise ValidationError(
                    f"Company '{wh.company_id.name}' already has a transit warehouse "
                    f"('{duplicate.name}'). Only one transit warehouse per company is allowed."
                )

    def write(self, vals):
        if not self.env.context.get('skip_stock_warehouse_write_protection'):
            restricted_fields = {'lot_stock_id', 'view_location_id', 'company_id', 'active'}
            if any(field in vals for field in restricted_fields):
                blocked = self.filtered(lambda w: w.is_transit_warehouse)
                if blocked:
                    names = ', '.join(f"'{w.name}'" for w in blocked)
                    raise ValidationError(
                        f"Cannot modify the stock or view location of warehouse(s) {names} "
                        f"as they are configured in an inter-company transit picking type."
                    )
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get('skip_stock_warehouse_unlink_protection'):
            blocked = self.filtered(lambda w: w.is_transit_warehouse)
            if blocked:
                names = ', '.join(f"'{w.name}'" for w in blocked)
                raise ValidationError(
                    f"Cannot delete warehouse(s) {names} "
                    f"as they are configured in an inter-company transit picking type."
                )
        return super().unlink()