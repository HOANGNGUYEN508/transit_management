import { Component } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { _t } from "@web/core/l10n/translation";

export class T4tekTransitLotDialog extends Component {
	static template = "t4tek_transit.TransitLotDialog";
	static components = { Dialog };
	static props = {
		// Product display name shown in dialog title
		productName: { type: String },
		// Array of { lot: string, qty: number }
		srcLines: { type: Array },
		// Array of { lot: string, qty: number }
		destLines: { type: Array },
		// Whether lot_mismatch is in the status array
		hasLotMismatch: { type: Boolean, optional: true },
		// Whether qty_mismatch is in the status array
		hasQtyMismatch: { type: Boolean, optional: true },
		// Injected by dialogService
		close: { type: Function },
	};

	static defaultProps = {
		hasLotMismatch: false,
		hasQtyMismatch: false,
	};

	// ── Helpers ───────────────────────────────────────────────────────────────

	get title() {
		return _t("%s — Lot / Serial Detail", this.props.productName);
	}

	get srcLots() {
		return this.props.srcLines || [];
	}

	get destLots() {
		return this.props.destLines || [];
	}

	get maxRows() {
		return Math.max(this.srcLots.length, this.destLots.length);
	}

	/**
	 * Zip src and dest rows together so the table renders row-by-row.
	 * Fills missing side with null.
	 */
	get zippedRows() {
		const rows = [];
		for (let i = 0; i < this.maxRows; i++) {
			rows.push({
				src: this.srcLots[i] || null,
				dest: this.destLots[i] || null,
			});
		}
		return rows;
	}

	/**
	 * A dest lot is mismatched if it doesn't appear on the src side.
	 */
	isDestLotMismatched(destLot) {
		if (!this.props.hasLotMismatch || !destLot?.lot) return false;
		return !this.srcLots.some((s) => s.lot === destLot.lot);
	}

	isSrcLotMismatched(srcLot) {
		if (!this.props.hasLotMismatch || !srcLot?.lot) return false;
		return !this.destLots.some((d) => d.lot === srcLot.lot);
	}

	formatQty(qty) {
		if (qty === null || qty === undefined) return "—";
		return String(Math.round(qty * 10000) / 10000);
	}
}
