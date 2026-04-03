from odoo import api, fields, models # type: ignore
from odoo.exceptions import UserError, ValidationError # type: ignore
from markupsafe import Markup, escape # type: ignore
from collections import defaultdict
from datetime import datetime
import logging

_logger = logging.getLogger(__name__)


class TransitOrder(models.Model):
    """
    Inter-Company Transit Management
    
    Architecture:
    - Each parent company (even if itself is a child company) has a transit location ([CompanyName].TRANSIT/Stock).
    - Parent company orders transit between its direct children OR from itself to direct children.
    - If child of child, the transit must process through 2 levels of transit locations, 
    but this is not handled automatically.
    
    Transit Order Flow Example:
    
    Case 1: Mother orders Child A → Child B
    ┌─────────────┐         ┌──────────────┐         ┌─────────────┐
    │  Child A    │   OUT   │   Mother     │   IN    │  Child B    │
    │   Stock     │ ──────> │   TRANSIT    │ ──────> │   Stock     │
    └─────────────┘         └──────────────┘         └─────────────┘
    
    Case 2: Mother orders Self → Child
    ┌─────────────┐         ┌──────────────┐         ┌─────────────┐
    │  Mother     │   OUT   │   Mother     │   IN    │   Child     │
    │   Stock     │ ──────> │   TRANSIT    │ ──────> │   Stock     │
    └─────────────┘         └──────────────┘         └─────────────┘

    Case 3: Mother orders Child → Self
    ┌─────────────┐         ┌──────────────┐         ┌─────────────┐
    │  Child      │   OUT   │   Mother     │   IN    │  Mother     │
    │   Stock     │ ──────> │   TRANSIT    │ ──────> │   Stock     │
    └─────────────┘         └──────────────┘         └─────────────┘

    Case 4: Mother → Grandchild process (not automatic)
        ┌─────────────┐         ┌──────────────┐         ┌──────────────┐
    1.  │  Mother     │   OUT   │   Mother     │   IN    │   Child      │
        │   Stock     │ ──────> │   TRANSIT    │ ──────> │   Stock      │
        └─────────────┘         └──────────────┘         └──────────────┘
        
        ┌─────────────┐         ┌──────────────┐         ┌──────────────┐
    2.  │   Child     │   OUT   │    Child     │   IN    │ Grandchild   │
        │   Stock     │ ──────> │   TRANSIT    │ ──────> │   Stock      │
        └─────────────┘         └──────────────┘         └──────────────┘

    Workflow:
    TRANSIT ORDER (transit.order):
    ┌─────────────┐  [action_confirm]  ┌──────────────┐  [1st src _action_done] ┌──────────────┐ [last dest _action_done]┌──────────────┐
    │    draft    │ ─────────────────> │   assigned   │ ──────────────────────> │  in_progress │ ──────────────────────> │     done     │
    └─────┬───────┘                    └──────┬───────┘                         └───┬──────────┘                         └──────────────┘
          |           [action_cancel]         |                                     |   ↑                                         ↑
          └─────────────────┬─────────────────┘                                     |   | ≥1 transit picking(s) not done/cancel   │ all transit picking(s)
                            |                                                       |   └─────────────────────────────────────────◇     done/cancel
                     ┌──────┴──────┐                                                |                                             │
                     │    cancel   │                                                └─────────────────────────────────────────────┘
                     └─────────────┘                                                                   [action_stop]

    TRANSIT PICKING (transit.picking): 
    - state mirror transit order, considered done when both sides are done, considered cancelled when both sides are cancelled.
    - can execute action_cancel/action_stop independently from the transit order, dictated by the state of transit order 
    and the scope is for the transit picking itself (not the entire transit).
                     
    STOCK PICKING (stock.picking):
    - outgoing (src side):
       ┌─────────────────────────────────────────────────────────────────────────────────────┐
       |                                                                                     |
       |                    whole              [delegate]            partial                 |
       |   ┌───────────────────────────────────────◇────────────────────────────────────────┴────────────────────────────────────────┐
       |   |     (only if no partial before)       ↑                                                                                  |
       |   |                                       |                                                                                  |                                                                            |
       |   |   ┌───────────────────────────────────┤                                                                                  |
       ↓   ↓   |                                   |                                                                                  |
    ┌──────────┴──┐     [action_confirm]    ┌──────┴───────┐     [_action_done]      ┌──────────────┐   [_create_backorder]           ↓
    │    draft    │ ──────────────────────> │   assigned   │ ──────────────────────> │     done     │ ──────────────────────> New transit route
    └─────┬───────┘                         └──────┬───────┘                         └──────────────┘                         
          |    ┌transit.picking/transit.order┐     |    
          |    └  action_cancel/action_stop  ┘     |      - Delegation:
          └───────────────────┬────────────────────┘      + Whole: transfer the responsibility of the entire picking to the direct child company.
                              |                           + Partial: transfer the responsibility of part of the picking by creating a new route,
                       ┌──────┴──────┐                    by creating a new picking and moves for the delegated lines in the child company,
                       │    cancel   │                    and link it to the original picking, then set the state of the original picking to 'draft'.
                       └─────────────┘
                       
    - incoming (dest side):
    ┌─────────────┐   [propgate from src]   ┌──────────────┐     [_action_done]      ┌──────────────┐   [_create_backorder]
    │    draft    │ ──────────────────────> │   assigned   │ ──────────────────────> │     done     │ ──────────────────────> New transit route
    └─────┬───────┘                         └──────┬───────┘                         └──────────────┘                         
          |    ┌transit.picking/transit.order┐     |
          |    └  action_cancel/action_stop  ┘     |
          └───────────────────┬────────────────────┘
                              |
                       ┌──────┴──────┐
                       │    cancel   │
                       └─────────────┘

    Note: 
    - src.location_dest_id MUST equal dest.location_id (the transit location).
    - The transit location in this inter-transit context is just an implication that these 
    goods are in process between companies and not physically present (virtual location).
    - While stock locations still follow Odoo's multi-company transfer rules (belonging to no company), 
    the transit location itself has a relation to a specific company (relation field from the company) 
    to indicate that it belongs to that company for stock reporting purposes.
    - Support delegation of the dest picking to a child company, but not the src picking 
    (as the receiving company must be the one to confirm receipt of goods).

    Matching Strategy (product-based):
    - Moves between src/dest pickings are matched by product_id.
    - No persistent link field on stock.move is required.
    - This means the transit is resilient to move deletion/recreation.
    - Limitation: have to merge duplicated lines and if picking contains multiple moves of the same product, 
    they will all be merged into one move on the other side. Dest validation to populate the done quantity 
    will be based on the total quantity of that product in the picking, not individual moves.

    Key Components:
    - transit.order.line: User-defined transit lines.
    - transit.picking: Mapping src/dest pickings with transit location.
    - transit.picking.type: Mapping src/dest operations types that use for transit process. 
    """
    _name = 'transit.order'
    _description = 'Inter-Company Transit'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _rec_name = 'name'
    
    name = fields.Char(
        'Reference', 
        readonly=False,
        copy=False,
        required=True,
        default='New',
    )

    is_reviewed = fields.Boolean(
        string='Mismatch Acknowledged',
        default=False,
        copy=False,
        tracking=True,
        help="Set manually when a quantity/lot mismatch is intentional. "
            "Clears the danger decoration and restores the done theme.",
    )

    note = fields.Html('Notes')
    
    # Companies involved 
    company_id = fields.Many2one(
        'res.company', 
        string='Ordering Company', 
        required=True, 
        default=lambda self: self.env.company,
        help="Company ordering this transit (must be parent or one of the involved companies)"
    )
    
    src_company_id = fields.Many2one(
        'res.company',
        string='Source Company', 
        required=True, 
        tracking=True,
    )
    
    dest_company_id = fields.Many2one(
        'res.company', 
        string='Destination Company', 
        required=True, 
        tracking=True,
    )
    
    # Transit location — resolved and written by action_confirm (via _validate_and_get_companies)
    transit_location_id = fields.Many2one(
        'stock.location',
        string='Transit Location',
        store=True,
        readonly=True,
        help="Transit location based on parent company. Populated on confirm."
    )
    
    # Domain for allowed companies
    allowed_company_ids = fields.Many2many(
        'res.company',
        compute='_compute_allowed_company_ids',
        store=False
    )
    
    # Linked picking pairs
    transit_picking_ids = fields.One2many(
        'transit.picking',
        'transit_order_id',
        string='Transit Picking Pairs',
        readonly=True,
        help="The picking pairs (OUT/IN) for this transit order"
    )
    
    # Transit lines
    line_ids = fields.One2many(
        'transit.order.line',
        'transit_id',
        string='Transit Lines'
    )
    
    # Dates
    scheduled_date = fields.Datetime('Scheduled Date', tracking=True)
    date_done = fields.Datetime('Effective Date', readonly=True, tracking=True, help="Date at which the inter-transit order have been processed or canceled")
    
    # State
    state = fields.Selection([
        ('draft', 'Draft'),
        ('assigned', 'Assigned'),
        ('in_progress', 'In Progress'),
        ('done', 'Done'),
        ('cancel', 'Cancelled')
    ], default='draft', tracking=True, required=True)
    
    is_late = fields.Boolean(compute='_compute_order_warnings', store=False)
    is_today = fields.Boolean(compute='_compute_order_warnings', store=False)
    is_very_late = fields.Boolean(compute='_compute_order_warnings', store=False)
    has_mismatch = fields.Boolean(compute='_compute_order_warnings', store=False)
    has_done_pickings = fields.Boolean(compute='_compute_has_done_pickings', store=False)
    is_fully_stopped = fields.Boolean(compute='_compute_order_warnings', store=False)

    @api.depends(
        'state', 'scheduled_date',
        'transit_picking_ids.is_late',
        'transit_picking_ids.is_today',
        'transit_picking_ids.is_very_late',
        'transit_picking_ids.has_mismatch',
        'transit_picking_ids.src_picking_state',
        'transit_picking_ids.dest_picking_state',
    )
    def _compute_order_warnings(self):
        now = fields.Datetime.now()
        today = now.date()
        very_late_threshold = 3

        for rec in self:
            terminal = rec.state in ('done', 'cancel')
            sched = rec.scheduled_date

            rec.is_today     = bool(sched and sched.date() == today and not terminal)
            rec.is_late      = bool(sched and sched < now and not terminal)
            rec.is_very_late = bool(sched and (now - sched).days >= very_late_threshold and not terminal)

            # Move-level mismatches (qty/lot)
            # Skip fully-cancelled TPs — both sides cancel means pure history from a previous cycle
            move_mismatch = any(
                tp.has_mismatch
                for tp in rec.transit_picking_ids
                if not (tp.src_picking_state == 'cancel' and tp.dest_picking_state == 'cancel')
            )

            # Structural mismatch: order is done but some pairs were cancelled
            structural_mismatch = (
                rec.state == 'done'
                and any(
                    tp.src_picking_state == 'cancel' or tp.dest_picking_state == 'cancel'
                    for tp in rec.transit_picking_ids
                )
            )

            rec.has_mismatch = move_mismatch or structural_mismatch

            # True when there is nothing left for action_stop to act on
            rec.is_fully_stopped = (
                bool(rec.transit_picking_ids)
                and all(
                    tp.src_picking_state in ('done', 'cancel')
                    for tp in rec.transit_picking_ids
                )
            )

    @api.depends('transit_picking_ids')
    def _compute_has_done_pickings(self):
        for rec in self:
            rec.has_done_pickings = any(tp.state == 'done' for tp in rec.transit_picking_ids)
                
    @api.depends('company_id')
    def _compute_allowed_company_ids(self):
        """
        Return only the direct children of company_id plus company_id itself.
        This matches the direct-child-only transit authorization rule.
        """
        for record in self:
            if not record.company_id:
                record.allowed_company_ids = self.env['res.company']
                continue

            record.allowed_company_ids = record.company_id | record.company_id.child_ids
    
    def _validate_transit_authorization(self, ordering_company, start_company, end_company):
        """
        Validate that ordering_company may create a transit between start and end.

        Rule: ordering_company must be the DIRECT parent of both start_company and
        end_company. The three valid cases are:

            Case 1 — ordering orders between two direct children:
                start_company.parent_id == ordering_company
                dest_company.parent_id  == ordering_company

            Case 2 — ordering orders from itself to a direct child:
                start_company           == ordering_company
                dest_company.parent_id  == ordering_company

            Case 3 — ordering orders from a direct child to itself:
                start_company.parent_id == ordering_company
                dest_company            == ordering_company

        All three cases reduce to the same check:
            LCA(start, end) == ordering_company  (exact equality, not ancestor)

        The LCA is still computed so that the transit location can be resolved
        from the ordering company's transit warehouse downstream.

        Returns:
            (True,  None,      lca_company)  — valid
            (False, error_msg, empty)        — invalid
        """
        if not ordering_company or not start_company or not end_company:
            return False, "One or more companies not found", self.env['res.company']

        if start_company == end_company:
            return False, "Cannot create transit to the same company", self.env['res.company']

        lca = self.env['res.company']._compute_lca(start_company, end_company)
        if not lca:
            return False, (
                f"'{start_company.name}' and '{end_company.name}' are not in the "
                f"same company tree and cannot be linked by a transit order."
            ), self.env['res.company']

        # Direct-child-only rule: ordering company must BE the LCA exactly.
        # Allowing ancestors of the LCA is intentionally excluded here —
        # multi-level transits are not supported and must be split manually.
        if ordering_company.id != lca.id:
            return False, (
                f"Company '{ordering_company.name}' cannot order transit between "
                f"'{start_company.name}' and '{end_company.name}'. "
                f"Only '{lca.name}' (direct parent of both) may order this transit."
            ), self.env['res.company']

        return True, None, lca

    def _validate_and_get_companies(self, transits):
        """
        Validate all transits and resolve their picking-type configs.
    
        Validation rules are described in _validate_transit_authorization. The resolution steps are:
        - Step 1: validate authorization and compute LCA for each transit
                  (LCA is needed to resolve the transit location in Step 2, and also serves as a reference for error messages)
        - Step 2: relation_type lookup REMOVED; transit_location is resolved from LCA
                  by querying the LCA's transit warehouse directly.
        - Step 3: configs looked up by (company_id, transit_location_id) — the new
                  identity key on transit.picking.type.
        - Step 4: transit_location written from LCA resolution (same as before but
                  the source is now explicit tree computation, not inferred from config).
    
        Returns:
            (all_companies, transit_config_map, transit_lca_map)
            where transit_lca_map[transit.id] = lca_company
        """
        errors = []
        all_companies    = self.env['res.company']
        transit_config_map = {}
        transit_lca_map    = {}
    
        for transit in transits:
            ordering_company = transit.company_id
            start_company    = transit.src_company_id
            end_company      = transit.dest_company_id
    
            # ── 1. Authorization check → also returns LCA ────────────────────────
            is_valid, error_msg, lca = self._validate_transit_authorization(
                ordering_company, start_company, end_company
            )
            if not is_valid:
                errors.append(f"Transit '{transit.name}': {error_msg}")
                continue
    
            # ── 2. Resolve transit location from LCA's transit warehouse ──────────
            #   Guaranteed to exist: LCA has children by definition (it's the meeting
            #   point of two branches), therefore always has a transit warehouse.
            lca_transit_wh = self.env['stock.warehouse'].sudo().search([
                '&',
                ('company_id', '=', lca.id),
                ('is_transit_warehouse', '=', True),
            ], limit=1)
    
            if not lca_transit_wh:
                errors.append(
                    f"Transit '{transit.name}': LCA company '{lca.name}' has no "
                    f"transit warehouse. Run the setup wizard or check company structure."
                )
                continue
    
            transit_location = lca_transit_wh.lot_stock_id
            if not transit_location:
                errors.append(
                    f"Transit '{transit.name}': '{lca.name}'.TRANSIT warehouse "
                    f"has no stock location configured."
                )
                continue
    
            # ── 3. Look up picking-type configs by transit_location_id ────────────
            #   Both sides must have a config pointing at the SAME transit location.
            #   This replaces the old relation_type lookup entirely.
            src_config = self.env['transit.picking.type'].search([
                '&',
                ('company_id', '=', start_company.id),
                ('transit_location_id', '=', transit_location.id),
            ], order='create_date ASC', limit=1)
    
            if not src_config:
                errors.append(
                    f"Transit '{transit.name}': No transit picking type configuration "
                    f"for source company '{start_company.name}' via '{lca.name}.TRANSIT'. "
                    f"Ensure all warehouses of '{start_company.name}' have been configured."
                )
                continue
    
            dest_config = self.env['transit.picking.type'].search([
                '&',
                ('company_id', '=', end_company.id),
                ('transit_location_id', '=', transit_location.id),
            ], order='create_date ASC', limit=1)
    
            if not dest_config:
                errors.append(
                    f"Transit '{transit.name}': No transit picking type configuration "
                    f"for destination company '{end_company.name}' via '{lca.name}.TRANSIT'. "
                    f"Ensure all warehouses of '{end_company.name}' have been configured."
                )
                continue
    
            # ── 4. Persist resolved transit location ──────────────────────────────
            transit.write({'transit_location_id': transit_location.id})
    
            all_companies             |= start_company | end_company | lca
            transit_lca_map[transit.id] = lca
            transit_config_map[transit.id] = {
                'src_config':  src_config,
                'dest_config': dest_config,
            }
    
        if errors:
            raise UserError('Validation issues:\n• ' + '\n• '.join(errors))
    
        return all_companies, transit_config_map, transit_lca_map

    def _create_transfer_pickings(self, transits, transit_config_map, transit_lca_map):
        """
        Create picking pairs for DRAFT transits.
        
        Returns: dict {transit_id: {'transit_picking': record, 'start': picking, 'end': picking, 'transit_location': location}}
        """
        transit_picking_map = {}
        errors = []
        
        non_draft = transits.filtered(lambda t: t.state != 'draft')
        if non_draft:
            errors.append(
                f'_create_transfer_pickings called with non-draft transits: '
                f'{", ".join(non_draft.mapped("name"))}'
            )
        
        draft_transits = transits - non_draft
        
        start_picking_vals_list = []
        end_picking_vals_list = []
        transit_data_list = []
        
        for transit in draft_transits:
            start_company = transit.src_company_id
            end_company = transit.dest_company_id
            lca = transit_lca_map.get(transit.id)
            
            if not lca:
                errors.append(f"Transit '{transit.name}': No LCA resolved (validate first)")
                continue
            
            # transit_location_id already written by _validate_and_get_companies — no re-query needed
            transit_location = transit.transit_location_id
            if not transit_location:
                errors.append(
                    f"Transit '{transit.name}': transit_location_id not set (validate first)"
                )
                continue

            config = transit_config_map.get(transit.id)
            if not config:
                errors.append(f"Transit '{transit.name}': No picking type configuration")
                continue
            
            src_config = config['src_config']
            dest_config = config['dest_config']
            
            src_type = src_config.src_picking_type_id
            dest_type = dest_config.dest_picking_type_id
            
            start_picking_vals_list.append({
                'partner_id': end_company.partner_id.id,
                'picking_type_id': src_type.id,
                'scheduled_date': transit.scheduled_date,
                'company_id': start_company.id,
                'origin': transit.name,
            })
            
            end_picking_vals_list.append({
                'partner_id': start_company.partner_id.id,
                'picking_type_id': dest_type.id,
                'scheduled_date': transit.scheduled_date,
                'company_id': end_company.id,
                'origin': transit.name,
            })
            
            transit_data_list.append({'transit': transit})
        
        if errors:
            raise UserError('Picking validation errors:\n• ' + '\n• '.join(errors))
        
        if not start_picking_vals_list:
            return transit_picking_map
        
        try:
            src_pickings = self.env['stock.picking'].sudo().create(start_picking_vals_list)
        except Exception as e:
            raise UserError(f'Failed to batch create source transfers: {str(e)}')
        
        try:
            dest_pickings = self.env['stock.picking'].sudo().create(end_picking_vals_list)
        except Exception as e:
            raise UserError(f'Failed to batch create destination transfers: {str(e)}')
        
        if len(src_pickings) != len(dest_pickings) or len(src_pickings) != len(transit_data_list):
            raise UserError(
                f'Transfer creation mismatch: '
                f'{len(src_pickings)} OUT, {len(dest_pickings)} IN, '
                f'{len(transit_data_list)} expected'
            )
        
        transit_picking_vals_list = []
        for i, transit_data in enumerate(transit_data_list):
            transit = transit_data['transit']
            src_picking = src_pickings[i]
            dest_picking = dest_pickings[i]
            
            if src_picking.location_dest_id.id != dest_picking.location_id.id:
                errors.append(
                    f"Transit '{transit.name}': Location mismatch! "
                    f"OUT.location_dest_id ({src_picking.location_dest_id.name}) != "
                    f"IN.location_id ({dest_picking.location_id.name})"
                )
                continue
            
            transit_picking_vals_list.append({
                'transit_order_id': transit.id,
                'src_picking_id': src_picking.id,
                'dest_picking_id': dest_picking.id,
            })
        
        if errors:
            raise UserError('Picking errors:\n• ' + '\n• '.join(errors))
        
        try:
            transit_pickings = self.env['transit.picking'].sudo().create(
                transit_picking_vals_list
            )
        except Exception as e:
            raise UserError(f'Failed to create transit picking pairs: {str(e)}')
        
        for i, transit_data in enumerate(transit_data_list):
            transit = transit_data['transit']
            transit_picking_map[transit.id] = {
                'transit_picking': transit_pickings[i],
                'start': src_pickings[i],
                'end': dest_pickings[i],
            }
        
        return transit_picking_map

    def _create_moves_for_transit(self, transit, src_picking, dest_picking):
        """
        Create stock moves for a transit order.
        Moves are matched to transit lines by product_id — no persistent link field needed.
        """
        if not transit.line_ids:
            return

        out_move_vals_list = []
        in_move_vals_list  = []

        for line in transit.line_ids:
            if not line.product_id:
                raise UserError(f"Transit '{transit.name}': Product not specified for a line")
            if line.product_uom_qty <= 0:
                raise UserError(
                    f"Transit '{transit.name}': Product '{line.product_id.display_name}': "
                    f"Invalid quantity"
                )

            product = line.product_id
            uom = line.product_uom or product.uom_id
            qty = line.product_uom_qty

            if uom.id != product.uom_id.id:
                qty = uom._compute_quantity(qty, product.uom_id, rounding_method='HALF-UP')

            common = {
                'name': line.name or product.display_name,
                'product_id': product.id,
                'product_uom_qty': qty,
                'product_uom': product.uom_id.id,
                # No transit_line_id — matching is done by product_id at runtime
            }

            out_move_vals_list.append({**common, 'picking_id': src_picking.id, 'company_id': src_picking.company_id.id})
            in_move_vals_list.append({**common, 'picking_id': dest_picking.id, 'company_id': dest_picking.company_id.id})

        try:
            self.env['stock.move'].sudo().create(out_move_vals_list)
        except Exception as e:
            raise UserError(f"Failed to create OUT moves: {str(e)}")

        try:
            self.env['stock.move'].sudo().create(in_move_vals_list)
        except Exception as e:
            raise UserError(f"Failed to create IN moves: {str(e)}")

    def _merge_duplicate_lines(self, transits):
        """
        Merge transit order lines that share the same product_id within each transit.

        This is a prerequisite for product-based move matching, which requires at most
        one line (and therefore one move) per product per picking.

        Merge rules:
        - All quantities are converted to the product's base UOM before summing.
        - The first line (lowest id) survives; the rest are deleted.
        - The surviving line is updated to: base UOM + summed quantity.
        - A single line whose UOM already differs from the base UOM is also normalised
          to the base UOM so that the picking moves are always created in base UOM.
        - Notes from duplicate lines are concatenated onto the surviving line (separated
          by " | ") so that no information is silently lost.

        All DB writes happen in two batched operations:
          1. One write() per unique (uom_id, qty) combination across all surviving lines.
          2. One unlink() for all lines to be deleted.

        Returns a summary dict for logging:
          {transit.id: [(product_name, original_count, merged_qty_in_base_uom), ...]}
        """
        if not transits:
            return {}

        lines_to_delete = self.env['transit.order.line']
        # {line.id: {'product_uom': uom_id, 'product_uom_qty': qty, 'note': str}}
        lines_to_update = {}
        summary = {}

        for transit in transits:
            if not transit.line_ids:
                continue

            # Group lines by product_id, preserving insertion order (lowest id first)
            groups = {}  # {product_id: [line, ...]}
            for line in transit.line_ids.sorted('id'):
                pid = line.product_id.id
                groups.setdefault(pid, []).append(line)

            transit_summary = []

            for product_id, group_lines in groups.items():
                product   = group_lines[0].product_id
                base_uom  = product.uom_id
                survivor  = group_lines[0]

                if len(group_lines) == 1:
                    line = group_lines[0]
                    current_uom = line.product_uom or base_uom

                    if current_uom.id != base_uom.id:
                        # Normalise single line to base UOM
                        qty_in_base = current_uom._compute_quantity(
                            line.product_uom_qty, base_uom, rounding_method='HALF-UP'
                        )
                        lines_to_update[line.id] = {
                            'product_uom':     base_uom.id,
                            'product_uom_qty': qty_in_base,
                        }
                    # Nothing to merge; skip summary entry
                    continue

                # Multiple lines for same product — merge into survivor
                total_qty = 0.0
                collected_notes = []

                for line in group_lines:
                    uom = line.product_uom or base_uom
                    if uom.id != base_uom.id:
                        qty_in_base = uom._compute_quantity(
                            line.product_uom_qty, base_uom, rounding_method='HALF-UP'
                        )
                    else:
                        qty_in_base = line.product_uom_qty
                    total_qty += qty_in_base

                    if line.note:
                        collected_notes.append(line.note.strip())

                # Survivor gets the merged total and the base UOM
                merged_note = ' | '.join(filter(None, collected_notes)) or survivor.note or False
                lines_to_update[survivor.id] = {
                    'product_uom':     base_uom.id,
                    'product_uom_qty': total_qty,
                    'note':            merged_note,
                }

                # All other lines in the group are redundant
                for line in group_lines[1:]:
                    lines_to_delete |= line

                transit_summary.append((product.display_name, len(group_lines), total_qty))
                _logger.info(
                    "Transit '%s': merged %d lines for product '%s' → %.4f %s",
                    transit.name, len(group_lines), product.display_name,
                    total_qty, base_uom.name,
                )

            if transit_summary:
                summary[transit.id] = transit_summary

        # ── Batch delete redundant lines ──────────────────────────────────────
        if lines_to_delete:
            lines_to_delete.with_context(transit_pickings_sync=True).unlink()
            _logger.info("Merge: deleted %d duplicate transit line(s)", len(lines_to_delete))

        # ── Batch write surviving lines ───────────────────────────────────────
        # Group by identical update values for maximum batch efficiency
        if lines_to_update:
            buckets = {}  # {frozenset(vals.items()): [line_id, ...]}
            for line_id, vals in lines_to_update.items():
                key = frozenset(vals.items())
                buckets.setdefault(key, []).append(line_id)

            for vals_key, line_ids in buckets.items():
                self.env['transit.order.line'].with_context(
                    transit_pickings_sync=True
                ).browse(line_ids).write(dict(vals_key))

            _logger.info(
                "Merge: updated %d surviving transit line(s) in %d batch(es)",
                len(lines_to_update), len(buckets),
            )

        return summary

    def _warn_pickings_of_change(self, reason='transit_line_change'):
        user = self.env.user

        reason_texts = {
            'transit_line_change': "One or more transit order lines have been modified (added, edited, or deleted).",
            'reconfirm': "The transit order has been re-confirmed after a previous cancellation.",
        }
        reason_text = reason_texts.get(reason, "The transit order has been modified.")

        for transit in self:
            src_pickings = self.env['stock.picking']
            for tp in transit.transit_picking_ids:
                src_pickings |= tp.sudo().src_picking_id

            if not src_pickings:
                continue

            # ── Only act on pickings not yet flagged ──────────────────────────────
            unflagged = src_pickings.filtered(lambda p: not p.needs_review)
            if not unflagged:
                continue

            msg = Markup(
                "<b>⚠️ Transit Order Modified — Review Required</b><br/>"
                "<b>Transit:</b> {transit_name}<br/>"
                "<b>Reason:</b> {reason_text}<br/>"
                "<b>Modified by:</b> {user_name}<br/>"
                "This transfer is part of a transit order that has been changed after confirmation. "
                "The picking tree may include backorders or delegations that were not automatically "
                "updated. Please consult your manager or the person responsible for this transit "
                "order to confirm whether this transfer needs to be adjusted."
            ).format(
                transit_name=escape(transit.name or ''),
                reason_text=escape(reason_text),
                user_name=escape(user.name or ''),
            )

            for picking in unflagged.sudo():
                try:
                    picking.message_post(body=msg)
                except Exception as e:
                    _logger.warning(
                        "Failed to post review warning on picking '%s' (transit '%s'): %s",
                        picking.name, transit.name, str(e),
                    )

            try:
                unflagged.sudo().write({'needs_review': True})
                _logger.info(
                    "Set needs_review=True on %d src picking(s) for transit '%s' (reason: %s)",
                    len(unflagged), transit.name, reason,
                )
            except Exception as e:
                _logger.warning(
                    "Failed to set needs_review on src pickings for transit '%s': %s",
                    transit.name, str(e),
                )

    def action_confirm(self):
        if not self:
            return True

        draft  = self.filtered(lambda t: t.state == 'draft')

        invalid = self - draft
        if invalid:
            state_names = dict(self._fields['state'].selection)
            states = [state_names[t.state] for t in invalid[:5]]
            if len(invalid) > 5:
                states.append(f"and {len(invalid) - 5} more")
            raise UserError(f'Cannot confirm from states: {", ".join(states)}')

        # Legacy filter for 'cancel' state — allow reconfirmation from cancel to support the "cancel and redo" pattern, 
        # but exclude from the main processing loop as the existing pickings in cancel state are not relevant and should not be merged into.
        # to_process = draft
        if not draft:
            return True

        empty = draft.filtered(lambda t: not t.line_ids)
        if empty:
            names = empty[:10].mapped('name')
            if len(empty) > 10:
                names.append(f"and {len(empty) - 10} more")
            raise UserError(f'No moves defined for transits: {", ".join(names)}')

        self._merge_duplicate_lines(draft)

        all_companies, transit_config_map, transit_lca_map = \
            self._validate_and_get_companies(draft)

        # Both draft and cancel always create fresh pickings.
        # draft:  no TPs exist yet.
        # cancel: all TPs have src_picking_state='cancel' — left as historical records,
        #         new TP created alongside them. No reset, no reuse.
        transit_picking_map = self._create_transfer_pickings(
            draft, transit_config_map, transit_lca_map
        )

        errors = []
        for transit in draft:
            picking_info = transit_picking_map.get(transit.id)
            if not picking_info:
                errors.append(f"Transit '{transit.name}': No picking info")
                continue
            try:
                self._create_moves_for_transit(
                    transit,
                    picking_info['start'].sudo(),
                    picking_info['end'].sudo(),
                )
            except Exception as e:
                errors.append(f"Transit '{transit.name}': {str(e)}")

        if errors:
            raise UserError('Confirm errors:\n• ' + '\n• '.join(errors))

        draft.write({'state': 'assigned'})

        return True
    
    def _batch_confirm_pickings(self, src_pickings):
        """Helper to batch confirm pickings with error handling"""
        confirm_errors = []
        try:
            src_pickings.sudo().action_confirm()
        except Exception as e:
            _logger.error(f"Batch picking confirmation failed: {str(e)}", exc_info=True)
            for picking in src_pickings:
                try:
                    picking.sudo().action_confirm()
                except Exception as pick_err:
                    transit_picking = self.env['transit.picking'].search([
                        ('src_picking_id', '=', picking.id)
                    ], limit=1)
                    transit_name = transit_picking.transit_order_id.name if transit_picking else 'Unknown'
                    confirm_errors.append(
                        f"Transit '{transit_name}' - OUT picking '{picking.name}': {str(pick_err)}"
                    )
        
        if confirm_errors:
            raise UserError('Picking confirmation errors:\n• ' + '\n• '.join(confirm_errors))
    
    def _sync_lines_to_pickings(self, transits):
        """
        Sync transit order lines → stock moves on both src and dest pickings.

        Matching is product-based: each line is matched to existing moves by product_id.
        Moves whose product is no longer in the transit lines are deleted.
        New lines get new moves created.
        Existing moves get their quantity updated if it changed.

        Limitation: if two lines share the same product_id only the first is matched;
        consolidate duplicate-product lines before confirming.
        """
        errors = []
        moves_to_update  = {}
        moves_to_delete  = self.env['stock.move']
        create_vals_list = []

        for transit in transits:
            transit_picking = transit.transit_picking_ids.filtered(lambda tp: tp.state == 'assigned')
            if not transit_picking:
                errors.append(f"Transit '{transit.name}': No transit picking in assigned state")
                continue
            transit_picking = transit_picking[0]

            src_picking  = transit_picking.src_picking_id
            dest_picking = transit_picking.dest_picking_id

            try:
                # Match moves by product_id (first occurrence wins for duplicates)
                src_by_product  = {m.product_id.id: m for m in src_picking.move_ids}
                dest_by_product = {m.product_id.id: m for m in dest_picking.move_ids}

                current_product_ids = {line.product_id.id for line in transit.line_ids}

                # Remove moves whose product was deleted from the lines
                for prod_id, move in src_by_product.items():
                    if prod_id not in current_product_ids:
                        moves_to_delete |= move
                for prod_id, move in dest_by_product.items():
                    if prod_id not in current_product_ids:
                        moves_to_delete |= move

                # Update or create per line
                for line in transit.line_ids:
                    product = line.product_id
                    uom = line.product_uom or product.uom_id
                    qty = line.product_uom_qty

                    if uom.id != product.uom_id.id:
                        qty = uom._compute_quantity(qty, product.uom_id, rounding_method='HALF-UP')

                    common_create = {
                        'name': line.name or product.display_name,
                        'product_id': product.id,
                        'product_uom_qty': qty,
                        'product_uom': product.uom_id.id,
                        'state': 'assigned',
                    }

                    src_move = src_by_product.get(product.id)
                    if src_move:
                        if src_move.product_uom_qty != qty:
                            moves_to_update[src_move.id] = {'product_uom_qty': qty}
                    else:
                        create_vals_list.append({
                            **common_create,
                            'picking_id': src_picking.id,
                            'company_id': src_picking.company_id.id,
                        })

                    dest_move = dest_by_product.get(product.id)
                    if dest_move:
                        if dest_move.product_uom_qty != qty:
                            moves_to_update[dest_move.id] = {'product_uom_qty': qty}
                    else:
                        create_vals_list.append({
                            **common_create,
                            'picking_id': dest_picking.id,
                            'company_id': dest_picking.company_id.id,
                        })

            except Exception as e:
                errors.append(f"Transit '{transit.name}': {str(e)}")

        if errors:
            raise UserError('Move sync errors:\n• ' + '\n• '.join(errors))

        if moves_to_delete:
            try:
                moves_to_delete.sudo().unlink()
            except Exception as e:
                raise UserError(f'Failed to delete moves: {str(e)}')

        if moves_to_update:
            try:
                moves_by_vals = {}
                for move_id, vals in moves_to_update.items():
                    moves_by_vals.setdefault(tuple(sorted(vals.items())), []).append(move_id)
                for vals_key, ids in moves_by_vals.items():
                    self.env['stock.move'].browse(ids).sudo().write(dict(vals_key))
            except Exception as e:
                raise UserError(f'Failed to update moves: {str(e)}')

        if create_vals_list:
            try:
                self.env['stock.move'].sudo().create(create_vals_list)
            except Exception as e:
                raise UserError(f'Failed to create moves: {str(e)}')

    def _sync_scheduled_date_to_pickings(self):
        """
        Push the transit order's scheduled_date to every non-terminal picking pair.
    
        Called automatically from write() when scheduled_date changes on a
        draft/assigned transit that already has picking pairs.
        """
        for transit in self:
            if not transit.scheduled_date:
                continue
    
            all_pickings = self.env['stock.picking']
            for tp in transit.transit_picking_ids:
                all_pickings |= tp.src_picking_id | tp.dest_picking_id
    
            updatable = all_pickings.filtered(
                lambda p: p.state not in ('done', 'cancel')
            )
            if not updatable:
                continue
    
            try:
                updatable.sudo().write({'scheduled_date': transit.scheduled_date})
                _logger.info(
                    "Transit '%s': scheduled_date synced to %d picking(s)",
                    transit.name, len(updatable),
                )
            except Exception as e:
                # Non-blocking — log and continue so the transit write still succeeds.
                _logger.warning(
                    "Transit '%s': failed to sync scheduled_date to transfers: %s",
                    transit.name, str(e),
                )
    
    def _sync_company_changes_to_pickings(self):
        if not self:
            return

        errors = []

        try:
            _, transit_config_map, _ = self._validate_and_get_companies(self)
        except UserError as e:
            raise UserError(f"Company change blocked by validation:\n{e.args[0]}")

        for transit in self:
            config = transit_config_map.get(transit.id)
            if not config:
                errors.append(f"Transit '{transit.name}': no config resolved after re-validation")
                continue

            src_type      = config['src_config'].src_picking_type_id
            dest_type     = config['dest_config'].dest_picking_type_id
            start_company = transit.src_company_id
            end_company   = transit.dest_company_id

            for tp in transit.transit_picking_ids:
                src_picking  = tp.src_picking_id.sudo()
                dest_picking = tp.dest_picking_id.sudo()

                # location_id / location_dest_id will recompute automatically
                # via _compute_location_id when picking_type_id changes
                try:
                    src_picking.write({
                        'company_id':      start_company.id,
                        'picking_type_id': src_type.id,
                        'partner_id':      end_company.partner_id.id,
                    })
                    src_picking.move_ids.sudo().write({'company_id': start_company.id})
                except Exception as e:
                    errors.append(f"Transit '{transit.name}': src transfer update failed: {e}")
                    continue

                try:
                    dest_picking.write({
                        'company_id':      end_company.id,
                        'picking_type_id': dest_type.id,
                        'partner_id':      start_company.partner_id.id,
                    })
                    dest_picking.move_ids.sudo().write({'company_id': end_company.id})
                except Exception as e:
                    errors.append(f"Transit '{transit.name}': dest transfer update failed: {e}")

        if errors:
            raise UserError('Company sync errors:\n• ' + '\n• '.join(errors))

    def action_cancel(self):
        """
        Cancel transit orders that still draft.
        """
        non_cancellable = self.filtered(lambda t: t.state in ('done', 'cancel', 'in_progress'))
        cancellable     = self - non_cancellable

        errors = [
            f"'{t.name}': Cannot cancel in state '{t.state}'"
            for t in non_cancellable
        ]

        if cancellable:
            transit_pickings = cancellable.mapped('transit_picking_ids').filtered(
                lambda tp: tp.src_picking_state not in ('done', 'cancel')
            )
            if transit_pickings:
                try:
                    transit_pickings.action_batch_cancel()
                except UserError as e:
                    raise UserError(f'Failed to cancel transfers: {e.args[0]}')

            cancellable.write({
                'state': 'cancel',
                'date_done': fields.Datetime.now(),
            })

        if errors:
            raise UserError('Cancel errors:\n• ' + '\n• '.join(errors))

        return True

    def action_stop(self):
        """
        Stop transit orders that are in progress, cancel any route that the src is not done.
        """
        non_stoppable = self.filtered(lambda t: t.state != 'in_progress')
        if non_stoppable:
            state_names = dict(self._fields['state'].selection)
            raise UserError(
                "Can only stop transit orders that are already in progress:\n• "
                + '\n• '.join(
                    f"'{t.name}' ({state_names.get(t.state, t.state)})"
                    for t in non_stoppable
                )
            )

        cancellable = self.mapped('transit_picking_ids').filtered(
            lambda tp: tp.src_picking_state not in ('done', 'cancel')
        )
        if cancellable:
            cancellable.action_batch_stop()

        return True
    
    def action_review(self):
        """
        Mark an intentional mismatch as acknowledged.
        Only meaningful on done orders that have an active mismatch flag.
        """
        invalid = self.filtered(lambda t: t.state != 'done' or not t.has_mismatch)
        if invalid:
            raise UserError(
                "Only done orders with a mismatch can be acknowledged:\n• "
                + '\n• '.join(invalid.mapped('name'))
            )
        self.write({'is_reviewed': True})
        return True

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if 'company_id' not in vals:
                vals['company_id'] = self.env.company.id
            if 'scheduled_date' not in vals or vals['scheduled_date'] == False:
                vals['scheduled_date'] = fields.Datetime.now()
        
        records_by_company = {}
        for vals in vals_list:
            company_id = vals['company_id']
            if company_id not in records_by_company:
                records_by_company[company_id] = []
            records_by_company[company_id].append(vals)
        
        all_company_ids = set()
        for vals in vals_list:
            if vals.get('src_company_id'):
                all_company_ids.add(vals['src_company_id'])
            if vals.get('dest_company_id'):
                all_company_ids.add(vals['dest_company_id'])
        
        companies = self.env['res.company'].browse(list(all_company_ids))
        company_map = {c.id: c for c in companies}
        
        for company_id, company_vals_list in records_by_company.items():
            company = self.env['res.company'].browse(company_id)
            parent_name = company.name
            
            for vals in company_vals_list:
                src_company_id = vals.get('src_company_id')	
                dest_company_id = vals.get('dest_company_id')
                
                if not all([company_id, src_company_id, dest_company_id]):
                    continue
                
                sequence = self.env['ir.sequence'].with_company(company_id).next_by_code(
                    'transit.order'
                )
                
                if not sequence:
                    continue
                
                parts = sequence.split('/')
                sequence_number = parts[-1]
                
                src_company = company_map.get(src_company_id)
                dest_company = company_map.get(dest_company_id)
                
                if not src_company or not dest_company:
                    continue
                
                start_name = src_company.name.replace(' ', '_')
                end_name = dest_company.name.replace(' ', '_')
                
                new_name = f"{parent_name}/TRANSIT/{start_name}.{end_name}/{sequence_number}"
                vals['name'] = new_name
        
        return super().create(vals_list)

    def _check_write_state(self, vals):
        """
        Block writes on in_progress / done orders.

        Exempt fields (state, date_done, is_reviewed …) are always allowed —
        they are written by internal transitions, not by the user.

        Skip entirely when context key is set.
        """
        if self.env.context.get('skip_transit_order_write_state_check'):
            return
        _WRITE_BLOCKED_STATES = frozenset({'in_progress', 'done'})

        # Fields that are always allowed to be written regardless of state
        # (internal transitions, system flags, computed fields)
        _WRITE_STATE_EXEMPT_FIELDS = frozenset({
            'state',
            'date_done',
            'is_reviewed',
            'transit_location_id',
            'transit_picking_ids',
            'note',
        })
        # If every key in vals is exempt, nothing to guard
        non_exempt = set(vals) - _WRITE_STATE_EXEMPT_FIELDS
        if not non_exempt:
            return

        blocked = self.filtered(lambda t: t.state in _WRITE_BLOCKED_STATES)
        if not blocked:
            return

        state_names = dict(self._fields['state'].selection)
        raise UserError(
            "Cannot edit transit order(s) in state 'In Progress' or 'Done':\n• "
            + '\n• '.join(
                f"'{t.name}' ({state_names.get(t.state, t.state)})"
                for t in blocked
            )
            + "\n\nCancel the transit order first, or contact your manager."
        )

    def write(self, vals):
        self._check_write_state(vals)
        date_changed = 'scheduled_date' in vals

        if date_changed:
            pre_date_sync = self.filtered(
                lambda t: t.state in ('draft', 'assigned') and bool(t.transit_picking_ids)
            )
        else:
            pre_date_sync = self.env['transit.order']

        result = super().write(vals)

        for record in self:
            if (
                'name' in vals
                and record.state in ('in_progress', 'done')
                and not self.env.context.get('transit_order_skip_name_check')
            ):
                raise UserError("Cannot change the reference when in progress or done.")

        if pre_date_sync:
            new_date = vals.get('scheduled_date')
            if isinstance(new_date, str):
                new_date = fields.Datetime.from_string(new_date)
            if new_date and new_date < fields.Datetime.now():
                raise UserError(
                    f"Scheduled date cannot be set in the past: {new_date}"
                )
            all_pickings = pre_date_sync.mapped('transit_picking_ids').mapped(
                lambda tp: tp.src_picking_id | tp.dest_picking_id
            )
            updatable = all_pickings.filtered(lambda p: p.state not in ('done', 'cancel'))
            if updatable:
                updatable.sudo().write({'scheduled_date': new_date})

                not_yet_flagged = updatable.filtered(lambda p: not p.date_changed)
                if not_yet_flagged:
                    not_yet_flagged.sudo().write({'date_changed': True})

        return result

    def unlink(self):
        """Override unlink - simplified with cascade delete"""
        errors = []
        
        invalid_states = ['assigned', 'in_progress', 'done']
        invalid_transits = self.filtered(lambda t: t.state in invalid_states)
        
        if invalid_transits:
            state_names = dict(self._fields['state'].selection)
            for transit in invalid_transits:
                state_name = state_names.get(transit.state, transit.state)
                errors.append(
                    f"Transit '{transit.name}': Cannot delete in state '{state_name}'. "
                    f"Only 'Draft' and 'Cancelled' transits can be deleted."
                )
        
        valid_transits = self - invalid_transits
        
        if not valid_transits:
            raise UserError('Delete errors:\n• ' + '\n• '.join(errors))
        
        transit_pickings = valid_transits.mapped('transit_picking_ids').filtered(lambda tp: tp.exists())
        stock_pickings = (
            transit_pickings.mapped('src_picking_id') | 
            transit_pickings.mapped('dest_picking_id')
        ).filtered(lambda p: p.exists())

        try:
            result = super(TransitOrder, valid_transits).unlink()
        except Exception as e:
            _logger.error("Failed to delete transits: %s", str(e), exc_info=True)
            errors.append(f"Failed to delete transits: {str(e)}")
            raise UserError('Delete errors:\n• ' + '\n• '.join(errors))
        
        if transit_pickings:
            try:
                transit_pickings.sudo().unlink()
                _logger.info(f"Deleted {len(transit_pickings)} transit picking pairs")
            except Exception as e:
                _logger.error("Failed to delete transit transfers: %s", str(e), exc_info=True)

        if stock_pickings:
            try:
                stock_pickings.sudo().unlink()
                _logger.info(f"Deleted {len(stock_pickings)} stock transfers")
            except Exception as e:
                _logger.error("Failed to delete stock transfers: %s", str(e), exc_info=True)
                errors.append(f"Failed to delete stock transfers: {str(e)}")
        
        if errors:
            raise UserError('Delete errors:\n• ' + '\n• '.join(errors))
        
        return result

    @api.constrains('src_company_id', 'dest_company_id')
    def _check_different_companies(self):
        """Ensure source and destination companies are different"""
        for transit in self:
            if transit.src_company_id and transit.dest_company_id:
                if transit.src_company_id.id == transit.dest_company_id.id:
                    raise ValidationError(
                        f"Transit '{transit.name}': Source and Destination companies "
                        f"must be different."
                    )
                
    @api.onchange('src_company_id', 'dest_company_id')
    def _onchange_companies(self):
        if self.src_company_id and self.dest_company_id and self.company_id and self.name:
            if '/' in self.name:
                parts = self.name.split('/')
                if len(parts) >= 4:
                    sequence_number = parts[-1]
                    parent_name = self.company_id.name
                    start_name = self.src_company_id.name.replace(' ', '_')
                    end_name = self.dest_company_id.name.replace(' ', '_')
                    self.name = f"{parent_name}/TRANSIT/{start_name}.{end_name}/{sequence_number}"