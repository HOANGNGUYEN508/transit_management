from odoo import models # type: ignore
from odoo.exceptions import ValidationError # type: ignore


class StockLocation(models.Model):
    _inherit = "stock.location"

    def _assert_not_protected(self, operation):
        """
        Raise ValidationError for any location that belongs to a transit warehouse.

        Uses stock.location.warehouse_id (Odoo built-in computed field) to
        resolve which warehouse owns each location, then checks
        is_t4tek_transit_warehouse on that warehouse.

        :param operation: 'write' or 'unlink' (used only for the error message)
        """

        blocked = self.filtered(lambda l: l.warehouse_id.is_t4tek_transit_warehouse)

        if not blocked:
            return

        names = ', '.join(f"'{loc.complete_name}'" for loc in blocked)
        if operation == 'unlink':
            raise ValidationError(
                f"Cannot delete location(s) {names} "
                f"as they belong to an inter-company transit warehouse."
            )
        raise ValidationError(
            f"Cannot modify location(s) {names} "
            f"as they belong to an inter-company transit warehouse."
        )

    def write(self, vals):
        if not self.env.context.get('skip_t4tek_stock_location_write_protection'):
            restricted_fields = {'location_id', 'usage', 'company_id', 'active'}
            if any(field in vals for field in restricted_fields):
                self._assert_not_protected('write')
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get('skip_t4tek_stock_location_unlink_protection'):
            self._assert_not_protected('unlink')
        return super().unlink()