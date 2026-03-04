from odoo import models, fields, api # type: ignore
from odoo.exceptions import UserError # type: ignore


class IrSequence(models.Model):
    _inherit = "ir.sequence"

    def _check_protected_ir_sequence(self, operation_type='write', vals=None):
        """
        Check if sequence is protected as a transit sequence.
        
        :param operation_type: 'write' or 'unlink'
        :param vals: Dictionary of values being written (for write operations)
        """
        # Skip check if bypass is enabled
        if self.env.context.get('bypass_inter_transit_ir_sequence_protection'):
            return
        
        # Find all protected transit sequences
        protected_sequence_ids = self.env['ir.sequence'].search(
            [('code', 'in', ['t4tek.transit.order', 't4tek.transit.picking'])]
        ).mapped('id')
        
        if not protected_sequence_ids:
            return
            
        # Check each record being operated on
        for sequence in self:
            if sequence.id in protected_sequence_ids:
                # For unlink operations, always deny
                if operation_type == 'unlink':
                    raise UserError(
                        f"Cannot delete transit sequence '{sequence.name}' "
                        f"as it is used for inter-company transfers."
                    )
                
                # For write operations, always deny if sequence is protected
                elif operation_type == 'write':
                    raise UserError(
                        f"Cannot modify field {vals} of transit sequence '{sequence.name}' "
                        f"as it is used for inter-company transit picking."
                    )

    def write(self, vals):
        restricted_fields = {'active', 'code', 'company_id', 'name'}
        if any(field in vals for field in restricted_fields):
            self._check_protected_ir_sequence('write', vals)
        return super().write(vals)

    def unlink(self):
        self._check_protected_ir_sequence('unlink')
        return super().unlink()