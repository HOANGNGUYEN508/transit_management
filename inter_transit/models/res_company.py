from odoo import api, fields, models  # type: ignore
from odoo.exceptions import ValidationError  # type: ignore
import re
import logging

_logger = logging.getLogger(__name__)


class ResCompany(models.Model):
    _inherit = 'res.company'

    def _get_all_descendants(self):
        """
        Return all companies in the subtree rooted at each company in self,
        EXCLUDING self.

        Used by _create_transit_warehouse action to propagate new transit
        warehouse configs down to all existing descendants, not just
        direct children.

        Example:
            A._get_all_descendants() → {A1, A1a, A1aa, A1aaa, A1aaaa, A2, ...}
        """
        result = self.env['res.company']
        queue = list(self.mapped('child_ids'))
        while queue:
            company = queue.pop()
            result |= company
            queue.extend(company.child_ids)
        return result

    @staticmethod
    def _compute_lca(company_a, company_b):
        """
        Compute the Lowest Common Ancestor of two companies in the same tree.

        Returns the LCA res.company record, or empty recordset if the two
        companies are in different trees (no shared root).

        Algorithm:
          1. Build ancestor chain for company_a (inclusive): [a, a.parent, ...]
          2. Walk company_b's ancestor chain (inclusive) until a match is found.
          3. First match is the LCA.

        Guaranteed:
          - LCA always has children (it has at least company_a and company_b
            as descendants), therefore always has a transit warehouse.
        """
        # Build set of company_a's ancestry (id → record)
        a_ancestors = {}
        current = company_a
        while current:
            a_ancestors[current.id] = current
            current = current.parent_id

        # Walk company_b upward until we hit something in a_ancestors
        current = company_b
        while current:
            if current.id in a_ancestors:
                return current
            current = current.parent_id

        return company_a.env['res.company']  # empty — different trees

    def _create_transit_warehouse(self):
        """
        Create the transit warehouse for companies that have children.

        Structure created:
        - Warehouse: {company_name}.TRANSIT  (belongs to company)
        - View Location: {company_name}.TRANSIT  (company_id=False)
        - Stock Location: Stock  (type=transit, parent=View, company_id=False)
        - Complete name: {company_name}.TRANSIT/Stock
        """
        Warehouse = self.env['stock.warehouse'].sudo()
        Location = self.env['stock.location'].sudo()
        Sequence = self.env['ir.sequence'].sudo()

        for company in self:
            if not company.child_ids:
                _logger.info(
                    f"[res.company] Company '{company.name}' has no children, "
                    f"skipping transit warehouse creation"
                )
                continue

            existing_transit_wh = Warehouse.search([
                ('company_id', '=', company.id),
                ('is_transit_warehouse', '=', True),
            ])

            if existing_transit_wh:
                _logger.info(
                    f"[res.company] Transit warehouse already exists for '{company.name}'"
                )
                continue

            clean_name = re.sub(r'[^A-Z0-9]', '', company.name.upper())
            unique_code = (clean_name[:3] + '_T') if clean_name else 'WH_T'

            view_location = Location.create({
                'name': f"{company.name}.TRANSIT",
                'usage': 'view',
                'company_id': False,
            })

            transit_location = Location.create({
                'name': 'Stock',
                'location_id': view_location.id,
                'usage': 'transit',
                'active': True,
                'company_id': False,
            })

            transit_wh = Warehouse.with_context(
                skip_create_warehouse_transit_picking_types=True,
                skip_stock_picking_type_write_protection=True,
            ).create({
                'name': f"{company.name}.TRANSIT",
                'code': unique_code,
                'company_id': company.id,
                'reception_steps': 'one_step',
                'delivery_steps': 'ship_only',
                'is_transit_warehouse': True,
            })

            temp_transit_location = transit_wh.lot_stock_id
            temp_transit_view_location = transit_wh.view_location_id

            transit_wh.with_context(
                skip_create_warehouse_transit_picking_types=True,
                skip_stock_warehouse_write_protection=True,
            ).sudo().write({
                'view_location_id': view_location.id,
                'lot_stock_id': transit_location.id,
            })

            bypass_ctx = {'skip_stock_location_write_protection': True}
            temp_transit_location.sudo().with_context(**bypass_ctx).write({'active': False})
            temp_transit_view_location.sudo().with_context(**bypass_ctx).write({'active': False})

            sequence_code = 'transit.order'
            company_name_clean = company.name.replace(' ', '_')
            prefix = f"{company_name_clean}/TRANSIT/"

            existing_seq = Sequence.search([
                ('code', '=', sequence_code),
                ('company_id', '=', company.id),
            ], limit=1)

            if not existing_seq:
                Sequence.create({
                    'name': f'{company.name} Sequence Transit Order',
                    'code': sequence_code,
                    'prefix': prefix,
                    'padding': 5,
                    'number_increment': 1,
                    'number_next': 1,
                    'company_id': company.id,
                    'implementation': 'standard',
                })

            company._archive_transit_warehouse_defaults()

            _logger.info(
                f"[res.company] Created transit warehouse '{transit_wh.name}' "
                f"(id={transit_wh.id}) for company '{company.name}'"
            )

    def _archive_transit_warehouse_defaults(self):
        """Archive default sequences and picking types auto-created by Odoo on transit warehouse creation."""
        PickingType = self.env['stock.picking.type'].sudo()
        Sequence = self.env['ir.sequence'].sudo()

        for company in self:
            transit_wh = self.env['stock.warehouse'].sudo().search([
                ('company_id', '=', company.id),
                ('is_transit_warehouse', '=', True),
            ])
            if not transit_wh:
                continue

            picking_types = PickingType.search([
                ('warehouse_id', '=', transit_wh.id),
                ('active', '=', True),
            ])
            if not picking_types:
                continue

            sequence_ids = picking_types.mapped('sequence_id.id')
            picking_types.with_context(
                skip_stock_picking_type_write_protection=True
            ).write({'active': False})

            if sequence_ids:
                sequences = Sequence.search([
                    ('id', 'in', sequence_ids),
                    ('active', '=', True),
                ])
                if sequences:
                    sequences.write({'active': False})

    def _get_transit_targets_for_company(self, company):
        """
        Return the ordered list of transit locations a company's warehouses
        must be able to route through.

        Each entry:
            {
                'transit_location': stock.location,   # lot_stock_id of the transit WH
                'transit_company':  res.company,       # owner of that transit WH
                'transit_wh_code':  str,               # short code for sequence prefixes
            }

        Order:
            1. Ancestor transit locations, nearest-first (direct parent → root).
            2. Own transit location last (if company has children).

        The order is cosmetic — lookup is always by transit_location_id,
        not by position.
        """
        Warehouse = self.env['stock.warehouse'].sudo()
        targets = []

        # ── 1. Walk up ancestor chain ─────────────────────────────────────────
        ancestor = company.parent_id
        while ancestor:
            transit_wh = Warehouse.search([
                ('company_id', '=', ancestor.id),
                ('is_transit_warehouse', '=', True),
            ], limit=1)

            if transit_wh and transit_wh.lot_stock_id:
                targets.append({
                    'transit_location': transit_wh.lot_stock_id,
                    'transit_company':  ancestor,
                    # Keep codes short to stay within reasonable sequence prefix lengths.
                    'transit_wh_code':  transit_wh.code or re.sub(r'[^A-Z0-9]', '', ancestor.name.upper())[:6],
                })
            else:
                _logger.warning(
                    f"[res.company] Ancestor '{ancestor.name}' of '{company.name}' "
                    f"has no transit warehouse — skipping this level."
                )

            ancestor = ancestor.parent_id

        # ── 2. Own transit location (only if company has children) ────────────
        if company.child_ids:
            own_transit_wh = Warehouse.search([
                ('company_id', '=', company.id),
                ('is_transit_warehouse', '=', True),
            ], limit=1)

            if own_transit_wh and own_transit_wh.lot_stock_id:
                targets.append({
                    'transit_location': own_transit_wh.lot_stock_id,
                    'transit_company':  company,
                    'transit_wh_code':  own_transit_wh.code or re.sub(r'[^A-Z0-9]', '', company.name.upper())[:6],
                })
            else:
                _logger.warning(
                    f"[res.company] '{company.name}' has children but no transit warehouse yet. "
                    f"Own-transit config will be created once the transit warehouse exists."
                )

        return targets

    def _find_or_create_transit_picking_type(
        self, PickingType, Sequence,
        wh, company, src_location, dest_location,
        code, name, sequence_prefix, dest_label,
    ):
        """
        Return the stock.picking.type for the given (wh, company, src, dest),
        creating it (and its ir.sequence) if it does not already exist.

        Args:
            PickingType:      sudo env for stock.picking.type
            Sequence:         sudo env for ir.sequence
            wh:               stock.warehouse record
            company:          res.company record
            src_location:     stock.location — default_location_src_id
            dest_location:    stock.location — default_location_dest_id
            code:             'outgoing' | 'incoming'
            name:             human-readable name for the operation type
            sequence_prefix:  unique prefix string for the ir.sequence
            dest_label:       destination company name (used in sequence name)

        Returns:
            stock.picking.type record, or False on failure.
        """
        existing = PickingType.search([
            ('warehouse_id', '=', wh.id),
            ('company_id', '=', company.id),
            ('default_location_src_id', '=', src_location.id),
            ('default_location_dest_id', '=', dest_location.id),
        ], limit=1)

        if existing:
            _logger.info(
                f"[res.company] Picking type already exists: '{existing.name}' "
                f"(id={existing.id}) for warehouse '{wh.name}'"
            )
            return existing

        # ── Create ir.sequence ────────────────────────────────────────────────
        sequence = Sequence.search([
            ('code', '=', 'transit.picking'),
            ('company_id', '=', company.id),
            ('prefix', '=', sequence_prefix),
        ], limit=1)

        if not sequence:
            direction = 'OUT' if code == 'outgoing' else 'IN'
            sequence = Sequence.create({
                'name': f"{wh.name} -> {dest_label} - Transit {direction}",
                'code': 'transit.picking',
                'prefix': sequence_prefix,
                'padding': 5,
                'number_increment': 1,
                'number_next': 1,
                'company_id': company.id,
                'implementation': 'standard',
            })
            _logger.info(
                f"[res.company] Created sequence prefix '{sequence_prefix}' "
                f"for warehouse '{wh.name}'"
            )

        # ── Create stock.picking.type ─────────────────────────────────────────
        picking_data = {
            'name': name,
            'code': code,
            'sequence_id': sequence.id,
            'sequence_code': 'TRANSIT/OUT' if code == 'outgoing' else 'TRANSIT/IN',
            'warehouse_id': wh.id,
            'company_id': company.id,
            'default_location_src_id': src_location.id,
            'default_location_dest_id': dest_location.id,
            'return_picking_type_id': False,
            # OUT: use existing lots (goods already tracked); IN: create lots on receipt
            'use_create_lots': code == 'incoming',
            'use_existing_lots': code == 'outgoing',
        }
        if code == 'outgoing':
            picking_data['reservation_method'] = 'at_confirm'

        picking_type = PickingType.create(picking_data)
        _logger.info(
            f"[res.company] Created picking type '{name}' (id={picking_type.id}, "
            f"code={code}) for warehouse '{wh.name}'"
        )
        return picking_type

    def _create_warehouse_transit_picking_types(self, warehouse=None):
        """
        Create warehouse-level transit picking type pairs for all transit
        locations a company can legally route through.

        New philosophy (replaces binary relation_type approach):
        - For each company, collect ALL transit targets:
            1. Each ancestor company's transit location (nearest → root).
            2. Own transit location (if company has children).
        - For each normal warehouse × each transit target:
            * Create OUT picking type:  warehouse.Stock → transit_location
            * Create IN  picking type:  transit_location → warehouse.Stock
            * Create transit.picking.type record keyed by transit_location_id.

        Record count per warehouse:
            depth D, non-leaf:  D + 1 records
            depth D, leaf:      D     records

        Idempotent: existing picking types and config records are detected and
        skipped, so this method is safe to call multiple times.

        Args:
            warehouse: Optional specific stock.warehouse to scope creation to.
                       If None, processes all normal warehouses of each company.
        """
        # ======================================================================================
        if self.env.context.get('skip_create_warehouse_transit_picking_types'):
            return
        # ======================================================================================

        # Allow cross-company location access (transit locations have company_id=False)
        all_company_ids = self.env['res.company'].sudo().search([]).ids
        PickingType = self.env['stock.picking.type'].sudo().with_context(
            allowed_company_ids=all_company_ids
        )
        Sequence = self.env['ir.sequence'].sudo()
        TransitPickingType = self.env['transit.picking.type'].sudo()
        Warehouse = self.env['stock.warehouse'].sudo()

        for company in self:
            # ── 1. Resolve all transit targets for this company ───────────────
            transit_targets = self._get_transit_targets_for_company(company)

            if not transit_targets:
                _logger.info(
                    f"[res.company] '{company.name}' has no transit targets "
                    f"(no ancestors with transit warehouses and no own transit). Skipping."
                )
                continue

            # ── 2. Resolve normal warehouses to configure ─────────────────────
            # Transit warehouses are excluded via lot_stock_id.usage='internal' filter.
            if warehouse:
                warehouses = warehouse if warehouse.company_id == company else Warehouse.browse()
            else:
                warehouses = Warehouse.search([
                    ('company_id', '=', company.id),
                    ('lot_stock_id.usage', '=', 'internal'),
                ])

            if not warehouses:
                _logger.warning(
                    f"[res.company] '{company.name}' has no normal warehouses. "
                    f"Skipping picking type creation."
                )
                continue

            # ── 3. For each warehouse × each transit target, create the pair ──
            for wh in warehouses:
                if not wh.lot_stock_id or wh.lot_stock_id.usage != 'internal':
                    _logger.warning(
                        f"[res.company] Warehouse '{wh.name}' has no valid stock location. Skipping."
                    )
                    continue

                wh_stock = wh.lot_stock_id
                wh_code = wh.code or re.sub(r'[^A-Z0-9_]', '', wh.name.upper().replace(' ', '_'))

                for target in transit_targets:
                    transit_location = target['transit_location']
                    transit_company  = target['transit_company']
                    transit_wh_code  = target['transit_wh_code']
                    label = transit_company.name

                    out_prefix = f'{wh_code}/TRANSIT/OUT/'
                    in_prefix  = f'{wh_code}/TRANSIT/IN/'

                    # OUT: wh_stock → transit_location
                    out_type = self._find_or_create_transit_picking_type(
                        PickingType, Sequence,
                        wh=wh,
                        company=company,
                        src_location=wh_stock,
                        dest_location=transit_location,
                        code='outgoing',
                        name=f'Transit Deliveries to {label}',
                        sequence_prefix=out_prefix,
                        dest_label=label,
                    )

                    # IN: transit_location → wh_stock
                    in_type = self._find_or_create_transit_picking_type(
                        PickingType, Sequence,
                        wh=wh,
                        company=company,
                        src_location=transit_location,
                        dest_location=wh_stock,
                        code='incoming',
                        name=f'Transit Receipts from {label}',
                        sequence_prefix=in_prefix,
                        dest_label=label,
                    )

                    if not out_type or not in_type:
                        _logger.warning(
                            f"[res.company] Could not create picking type pair "
                            f"for '{wh.name}' via {label}.TRANSIT. Skipping config record."
                        )
                        continue

                    # transit.picking.type record — keyed by transit_location_id
                    existing_config = TransitPickingType.search([
                        ('warehouse_id', '=', wh.id),
                        ('company_id', '=', company.id),
                        ('transit_location_id', '=', transit_location.id),
                    ], limit=1)

                    if not existing_config:
                        TransitPickingType.create({
                            'warehouse_id':        wh.id,
                            'company_id':          company.id,
                            'transit_location_id': transit_location.id,
                            'src_picking_type_id': out_type.id,
                            'dest_picking_type_id': in_type.id,
                        })
                        _logger.info(
                            f"[res.company] Created transit config: "
                            f"warehouse='{wh.name}' company='{company.name}' "
                            f"via {label}.TRANSIT"
                        )
                    else:
                        _logger.info(
                            f"[res.company] Config already exists: "
                            f"warehouse='{wh.name}' via {label}.TRANSIT"
                        )

    def _update_transit_sequences_for_warehouse(self, company, warehouse, warehouse_code):
        """
        Update IN and OUT transit sequence prefixes for a single warehouse.

        Prefix format: {WH_CODE}/TRANSIT/{IN|OUT}/

        Called when:
        - Company is renamed (automation_handle_company_name_change)
        - Warehouse code changes (extend if needed)
        """
        TransitPickingType = self.env['transit.picking.type'].sudo()
        Warehouse = self.env['stock.warehouse'].sudo()
        ctx = {'skip_ir_sequence_write_protection': True}

        transit_configs = TransitPickingType.search([
            ('warehouse_id', '=', warehouse.id),
            ('company_id', '=', company.id),
        ])

        if not transit_configs:
            _logger.info(
                f"[res.company] No transit configs found for warehouse '{warehouse.name}', "
                f"skipping sequence update."
            )
            return

        for config in transit_configs:
            # Resolve the transit WH code from transit_location_id
            transit_wh = Warehouse.search([
                ('lot_stock_id', '=', config.transit_location_id.id),
                ('is_transit_warehouse', '=', True),
            ], limit=1)

            if not transit_wh:
                _logger.warning(
                    f"[res.company] Cannot resolve transit WH for config id={config.id}. "
                    f"Skipping sequence update."
                )
                continue

            transit_wh_code = transit_wh.code or re.sub(
                r'[^A-Z0-9]', '', transit_wh.company_id.name.upper()
            )[:6]

            new_out_prefix = f'{warehouse_code}/TRANSIT/OUT/'
            new_in_prefix  = f'{warehouse_code}/TRANSIT/IN/'

            out_seq = config.src_picking_type_id.sequence_id
            if out_seq and out_seq.prefix != new_out_prefix:
                out_seq.with_context(**ctx).write({'prefix': new_out_prefix})
                _logger.info(
                    f"[res.company] Updated OUT prefix → '{new_out_prefix}' "
                    f"for warehouse '{warehouse.name}' via {transit_wh.company_id.name}.TRANSIT"
                )

            in_seq = config.dest_picking_type_id.sequence_id
            if in_seq and in_seq.prefix != new_in_prefix:
                in_seq.with_context(**ctx).write({'prefix': new_in_prefix})
                _logger.info(
                    f"[res.company] Updated IN  prefix → '{new_in_prefix}' "
                    f"for warehouse '{warehouse.name}' via {transit_wh.company_id.name}.TRANSIT"
                )
                
    def automation_handle_company_name_change(self):
        """
        AUTOMATION WRAPPER: Update transit warehouse/sequence names when company name changes.
        """
        errors = []

        for company in self:
            try:
                transit_wh = self.env['stock.warehouse'].sudo().search([
                    ('company_id', '=', company.id),
                    ('is_transit_warehouse', '=', True),
                ])

                if not transit_wh:
                    continue

                new_wh_name = f"{company.name}.TRANSIT"
                if transit_wh.name != new_wh_name:
                    transit_wh.sudo().with_context(
                        skip_stock_warehouse_write_protection=True
                    ).write({'name': new_wh_name})

                if transit_wh.view_location_id:
                    new_view_name = f"{company.name}.TRANSIT"
                    if transit_wh.view_location_id.name != new_view_name:
                        transit_wh.view_location_id.with_context(
                            skip_stock_location_write_protection=True
                        ).sudo().write({'name': new_view_name})

                company_code = company.name.replace(' ', '_')
                new_order_prefix = f"{company_code}/TRANSIT/"

                order_seq = self.env['ir.sequence'].sudo().search([
                    ('code', '=', 'transit.order'),
                    ('company_id', '=', company.id),
                ], limit=1)

                if order_seq and order_seq.prefix != new_order_prefix:
                    order_seq.with_context(
                        skip_ir_sequence_write_protection=True
                    ).sudo().write({'prefix': new_order_prefix})

                warehouses = self.env['stock.warehouse'].sudo().search([
                    ('company_id', '=', company.id),
                    ('lot_stock_id.usage', '=', 'internal'),
                ])

                for wh in warehouses:
                    wh_code = wh.code or re.sub(
                        r'[^A-Z0-9_]', '', wh.name.upper().replace(' ', '_')
                    )
                    self._update_transit_sequences_for_warehouse(company, wh, wh_code)

            except Exception as e:
                _logger.error(
                    f"Error updating transit on name change for '{company.name}': {str(e)}",
                    exc_info=True,
                )
                errors.append(f"Company '{company.name}': {str(e)}")

        if errors:
            raise ValidationError(
                "Failed to update transit warehouse/sequences:\n• " + "\n• ".join(errors)
            )