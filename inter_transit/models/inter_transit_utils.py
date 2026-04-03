TRANSIT_PAIRS = {
    "inter_transit.action_create_transit_warehouse":       "inter_transit.automation_create_transit_warehouse",
    "inter_transit.action_update_transit_on_name_change":  "inter_transit.automation_update_transit_on_name_change",
    "inter_transit.action_warehouse_created":              "inter_transit.automation_warehouse_created",
    "inter_transit.action_transit_src_picking_done":       "inter_transit.automation_transit_src_picking_done",
    "inter_transit.action_transit_src_backorder_created": "inter_transit.automation_transit_src_backorder_created",
    "inter_transit.action_transit_dest_backorder_created": "inter_transit.automation_transit_dest_backorder_created",
    "inter_transit.action_transit_dest_picking_done":      "inter_transit.automation_transit_dest_picking_done",
    "inter_transit.action_transit_picking_cancelled":      "inter_transit.automation_transit_picking_cancelled",
    "inter_transit.action_transit_picking_deleted":        "inter_transit.automation_transit_picking_deleted",
    "inter_transit.action_transit_dest_picking_assigned_guard": "inter_transit.automation_transit_dest_picking_assigned_guard",
    "inter_transit.action_transit_dest_picking_validate_guard": "inter_transit.automation_transit_dest_picking_validate_guard",
		"inter_transit.action_transit_dest_backorder_assigned_guard": "inter_transit.automation_transit_dest_backorder_assigned_guard",
}

TRANSIT_PAIRS_REVERSED = {v: k for k, v in TRANSIT_PAIRS.items()}