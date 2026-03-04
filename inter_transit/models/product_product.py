from odoo import api, fields, models # type: ignore
from odoo.tools.float_utils import float_round # type: ignore
from odoo.osv import expression # type: ignore
from collections import defaultdict
import logging

_logger = logging.getLogger(__name__)


class ProductProduct(models.Model):
    _inherit = 'product.product'

#     inter_transit_location_ids = fields.Many2many(
#         'stock.location',
#         string='Transit Locations',
#         compute='_compute_inter_transit_location_ids',
#         search='_search_inter_transit_location_ids',
#         store=False,
#     )

#     @api.depends_context('allowed_company_ids')
#     def _compute_inter_transit_location_ids(self):
#         """Compute available transit locations"""
#         accessible_locations = self._get_accessible_transit_locations()
#         for product in self:
#             product.inter_transit_location_ids = accessible_locations

#     # =========================================================================
#     # CRITICAL: Override _compute_quantities to add transit_location_list dependency
#     # =========================================================================
#     @api.depends('stock_move_ids.product_qty', 'stock_move_ids.state', 'stock_move_ids.quantity')
#     @api.depends_context(
#         'lot_id', 'owner_id', 'package_id', 'from_date', 'to_date',
#         'location', 'warehouse_id', 'allowed_company_ids', 'is_storable',
#         'transit_location_list'  # ← NEW: Add our context key
#     )
#     def _compute_quantities(self):
#         """
#         Override to add transit_location_list to depends_context.
#         This ensures Odoo recalculates when transit location selection changes.
#         """
#         _logger.info(f"🔑 Full Context when _compute_quantities() called: {dict(self._context)}")
        
#         # Call super - it will use our overridden _compute_quantities_dict
#         return super()._compute_quantities()

#     def _compute_quantities_dict(self, lot_id, owner_id, package_id, from_date=False, to_date=False):
#         """
#         Override to add in-transit quantities to free_qty
        
#         Workflow:
#         1. Get Odoo's base calculations
#         2. Get in-transit quantities for selected transit locations
#         3. Add transit quantities to free_qty
#         4. Return enhanced results
#         """
#         # Get base calculations from Odoo
#         res = super()._compute_quantities_dict(lot_id, owner_id, package_id, from_date, to_date)
        
#         # Get selected transit locations from context
#         # IMPORTANT: Use 'transit_location_list' - must match @api.depends_context above
#         transit_location_ids = self._context.get('transit_location_list', [])
        
#         if not transit_location_ids:
#             # No transit locations selected, return base results
#             _logger.info("⚠️  No transit_location_list in context during _compute_quantities_dict")
#             return res
        
#         _logger.info("=" * 80)
#         _logger.info("🚚 COMPUTING TRANSIT QUANTITIES")
#         _logger.info(f"📦 Selected transit locations: {transit_location_ids}")
        
#         # Get in-transit quantities
#         transit_quantities = self._get_transit_quantities(
#             transit_location_ids, 
#             lot_id, 
#             owner_id, 
#             from_date, 
#             to_date
#         )
        
#         # Add transit quantities to free_qty
#         for product in self:
#             product_id = product.id
#             if product_id in transit_quantities and product_id in res:
#                 transit_qty = transit_quantities[product_id]
#                 original_free_qty = res[product_id].get('free_qty', 0.0)
                
#                 # Add transit quantity to free_qty
#                 res[product_id]['free_qty'] = float_round(
#                     original_free_qty + transit_qty,
#                     precision_rounding=product.uom_id.rounding
#                 )
                
#                 _logger.info(f"Product {product.display_name}:")
#                 _logger.info(f"  Original free_qty: {original_free_qty}")
#                 _logger.info(f"  Transit qty: {transit_qty}")
#                 _logger.info(f"  New free_qty: {res[product_id]['free_qty']}")
        
#         _logger.info("=" * 80)
#         return res

#     def _get_transit_quantities(self, transit_location_ids, lot_id=None, owner_id=None, 
#                                 from_date=False, to_date=False):
#         """
#         Calculate in-transit quantities for selected transit locations
        
