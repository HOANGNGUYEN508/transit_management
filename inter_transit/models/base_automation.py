from odoo import models  # type: ignore
from odoo.exceptions import UserError  # type: ignore
from odoo.fields import Command  # type: ignore
from .inter_transit_utils import TRANSIT_PAIRS, TRANSIT_PAIRS_REVERSED


class BaseAutomation(models.Model):
    _inherit = "base.automation"

    def _is_inter_transit_protected_base_automation(self):
        if self.env.registry._init:
            return
        if self.env.context.get('skip_base_automation_protection') is True:
            return
        protected_ids = self._get_protected_ids()
        if protected_ids.intersection(self.ids):
            raise UserError(
                "This automation rule is protected by the module inter_transit "
                "and cannot be modified or deleted nor archived."
            )

    def _get_protected_ids(self):
        protected_ids = set()
        for automation_xmlid in TRANSIT_PAIRS_REVERSED:
            rec = self.env.ref(automation_xmlid, raise_if_not_found=False)
            if rec:
                protected_ids.add(rec.id)
        return protected_ids

    def write(self, vals):
        self._is_inter_transit_protected_base_automation()
        return super().write(vals)

    def unlink(self):
        self._is_inter_transit_protected_base_automation()
        return super().unlink()