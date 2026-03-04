from odoo import api, fields, models # type: ignore
from odoo.exceptions import UserError, ValidationError # type: ignore
from collections import defaultdict
from datetime import datetime
import logging

_logger = logging.getLogger(__name__)


class T4tekTransitOrder(models.Model):
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
    │  Mother     │   OUT   │   Mother     │   IN    │  Child      │
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

    Internal State Flow:    
    1. Forward Flow:
    - draft -> assigned: action_confirm()
    - assigned -> in_progress: automatic when OUT picking is validated
    - in_progress -> done: automatic when IN picking is validated

    2. Reassignment Flow:
    - assigned -> assigned: 
    + onchange of companies (updates pickings/moves)
    + onchange of transit moves (updates stock moves)

    3. Cancel Flow:
    - draft -> cancel: action_cancel()
    - assigned -> cancel: action_cancel()

    4. Recovery from Cancel:
    - cancel -> assigned: action_confirm() (after changing companies/moves as needed)

    5. Deletion:
    - draft -> unlink: unlink()
    - cancel -> unlink: unlink()

    Note: 
    - src.location_dest_id MUST equal dest.location_id (the transit location).
    - The transit location in this inter-transit context is just an implication that these 
    goods are in process between companies and not physically present (virtual location).
    - While stock locations still follow Odoo's multi-company transfer rules (belonging to no company), 
    the transit location itself has a relation to a specific company (relation field from the company) 
    to indicate that it belongs to that company for stock reporting purposes.

    Key Components:
    - t4tek.transit.order.line: User-defined transit lines.
    - t4tek.transit.picking: Mapping src/dest pickings with transit location.
    - t4tek.transit.picking.type: Mapping src/dest operations types that use for transit process. 
    """
    _name = 't4tek.transit.order'
    _description = 'T4tek Inter-Company Transit'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _rec_name = 'name'
    
    name = fields.Char(
        'Reference', 
        readonly=True,
        copy=False,
        required=True,
        default='New',
    )
    
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
    
    # Transit location computed from company relationships
    transit_location_id = fields.Many2one(
        'stock.location',
        string='Transit Location',
        compute='_compute_transit_location',
        store=True,
        readonly=True,
        help="Transit location based on parent company"
    )
    
    # Operation Types determined automatically via transit picking type configs
    t4tek_src_transit_picking_type_id = fields.Many2one(
        't4tek.transit.picking.type',
        string='Source Transit Picking Type',
        compute='_compute_transit_picking_types',
        store=True,
        readonly=True,
        help="Transit picking type configuration used for source company"
    )
    
    t4tek_dest_transit_picking_type_id = fields.Many2one(
        't4tek.transit.picking.type',
        string='Destination Transit Picking Type',
        compute='_compute_transit_picking_types',
        store=True,
        readonly=True,
        help="Transit picking type configuration used for destination company"
    )
    
    # Domain for allowed companies
    allowed_company_ids = fields.Many2many(
        'res.company',
        compute='_compute_allowed_company_ids',
        store=False
    )
    
    # Linked picking pairs
    transit_picking_ids = fields.One2many(
        't4tek.transit.picking',
        't4tek_transit_order_id',
        string='Transit Picking Pairs',
        readonly=True,
        help="The picking pairs (OUT/IN) for this transit order"
    )
    
    # Transit lines
    line_ids = fields.One2many(
        't4tek.transit.order.line',
        't4tek_transit_id',
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

    @api.depends(
        'state', 'scheduled_date',
        'transit_picking_ids.is_late',
        'transit_picking_ids.is_today',
        'transit_picking_ids.is_very_late',
        'transit_picking_ids.has_mismatch',
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
            rec.has_mismatch = any(tp.has_mismatch for tp in rec.transit_picking_ids)

    @api.depends('transit_picking_ids')
    def _compute_has_done_pickings(self):
      for rec in self:
        rec.has_done_pickings = any(tp.state == 'done' for tp in rec.transit_picking_ids)
                
    @api.depends('company_id')
    def _compute_allowed_company_ids(self):
        for record in self:
            if record.company_id:
                # Direct children + the company itself (for self → child or child → self cases)
                allowed = record.company_id.child_ids | record.company_id
                record.allowed_company_ids = allowed
            else:
                record.allowed_company_ids = self.env['res.company']
    
    @api.depends('src_company_id', 'dest_company_id', 'company_id')
    def _compute_transit_location(self):
        """Compute the transit location based on company relationships"""
        for transit in self:
            if not transit.src_company_id or not transit.dest_company_id:
                transit.transit_location_id = False
                continue
            
            parent_company = self._get_parent_company(
                transit.src_company_id, 
                transit.dest_company_id
            )
            
            if parent_company:
                transit_location = parent_company._get_transit_location()
                transit.transit_location_id = transit_location.id if transit_location else False
            else:
                transit.transit_location_id = False
    
    @api.depends('src_company_id', 'dest_company_id', 'transit_location_id')
    def _compute_transit_picking_types(self):
        """
        Compute transit picking type configurations based on transit location
        
        Note: Must filter by transit_location_id to select the correct configuration.
        A company can have up to 2 transit picking type configs (one for parent's transit location,
        one for its own transit location). We must pick the one matching the computed transit_location_id.
        """
        for transit in self:
            # Must have transit location computed first
            if not transit.transit_location_id:
                transit.t4tek_src_transit_picking_type_id = False
                transit.t4tek_dest_transit_picking_type_id = False
                continue
            
            if transit.src_company_id:
                src_config = self.env['t4tek.transit.picking.type'].search([
                    ('company_id', '=', transit.src_company_id.id),
                    ('transit_location_id', '=', transit.transit_location_id.id)
                ], order='create_date ASC', limit=1)
                transit.t4tek_src_transit_picking_type_id = src_config.id if src_config else False
            else:
                transit.t4tek_src_transit_picking_type_id = False
            
            if transit.dest_company_id:
                dest_config = self.env['t4tek.transit.picking.type'].search([
                    ('company_id', '=', transit.dest_company_id.id),
                    ('transit_location_id', '=', transit.transit_location_id.id)
                ], order='create_date ASC', limit=1)
                transit.t4tek_dest_transit_picking_type_id = dest_config.id if dest_config else False
            else:
                transit.t4tek_dest_transit_picking_type_id = False
    
    def _get_parent_company(self, start_company, end_company):
        """Get the parent company for the transit relationship"""
        if not start_company or not end_company:
            return self.env['res.company']
        
        # Case 1: end_company is child of start_company
        if end_company in start_company.child_ids:
            return start_company
        
        # Case 2: start_company is child of end_company
        if start_company in end_company.child_ids:
            return end_company
        
        # Case 3: Both are children of same parent (siblings)
        if start_company.parent_id and start_company.parent_id == end_company.parent_id:
            return start_company.parent_id
        
        return self.env['res.company']
    
    def _validate_transit_authorization(self, ordering_company, start_company, end_company):
        """Validate that ordering company can create transit between start and end
        """
        if not ordering_company or not start_company or not end_company:
            return False, "One or more companies not found", self.env['res.company']
        
        if start_company == end_company:
            return False, "Cannot create transit to the same company", self.env['res.company']
        
        # Case 1: Ordering company is parent of both (siblings)
        if end_company in ordering_company.child_ids and start_company in ordering_company.child_ids:
            return True, None, ordering_company
        
        # Case 2: Ordering company is start, and end is its child
        if ordering_company == start_company and end_company in start_company.child_ids:
            return True, None, start_company
        
        # Case 3: Ordering company is end, and start is its child
        if ordering_company == end_company and start_company in end_company.child_ids:
            return True, None, end_company
        
        return False, (
            f"Company '{ordering_company.name}' cannot order transit between "
            f"'{start_company.name}' and '{end_company.name}'. "
            f"Only parent companies can order transits between their direct children, "
            f"or a company can order transit from itself to its direct children."
        ), self.env['res.company']

    def _validate_and_get_companies(self, transits):
        """
        Validate transit authorization and company setup
        """
        errors = []
        all_companies = self.env['res.company']
        transit_config_map = {}
        transit_parent_map = {}
        
        for transit in transits:
            ordering_company = transit.company_id
            start_company = transit.src_company_id
            end_company = transit.dest_company_id
            
            # Validate authorization
            is_valid, error_msg, parent_company = self._validate_transit_authorization(
                ordering_company, start_company, end_company
            )
            
            if not is_valid:
                errors.append(f"Transit '{transit.name}': {error_msg}")
                continue
            
            # CHANGED: Check for transit warehouse/location through helper method
            transit_location = parent_company._get_transit_location()
            if not transit_location:
                errors.append(
                    f"Transit '{transit.name}': Parent company '{parent_company.name}' "
                    f"has no transit warehouse or transit location"
                )
                continue
            
            # Check transit picking type configs
            if not transit.t4tek_src_transit_picking_type_id:
                errors.append(
                    f"Transit '{transit.name}': No transit picking type configuration "
                    f"for source company '{start_company.name}'"
                )
                continue
            
            if not transit.t4tek_dest_transit_picking_type_id:
                errors.append(
                    f"Transit '{transit.name}': No transit picking type configuration "
                    f"for destination company '{end_company.name}'"
                )
                continue
            
            all_companies |= start_company
            all_companies |= end_company
            all_companies |= parent_company
            
            transit_parent_map[transit.id] = parent_company
            
            transit_config_map[transit.id] = {
                'src_config': transit.t4tek_src_transit_picking_type_id,
                'dest_config': transit.t4tek_dest_transit_picking_type_id,
            }
        
        if errors:
            raise UserError('Validation issues:\n• ' + '\n• '.join(errors))
        
        return all_companies, transit_config_map, transit_parent_map

    def _create_transfer_pickings(self, transits, transit_config_map, transit_parent_map):
        """
        Create picking pairs for DRAFT transits
        
        Returns: dict {transit_id: {'transit_picking': record, 'start': picking, 'end': picking, 'transit_location': location}}
        """
        transit_picking_map = {}
        errors = []
        
        # Validate all transits are draft
        non_draft = transits.filtered(lambda t: t.state != 'draft')
        if non_draft:
            errors.append(
                f'_create_transfer_pickings called with non-draft transits: '
                f'{", ".join(non_draft.mapped("name"))}'
            )
        
        draft_transits = transits.filtered(lambda t: t.state == 'draft')
        
        # Prepare data for batch creation
        start_picking_vals_list = []
        end_picking_vals_list = []
        transit_data_list = []
        
        for transit in draft_transits:
            start_company = transit.src_company_id
            end_company = transit.dest_company_id
            parent_company = transit_parent_map.get(transit.id)
            
            if not parent_company:
                errors.append(f"Transit '{transit.name}': No parent company found")
                continue
            
            transit_location = parent_company._get_transit_location()
            
            if not transit_location:
                errors.append(
                    f"Transit '{transit.name}': No transit location for parent '{parent_company.name}'"
                )
                continue
            
            # Get picking types from config
            config = transit_config_map.get(transit.id)
            if not config:
                errors.append(f"Transit '{transit.name}': No picking type configuration")
                continue
            
            src_config = config['src_config']
            dest_config = config['dest_config']
            
            src_type = src_config.src_picking_type_id
            dest_type = dest_config.dest_picking_type_id
            
            # Prepare OUT picking values (NO manual location assignment)
            start_picking_vals_list.append({
                'partner_id': end_company.partner_id.id,
                'picking_type_id': src_type.id,
                'scheduled_date': transit.scheduled_date,
                'company_id': start_company.id,
                'origin': transit.name,
            })
            
            # Prepare IN picking values (NO manual location assignment)
            end_picking_vals_list.append({
                'partner_id': start_company.partner_id.id,
                'picking_type_id': dest_type.id,
                'scheduled_date': transit.scheduled_date,
                'company_id': end_company.id,
                'origin': transit.name,
            })
            
            transit_data_list.append({
                'transit': transit,
                'transit_location': transit_location,
            })
        
        if errors:
            raise UserError('Picking validation errors:\n• ' + '\n• '.join(errors))
        
        if not start_picking_vals_list:
            return transit_picking_map
        
        # Batch create all source pickings
        try:
            src_pickings = self.env['stock.picking'].sudo().create(start_picking_vals_list)
        except Exception as e:
            raise UserError(f'Failed to batch create source pickings: {str(e)}')
        
        # Batch create all destination pickings
        try:
            dest_pickings = self.env['stock.picking'].sudo().create(end_picking_vals_list)
        except Exception as e:
            raise UserError(f'Failed to batch create destination pickings: {str(e)}')
        
        # Validate creation
        if len(src_pickings) != len(dest_pickings) or len(src_pickings) != len(transit_data_list):
            raise UserError(
                f'Picking creation mismatch: '
                f'{len(src_pickings)} OUT, {len(dest_pickings)} IN, '
                f'{len(transit_data_list)} expected'
            )
        
        # Create t4tek.transit.picking records
        transit_picking_vals_list = []
        for i, transit_data in enumerate(transit_data_list):
            transit = transit_data['transit']
            src_picking = src_pickings[i]
            dest_picking = dest_pickings[i]
            
            # Verify location match (locations set by picking type)
            if src_picking.location_dest_id.id != dest_picking.location_id.id:
                errors.append(
                    f"Transit '{transit.name}': Location mismatch! "
                    f"OUT.location_dest_id ({src_picking.location_dest_id.name}) != "
                    f"IN.location_id ({dest_picking.location_id.name})"
                )
                continue
            
            transit_picking_vals_list.append({
                't4tek_transit_order_id': transit.id,
                'src_picking_id': src_picking.id,
                'dest_picking_id': dest_picking.id,
            })
        
        if errors:
            raise UserError('Picking errors:\n• ' + '\n• '.join(errors))
        
        # Batch create transit picking pairs
        try:
            transit_pickings = self.env['t4tek.transit.picking'].sudo().create(
                transit_picking_vals_list
            )
        except Exception as e:
            raise UserError(f'Failed to create transit picking pairs: {str(e)}')
        
        # Build result map
        for i, transit_data in enumerate(transit_data_list):
            transit = transit_data['transit']
            transit_location = transit_data['transit_location']
            
            transit_picking_map[transit.id] = {
                'transit_picking': transit_pickings[i],
                'start': src_pickings[i],
                'end': dest_pickings[i],
                'transit_location': transit_location,
            }
        
        return transit_picking_map

    def _create_moves_for_transit(self, transit, src_picking, dest_picking):
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
                't4tek_transit_line_id': line.id,
            }

            out_move_vals_list.append({**common,'picking_id': src_picking.id,'company_id': src_picking.company_id.id})
            in_move_vals_list.append({ **common,'picking_id': dest_picking.id,'company_id': dest_picking.company_id.id})

        try:
            self.env['stock.move'].sudo().create(out_move_vals_list)
        except Exception as e:
            raise UserError(f"Failed to create OUT moves: {str(e)}")

        try:
            self.env['stock.move'].sudo().create(in_move_vals_list)
        except Exception as e:
            raise UserError(f"Failed to create IN moves: {str(e)}")

    def action_confirm(self):
        """
        Initiate transit process
        """
        if not self:
            return True
        
        # Separate transits by state
        draft = self.filtered(lambda t: t.state == 'draft')
        cancel = self.filtered(lambda t: t.state == 'cancel')
        
        # Reject invalid states
        invalid = self - draft - cancel
        if invalid:
            state_names = dict(self._fields['state'].selection)
            states = [state_names[t.state] for t in invalid[:5]]
            if len(invalid) > 5:
                states.append(f"and {len(invalid) - 5} more")
            raise UserError(f'Cannot confirm from states: {", ".join(states)}')
        
        # Process both cancel and draft
        to_process = cancel | draft
        
        if not to_process:
            return True
        
        # Validate lines exist
        empty = to_process.filtered(lambda t: not t.line_ids)
        if empty:
            names = empty[:10].mapped('name')
            if len(empty) > 10:
                names.append(f"and {len(empty) - 10} more")
            raise UserError(f'No moves defined for transits: {", ".join(names)}')
        
        # Validate and get company setup
        all_companies, transit_config_map, transit_parent_map = self._validate_and_get_companies(to_process)
        
        # Separate transits with/without existing pickings
        with_pickings = to_process.filtered(lambda t: t.transit_picking_ids)
        without_pickings = to_process - with_pickings
        
        # ===== INLINE: Handle transits WITH existing pickings =====
        if with_pickings:
            errors = []
            transit_picking_map = {}
            
            for transit in with_pickings:
                # Get first transit picking pair
                transit_picking = transit.transit_picking_ids.filtered(lambda tp: tp.exists())
                if not transit_picking:
                    errors.append(f"Transit '{transit.name}': No valid transit picking found")
                    continue
                
                transit_picking = transit_picking[0]
                src_picking = transit_picking.src_picking_id.sudo()
                dest_picking = transit_picking.dest_picking_id.sudo()
                
                transit_picking_map[transit.id] = {
                    'transit_picking': transit_picking,
                    'start': src_picking,
                    'end': dest_picking,
                    'transit_location': transit.transit_location_id,
                }
                
                # Check if pickings have moves
                has_moves = bool(src_picking.move_ids or dest_picking.move_ids)
                
                if has_moves:
                    # Pickings have moves → sync lines to moves
                    try:
                        self._sync_lines_to_pickings(transit)
                        _logger.info(f"Transit '{transit.name}': Synced lines to existing moves")
                    except Exception as e:
                        errors.append(f"Transit '{transit.name}': Failed to sync lines: {str(e)}")
                else:
                    # Pickings don't have moves → create moves
                    try:
                        self._create_moves_for_transit(transit, src_picking, dest_picking)
                        _logger.info(f"Transit '{transit.name}': Created moves from lines")
                    except Exception as e:
                        errors.append(f"Transit '{transit.name}': Failed to create moves: {str(e)}")
            
            if errors:
                raise UserError('Reconfirm errors:\n• ' + '\n• '.join(errors))
            
            # Confirm OUT pickings that aren't already confirmed
            src_pickings = self.env['stock.picking']
            for picking_info in transit_picking_map.values():
                if picking_info['start'].state == 'draft':
                    src_pickings |= picking_info['start']
            
            if src_pickings:
                self._batch_confirm_pickings(src_pickings)
            
            # INLINE: Update state directly (was _update_transit_state_and_refs)
            with_pickings.write({'state': 'assigned'})
        
        # ===== INLINE: Handle transits WITHOUT existing pickings =====
        if without_pickings:
            # Create picking pairs
            transit_picking_map = self._create_transfer_pickings(
                without_pickings,
                transit_config_map,
                transit_parent_map
            )
            
            # Create moves directly
            errors = []
            for transit in without_pickings:
                picking_info = transit_picking_map.get(transit.id)
                if not picking_info:
                    errors.append(f"Transit '{transit.name}': No picking info")
                    continue
                
                try:
                    self._create_moves_for_transit(
                        transit,
                        picking_info['start'].sudo(),
                        picking_info['end'].sudo()
                    )
                except Exception as e:
                    errors.append(f"Transit '{transit.name}': {str(e)}")
            
            if errors:
                raise UserError('Move creation errors:\n• ' + '\n• '.join(errors))
            
            # Confirm OUT pickings
            src_pickings = self.env['stock.picking']
            for picking_info in transit_picking_map.values():
                src_pickings |= picking_info['start'].sudo()
            
            if src_pickings:
                self._batch_confirm_pickings(src_pickings)
            
            # INLINE: Update state directly
            without_pickings.write({'state': 'assigned'})
        
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
                    transit_picking = self.env['t4tek.transit.picking'].search([
                        ('src_picking_id', '=', picking.id)
                    ], limit=1)
                    transit_name = transit_picking.t4tek_transit_order_id.name if transit_picking else 'Unknown'
                    confirm_errors.append(
                        f"Transit '{transit_name}' - OUT picking '{picking.name}': {str(pick_err)}"
                    )
        
        if confirm_errors:
            raise UserError('Picking confirmation errors:\n• ' + '\n• '.join(confirm_errors))
    
    def _sync_lines_to_pickings(self, transits):
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
                src_by_line = {m.transit_line_id.id: m for m in src_picking.move_ids if m.transit_line_id}
                dest_by_line = {m.transit_line_id.id: m for m in dest_picking.move_ids if m.transit_line_id}

                current_line_ids = {line.id for line in transit.line_ids}

                # Remove moves whose line was deleted
                for line_id, move in src_by_line.items():
                    if line_id not in current_line_ids:
                        moves_to_delete |= move
                for line_id, move in dest_by_line.items():
                    if line_id not in current_line_ids:
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
                        't4tek_transit_line_id': line.id,
                        'state': 'assigned',
                    }

                    src_move = src_by_line.get(line.id)
                    if src_move:
                        if src_move.product_uom_qty != qty:
                            moves_to_update[src_move.id] = {'product_uom_qty': qty}
                    else:
                        create_vals_list.append({**common_create, 'picking_id': src_picking.id, 'company_id': src_picking.company_id.id})

                    dest_move = dest_by_line.get(line.id)
                    if dest_move:
                        if dest_move.product_uom_qty != qty:
                            moves_to_update[dest_move.id] = {'product_uom_qty': qty}
                    else:
                        create_vals_list.append({**common_create, 'picking_id': dest_picking.id, 'company_id': dest_picking.company_id.id})

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

    def action_cancel(self):
        """Cancel transit"""
        errors = []
        
        for transit in self:
            if transit.state in ['done', 'cancel', 'in_progress']:
                errors.append(
                    f"'{transit.name}': Cannot cancel in state '{transit.state}'"
                )
                continue
            
            try:
                if transit.transit_picking_ids:
                    pickings = self.env['stock.picking']
                    for transit_pair in transit.transit_picking_ids:
                        pickings |= transit_pair.src_picking_id | transit_pair.dest_picking_id
                    
                    pickings = pickings.filtered(lambda p: p.state not in ['done', 'cancel'])
                    
                    if pickings:
                        pickings.sudo().action_cancel()
                
                transit.state = 'cancel'
                transit.date_done = fields.Datetime.now()
                
            except Exception as e:
                errors.append(f"'{transit.name}': {str(e)}")
        
        if errors:
            raise UserError('Cancel errors:\n• ' + '\n• '.join(errors))
        
        return True

    @api.model_create_multi
    def create(self, vals_list):
        # Set defaults
        for vals in vals_list:
            if 'company_id' not in vals:
                vals['company_id'] = self.env.company.id
            if 'scheduled_date' not in vals or vals['scheduled_date'] == False:
                vals['scheduled_date'] = fields.Datetime.now()
        
        # Group by company
        records_by_company = {}
        for vals in vals_list:
            company_id = vals['company_id']
            if company_id not in records_by_company:
                records_by_company[company_id] = []
            records_by_company[company_id].append(vals)
        
        # Pre-collect all unique company IDs
        all_company_ids = set()
        for vals in vals_list:
            if vals.get('src_company_id'):
                all_company_ids.add(vals['src_company_id'])
            if vals.get('dest_company_id'):
                all_company_ids.add(vals['dest_company_id'])
        
        # Batch browse all companies
        companies = self.env['res.company'].browse(list(all_company_ids))
        company_map = {c.id: c for c in companies}
        
        # Generate names per company
        for company_id, company_vals_list in records_by_company.items():
            company = self.env['res.company'].browse(company_id)
            parent_name = company.name
            
            for vals in company_vals_list:
                src_company_id = vals.get('src_company_id')
                dest_company_id = vals.get('dest_company_id')
                
                if not all([company_id, src_company_id, dest_company_id]):
                    continue
                
                # Get sequence
                sequence = self.env['ir.sequence'].with_company(company_id).next_by_code(
                    't4tek.transit.order'
                )
                
                if not sequence:
                    continue
                
                parts = sequence.split('/')
                if len(parts) < 3:
                    continue
                
                sequence_number = parts[-1]
                
                # Get company names from map
                src_company = company_map.get(src_company_id)
                dest_company = company_map.get(dest_company_id)
                
                if not src_company or not dest_company:
                    continue
                
                start_name = src_company.name.replace(' ', '_')
                end_name = dest_company.name.replace(' ', '_')
                
                # Build full name
                new_name = f"{parent_name}/TRANSIT/{start_name}.{end_name}/{sequence_number}"
                vals['name'] = new_name
        
        return super().create(vals_list)

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
        
        # Collect transit picking pairs and their related stock.pickings BEFORE deleting
        transit_pickings = valid_transits.mapped('transit_picking_ids').filtered(lambda tp: tp.exists())
        stock_pickings = (
            transit_pickings.mapped('src_picking_id') | 
            transit_pickings.mapped('dest_picking_id')
        ).filtered(lambda p: p.exists())

        # Delete transits
        try:
            result = super(T4tekTransitOrder, valid_transits).unlink()
        except Exception as e:
            _logger.error("Failed to delete transits: %s", str(e), exc_info=True)
            errors.append(f"Failed to delete transits: {str(e)}")
            raise UserError('Delete errors:\n• ' + '\n• '.join(errors))
        
        # Delete transit picking pairs (mapping records)
        if transit_pickings:
            try:
                transit_pickings.sudo().unlink()
                _logger.info(f"Deleted {len(transit_pickings)} transit picking pairs")
            except Exception as e:
                _logger.error("Failed to delete transit pickings: %s", str(e), exc_info=True)

        # Delete the actual stock.picking records
        if stock_pickings:
            try:
                stock_pickings.sudo().unlink()
                _logger.info(f"Deleted {len(stock_pickings)} stock pickings")
            except Exception as e:
                _logger.error("Failed to delete stock pickings: %s", str(e), exc_info=True)
                errors.append(f"Failed to delete stock pickings: {str(e)}")
        
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