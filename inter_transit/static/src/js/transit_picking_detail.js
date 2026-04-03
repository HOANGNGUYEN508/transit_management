import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component } from "@odoo/owl";
import { TransitLotDialog } from "./transit_picking_dialog";

export class TransitPickingDetail extends Component {
	static template = "inter_transit.TransitPickingDetail";
	static components = { TransitLotDialog };
	static props = {
		id: { type: String, optional: true },
		name: { type: String },
		readonly: { type: Boolean, optional: true },
		record: { type: Object },
		className: { type: String, optional: true },
	};

	setup() {
		this.dialogService = useService("dialog");
	}

	// ── Data ──────────────────────────────────────────────────────────────────

	get comparisonData() {
		return (
			this.props.record.data.comparison_data || { lines: [], dest_state: "" }
		);
	}

	get lines() {
		return this.comparisonData.lines || [];
	}

	get destState() {
		return this.comparisonData.dest_state || "";
	}

	get isEmpty() {
		return this.lines.length === 0;
	}

	get mismatchLines() {
		return this.lines.filter((line) =>
			(line.status || []).some((s) => !["ok", "pending"].includes(s)),
		);
	}

	get hasMismatches() {
		return this.mismatchLines.length > 0;
	}

	// ── Dialog ────────────────────────────────────────────────────────────────

	openLotDialog(line) {
		this.dialogService.add(TransitLotDialog, {
			productName: line.product_name,
			srcLines: line.src_lines || [],
			destLines: line.dest_lines || [],
			hasLotMismatch: (line.status || []).includes("lot_mismatch"),
			hasQtyMismatch: (line.status || []).includes("qty_mismatch"),
			close: () => {},
		});
	}

	hasLotData(line) {
		return (
			(line.src_lines && line.src_lines.length > 0) ||
			(line.dest_lines && line.dest_lines.length > 0)
		);
	}

	// ── Status helpers ────────────────────────────────────────────────────────

	primaryStatus(statuses) {
		if (!statuses || statuses.length === 0) return "pending";
		const priority = [
			"missing",
			"qty_mismatch",
			"lot_mismatch",
			"src_only",
			"dest_only",
			"pending",
			"ok",
		];
		for (const p of priority) {
			if (statuses.includes(p)) return p;
		}
		return statuses[0];
	}

	statusLabel(status) {
		const labels = {
			ok: _t("OK"),
			pending: _t("Pending"),
			qty_mismatch: _t("Quantity"),
			lot_mismatch: _t("Lot / SN"),
			missing: _t("Missing"),
			src_only: _t("Source Only"),
			dest_only: _t("Dest Only"),
		};
		return labels[status] || status;
	}

	statusBadgeClass(status) {
		const map = {
			ok: "badge text-bg-success",
			pending: "badge text-bg-secondary",
			qty_mismatch: "badge badge-qty-issue", // 🟠 orange
			lot_mismatch: "badge text-bg-danger", // 🔴 red
			missing: "badge text-bg-warning text-dark", // 🟡 yellow
			src_only: "badge text-bg-warning text-dark", // 🟡 yellow
			dest_only: "badge text-bg-warning text-dark", // 🟡 yellow
		};
		return map[status] || "badge text-bg-secondary";
	}

	/**
	 * Returns an array of icon descriptors for a status badge.
	 * Each entry: { cls: "fa fa-...", small: bool }
	 *
	 * Most statuses have one icon. src_only and dest_only use two icons
	 * (direction arrow + question mark) rendered side-by-side inside the badge.
	 */
	statusBadgeIcons(status) {
		const map = {
			ok: [{ cls: "fa fa-check" }],
			pending: [{ cls: "fa fa-clock-o" }],
			qty_mismatch: [{ cls: "fa fa-balance-scale" }],
			lot_mismatch: [{ cls: "fa fa-barcode" }],
			missing: [{ cls: "fa fa-times" }],
			src_only: [
				{ cls: "fa fa-sign-in" },
				{ cls: "fa fa-question", small: true },
			],
			dest_only: [
				{ cls: "fa fa-sign-out" },
				{ cls: "fa fa-question", small: true },
			],
		};
		return map[status] || [{ cls: "fa fa-question" }];
	}

	rowClass(line) {
		const s = this.primaryStatus(line.status);
		if (s === "lot_mismatch") return "table-danger"; // 🔴
		if (s === "qty_mismatch") return "table-orange"; // 🟠
		if (["missing", "src_only", "dest_only"].includes(s))
			return "table-warning"; // 🟡
		if (s === "ok") return "table-success";
		return "";
	}

	// ── Quantity helpers ──────────────────────────────────────────────────────

