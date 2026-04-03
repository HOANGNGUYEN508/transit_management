from odoo import models  # type: ignore
from odoo.exceptions import ValidationError  # type: ignore


class StockPickingType(models.Model):
    _inherit = 'stock.picking.type'

    def _is_inter_transit_protected_stock_picking_type(self):
        """
        A picking type is transit-managed if:
          - It belongs to a transit warehouse itself, OR
          - incoming: its source location's parent warehouse is a transit warehouse
          - outgoing: its destination location's parent warehouse is a transit warehouse
        """
        def check(pt):
            if pt.warehouse_id.is_transit_warehouse:
                return True
            if pt.code == 'incoming':
                return pt.default_location_src_id.warehouse_id.is_transit_warehouse
            if pt.code == 'outgoing':
                return pt.default_location_dest_id.warehouse_id.is_transit_warehouse
            return False

        return self.filtered(check)

    def write(self, vals):
        if not self.env.context.get('skip_stock_picking_type_write_protection'):
            restricted = {
                'default_location_src_id', 'default_location_dest_id',
                'code', 'warehouse_id', 'company_id', 'active',
            }
            if any(f in vals for f in restricted):
                blocked = self._is_inter_transit_protected_stock_picking_type()
                if blocked:
                    names = ', '.join(f"'{pt.name}'" for pt in blocked)
                    raise ValidationError(
                        f"Cannot modify picking type(s) {names} as they are "
                        f"managed by the transit setup."
                    )
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get('skip_stock_picking_type_unlink_protection'):
            blocked = self._is_inter_transit_protected_stock_picking_type()
            if blocked:
                names = ', '.join(f"'{pt.name}'" for pt in blocked)
                raise ValidationError(
                    f"Cannot delete picking type(s) {names} as they are "
                    f"managed by the transit setup."
                )
        return super().unlink()