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

    Matching Strategy (product-based):
    - Moves between src/dest pickings are matched by product_id.
    - No persistent link field on stock.move is required.
    - This means the transit is resilient to move deletion/recreation.
    - Limitation: if two lines in the same transit share the same product, only the
      first match is used (aggregate or split into separate transits instead).

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
        readonly=False,
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
        errors = []
        all_companies = self.env['res.company']
        transit_config_map = {}
        transit_parent_map = {}

        for transit in transits:
            ordering_company = transit.company_id
            start_company    = transit.src_company_id
            end_company      = transit.dest_company_id

            # ── 1. Authorisation check ────────────────────────────────────────
            is_valid, error_msg, parent_company = self._validate_transit_authorization(
                ordering_company, start_company, end_company
            )
            if not is_valid:
                errors.append(f"Transit '{transit.name}': {error_msg}")
                continue

            # ── 2. Resolve relation_type per company ──────────────────────────
            def _relation_type(company):
                return 'parent_to_child' if company == parent_company else 'child_to_parent'

            # ── 3. Look up picking-type configs ───────────────────────────────
            src_config = self.env['t4tek.transit.picking.type'].search([
                ('company_id', '=', start_company.id),
                ('relation_type', '=', _relation_type(start_company)),
            ], order='create_date ASC', limit=1)
            if not src_config:
                errors.append(
                    f"Transit '{transit.name}': No transit picking type configuration "
                    f"for source company '{start_company.name}' "
                    f"(relation: {_relation_type(start_company)})"
                )
                continue

            dest_config = self.env['t4tek.transit.picking.type'].search([
                ('company_id', '=', end_company.id),
                ('relation_type', '=', _relation_type(end_company)),
            ], order='create_date ASC', limit=1)
            if not dest_config:
                errors.append(
                    f"Transit '{transit.name}': No transit picking type configuration "
                    f"for destination company '{end_company.name}' "
                    f"(relation: {_relation_type(end_company)})"
                )
                continue

            # ── 4. Derive transit location from src config ────────────────────
            transit_location = src_config.src_picking_type_id.default_location_dest_id
            if not transit_location:
                errors.append(
                    f"Transit '{transit.name}': Source picking type has no destination location configured"
                )
                continue

            # ── 5. Persist resolved values ────────────────────────────────────
            transit.write({'transit_location_id': transit_location.id})

            all_companies |= start_company | end_company | parent_company
            transit_parent_map[transit.id] = parent_company
            transit_config_map[transit.id] = {
                'src_config':  src_config,
                'dest_config': dest_config,
            }

        if errors:
            raise UserError('Validation issues:\n• ' + '\n• '.join(errors))

        return all_companies, transit_config_map, transit_parent_map

    def _create_transfer_pickings(self, transits, transit_config_map, transit_parent_map):
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
        
        draft_transits = transits.filtered(lambda t: t.state == 'draft')
        
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
            raise UserError(f'Failed to batch create source pickings: {str(e)}')
        
        try:
            dest_pickings = self.env['stock.picking'].sudo().create(end_picking_vals_list)
        except Exception as e:
            raise UserError(f'Failed to batch create destination pickings: {str(e)}')
        
        if len(src_pickings) != len(dest_pickings) or len(src_pickings) != len(transit_data_list):
            raise UserError(
                f'Picking creation mismatch: '
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
                't4tek_transit_order_id': transit.id,
                'src_picking_id': src_picking.id,
                'dest_picking_id': dest_picking.id,
            })
        
        if errors:
            raise UserError('Picking errors:\n• ' + '\n• '.join(errors))
        
        try:
            transit_pickings = self.env['t4tek.transit.picking'].sudo().create(
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
                # No t4tek_transit_line_id — matching is done by product_id at runtime
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

        lines_to_delete = self.env['t4tek.transit.order.line']
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
                self.env['t4tek.transit.order.line'].with_context(
                    transit_pickings_sync=True
                ).browse(line_ids).write(dict(vals_key))

            _logger.info(
                "Merge: updated %d surviving transit line(s) in %d batch(es)",
                len(lines_to_update), len(buckets),
            )

        return summary

    def action_confirm(self):
        """
        Initiate transit process
        """
        if not self:
            return True
        
        draft = self.filtered(lambda t: t.state == 'draft')
        cancel = self.filtered(lambda t: t.state == 'cancel')
        
        invalid = self - draft - cancel
        if invalid:
            state_names = dict(self._fields['state'].selection)
            states = [state_names[t.state] for t in invalid[:5]]
            if len(invalid) > 5:
                states.append(f"and {len(invalid) - 5} more")
            raise UserError(f'Cannot confirm from states: {", ".join(states)}')
        
        to_process = cancel | draft
        
        if not to_process:
            return True
        
        empty = to_process.filtered(lambda t: not t.line_ids)
        if empty:
            names = empty[:10].mapped('name')
            if len(empty) > 10:
                names.append(f"and {len(empty) - 10} more")
            raise UserError(f'No moves defined for transits: {", ".join(names)}')

        # Merge duplicate-product lines before any picking/move work.
        # This guarantees product-based matching works cleanly (1 line per product).
        self._merge_duplicate_lines(to_process)
        
        all_companies, transit_config_map, transit_parent_map = self._validate_and_get_companies(to_process)
        
        with_pickings = to_process.filtered(lambda t: t.transit_picking_ids)
        without_pickings = to_process - with_pickings
        
        # ===== Handle transits WITH existing pickings =====
        if with_pickings:
            with_pickings.write({'state': 'assigned'})
            
            errors = []
            transit_picking_map = {}
            
            for transit in with_pickings:
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
                }
                
                has_moves = bool(src_picking.move_ids or dest_picking.move_ids)
                
                if has_moves:
                    try:
                        self._sync_lines_to_pickings(transit)
                        _logger.info(f"Transit '{transit.name}': Synced lines to existing moves")
                    except Exception as e:
                        errors.append(f"Transit '{transit.name}': Failed to sync lines: {str(e)}")
                else:
                    try:
                        self._create_moves_for_transit(transit, src_picking, dest_picking)
                        _logger.info(f"Transit '{transit.name}': Created moves from lines")
                    except Exception as e:
                        errors.append(f"Transit '{transit.name}': Failed to create moves: {str(e)}")
            
            if errors:
                raise UserError('Reconfirm errors:\n• ' + '\n• '.join(errors))
            
            src_pickings = self.env['stock.picking']
            for picking_info in transit_picking_map.values():
                if picking_info['start'].state == 'draft':
                    src_pickings |= picking_info['start']
            
            if src_pickings:
                self._batch_confirm_pickings(src_pickings)
        
        # ===== Handle transits WITHOUT existing pickings =====
        if without_pickings:
            transit_picking_map = self._create_transfer_pickings(
                without_pickings,
                transit_config_map,
                transit_parent_map
            )
            
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
            
            src_pickings = self.env['stock.picking']
            for picking_info in transit_picking_map.values():
                src_pickings |= picking_info['start'].sudo()
            
            if src_pickings:
                self._batch_confirm_pickings(src_pickings)
            
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
                    "Transit '%s': failed to sync scheduled_date to pickings: %s",
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
                    errors.append(f"Transit '{transit.name}': src picking update failed: {e}")
                    continue

                try:
                    dest_picking.write({
                        'company_id':      end_company.id,
                        'picking_type_id': dest_type.id,
                        'partner_id':      start_company.partner_id.id,
                    })
                    dest_picking.move_ids.sudo().write({'company_id': end_company.id})
                except Exception as e:
                    errors.append(f"Transit '{transit.name}': dest picking update failed: {e}")

        if errors:
            raise UserError('Company sync errors:\n• ' + '\n• '.join(errors))

    def action_cancel(self):
        """
        Cancel transit orders.
        """
        non_cancellable = self.filtered(lambda t: t.state in ('done', 'cancel', 'in_progress'))
        cancellable     = self - non_cancellable

        errors = [
            f"'{t.name}': Cannot cancel in state '{t.state}'"
            for t in non_cancellable
        ]

        if cancellable:
            all_pickings = self.env['stock.picking']
            for transit in cancellable:
                for tp in transit.transit_picking_ids:
                    all_pickings |= tp.src_picking_id | tp.dest_picking_id
            all_pickings = all_pickings.filtered(
                lambda p: p.state not in ('done', 'cancel')
            )

            try:
                if all_pickings:
                    all_pickings.sudo().action_cancel()
            except Exception as e:
                raise UserError(f'Failed to cancel pickings: {str(e)}')

            cancellable.write({
                'state': 'cancel',
                'date_done': fields.Datetime.now(),
            })

        if errors:
            raise UserError('Cancel errors:\n• ' + '\n• '.join(errors))

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
                    't4tek.transit.order'
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

    def write(self, vals):
        # ── 1. Identify which records will need post-write sync ───────────────
        company_fields   = {'src_company_id', 'dest_company_id'}
        company_changed  = bool(company_fields & vals.keys())
        date_changed     = 'scheduled_date' in vals
    
        # We only propagate to pickings for draft/assigned records that already
        # have picking pairs (draft records have no pickings yet, but guard anyway).
        syncable_states = ('draft', 'assigned')
    
        if company_changed:
            pre_company_sync = self.filtered(
                lambda t: t.state in syncable_states and bool(t.transit_picking_ids)
            )
        else:
            pre_company_sync = self.env['t4tek.transit.order']
    
        if date_changed:
            pre_date_sync = self.filtered(
                lambda t: t.state in syncable_states and bool(t.transit_picking_ids)
            )
        else:
            pre_date_sync = self.env['t4tek.transit.order']
    
        # ── 2. Core write (existing logic preserved) ──────────────────────────
        result = super().write(vals)
    
        # ── 3. Rename order reference when companies change (existing) ────────
        if company_changed:
            for record in self:
                if (
                    record.state not in ('in_progress', 'done')
                    and record.name
                    and '/' in record.name
                ):
                    parts = record.name.split('/')
                    if len(parts) >= 4:
                        sequence_number = parts[-1]
                        parent_name = record.company_id.name
                        start_name  = record.src_company_id.name.replace(' ', '_')
                        end_name    = record.dest_company_id.name.replace(' ', '_')
                        new_name    = (
                            f"{parent_name}/TRANSIT/{start_name}.{end_name}/{sequence_number}"
                        )
                        if record.name != new_name:
                            record.with_context(skip_name_check=True).write({'name': new_name})
    
        # Guard: cannot rename a terminal order (existing)
        for record in self:
            if (
                'name' in vals
                and record.state in ('in_progress', 'done')
                and not self.env.context.get('skip_name_check')
            ):
                raise UserError("Cannot change the reference when in progress or done.")
    
        # ── 4. Propagate company changes → pickings ───────────────────────────
        if pre_company_sync:
            pre_company_sync._sync_company_changes_to_pickings()
    
        # ── 5. Propagate date changes → pickings ─────────────────────────────
        if pre_date_sync:
            pre_date_sync._sync_scheduled_date_to_pickings()
    
        return result

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
            result = super(T4tekTransitOrder, valid_transits).unlink()
        except Exception as e:
            _logger.error("Failed to delete transits: %s", str(e), exc_info=True)
            errors.append(f"Failed to delete transits: {str(e)}")
            raise UserError('Delete errors:\n• ' + '\n• '.join(errors))
        
        if transit_pickings:
            try:
                transit_pickings.sudo().unlink()
                _logger.info(f"Deleted {len(transit_pickings)} transit picking pairs")
            except Exception as e:
                _logger.error("Failed to delete transit pickings: %s", str(e), exc_info=True)

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