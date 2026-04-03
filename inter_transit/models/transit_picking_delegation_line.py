from odoo import fields, models # type: ignore


class TransitPickingDelegationLine(models.TransientModel):
    _name = 'transit.picking.delegation.line'
    _description = 'Transit Delegation Line'

    delegation_wizard_id = fields.Many2one(
        'transit.picking.delegation',
        ondelete='cascade',
    )

    move_id = fields.Many2one('stock.move', required=True)

    delegated_qty = fields.Float(
        string='Delegate',
        default=0.0,
        digits='Product Unit of Measure',
    )

    # Display only — never trust these coming back from client
    product_id  = fields.Many2one(related='move_id.product_id',      string='Product',          readonly=True)
    original_qty = fields.Float(  related='move_id.product_uom_qty', string='Demand',            readonly=True)
    product_uom  = fields.Many2one(related='move_id.product_uom',    string='Unit of Measure',   readonly=True)