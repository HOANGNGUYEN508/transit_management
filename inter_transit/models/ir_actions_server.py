from odoo import models  # type: ignore
from odoo.exceptions import UserError  # type: ignore
from .inter_transit_utils import TRANSIT_PAIRS, TRANSIT_PAIRS_REVERSED


class IrActionsServer(models.Model):
    _inherit = "ir.actions.server"

    def _is_inter_transit_protected_ir_actions_server(self):
        if self.env.registry._init:
            return
        if self.env.context.get('skip_ir_actions_server_protection') is True:
            return
        protected_ids = self._get_protected_ids()
        if protected_ids.intersection(self.ids):
            raise UserError(
                "This action server is protected by the module inter_transit "
                "and cannot be modified or deleted nor archived."
            )

    def _get_protected_ids(self):
        protected_ids = set()
        for action_xmlid in TRANSIT_PAIRS:
            rec = self.env.ref(action_xmlid, raise_if_not_found=False)
            if rec:
                protected_ids.add(rec.id)
        return protected_ids

    def write(self, vals):
        self._is_inter_transit_protected_ir_actions_server()
        return super().write(vals)

    def unlink(self):
        self._is_inter_transit_protected_ir_actions_server()
        return super().unlink()