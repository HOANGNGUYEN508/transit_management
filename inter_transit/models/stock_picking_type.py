from odoo import models # type: ignore
from odoo.exceptions import ValidationError # type: ignore


class StockPickingType(models.Model):
    _inherit = 'stock.picking.type'

    def write(self, vals):
        if not self.env.context.get('skip_t4tek_stock_picking_type_write_protection'):
            restricted = {'default_location_src_id', 'default_location_dest_id', 'code', 'warehouse_id', 'company_id', 'active'}
            if any(f in vals for f in restricted):
                blocked = self.filtered(
                    lambda pt: pt.warehouse_id.is_t4tek_transit_warehouse
                )
                if blocked:
                    names = ', '.join(f"'{pt.name}'" for pt in blocked)
                    raise ValidationError(
                        f"Cannot modify picking type(s) {names} as they belong to a transit warehouse."
                    )
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get('skip_t4tek_stock_picking_type_unlink_protection'):
            blocked = self.filtered(
                lambda pt: pt.warehouse_id.is_t4tek_transit_warehouse
            )
            if blocked:
                names = ', '.join(f"'{pt.name}'" for pt in blocked)
                raise ValidationError(
                    f"Cannot delete picking type(s) {names} as they belong to a transit warehouse."
                )
        return super().unlink()