	formatQty(qty, uom) {
		if (qty === null || qty === undefined) return "\u2014";
		const n = Math.round(qty * 10000) / 10000;
		return uom ? `${n} ${uom}` : String(n);
	}

	srcQtyClass(line) {
		if (this.primaryStatus(line.status) === "qty_mismatch")
			return "text-danger fw-semibold";
		return "";
	}

	destQtyClass(line) {
		const s = this.primaryStatus(line.status);
		if (s === "qty_mismatch") return "text-danger fw-semibold";
		if (s === "ok") return "text-success";
		return "text-muted";
	}

	// ── Mismatch summary helpers ──────────────────────────────────────────────

	mismatchSummaryClass(status) {
		if (status === "lot_mismatch") return "text-danger"; // 🔴
		if (status === "qty_mismatch") return "text-orange"; // 🟠
		if (["missing", "src_only", "dest_only"].includes(status))
			return "text-warning"; // 🟡
		return "";
	}

	mismatchIcon(status) {
		if (status === "missing") return "fa fa-times-circle";
		if (status === "qty_mismatch") return "fa fa-balance-scale";
		if (status === "lot_mismatch") return "fa fa-barcode";
		return "fa fa-question-circle";
	}

	/**
	 * Per-lot detail objects for the mismatch summary.
	 *
	 * qty_diff  → { type, lot, srcQty, destQty, icon, cls }
	 * src_only  → { type, lot, qty, icon, cls }   lot=null for untracked products
	 * dest_only → { type, lot, qty, icon, cls }
	 * missing   → { type, text, icon, cls }
	 */
	mismatchDetails(line) {
		const details = [];
		const statuses = line.status || [];
		const srcLines = line.src_lines || [];
		const destLines = line.dest_lines || [];
		const fmtQty = (q, uom) => this.formatQty(q, uom);

		// ── Lot-level mismatches ───────────────────────────────────────────────
		if (
			statuses.includes("lot_mismatch") ||
			statuses.includes("qty_mismatch")
		) {
			const srcMap = Object.fromEntries(srcLines.map((l) => [l.lot, l]));
			const destMap = Object.fromEntries(destLines.map((l) => [l.lot, l]));
			const allLots = [
				...new Set([...Object.keys(srcMap), ...Object.keys(destMap)]),
			];

			for (const lot of allLots) {
				const src = srcMap[lot];
				const dest = destMap[lot];

				if (src && dest) {
					// Same lot both sides — check per-lot qty
					const srcQty = Math.round((src.qty || 0) * 10000) / 10000;
					const destQty = Math.round((dest.qty || 0) * 10000) / 10000;
					if (srcQty !== destQty) {
						details.push({
							type: "qty_diff",
							lot,
							srcQty: fmtQty(src.qty, line.src_uom),
							destQty: fmtQty(dest.qty, line.dest_uom),
							icon: "fa fa-balance-scale",
							cls: "text-orange",
						});
					}
				} else if (src && !dest) {
					details.push({
						type: "src_only",
						lot,
						qty: fmtQty(src.qty, line.src_uom),
						icon: "fa fa-sign-in",
						cls: "text-warning",
					});
				} else if (!src && dest) {
					details.push({
						type: "dest_only",
						lot,
						qty: fmtQty(dest.qty, line.dest_uom),
						icon: "fa fa-sign-out",
						cls: "text-warning",
					});
				}
			}
		}

		// ── Product-level (no lot tracking) ───────────────────────────────────
		if (statuses.includes("src_only") && srcLines.length === 0) {
			details.push({
				type: "src_only",
				lot: null,
				qty: fmtQty(line.src_qty, line.src_uom),
				icon: "fa fa-sign-in",
				cls: "text-warning",
			});
		}
		if (statuses.includes("dest_only") && destLines.length === 0) {
			details.push({
				type: "dest_only",
				lot: null,
				qty: fmtQty(line.dest_qty, line.dest_uom),
				icon: "fa fa-sign-out",
				cls: "text-warning",
			});
		}
		if (statuses.includes("missing")) {
			details.push({
				type: "missing",
				text: _t("Product not found in destination picking"),
				icon: "fa fa-times-circle",
				cls: "text-warning",
			});
		}

		return details;
	}
}

export const TransitPickingDetailField = {
	component: TransitPickingDetail,
	isEmpty: (record, fieldName) => {
		const data = record.data[fieldName];
		return !data || !data.lines || data.lines.length === 0;
	},
	extractProps: ({ attrs, options }) => ({
		readonly: attrs.readonly === "1",
		className: attrs.class,
	}),
};

registry
	.category("fields")
	.add("transit_picking_detail", TransitPickingDetailField);
