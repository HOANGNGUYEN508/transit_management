from odoo import models, fields, api # type: ignore
from odoo.exceptions import UserError # type: ignore


class StockLocation(models.Model):
    _inherit = "stock.location"

    def _get_protected_transit_location_ids(self):
        """Get all location IDs that belong to transit warehouses and must be protected."""
        protected_ids = set()
        companies = self.env['res.company'].sudo().search([('child_ids', '!=', False)])
        for company in companies:
            transit_wh = self.env['stock.warehouse'].sudo().search([
                ('company_id', '=', company.id),
                ('name', '=', f"{company.name}.TRANSIT")
            ], limit=1)
            if transit_wh:
                # Protect the transit stock location (lot_stock_id)
                if transit_wh.lot_stock_id:
                    protected_ids.add(transit_wh.lot_stock_id.id)
                # Protect the view location parent
                if transit_wh.view_location_id:
                    protected_ids.add(transit_wh.view_location_id.id)
        return protected_ids

    def _check_protected_inter_transit_location(self, operation_type='write'):
        """
        Check if location is protected as part of a transit warehouse.

        :param operation_type: 'write' or 'unlink'
        """
        if self.env.context.get('bypass_inter_transit_location_protection'):
            return

        protected_ids = self._get_protected_transit_location_ids()
        if not protected_ids:
            return

        for location in self:
            if location.id in protected_ids:
                if operation_type == 'unlink':
                    raise UserError(
                        f"Cannot delete location '{location.complete_name}' "
                        f"as it belongs to an inter-company transit warehouse."
                    )
                elif operation_type == 'write':
                    raise UserError(
                        f"Cannot modify location '{location.complete_name}' "
                        f"as it belongs to an inter-company transit warehouse."
                    )

    def write(self, vals):
        restricted_fields = {'location_id', 'usage', 'company_id', 'active'}
        if any(field in vals for field in restricted_fields):
            self._check_protected_inter_transit_location('write')
        return super().write(vals)

    def unlink(self):
        self._check_protected_inter_transit_location('unlink')
        return super().unlink()