#         Args:
#             transit_location_ids: List of transit location IDs to include
#             lot_id: Optional lot/serial number filter
#             owner_id: Optional owner filter
#             from_date: Optional start date filter
#             to_date: Optional end date filter
            
#         Returns:
#             dict: {product_id: transit_quantity}
#         """
#         if not transit_location_ids:
#             return {}
        
#         # Build domain for in-progress transit pickings
#         domain = [
#             ('state', '=', 'in_progress'),  # Only in-progress transits
#             ('transit_location_id', 'in', transit_location_ids),  # Selected locations
#         ]
        
#         # Apply warehouse filtering if specific warehouse selected
#         warehouse_id = self._context.get('warehouse_id')
#         if warehouse_id:
#             domain = self._add_warehouse_filter_to_transit_domain(domain, warehouse_id)
        
#         # Date filters (if applicable)
#         if from_date:
#             domain.append(('scheduled_date', '>=', from_date))
#         if to_date:
#             domain.append(('scheduled_date', '<=', to_date))
        
#         _logger.info(f"🔍 Transit picking domain: {domain}")
        
#         # Get in-progress transit pickings
#         TransitPicking = self.env['t4tek.transit.picking']
#         transit_pickings = TransitPicking.search(domain)
        
#         _logger.info(f"📦 Found {len(transit_pickings)} in-progress transit pickings")
        
#         if not transit_pickings:
#             return {}
        
#         # Collect all dest_picking IDs
#         dest_picking_ids = transit_pickings.mapped('dest_picking_id').ids
        
#         # Build domain for stock moves in destination pickings
#         move_domain = [
#             ('picking_id', 'in', dest_picking_ids),
#             ('product_id', 'in', self.ids),  # Only products we're computing
#             ('state', 'in', ['assigned', 'confirmed', 'waiting']),  # Not done yet
#         ]
        
#         # Apply lot/owner filters if provided
#         if lot_id is not None:
#             move_domain.append(('lot_ids', 'in', [lot_id]))
#         if owner_id is not None:
#             move_domain.append(('restrict_partner_id', '=', owner_id))
        
#         _logger.info(f"🔍 Stock move domain: {move_domain}")
        
#         # Query stock moves with grouping by product and UOM
#         Move = self.env['stock.move']
#         move_groups = Move._read_group(
#             move_domain,
#             ['product_id', 'product_uom'],
#             ['product_uom_qty:sum']
#         )
        
#         # Aggregate quantities with UOM conversion
#         transit_quantities = defaultdict(float)
        
#         for product, uom, qty_sum in move_groups:
#             # Convert to product's base UOM
#             converted_qty = uom._compute_quantity(qty_sum, product.uom_id)
#             transit_quantities[product.id] += converted_qty
            
#             _logger.info(f"  Product: {product.display_name}")
#             _logger.info(f"    Raw qty: {qty_sum} {uom.name}")
#             _logger.info(f"    Converted: {converted_qty} {product.uom_id.name}")
        
#         return dict(transit_quantities)

#     def _add_warehouse_filter_to_transit_domain(self, domain, warehouse_id):
#         """
#         Option 4: Transit value shown ONLY at "All Warehouses" level.
        
#         Business Logic:
#         - Transit value is COMPANY-LEVEL, not warehouse-level
#         - When specific warehouse selected → Exclude all transits
#         - When "All Warehouses" (no warehouse_id) → Show transits normally
        
#         This prevents confusion about which warehouse "owns" in-transit goods.
        
#         Args:
#             domain: Existing domain
#             warehouse_id: Warehouse ID from context (specific warehouse selected)
            
#         Returns:
#             Modified domain that excludes all transits
#         """
#         warehouse = self.env['stock.warehouse'].browse(warehouse_id)
        
#         _logger.info(f"🏭 Warehouse {warehouse.name} selected → Excluding ALL transits (Option 4)")
#         _logger.info(f"   Transit value only shown at 'All Warehouses' company level")
        
#         # Exclude all transit pickings when specific warehouse is selected
#         # This makes transit value appear ONLY when viewing "All Warehouses"
#         domain.append(('id', '=', False))
        
#         return domain

