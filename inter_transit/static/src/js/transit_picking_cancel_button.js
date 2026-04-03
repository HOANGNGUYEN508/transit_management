import { registry } from "@web/core/registry";
import { Component } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { evaluateBooleanExpr } from "@web/core/py_js/py";

class TransitPickingCancelButton extends Component {
	static template = "inter_transit.TransitPickingCancelButton";
	static props = {
		record: { type: Object },
		readonly: { type: String, optional: true },
		invisible: { type: String, optional: true },
	};

	setup() {
		this.orm = useService("orm");
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

	async onClickMyButton() {
		const record = this.props.record;
		await this.orm.call(record.resModel, "action_cancel", [[record.resId]]);
		await record.model.root.load();
		record.model.notify();
	}
}

export const TransitPickingCancelButtonViewWidget = {
	component: TransitPickingCancelButton,
	extractProps: ({ attrs, options }) => ({
		readonly: attrs.readonly,
		invisible: attrs.invisible,
	}),
};

registry
	.category("view_widgets")
	.add(
		"transit_picking_cancel_button",
		TransitPickingCancelButtonViewWidget,
	);
