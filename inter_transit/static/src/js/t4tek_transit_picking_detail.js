import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component } from "@odoo/owl";
import { T4tekTransitLotDialog } from "./t4tek_transit_picking_dialog";

export class T4tekTransitPickingDetail extends Component {
	static template = "t4tek_transit.TransitPickingDetail";
	static components = { T4tekTransitLotDialog };
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
		this.dialogService.add(T4tekTransitLotDialog, {
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

	statusBadgeIcon(status) {
		const map = {
			ok: "fa fa-check",
			pending: "fa fa-clock-o",
			qty_mismatch: "fa fa-exclamation-triangle",
			lot_mismatch: "fa fa-tag",
			missing: "fa fa-times",
			src_only: "fa fa-arrow-right",
			dest_only: "fa fa-arrow-left",
		};
		return map[status] || "fa fa-question";
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
		if (status === "qty_mismatch") return "fa fa-balance-scale"; // distinct from lot
		if (status === "lot_mismatch") return "fa fa-exclamation-triangle";
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
							cls: "text-orange", // was "text-danger"
						});
					}
				} else if (src && !dest) {
					details.push({
						type: "src_only",
						lot,
						qty: fmtQty(src.qty, line.src_uom),
						icon: "fa fa-arrow-right",
						cls: "text-warning", // unchanged, already correct
					});
				} else if (!src && dest) {
					details.push({
						type: "dest_only",
						lot,
						qty: fmtQty(dest.qty, line.dest_uom),
						icon: "fa fa-arrow-left",
						cls: "text-warning", // unchanged
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
				icon: "fa fa-arrow-right",
				cls: "text-warning",
			});
		}
		if (statuses.includes("dest_only") && destLines.length === 0) {
			details.push({
				type: "dest_only",
				lot: null,
				qty: fmtQty(line.dest_qty, line.dest_uom),
				icon: "fa fa-arrow-left",
				cls: "text-warning",
			});
		}
		if (statuses.includes("missing")) {
			details.push({
				type: "missing",
				text: _t("Product not found in destination picking"),
				icon: "fa fa-times-circle",
				cls: "text-warning", // was "text-danger" — product issue, not lot
			});
		}

		return details;
	}
}

export const T4tekTransitPickingDetailField = {
	component: T4tekTransitPickingDetail,
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
	.add("t4tek_transit_picking_detail", T4tekTransitPickingDetailField);