#     # =========================================================================
#     # CRITICAL FIX: Inject context in web_search_read
#     # =========================================================================
#     @api.model
#     def web_search_read(self, domain=None, specification=None, offset=0, limit=None, order=None, count_limit=None):
#         """
#         CRITICAL: Extract transit location selection from domain and inject into context
        
#         This ensures transit_location_list is in context when _compute_quantities is called
#         """
#         self._log_warehouse_debug("web_search_read", domain=domain)
        
#         # Extract transit location IDs from domain
#         transit_location_ids = self._extract_transit_locations_from_domain(domain)
        
#         if transit_location_ids:
#             _logger.info(f"💉 INJECTING transit_location_list into context: {transit_location_ids}")
#             # Set context with transit locations - this will trigger recalculation
#             self = self.with_context(transit_location_list=transit_location_ids)
        
#         return super().web_search_read(domain, specification, offset, limit, order, count_limit)

#     def _extract_transit_locations_from_domain(self, domain):
#         """
#         Extract transit location IDs from domain
        
#         Looks for clauses like: ['inter_transit_location_ids', 'in', [77]]
        
#         Returns:
#             list: Transit location IDs, or empty list if none found
#         """
#         if not domain:
#             return []
        
#         transit_location_ids = []
        
#         for clause in domain:
#             if isinstance(clause, (list, tuple)) and len(clause) >= 3:
#                 field_name, operator, value = clause[0], clause[1], clause[2]
                
#                 if field_name == 'inter_transit_location_ids':
#                     if operator == 'in' and isinstance(value, list):
#                         transit_location_ids.extend(value)
#                     elif operator == '=' and value:
#                         transit_location_ids.append(value)
        
#         return transit_location_ids

#     # =========================================================================
#     # SEARCHPANEL & SEARCH METHOD
#     # =========================================================================
#     @api.model
#     def search_panel_select_multi_range(self, field_name, **kwargs):
#         """
#         Override searchpanel to:
#         1. Force fresh data on warehouse change
#         2. Auto-filter invalid selections
#         3. Log current selections
#         """
#         if field_name == 'inter_transit_location_ids':
#             # === LOGGING CURRENT SELECTION ===
#             filter_domain = kwargs.get('filter_domain', [])
#             current_selection = self._extract_selection_from_domain(
#                 filter_domain, 
#                 'inter_transit_location_ids'
#             )
            
#             _logger.info("=" * 80)
#             _logger.info("🚚 TRANSIT LOCATION SEARCHPANEL")
#             _logger.info(f"🏢 Companies: {self.env.companies.mapped('name')}")
            
#             # Check warehouse context
#             warehouse_id = self._context.get('warehouse_id')
#             if warehouse_id:
#                 warehouse = self.env['stock.warehouse'].browse(warehouse_id)
#                 _logger.info(f"🏭 Warehouse: {warehouse.name} (ID: {warehouse_id})")
#             else:
#                 _logger.info("🏭 Warehouse: All Warehouses")
            
#             _logger.info(f"✅ Current selection: {current_selection}")
            
#             if current_selection:
#                 locs = self.env['stock.location'].browse(current_selection)
#                 _logger.info(f"   Selected locations: {locs.mapped('name')}")
            
#             # === GET ACCESSIBLE LOCATIONS (warehouse-aware) ===
#             accessible_locations = self._get_accessible_transit_locations()
#             accessible_ids = set(accessible_locations.ids)
#             _logger.info(f"📦 Available locations: {accessible_locations.mapped('name')}")
            
#             # === AUTO-FILTER INVALID SELECTIONS ===
#             if current_selection:
#                 valid_selection = [loc_id for loc_id in current_selection if loc_id in accessible_ids]
#                 invalid_selection = [loc_id for loc_id in current_selection if loc_id not in accessible_ids]
                
#                 if invalid_selection:
#                     invalid_locs = self.env['stock.location'].browse(invalid_selection)
#                     _logger.info(f"⚠️  Auto-filtering invalid selections: {invalid_locs.mapped('name')}")
#                     _logger.info(f"✅ Valid selections after filter: {valid_selection}")
            
#             # === RETURN AVAILABLE OPTIONS ===
#             values = [{
#                 'id': loc.id,
#                 'display_name': loc.name,
#             } for loc in accessible_locations]
            
