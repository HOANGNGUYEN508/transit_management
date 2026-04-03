import { registry } from "@web/core/registry";
import { Component, markup } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { evaluateBooleanExpr } from "@web/core/py_js/py";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";

class TransitPickingDelegationButton extends Component {
	static template = "inter_transit.TransitPickingDelegationButton";
	static props = {
		record: { type: Object },
		readonly: { type: String, optional: true },
		invisible: { type: String, optional: true },
	};

	setup() {
		this.orm = useService("orm");
		this.dialog = useService("dialog");
		this.action = useService("action");
	}

	get isInvisible() {
		if (!this.props.invisible) return false;
		return evaluateBooleanExpr(
			this.props.invisible,
			this.props.record.evalContext,
		);
	}

	get isReadonly() {
		if (!this.props.readonly) return false;
		return evaluateBooleanExpr(
			this.props.readonly,
			this.props.record.evalContext,
		);
	}

	async onClick() {
		const record = this.props.record;

		// Persist wizard data before calling server
		if (record.isDirty) {
			const saved = await record.save();
			if (!saved) return;
		}

		// Step 1 — lightweight pre-check, get confirmation message
		let result;
		try {
			result = await this.orm.call(
				"transit.picking.delegation",
				"action_confirm",
				[[record.resId]],
			);
		} catch {
			// UserError surfaced automatically by Odoo's RPC error handler
			return;
		}

		// Step 2 — show JS confirmation dialog (no second popup window)
		this.dialog.add(ConfirmationDialog, {
			title: "Confirm Delegation",
			body: markup(result.message),
			confirmLabel: "Confirm",
			cancelLabel: "Cancel",
			confirm: async () => {
				try {
					await this.orm.call(
						"transit.picking.delegation",
						"action_execute",
						[[record.resId]],
						{ resolved_break_type: result.resolved_break_type },
					);
					// Close the wizard dialog
					await this.action.doAction({ type: "ir.actions.act_window_close" });
				} catch {
					// UserError surfaced automatically
				}
			},
			cancel: () => {},
		});
	}
}

export const TransitPickingDelegationButtonWidget = {
	component: TransitPickingDelegationButton,
	extractProps: ({ attrs }) => ({
		readonly: attrs.readonly,
		invisible: attrs.invisible,
	}),
};

registry
	.category("view_widgets")
	.add(
		"transit_picking_delegation_button",
		TransitPickingDelegationButtonWidget,
	);
