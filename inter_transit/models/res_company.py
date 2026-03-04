from odoo import api, fields, models # type: ignore
from odoo.exceptions import UserError # type: ignore
import re
import logging

_logger = logging.getLogger(__name__)


class ResCompany(models.Model):
    _inherit = 'res.company'
    
    def _get_transit_location(self):
        """Get the transit location for this company through its transit warehouse
        
        Structure: Company -> Transit Warehouse -> View Location -> Transit Stock Location
        
        Returns:
            stock.location recordset (empty if not found)
        """
        self.ensure_one()
        transit_wh = self.env['stock.warehouse'].sudo().search([
            ('company_id', '=', self.id),
            ('name', '=', f"{self.name}.TRANSIT")
        ], limit=1)
        if transit_wh and transit_wh.lot_stock_id and transit_wh.lot_stock_id.usage == 'transit':
            return transit_wh.lot_stock_id
        return self.env['stock.location']
                                          
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

            # Idempotent: check if transit warehouse already exists
            existing_wh = Warehouse.search([
                ('company_id', '=', company.id),
                ('name', '=', f"{company.name}.TRANSIT")
            ], limit=1)
            
            if existing_wh:
                _logger.info(
                    f"[res.company] Transit warehouse already exists for company '{company.name}'"
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
                skip_transit_picking_type_creation=True
            ).create({
                'name': f"{company.name}.TRANSIT",
                'code': unique_code,
                'company_id': company.id,
                'reception_steps': 'one_step',
                'delivery_steps': 'ship_only',
            })

            temp_transit_location = transit_wh.lot_stock_id
            temp_transit_view_location = transit_wh.view_location_id
            
            # Note: we need to write separately to set the locations due to odoo logic that creates their own view/stock locations on warehouse creation.
            transit_wh.with_context(
                skip_transit_picking_type_creation=True
            ).sudo().write({
                'view_location_id': view_location.id,
                'lot_stock_id': transit_location.id,
            })

            temp_transit_location.sudo().write({'active': False})
            temp_transit_view_location.sudo().write({'active': False})

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
        """Archive the default sequences and picking types auto-created by Odoo on transit warehouse creation.
        
        When Odoo creates a warehouse, it auto-generates sequences and picking types we don't need.
        Strategy: find ir.sequence records named like '{company}.TRANSIT' then archive them
        and any stock.picking.type that references those sequences.
        """
        Sequence = self.env['ir.sequence'].sudo()
        PickingType = self.env['stock.picking.type'].sudo()

        for company in self:
            transit_wh_name = f"{company.name}.TRANSIT"

            # Find all auto-created sequences for this transit warehouse
            sequences = Sequence.search([
                ('name', 'like', transit_wh_name),
                ('company_id', '=', company.id),
                ('active', '=', True),
            ])

            if not sequences:
                _logger.info(
                    f"[res.company] No default sequences found to archive for transit warehouse of company '{company.name}'"
                )
                continue

            # Find picking types that use any of these sequences
            picking_types = PickingType.search([
                ('sequence_id', 'in', sequences.ids),
                ('active', '=', True),
            ])

            if picking_types:
                picking_types.write({'active': False})
                _logger.info(
                    f"[res.company] Archived {len(picking_types)} default picking type(s) for transit warehouse of company '{company.name}': {picking_types.mapped('name')}"
                )

            sequences.write({'active': False})
            _logger.info(
                f"[res.company] Archived {len(sequences)} default sequence(s) for transit warehouse of company '{company.name}': {sequences.mapped('name')}"
            )
    
    def _create_warehouse_transit_picking_types(self, warehouse=None):
        """Create warehouse-level transit picking types based on parent/child relation_typeships.
        
        For each warehouse belonging to a company:
        - If company has parent: warehouse gets 2 operation types for parent's transit (OUT and IN)
        - If company has children: warehouse gets 2 operation types for own transit (OUT and IN)
        
        These are created at WAREHOUSE level (warehouse_id populated) and mapped by warehouse + transit location.
        
        Args:
            warehouse: Optional specific warehouse to create types for. If None, processes all company warehouses.
        """
        # ======================================================================================
        if self.env.context.get('skip_transit_picking_type_creation'):
            return
        # ======================================================================================

        PickingType = self.env['stock.picking.type'].sudo()
        Sequence = self.env['ir.sequence'].sudo()
        TransitPickingType = self.env['t4tek.transit.picking.type'].sudo()
        Warehouse = self.env['stock.warehouse'].sudo()
        
        for company in self:
            # Check if company has parent or children (is part of inter-company structure)
            is_parent = bool(company.child_ids)
            is_child = bool(company.parent_id)
            
            if not (is_parent or is_child):
                # Company is standalone, no transit operations needed
                continue
            
            # Get warehouses to process (exclude transit warehouses)
            if warehouse:
                warehouses = warehouse if warehouse.company_id == company else Warehouse.browse()
            else:
                warehouses = Warehouse.search([
                    ('company_id', '=', company.id),
                    ('name', 'not like', '.TRANSIT'),
                    ('lot_stock_id.usage', '=', 'internal')
                ])
            
            if not warehouses:
                _logger.warning(
                    f"[res.company] Company '{company.name}' has no normal warehouses. Skipping transit picking type creation."
                )
                continue
            
            # Process each warehouse
            for wh in warehouses:
                if not wh.lot_stock_id or wh.lot_stock_id.usage != 'internal':
                    _logger.warning(
                        f"[res.company] Warehouse '{wh.name}' (id={wh.id}) has no valid stock location. Skipping."
                    )
                    continue
                
                warehouse_stock_location = wh.lot_stock_id
                
                # Use company name and warehouse code for prefixes
                warehouse_code = wh.code if wh.code else wh.name.replace(' ', '_')
                
                # Track which operation types to create
                operations_to_create = []
                
                # ============================================
                # Part 1: Operations to PARENT's transit location (if company has parent)
                # ============================================
                if is_child and company.parent_id:
                    parent_transit = company.parent_id._get_transit_location()
                    
                    if parent_transit:
                        operations_to_create.extend([
                            {
                                'type': 'parent_in',
                                'relation_type': 'child_to_parent',
                                'transit_location': parent_transit,
                                'name': f'Transit Receipts - {company.parent_id.name}',
                                'code': 'incoming',
                                'sequence_code': 'TRANSIT/IN',
                                'prefix': f'{warehouse_code}/TRANSIT/IN/',
                                'src_location': parent_transit,
                                'dest_location': warehouse_stock_location,
                                'use_create_lots': True,
                                'use_existing_lots': False,
                            },
                            {
                                'type': 'parent_out',
                                'relation_type': 'child_to_parent',
                                'transit_location': parent_transit,
                                'name': f'Transit Deliveries - {company.parent_id.name}',
                                'code': 'outgoing',
                                'sequence_code': 'TRANSIT/OUT',
                                'prefix': f'{warehouse_code}/TRANSIT/OUT/',
                                'src_location': warehouse_stock_location,
                                'dest_location': parent_transit,
                                'use_create_lots': False,
                                'use_existing_lots': True,
                                'reservation_method': 'at_confirm',
                            }
                        ])
                
                # ============================================
                # Part 2: Operations to OWN transit location (if company has children)
                # ============================================
                if is_parent:
                    own_transit = company._get_transit_location()
                    
                    if own_transit:
                        operations_to_create.extend([
                            {
                                'type': 'child_in',
                                'relation_type': 'parent_to_child',
                                'transit_location': own_transit,
                                'name': f'Transit Receipts - {company.name}',
                                'code': 'incoming',
                                'sequence_code': 'TRANSIT/IN',
                                'prefix': f'{warehouse_code}/TRANSIT/IN/',
                                'src_location': own_transit,
                                'dest_location': warehouse_stock_location,
                                'use_create_lots': True,
                                'use_existing_lots': False,
                            },
                            {
                                'type': 'child_out',
                                'relation_type': 'parent_to_child',
                                'transit_location': own_transit,
                                'name': f'Transit Deliveries - {company.name}',
                                'code': 'outgoing',
                                'sequence_code': 'TRANSIT/OUT',
                                'prefix': f'{warehouse_code}/TRANSIT/OUT/',
                                'src_location': warehouse_stock_location,
                                'dest_location': own_transit,
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
                    # Check if this specific operation type already exists
                    # Identified by: warehouse + src location + dest location
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
                            'transit_location': op_config['transit_location'],
                            'picking_type': existing
                        })
                        continue
                    
                    # Get or create sequence
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
                    
                    # Create picking type (WAREHOUSE-LEVEL)
                    picking_data = {
                        'name': op_config['name'],
                        'code': op_config['code'],
                        'sequence_id': sequence.id,
                        'sequence_code': op_config['sequence_code'],
                        'warehouse_id': wh.id,  # Warehouse-level operation type
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
                        'transit_location': op_config['transit_location'], 
                        'picking_type': picking_type
                    })
                    
                    _logger.info(
                        f"[res.company] Created transit operation type '{op_config['sequence_code']}' (id={picking_type.id}) for warehouse '{wh.name}' (id={wh.id})"
                    )
                
                if not picking_types_created:
                    continue
                
                # ============================================
                # Part 4: Create t4tek.transit.picking.type records (grouped by relation_type + transit location)
                # ============================================
                # Group by relation_type type and transit location
                from collections import defaultdict
                groups = defaultdict(list)
                
                for item in picking_types_created:
                    key = (item['relation_type'], item['transit_location'].id)
                    groups[key].append(item)
                
                # Create transit picking type records for each group
                for (relation_type, transit_loc_id), items in groups.items():
                    in_type = next((i['picking_type'] for i in items if i['type'] in ['parent_in', 'child_in']), None)
                    out_type = next((i['picking_type'] for i in items if i['type'] in ['parent_out', 'child_out']), None)
                    
                    if not (in_type and out_type):
                        _logger.warning(
                            f"[res.company] Incomplete pair for warehouse '{wh.name}', relation_type '{relation_type}'. Skipping transit picking type record."
                        )
                        continue
                    
                    # Check if record already exists
                    existing = TransitPickingType.search([
                        ('warehouse_id', '=', wh.id),
                        ('company_id', '=', company.id),
                        ('transit_location_id', '=', transit_loc_id),
                        ('dest_picking_type_id', '=', in_type.id),
                        ('src_picking_type_id', '=', out_type.id)
                    ], limit=1)
                    
                    if not existing:
                        TransitPickingType.create({
                            'warehouse_id': wh.id,
                            'company_id': company.id,
                            'relation_type': relation_type,
                            'transit_location_id': transit_loc_id,
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
                # Get transit warehouse
                transit_wh = self.env['stock.warehouse'].sudo().search([
                    ('company_id', '=', company.id),
                    ('name', 'like', '.TRANSIT')
                ], limit=1)

                if not transit_wh:
                    continue

                # Already up to date
                if transit_wh.name.rsplit('.TRANSIT', 1)[0] == company.name:
                    continue

                # Update warehouse name
                new_wh_name = f"{company.name}.TRANSIT"
                if transit_wh.name != new_wh_name:
                    transit_wh.sudo().write({'name': new_wh_name})

                # Update view location name
                if transit_wh.view_location_id:
                    new_view_name = f"{company.name}.TRANSIT"
                    if transit_wh.view_location_id.name != new_view_name:
                        transit_wh.view_location_id.with_context(
                            bypass_inter_transit_location_protection=True
                        ).sudo().write({'name': new_view_name})

                # Transit stock location name stays 'Stock' — complete_name updates automatically

                # Update transit order sequence prefix
                company_code = company.name.replace(' ', '_')
                new_order_prefix = f"{company_code}/TRANSIT/"

                order_seq = self.env['ir.sequence'].sudo().search([
                    ('code', '=', 't4tek.transit.order'),
                    ('company_id', '=', company.id)
                ], limit=1)

                if order_seq and order_seq.prefix != new_order_prefix:
                    order_seq.with_context(
                        bypass_inter_transit_ir_sequence_protection=True
                    ).sudo().write({'prefix': new_order_prefix})

                # Update IN/OUT sequence prefixes for each normal warehouse
                warehouses = self.env['stock.warehouse'].sudo().search([
                    ('company_id', '=', company.id),
                    ('name', 'not like', '.TRANSIT')
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
            raise UserError(
                "Failed to update transit warehouse/sequences:\n• " + "\n• ".join(errors)
            )

    def _update_transit_sequences_for_warehouse(self, company, warehouse, warehouse_code):
        """
        Helper: update IN and OUT transit sequence prefixes for a single warehouse.
        Extracted to keep automation_handle_company_name_change readable.
        """
        IrSequence = self.env['ir.sequence'].sudo()
        PickingType = self.env['stock.picking.type'].sudo()
        ctx = {'bypass_inter_transit_ir_sequence_protection': True}

        for picking_code, direction in [('incoming', 'IN'), ('outgoing', 'OUT')]:
            new_prefix = f"{warehouse_code}/TRANSIT/{direction}/"

            sequences = IrSequence.search([
                ('code', '=', 't4tek.transit.picking'),
                ('company_id', '=', company.id),
                ('prefix', 'like', f'%/TRANSIT/{direction}/'),
            ])

            for seq in sequences:
                linked_type = PickingType.search([
                    ('sequence_id', '=', seq.id),
                    ('warehouse_id', '=', warehouse.id),
                    ('code', '=', picking_code),
                ], limit=1)

                if linked_type and seq.prefix != new_prefix:
                    seq.with_context(**ctx).write({'prefix': new_prefix})