#             _logger.info(f"📤 Returning {len(values)} options")
#             _logger.info("=" * 80)
            
#             return {'values': values}
        
#         return super().search_panel_select_multi_range(field_name, **kwargs)

#     def _extract_selection_from_domain(self, domain, field_name):
#         """Helper to extract selected IDs from domain"""
#         selected_ids = []
#         for clause in domain:
#             if isinstance(clause, (list, tuple)) and len(clause) >= 3:
#                 if clause[0] == field_name and clause[1] in ('in', '='):
#                     value = clause[2]
#                     if isinstance(value, list):
#                         selected_ids.extend(value)
#                     elif value:
#                         selected_ids.append(value)
#         return selected_ids

#     def _get_accessible_transit_locations(self):
#         """
#         Get transit locations based on warehouse selection.
#         Mimics Odoo's warehouse searchpanel behavior:
#         - If warehouse_id in context → only that warehouse's company locations
#         - If no warehouse_id → all companies' locations (All Warehouses)
#         """
#         warehouse_id = self._context.get('warehouse_id')
        
#         if warehouse_id:
#             # Specific warehouse selected - get its company
#             warehouse = self.env['stock.warehouse'].browse(warehouse_id)
#             companies = warehouse.company_id
#             _logger.info(f"🏭 Getting transit locations for warehouse: {warehouse.name} (Company: {companies.name})")
#         else:
#             # No warehouse selected = "All Warehouses" - use all allowed companies
#             companies = self.env.companies
#             _logger.info(f"🏭 Getting transit locations for all companies: {companies.mapped('name')}")
        
#         accessible_ids = set()
        
#         # Handle single company or recordset
#         for company in companies:
#             # Direct transit location
#             if hasattr(company, 'transit_location_id') and company.transit_location_id:
#                 accessible_ids.add(company.transit_location_id.id)
            
#             # Parent company's transit location (if applicable)
#             if company.parent_id and hasattr(company.parent_id, 'transit_location_id'):
#                 if company.parent_id.transit_location_id:
#                     accessible_ids.add(company.parent_id.transit_location_id.id)
        
#         result = self.env['stock.location'].browse(list(accessible_ids)).exists() if accessible_ids else self.env['stock.location']
#         _logger.info(f"📦 Accessible transit location IDs: {list(accessible_ids)}")
#         return result

#     def _search_inter_transit_location_ids(self, operator, value):
#         """
#         Search method for inter_transit_location_ids field.
        
#         NOTE: This sets context, but it doesn't persist to _compute_quantities.
#         The real context injection happens in web_search_read (see above).
        
#         This method is still needed for the search to work, but context 
#         injection is handled elsewhere.
#         """
#         _logger.info(f"🔍 _search called with operator: {operator}, value: {value}")
        
#         if not value:
#             return []
        
#         # Get currently accessible locations (warehouse-aware)
#         accessible_locations = self._get_accessible_transit_locations()
#         accessible_ids = set(accessible_locations.ids)
        
#         _logger.info(f"📦 Accessible location IDs: {accessible_ids}")
        
#         # Filter requested value to only include accessible locations
#         if isinstance(value, (list, tuple)):
#             requested_ids = set(value)
#             valid_ids = list(requested_ids & accessible_ids)
            
#             _logger.info(f"✅ Requested: {requested_ids}")
#             _logger.info(f"✅ Valid (after filter): {valid_ids}")
            
#             # If some selections were invalid, log the auto-filtering
#             invalid_ids = requested_ids - accessible_ids
#             if invalid_ids:
#                 invalid_locs = self.env['stock.location'].browse(list(invalid_ids))
#                 _logger.info(f"⚠️  Auto-filtered out {len(invalid_ids)} invalid selection(s): {invalid_locs.mapped('name')}")
#         else:
#             # Single value - validate it
#             if value not in accessible_ids:
#                 _logger.info(f"⚠️  Invalid single selection: {value}")
        
#         # Return empty domain - filtering handled via context in web_search_read
#         return []

