from odoo import api, fields, models  # type: ignore
from odoo.exceptions import ValidationError  # type: ignore
import re
import logging

_logger = logging.getLogger(__name__)


class TransitPickingType(models.Model):
    """
    Maps a (warehouse, company) pair to the picking-type pair used when
    transiting through a specific transit location.

    Philosophy:
    - Identity key is transit_location_id — the LCA's transit location.
    - One record per (warehouse, company, transit_location).
    - A company at depth D with W warehouses therefore has (D or D+1) × W records:
        * D     records if leaf (no own transit warehouse)
        * D + 1 records if non-leaf (own transit warehouse + all ancestors)
    - Lookup key in _validate_and_get_companies:
        1. Compute LCA(src, dest)
        2. Resolve LCA.transit_location
        3. search(company_id=X, transit_location_id=LCA.transit_location)
      → always exactly one result per side, always exactly 2 pickings, always 1 hop.

    Constraint:
    - transit_location_id must belong to the company's own transit warehouse
      OR to a transit warehouse owned by an ancestor company.
      Cross-tree configs (e.g. A1aaaa pointing to B.TRANSIT) are rejected.
    """
    _name = 'transit.picking.type'
    _description = 'Transit Picking Type Pair'
    _rec_name = 'display_name'

    display_name = fields.Char(
        string='Display Name',
        compute='_compute_display_name',
        store=False,
    )
    
    transit_location_id_display = fields.Char(
        string='Transit Location Display Name',
        compute='_compute_display_name',
        store=False,
    )
    
    transit_company_id_display = fields.Char(
        string='Transit Company Display Name',
        compute='_compute_display_name',
        store=False,
    )

    warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Warehouse',
        required=True,
        ondelete='cascade',
        help='Normal (non-transit) warehouse that owns these operation types',
    )

    company_id = fields.Many2one(
        'res.company',
        string='Company',
        required=True,
        ondelete='cascade',
        help='Company that owns the warehouse',
    )

    src_picking_type_id = fields.Many2one(
        'stock.picking.type',
        string='Delivery Operation Type',
        required=True,
        ondelete='cascade',
        domain="[('warehouse_id', '=', warehouse_id), ('company_id', '=', company_id), ('code', '=', 'outgoing')]",
        help='Operation type for sending goods TO the transit location (OUT picking)',
    )

    dest_picking_type_id = fields.Many2one(
        'stock.picking.type',
        string='Receipt Operation Type',
        required=True,
        ondelete='cascade',
        domain="[('warehouse_id', '=', warehouse_id), ('company_id', '=', company_id), ('code', '=', 'incoming')]",
        help='Operation type for receiving goods FROM the transit location (IN picking)',
    )

    transit_location_id = fields.Many2one(
        'stock.location',
        string='Transit Location',
        required=True,
        ondelete='cascade',
        help=(
            'The transit stock location (lot_stock_id of a transit warehouse) '
            'that this picking type pair routes through. '
            'Must belong to an ancestor company (or own company if non-leaf). '
            'This is the LCA transit location for any transit involving this company.'
        ),
    )

    transit_company_id = fields.Many2one(
        'res.company',
        string='Transit Location Owner',
        compute='_compute_transit_company_id',
        store=True,
        readonly=True,
        help='Company whose transit warehouse owns transit_location_id (i.e. the LCA company for transits using this config).',
    )

    @api.depends('transit_location_id')
    def _compute_transit_company_id(self):
        """Resolve which company owns the transit_location_id via its transit warehouse."""
        for record in self:
            if not record.transit_location_id:
                record.transit_company_id = False
                continue

            transit_wh = self.env['stock.warehouse'].sudo().search([
                ('lot_stock_id', '=', record.transit_location_id.id),
                ('is_transit_warehouse', '=', True),
            ], limit=1)

            record.transit_company_id = transit_wh.company_id if transit_wh else False

    @api.depends('warehouse_id', 'transit_company_id', 'company_id', 'transit_location_id')
    def _compute_display_name(self):
        """
        Human-readable label derived from the actual transit location owner.
        Old model used parent_id.name (hardcoded 1 level). New model reads
        transit_company_id so it is correct at any depth.
        """
        for record in self:
            wh_name = record.warehouse_id.sudo().name or '?'
            if record.transit_company_id:
                record.transit_company_id_display = record.transit_company_id.sudo().name
                record.transit_location_id_display = record.transit_location_id.sudo().complete_name
                # Own transit: "WH: Own Transit (A1)"
                if record.transit_company_id == record.company_id:
                    record.display_name = f"{wh_name}: Own Transit ({record.transit_company_id.sudo().name})"
                # Ancestor transit: "WH: Transit via A.TRANSIT"
                else:
                    record.display_name = f"{wh_name}: Transit via {record.transit_company_id.sudo().name}.TRANSIT"
            else:
                record.display_name = f"{wh_name}: Transit — unconfigured"

    _sql_constraints = [
        (
            'unique_transit_config_per_location',
            'UNIQUE(warehouse_id, company_id, transit_location_id)',
            'Each warehouse can only have one transit config per transit location!',
        ),
    ]

    @api.constrains('transit_location_id', 'company_id')
    def _check_transit_location_is_ancestor_or_self(self):
        """
        Enforce same-tree constraint at the record level.

        transit_location_id must be owned by:
          (a) company_id itself (own transit — only valid when company has children), OR
          (b) a direct or indirect ancestor of company_id.

        Cross-tree assignments (e.g. A1aaaa → B.TRANSIT) are rejected here
        so that _validate_and_get_companies never receives a malformed config.
        """
        for record in self:
            if not record.transit_location_id or not record.company_id:
                continue

            transit_wh = self.env['stock.warehouse'].sudo().search([
                ('lot_stock_id', '=', record.transit_location_id.id),
                ('is_transit_warehouse', '=', True),
            ], limit=1)

            if not transit_wh:
                raise ValidationError(
                    f"Transit location '{record.transit_location_id.complete_name}' "
                    f"is not the stock location of any transit warehouse. "
                    f"Only transit warehouse stock locations are valid here."
                )

            transit_owner = transit_wh.company_id

            # Case (a): own transit
            if transit_owner == record.company_id:
                continue

            # Case (b): walk ancestor chain
            ancestor = record.company_id.parent_id
            while ancestor:
                if ancestor == transit_owner:
                    break
                ancestor = ancestor.parent_id
            else:
                raise ValidationError(
                    f"Company '{record.company_id.name}': transit location "
                    f"'{record.transit_location_id.complete_name}' belongs to "
                    f"'{transit_owner.name}' which is NOT an ancestor. "
                    f"Cross-tree transit configs are not allowed."
                )

    @api.constrains('src_picking_type_id', 'dest_picking_type_id', 'warehouse_id', 'company_id', 'transit_location_id')
    def _check_picking_types_consistency(self):
        """
        Validate internal consistency of the picking-type pair:
          1. Both operation types belong to the declared warehouse and company.
          2. src sends goods TO transit_location_id.
          3. dest receives goods FROM transit_location_id.
          4. Warehouse belongs to the declared company.
        """
        for record in self:
            # ── Ownership ────────────────────────────────────────────────────
            if record.src_picking_type_id.warehouse_id != record.warehouse_id:
                raise ValidationError(
                    f"Source operation type '{record.src_picking_type_id.name}' "
                    f"must belong to warehouse '{record.warehouse_id.name}'."
                )
            if record.dest_picking_type_id.warehouse_id != record.warehouse_id:
                raise ValidationError(
                    f"Destination operation type '{record.dest_picking_type_id.name}' "
                    f"must belong to warehouse '{record.warehouse_id.name}'."
                )
            if record.src_picking_type_id.company_id != record.company_id:
                raise ValidationError(
                    f"Source operation type must belong to company '{record.company_id.name}'."
                )
            if record.dest_picking_type_id.company_id != record.company_id:
                raise ValidationError(
                    f"Destination operation type must belong to company '{record.company_id.name}'."
                )
            if record.warehouse_id.company_id != record.company_id:
                raise ValidationError(
                    f"Warehouse '{record.warehouse_id.name}' must belong to "
                    f"company '{record.company_id.name}'."
                )

            # ── Location symmetry ─────────────────────────────────────────────
            # src must deliver TO transit_location_id
            src_dest = record.src_picking_type_id.default_location_dest_id
            if src_dest != record.transit_location_id:
                raise ValidationError(
                    f"Source operation type '{record.src_picking_type_id.name}' must deliver "
                    f"TO transit location '{record.transit_location_id.complete_name}'. "
                    f"Currently delivers to '{src_dest.complete_name}'."
                )

            # dest must receive FROM transit_location_id
            dest_src = record.dest_picking_type_id.default_location_src_id
            if dest_src != record.transit_location_id:
                raise ValidationError(
                    f"Destination operation type '{record.dest_picking_type_id.name}' must "
                    f"receive FROM transit location '{record.transit_location_id.complete_name}'. "
                    f"Currently receives from '{dest_src.complete_name}'."
                )