{
    "name": "Inter-Transit",
    "category": "Inventory",
    "summary": "Inter-Transit Management",
    "description":
    """
    - Add way for inter-transit between mother and child companies:
    + transit.order: Transit pickings.
    + transit.order.line: Transit moves.

    - Work flow:
    + Once user mark transit picking as to do -> create an OUT picking at the start point and an IN picking at the end point. 
    + Validate OUT picking with its move lines will propagate that move line to IN picking.
    """,
    "depends": [
        "base",
        "base_automation",
        "mail",
        "stock",
        "stock_account",
    ],
    "images": [
        "static/description/icon.png"
    ],
    "author": "Nguyen Cao Hoang",
    "data": [
        "views/t4tek_transit_order_views.xml",
        "views/t4tek_transit_order_line_views.xml",
        "views/t4tek_transit_picking_views.xml",
        "views/t4tek_transit_picking_type_views.xml",
        "views/product_product.xml",
        "data/res_groups.xml",
        "data/ir_rule.xml",
        "data/base_automation.xml",
        "data/ir_actions_server.xml",
        "security/ir.model.access.csv",
        "data/menu.xml",
    ],
    'assets': {
        'web.assets_backend': [
            'inter_transit/static/src/xml/t4tek_transit_picking_detail.xml',
            'inter_transit/static/src/js/t4tek_transit_picking_detail.js',
            'inter_transit/static/src/xml/t4tek_transit_picking_dialog.xml',
            'inter_transit/static/src/js/t4tek_transit_picking_dialog.js',
            'inter_transit/static/src/scss/t4tek_transit_picking.scss',
            # 'token_management/static/src/css/encode_qr_image.css',
        ],
    },
    "post_init_hook": "_hook_inter_transit",
    "installable": True,
    "application": True,
    "auto_install": False,
    "license": "LGPL-3",
} # type: ignore