#     def _log_warehouse_debug(self, method_name, domain=None, groupby=None):
#         """Centralized debug logging"""
#         _logger.info("=" * 100)
#         _logger.info(f"🔍 METHOD: {method_name}")
#         _logger.info(f"📦 Domain: {domain}")
#         if groupby:
#             _logger.info(f"📊 Groupby: {groupby}")

#         _logger.info(f"🔑 Full Context (env.context): {dict(self.env.context)}")
        
#         # Check for warehouse in context
#         warehouse_id = self._context.get('warehouse_id')
#         if warehouse_id:
#             warehouse = self.env['stock.warehouse'].browse(warehouse_id)
#             _logger.info(f"🏭 Warehouse selected: {warehouse.name} (ID: {warehouse_id})")
#         else:
#             _logger.info(f"🏭 Warehouse: All Warehouses (no warehouse_id in context)")
        
#         # Check for transit locations in context
#         transit_locations = self._context.get('transit_location_list')
#         if transit_locations:
#             locs = self.env['stock.location'].browse(transit_locations)
#             _logger.info(f"🚚 Transit locations in context: {locs.mapped('name')}")
        
#         # Check domain for warehouse/location related clauses
#         if domain:
#             for clause in domain:
#                 if isinstance(clause, (list, tuple)) and len(clause) >= 3:
#                     field_name = str(clause[0])
#                     if 'warehouse' in field_name.lower() or 'location' in field_name.lower():
#                         _logger.info(f"🏭 Domain clause: {clause}")
        
#         _logger.info("=" * 100)


#     # ===========================================================================
#     # total_value override compute
#     # ===========================================================================


#     @api.depends('stock_valuation_layer_ids')
#     @api.depends_context(
#         'to_date', 'company', 'allowed_company_ids',
#         'transit_location_list'  # ← NEW: Add our context key
#     )
#     def _compute_value_svl(self):
#         """
#         Override to add transit_location_list to depends_context.
#         This ensures Odoo recalculates total_value when transit location selection changes.
        
#         Mirrors the pattern used in _compute_quantities for free_qty.
#         """
#         _logger.info(f"🔑 Context in _compute_value_svl: {dict(self._context)}")
#         _logger.info(f"📦 Transit locations: {self._context.get('transit_location_list', [])}")
        
#         # Call super - it will calculate base total_value
#         # Then we'll enhance it with transit valuations
#         result = super()._compute_value_svl()
        
#         # Add transit valuations
#         self._add_transit_valuations()
        
#         return result

#     def _add_transit_valuations(self):
#         """
#         Add in-transit valuations to total_value.
        
#         This is called after base _compute_value_svl to add transit value.
#         Workflow matches _compute_quantities_dict for consistency.
#         """
#         # Get selected transit locations from context
#         transit_location_ids = self._context.get('transit_location_list', [])
        
#         if not transit_location_ids:
#             # No transit locations selected
#             _logger.info("⚠️  No transit_location_list in context during _add_transit_valuations")
#             return
        
#         _logger.info("=" * 80)
#         _logger.info("💰 ADDING TRANSIT VALUATIONS TO total_value")
#         _logger.info(f"📦 Selected transit locations: {transit_location_ids}")
        
#         # Get to_date from context (used for historical valuation)
#         to_date = self._context.get('to_date')
        
#         # Get in-transit valuations
#         transit_valuations = self._get_transit_valuations(
#             transit_location_ids,
#             to_date
#         )
        
#         # Add transit valuations to total_value
#         for product in self:
#             product_id = product.id
#             if product_id in transit_valuations:
#                 transit_value = transit_valuations[product_id]
#                 original_total_value = product.total_value
                
#                 # Add transit value to total_value
#                 product.total_value = original_total_value + transit_value
                
#                 _logger.info(f"Product {product.display_name}:")
#                 _logger.info(f"  Original total_value: ${original_total_value:,.2f}")
#                 _logger.info(f"  Transit value: ${transit_value:,.2f}")
#                 _logger.info(f"  New total_value: ${product.total_value:,.2f}")
        
#         _logger.info("=" * 80)

#     def _get_transit_valuations(self, transit_location_ids, to_date=False):
#         """
#         Calculate in-transit valuations for selected transit locations.
        
