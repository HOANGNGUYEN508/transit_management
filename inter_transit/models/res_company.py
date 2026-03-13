from odoo import api, fields, models # type: ignore
from odoo.exceptions import ValidationError # type: ignore
import re
import logging

_logger = logging.getLogger(__name__)


class ResCompany(models.Model):
    _inherit = 'res.company'
                                          
    def _create_transit_warehouse(self):
        """Create the transit warehouse for companies that have children.
        
        Structure created:
        - Warehouse: {company_name}.TRANSIT (belongs to company)
        - View Location: {company_name}.TRANSIT (company_id=False)
        - Stock Location: Stock (type=transit, parent=View, company_id=False)
        - Complete name: {company_name}.TRANSIT/Stock
        """
        Warehouse = self.env['stock.warehouse'].sudo()
        Location = self.env['stock.location'].sudo()
        Sequence = self.env['ir.sequence'].sudo()

        for company in self:
            # Only create for companies with children
            if not company.child_ids:
                _logger.info(
                    f"[res.company] Company '{company.name}' has no children, skipping transit warehouse creation"
                )
                continue

            # Idempotency check via t4tek.transit.picking.type
            existing_transit_wh = self.env['stock.warehouse'].sudo().search([
                '&',
                ('company_id', '=', company.id),
                ('is_t4tek_transit_warehouse', '=', True),
            ])

            if existing_transit_wh:
                _logger.info(
                    f"[res.company] Transit warehouse already exists for company '{company.name}'"
                    + (f" (warehouse: '{existing_transit_wh.name}', id={existing_transit_wh.id})" if existing_transit_wh else "")
                )
                continue

            # Derive unique 5-char code
            clean_name = re.sub(r'[^A-Z0-9]', '', company.name.upper())
            unique_code = (clean_name[:3] + '_T') if clean_name else 'WH_T'

            # Step 1: Create view location (company_id=False for cross-company access)
            view_location = Location.create({
                'name': f"{company.name}.TRANSIT",
                'usage': 'view',
                'company_id': False,
            })

            # Step 2: Create transit stock location (company_id=False for cross-company access)
            transit_location = Location.create({
                'name': 'Stock',  # Will show as {company_name}.TRANSIT/Stock
                'location_id': view_location.id,
                'usage': 'transit',
                'active': True,
                'company_id': False,
            })

            # Step 3: Create warehouse with context to skip auto-creation of picking types
            transit_wh = Warehouse.with_context(
                skip_create_warehouse_transit_picking_types=True,
                skip_t4tek_stock_picking_type_write_protection=True,
            ).create({
                'name': f"{company.name}.TRANSIT",
                'code': unique_code,
                'company_id': company.id,
                'reception_steps': 'one_step',
                'delivery_steps': 'ship_only',
                'is_t4tek_transit_warehouse': True,
            })

            temp_transit_location = transit_wh.lot_stock_id
            temp_transit_view_location = transit_wh.view_location_id
            
            # FIX (Bug 1): Pass bypass context so the write() guard on StockWarehouse
            # doesn't block us from swapping the auto-created locations with our own.
            # Note: we need to write separately to set the locations due to odoo logic
            # that creates their own view/stock locations on warehouse creation.
            transit_wh.with_context(
                skip_create_warehouse_transit_picking_types=True,
                skip_t4tek_stock_warehouse_write_protection=True,
            ).sudo().write({
                'view_location_id': view_location.id,
                'lot_stock_id': transit_location.id,
            })

            bypass_ctx = {'skip_t4tek_stock_location_write_protection': True}
            temp_transit_location.sudo().with_context(**bypass_ctx).write({'active': False})
            temp_transit_view_location.sudo().with_context(**bypass_ctx).write({'active': False})

            # Step 4: Create transit order sequence
            sequence_code = 't4tek.transit.order'
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
                _logger.info(
                    f"[res.company] Created transit ORDER sequence with prefix '{prefix}' for company '{company.name}' (id={company.id})"
                )

            # Step 5: Archive default sequences and picking types created by Odoo (we don't need them)
            company._archive_transit_warehouse_defaults()

            _logger.info(
                f"[res.company] Created transit warehouse '{transit_wh.name}' (id={transit_wh.id}, code={unique_code}) "
                f"with view location '{view_location.name}' (id={view_location.id}) and transit location '{transit_location.name}' (id={transit_location.id}, complete_name='{transit_location.complete_name}') "
                f"for company '{company.name}' (id={company.id})",
            )
            
    def _archive_transit_warehouse_defaults(self):
        """Archive the default sequences and picking types auto-created by Odoo on transit warehouse creation."""
        PickingType = self.env['stock.picking.type'].sudo()
        Sequence = self.env['ir.sequence'].sudo()

        for company in self:
            transit_wh = self.env['stock.warehouse'].sudo().search([
                '&',
                ('company_id', '=', company.id),
                ('is_t4tek_transit_warehouse', '=', True),
            ])

            if not transit_wh:
                _logger.info(
                    f"[res.company] No transit warehouse found for company '{company.name}', skipping archive."
                )
                continue

            picking_types = PickingType.search([
                ('warehouse_id', '=', transit_wh.id),
                ('active', '=', True),
            ])

            if not picking_types:
                _logger.info(
                    f"[res.company] No default picking types found to archive for transit warehouse of company '{company.name}'"
                )
                continue

            sequence_ids = picking_types.mapped('sequence_id.id')

            picking_types.with_context(skip_t4tek_stock_picking_type_write_protection=True).write({'active': False})
            _logger.info(
                f"[res.company] Archived {len(picking_types)} default picking type(s) for transit warehouse "
                f"of company '{company.name}': {picking_types.mapped('name')}"
            )

            if sequence_ids:
                sequences = Sequence.search([
                    ('id', 'in', sequence_ids),
                    ('active', '=', True),
                ])
                if sequences:
                    sequences.write({'active': False})
                    _logger.info(
                        f"[res.company] Archived {len(sequences)} default sequence(s) for transit warehouse "
                        f"of company '{company.name}': {sequences.mapped('name')}"
                    )
    
    def _create_warehouse_transit_picking_types(self, warehouse=None):
        """Create warehouse-level transit picking types based on parent/child relationships.
        
        For each warehouse belonging to a company:
        - If company has parent: warehouse gets 2 operation types for parent's transit (OUT and IN)
        - If company has children: warehouse gets 2 operation types for own transit (OUT and IN)
        
        These are created at WAREHOUSE level (warehouse_id populated) and mapped by warehouse + relation type.
        
        Args:
            warehouse: Optional specific warehouse to create types for. If None, processes all company warehouses.
        """
        # ======================================================================================
        if self.env.context.get('skip_create_warehouse_transit_picking_types'):
            return
        # ======================================================================================

        # FIX (Bug 2): Build a sudo env with all company IDs allowed so Odoo's cross-company
        # ORM check does not reject picking types whose src/dest locations have company_id=False
        # but are "owned" by a different company in the current environment context.
        all_company_ids = self.env['res.company'].sudo().search([]).ids
        PickingType = self.env['stock.picking.type'].sudo().with_context(
            allowed_company_ids=all_company_ids
        )
        Sequence = self.env['ir.sequence'].sudo()
        TransitPickingType = self.env['t4tek.transit.picking.type'].sudo()
        Warehouse = self.env['stock.warehouse'].sudo()
        
        for company in self:
            is_parent = bool(company.child_ids)
            is_child = bool(company.parent_id)
            
            if not (is_parent or is_child):
                continue
            
            # Transit warehouses have lot_stock_id.usage = 'transit', so the existing
            # ('lot_stock_id.usage', '=', 'internal') filter already excludes them.
            if warehouse:
                warehouses = warehouse if warehouse.company_id == company else Warehouse.browse()
            else:
                warehouses = Warehouse.search([
                    ('company_id', '=', company.id),
                    ('lot_stock_id.usage', '=', 'internal'),
                ])
            
            if not warehouses:
                _logger.warning(
                    f"[res.company] Company '{company.name}' has no normal warehouses. Skipping transit picking type creation."
                )
                continue
            
            for wh in warehouses:
                if not wh.lot_stock_id or wh.lot_stock_id.usage != 'internal':
                    _logger.warning(
                        f"[res.company] Warehouse '{wh.name}' (id={wh.id}) has no valid stock location. Skipping."
                    )
                    continue
                
                warehouse_stock_location = wh.lot_stock_id
                warehouse_code = wh.code if wh.code else wh.name.replace(' ', '_')
                operations_to_create = []
                
                # ============================================
                # Part 1: Operations to PARENT's transit location (if company has parent)
                # ============================================
                if is_child and company.parent_id:
                    parent_transit_wh = self.env['stock.warehouse'].sudo().search([
                        '&',
                        ('company_id', '=', company.parent_id.id),
                        ('is_t4tek_transit_warehouse', '=', True)
                    ])
                    parent_transit = parent_transit_wh.lot_stock_id if parent_transit_wh else self.env['stock.location']
                    
                    if parent_transit:
                        operations_to_create.extend([
                            {
                                'type': 'parent_in',
                                'relation_type': 'child_to_parent',
                                'src_location': parent_transit,
                                'dest_location': warehouse_stock_location,
                                'name': f'Transit Receipts - {company.parent_id.name}',
                                'code': 'incoming',
                                'sequence_code': 'TRANSIT/IN',
                                'prefix': f'{warehouse_code}/TRANSIT/IN/',
                                'use_create_lots': True,
                                'use_existing_lots': False,
                            },
                            {
                                'type': 'parent_out',
                                'relation_type': 'child_to_parent',
                                'src_location': warehouse_stock_location,
                                'dest_location': parent_transit,
                                'name': f'Transit Deliveries - {company.parent_id.name}',
                                'code': 'outgoing',
                                'sequence_code': 'TRANSIT/OUT',
                                'prefix': f'{warehouse_code}/TRANSIT/OUT/',
                                'use_create_lots': False,
                                'use_existing_lots': True,
                                'reservation_method': 'at_confirm',
                            }
                        ])
                
                # ============================================
                # Part 2: Operations to OWN transit location (if company has children)
                # ============================================
                if is_parent:
                    own_transit_wh = self.env['stock.warehouse'].sudo().search([
                        '&',
                        ('company_id', '=', company.id),
                        ('is_t4tek_transit_warehouse', '=', True)
                    ], limit=1)
                    own_transit = own_transit_wh.lot_stock_id if own_transit_wh else self.env['stock.location']

                    if own_transit:
                        operations_to_create.extend([
                            {
                                'type': 'child_in',
                                'relation_type': 'parent_to_child',
                                'src_location': own_transit,
                                'dest_location': warehouse_stock_location,
                                'name': f'Transit Receipts - {company.name}',
                                'code': 'incoming',
                                'sequence_code': 'TRANSIT/IN',
                                'prefix': f'{warehouse_code}/TRANSIT/IN/',
                                'use_create_lots': True,
                                'use_existing_lots': False,
                            },
                            {
                                'type': 'child_out',
                                'relation_type': 'parent_to_child',
                                'src_location': warehouse_stock_location,
                                'dest_location': own_transit,
                                'name': f'Transit Deliveries - {company.name}',
                                'code': 'outgoing',
                                'sequence_code': 'TRANSIT/OUT',
                                'prefix': f'{warehouse_code}/TRANSIT/OUT/',
                                'use_create_lots': False,
                                'use_existing_lots': True,
                                'reservation_method': 'at_confirm',
                            }
                        ])
                
                if not operations_to_create:
                    _logger.warning(
                        f"[res.company] No transit locations found for warehouse '{wh.name}'"
                    )
                    continue
                
                # ============================================
                # Part 3: Create missing operation types (warehouse-level)
                # ============================================
                picking_types_created = []
                
                for op_config in operations_to_create:
                    existing = PickingType.search([
                        ('warehouse_id', '=', wh.id),
                        ('company_id', '=', company.id),
                        ('default_location_src_id', '=', op_config['src_location'].id),
                        ('default_location_dest_id', '=', op_config['dest_location'].id),
                    ], limit=1)
                    
                    if existing:
                        _logger.debug(
                            f"[res.company] Operation type already exists for warehouse '{wh.name}' (src: {op_config['src_location'].name}, dest: {op_config['dest_location'].name})"
                        )
                        picking_types_created.append({
                            'type': op_config['type'],
                            'relation_type': op_config['relation_type'],
                            'picking_type': existing,
                        })
                        continue
                    
                    sequence = Sequence.search([
                        ('code', '=', 't4tek.transit.picking'),
                        ('company_id', '=', company.id),
                        ('prefix', '=', op_config['prefix'])
                    ], limit=1)
                    
                    if not sequence:
                        direction = 'Receipts' if op_config['code'] == 'incoming' else 'Deliveries'
                        sequence = Sequence.create({
                            'name': f"{wh.name} Sequence Transit {direction}",
                            'code': 't4tek.transit.picking',
                            'prefix': op_config['prefix'],
                            'padding': 5,
                            'number_increment': 1,
                            'number_next': 1,
                            'company_id': company.id,
                            'implementation': 'standard',
                        })
                        _logger.info(
                            f"[res.company] Created sequence with prefix '{op_config['prefix']}' for warehouse '{wh.name}'"
                        )
                    
                    picking_data = {
                        'name': op_config['name'],
                        'code': op_config['code'],
                        'sequence_id': sequence.id,
                        'sequence_code': op_config['sequence_code'],
                        'warehouse_id': wh.id,
                        'company_id': company.id,
                        'default_location_src_id': op_config['src_location'].id,
                        'default_location_dest_id': op_config['dest_location'].id,
                        'return_picking_type_id': False,
                        'use_create_lots': op_config['use_create_lots'],
                        'use_existing_lots': op_config['use_existing_lots'],
                    }

                    if 'reservation_method' in op_config:
                        picking_data['reservation_method'] = op_config['reservation_method']
                    
                    picking_type = PickingType.create(picking_data)
                    picking_types_created.append({
                        'type': op_config['type'],
                        'relation_type': op_config['relation_type'],
                        'picking_type': picking_type,
                    })
                    
                    _logger.info(
                        f"[res.company] Created transit operation type '{op_config['sequence_code']}' (id={picking_type.id}) for warehouse '{wh.name}' (id={wh.id})"
                    )
                
                if not picking_types_created:
                    continue
                
                # ============================================
                # Part 4: Create t4tek.transit.picking.type records (grouped by relation_type)
                # FIX (Bug 3): Removed transit_location_id — group and search by relation_type
                # only, which matches the unique SQL constraint on the model.
                # ============================================
                from collections import defaultdict
                groups = defaultdict(list)
                
                for item in picking_types_created:
                    groups[item['relation_type']].append(item)
                
                for relation_type, items in groups.items():
                    in_type = next((i['picking_type'] for i in items if i['type'] in ['parent_in', 'child_in']), None)
                    out_type = next((i['picking_type'] for i in items if i['type'] in ['parent_out', 'child_out']), None)
                    
                    if not (in_type and out_type):
                        _logger.warning(
                            f"[res.company] Incomplete pair for warehouse '{wh.name}', relation_type '{relation_type}'. Skipping transit picking type record."
                        )
                        continue
                    
                    existing = TransitPickingType.search([
                        ('warehouse_id', '=', wh.id),
                        ('company_id', '=', company.id),
                        ('relation_type', '=', relation_type),
                    ], limit=1)
                    
                    if not existing:
                        TransitPickingType.create({
                            'warehouse_id': wh.id,
                            'company_id': company.id,
                            'relation_type': relation_type,
                            'dest_picking_type_id': in_type.id,
                            'src_picking_type_id': out_type.id,
                        })
                        _logger.info(
                            f"[res.company] Created t4tek.transit.picking.type for warehouse '{wh.name}', relation_type '{relation_type}'"
                        )

    def automation_handle_company_name_change(self):
        """
        AUTOMATION WRAPPER: Update transit warehouse/sequence names when company name changes.

        Purpose: Hide business logic from UI-visible automation code
        Called when: res.company name field changes (company must have children)
        """
        errors = []

        for company in self:
            try:
                
                transit_wh = self.env['stock.warehouse'].sudo().search([
                    '&',
                    ('company_id', '=', company.id),
                    ('is_t4tek_transit_warehouse', '=', True)
                ])

                if not transit_wh:
                    continue

                new_wh_name = f"{company.name}.TRANSIT"
                if transit_wh.name != new_wh_name:
                    transit_wh.sudo().with_context(skip_t4tek_stock_warehouse_write_protection=True).write({'name': new_wh_name})

                if transit_wh.view_location_id:
                    new_view_name = f"{company.name}.TRANSIT"
                    if transit_wh.view_location_id.name != new_view_name:
                        transit_wh.view_location_id.with_context(
                            skip_t4tek_stock_location_write_protection=True
                        ).sudo().write({'name': new_view_name})

                # Transit stock location name stays 'Stock' — complete_name updates automatically

                company_code = company.name.replace(' ', '_')
                new_order_prefix = f"{company_code}/TRANSIT/"

                order_seq = self.env['ir.sequence'].sudo().search([
                    ('code', '=', 't4tek.transit.order'),
                    ('company_id', '=', company.id)
                ], limit=1)

                if order_seq and order_seq.prefix != new_order_prefix:
                    order_seq.with_context(
                        skip_t4tek_ir_sequence_write_protection=True
                    ).sudo().write({'prefix': new_order_prefix})

                # Transit warehouses have lot_stock_id.usage='transit';
                # normal warehouses have 'internal'.
                warehouses = self.env['stock.warehouse'].sudo().search([
                    ('company_id', '=', company.id),
                    ('lot_stock_id.usage', '=', 'internal'),
                ])

                for warehouse in warehouses:
                    warehouse_code = warehouse.code or warehouse.name.replace(' ', '_')
                    self._update_transit_sequences_for_warehouse(
                        company, warehouse, warehouse_code
                    )

                _logger.info(
                    f"Updated transit warehouse/sequences for company '{company.name}'"
                )

            except Exception as e:
                _logger.error(
                    f"Error updating transit on name change for '{company.name}': {str(e)}",
                    exc_info=True
                )
                errors.append(f"Company '{company.name}': {str(e)}")

        if errors:
            raise ValidationError(
                "Failed to update transit warehouse/sequences:\n• " + "\n• ".join(errors)
            )

    def _update_transit_sequences_for_warehouse(self, company, warehouse, warehouse_code):
        """
        Helper: update IN and OUT transit sequence prefixes for a single warehouse.

        Uses t4tek.transit.picking.type as the source of truth to find the exact
        picking types (and their sequences) for this warehouse — no fragile prefix
        pattern matching required.
        """
        TransitPickingType = self.env['t4tek.transit.picking.type'].sudo()
        ctx = {'skip_t4tek_ir_sequence_write_protection': True}

        transit_configs = TransitPickingType.search([
            ('warehouse_id', '=', warehouse.id),
            ('company_id', '=', company.id),
        ])

        if not transit_configs:
            _logger.debug(
                f"[res.company] No transit configs found for warehouse '{warehouse.name}' "
                f"(id={warehouse.id}), skipping sequence update."
            )
            return

        for config in transit_configs:
            out_seq = config.src_picking_type_id.sequence_id
            new_out_prefix = f"{warehouse_code}/TRANSIT/OUT/"
            if out_seq and out_seq.prefix != new_out_prefix:
                out_seq.with_context(**ctx).write({'prefix': new_out_prefix})
                _logger.info(
                    f"[res.company] Updated OUT sequence prefix to '{new_out_prefix}' "
                    f"for warehouse '{warehouse.name}' (relation: {config.relation_type})"
                )

            in_seq = config.dest_picking_type_id.sequence_id
            new_in_prefix = f"{warehouse_code}/TRANSIT/IN/"
            if in_seq and in_seq.prefix != new_in_prefix:
                in_seq.with_context(**ctx).write({'prefix': new_in_prefix})
                _logger.info(
                    f"[res.company] Updated IN sequence prefix to '{new_in_prefix}' "
                    f"for warehouse '{warehouse.name}' (relation: {config.relation_type})"
                )