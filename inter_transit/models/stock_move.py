from odoo import api, fields, models # type: ignore

class StockMove(models.Model):
    _inherit = 'stock.move'

    t4tek_transit_line_id = fields.Many2one(
        't4tek.transit.order.line',
        string='Transit Order Line',
        index=True,
        ondelete='set null',
        copy=True,   # CRITICAL: must copy so backorder moves inherit it
    )

    @api.model
    def _prepare_merge_moves_distinct_fields(self):
        fields = super()._prepare_merge_moves_distinct_fields()
        fields.append('t4tek_transit_line_id')
        return fields