#         Logic:
#         1. Find in-progress transit pickings (src done, dest not done)
#         2. Get stock valuation layers from source pickings
#         3. Sum up the absolute values (since outgoing SVLs are negative)
        
#         This represents the value of goods that have left the source company
#         but haven't arrived at the destination yet.
        
#         Args:
#             transit_location_ids: List of transit location IDs to include
#             to_date: Optional date filter for valuation at specific date
            
#         Returns:
#             dict: {product_id: transit_valuation}
#         """
#         if not transit_location_ids:
#             return {}
        
#         # Build domain for in-progress transit pickings
#         domain = [
#             ('state', '=', 'in_progress'),  # Only in-progress transits
#             ('transit_location_id', 'in', transit_location_ids),  # Selected locations
#         ]
        
#         # Apply warehouse filtering if specific warehouse selected
#         warehouse_id = self._context.get('warehouse_id')
#         if warehouse_id:
#             domain = self._add_warehouse_filter_to_transit_domain(domain, warehouse_id)
        
#         # Date filter (for historical valuation)
#         if to_date:
#             domain.append(('scheduled_date', '<=', to_date))
        
#         _logger.info(f"🔍 Transit picking domain: {domain}")
        
#         # Get in-progress transit pickings
#         TransitPicking = self.env['t4tek.transit.picking']
#         transit_pickings = TransitPicking.search(domain)
        
#         _logger.info(f"📦 Found {len(transit_pickings)} in-progress transit pickings")
        
#         if not transit_pickings:
#             return {}
        
#         # Collect all source picking IDs (where goods came FROM)
#         src_picking_ids = transit_pickings.mapped('src_picking_id').ids
        
#         _logger.info(f"📤 Source picking IDs: {src_picking_ids}")
        
#         # Build domain for stock valuation layers in source pickings
#         # We want SVLs from DONE moves in source pickings
#         svl_domain = [
#             ('stock_move_id.picking_id', 'in', src_picking_ids),
#             ('product_id', 'in', self.ids),  # Only products we're computing
#             ('stock_move_id.state', '=', 'done'),  # Completed moves only
#         ]
        
#         # Apply to_date filter to SVL creation date
#         if to_date:
#             svl_domain.append(('create_date', '<=', to_date))
        
#         _logger.info(f"🔍 Stock valuation layer domain: {svl_domain}")
        
#         # Query stock valuation layers with grouping by product
#         SVL = self.env['stock.valuation.layer']
#         svl_groups = SVL._read_group(
#             svl_domain,
#             ['product_id'],
#             ['value:sum']
#         )
        
#         # Aggregate valuations
#         transit_valuations = {}
        
#         for product, value_sum in svl_groups:
#             # CRITICAL: SVL value for outgoing moves is NEGATIVE
#             # (e.g., -$500 when $500 worth of goods leave)
#             # We need to ADD BACK this value to total_value
#             # So we use abs() to make it positive
#             transit_value = abs(value_sum)
            
#             transit_valuations[product.id] = transit_value
            
#             _logger.info(f"  Product: {product.display_name}")
#             _logger.info(f"    SVL sum (raw): ${value_sum:,.2f}")
#             _logger.info(f"    Transit value (abs): ${transit_value:,.2f}")
        
