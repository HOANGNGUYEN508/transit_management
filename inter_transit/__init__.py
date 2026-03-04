from . import models
import logging

_logger = logging.getLogger(__name__)

def _hook_inter_transit(env):
    """Post-init hook to create transit locations and picking types for inter-company transfers."""
    
    _logger.info("[post_init_hook] Starting inter-transit setup...")
    
    # 1. Create transit warehouse and sequences for all parent companies
    parent_companies = env['res.company'].search([('child_ids', '!=', False)])
    
    for company in parent_companies:
        try:
            company.sudo()._create_transit_warehouse()
            _logger.info("[post_init_hook] Created transit warehouse for parent company: %s", company.name)
        except Exception as e:
            _logger.error(
                "[post_init_hook] Error creating transit warehouse for company %s: %s",
                company.name, str(e)
            )
    
    # 2. Setup warehouse-level transit picking types for all companies in inter-company structure
    # (both parents and children)
    all_companies = env['res.company'].search([])
    
    for company in all_companies:
        try:
            company.sudo()._create_warehouse_transit_picking_types()
            _logger.info("[post_init_hook] Created transit picking types for company: %s", company.name)
        except Exception as e:
            _logger.error(
                "[post_init_hook] Error creating transit picking types for company %s: %s",
                company.name, str(e)
            )
    
    _logger.info("[post_init_hook] Inter-transit setup completed")