#         return transit_valuations

    transit_qty = fields.Float(
        'In Transit',
        compute='_compute_quantities',
        digits='Product Unit of Measure',
        aggregator='sum',
        compute_sudo=False,
        help=(
            "Quantity physically sitting in a transit location right now.\n"
            "Condition: source picking DONE, destination picking still pending.\n"
            "Purely informational — does not modify incoming/outgoing/free qty."
        ),
    )

    @api.depends(
        'stock_move_ids.product_qty',
        'stock_move_ids.state',
        'stock_move_ids.quantity',
    )
    @api.depends_context(
        'lot_id', 'owner_id', 'package_id', 'from_date', 'to_date',
        'location', 'warehouse_id', 'allowed_company_ids', 'is_storable',
    )
    def _compute_quantities(self):
        super()._compute_quantities()

        storable = self.filtered(lambda p: p.type != 'service')
        (self - storable).transit_qty = 0.0

        if not storable:
            return

        transit_res = storable._compute_transit_quantities_dict()
        rounding_map = {p.id: p.uom_id.rounding for p in storable}

        for product in storable:
            product.transit_qty = float_round(
                transit_res.get(product.id, 0.0),
                precision_rounding=rounding_map[product.id],
            )
        # incoming_qty / outgoing_qty / free_qty / virtual_available untouched.

    def _compute_transit_quantities_dict(self):
        """
        Returns {product_id: float}

        transit_qty = quantity whose src_picking is DONE but dest_picking is
                      still pending — goods physically sitting in the transit
                      location right now.

        Source: pending dest_picking moves.
        Scope filter applied when in individual warehouse/location mode.
        """
        location_ctx = self.env.context.get('location')
        warehouse_ctx = self.env.context.get('warehouse_id')
        is_all_warehouses = not location_ctx and not warehouse_ctx

        result = {p.id: 0.0 for p in self}

        in_pairs = self.env['t4tek.transit.picking'].search([
            ('src_picking_state', '=', 'done'),
            ('dest_picking_state', 'not in', ['done', 'cancel']),
            ('company_id', 'in', self.env.companies.ids),
        ])

        if not in_pairs:
            return result

        dest_picking_ids = in_pairs.mapped('dest_picking_id').ids
        pending_states = ('waiting', 'confirmed', 'assigned', 'partially_available')

        base_domain = [
            ('picking_id', 'in', dest_picking_ids),
            ('product_id', 'in', self.ids),
            ('state', 'in', pending_states),
        ]

        if not is_all_warehouses:
            # A warehouse is a transit warehouse if its view_location_id
            # has no company_id (null) — transit locations are company-neutral.
            # Transit WH already shows stock natively; transit_qty = 0 there.
            if self._is_transit_warehouse_scope(warehouse_ctx):
                return result

            scope_loc_ids = self._get_transit_scope_location_ids(
                location_ctx, warehouse_ctx
            )
            if not scope_loc_ids:
                return result
            # OR: covers both transit WH (location_id) and dest WH (location_dest_id)
            base_domain = expression.AND([
                base_domain,
                expression.OR([
                    [('location_id', 'in', scope_loc_ids)],
                    [('location_dest_id', 'in', scope_loc_ids)],
                ]),
            ])

        Move = self.env['stock.move'].with_context(active_test=False)
        for product, qty in Move._read_group(
            base_domain, ['product_id'], ['product_qty:sum']
        ):
            if product.id in result:
                result[product.id] = qty

        return result

    def _is_transit_warehouse_scope(self, warehouse_ctx):
        """
        Returns True if the current warehouse scope is a transit warehouse.
        A warehouse is considered transit if its view_location_id has no
        company_id (company_id = null) — transit locations are company-neutral
        by design to allow cross-company stock movement.
        Only meaningful when a specific warehouse is selected (not All WH).
        """
        if not warehouse_ctx:
            return False
        wh_ids = (
            warehouse_ctx if isinstance(warehouse_ctx, list) else [warehouse_ctx]
        )
        view_locations = self.env['stock.warehouse'].browse(wh_ids).mapped(
            'view_location_id'
        )
        # All selected warehouses must have company_id set to be non-transit.
        # If any view_location has no company_id, treat scope as transit WH.
        return any(not loc.company_id for loc in view_locations)

    def _get_transit_scope_location_ids(self, location_ctx, warehouse_ctx):
        """
        Resolve context scope into a flat list of child stock.location ids.
        """
        Location = self.env['stock.location']
        Warehouse = self.env['stock.warehouse']

        if warehouse_ctx:
            wh_ids = (
                warehouse_ctx if isinstance(warehouse_ctx, list) else [warehouse_ctx]
            )
            scope_roots = Warehouse.browse(wh_ids).mapped('view_location_id')
        elif location_ctx:
            loc_ids = (
                location_ctx if isinstance(location_ctx, list) else [location_ctx]
            )
            scope_roots = Location.browse(loc_ids)
        else:
            return []

        if not scope_roots:
            return []

        path_domain = expression.OR([
            [('parent_path', '=like', loc.parent_path + '%')]
            for loc in scope_roots
        ])
        return Location.search(path_domain).ids