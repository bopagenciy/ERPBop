app_name = "bop_erp"
app_title = "Bop ERP"
app_publisher = "Bop Agency"
app_description = "Modular industrial distribution ERP extension designed to replace Epicor Prophet 21 workflows while keeping ERPNext and Frappe upstream intact."
app_email = "info@bop.agency"
app_license = "Proprietary"
app_version = "0.0.1"

# Fixtures
# --------
fixtures = [
	"Role",
	"Custom Field",
]

# Document Events
# ---------------
doc_events = {
	"Sales Order": {
		"validate": "bop_erp.attribution.validate_sales_order_attribution",
	},
	"Pick List": {
		"before_insert": "bop_erp.attribution.propagate_attribution_to_pick_list",
		"validate": [
			"bop_erp.attribution.validate_transaction_attribution",
			"bop_erp.orders.guard.validate_operational_guard",
		],
	},
	"Delivery Note": {
		"before_insert": "bop_erp.attribution.propagate_attribution_to_delivery_note",
		"validate": [
			"bop_erp.attribution.validate_transaction_attribution",
			"bop_erp.orders.guard.validate_operational_guard",
		],
		"on_submit": "bop_erp.orders.fulfillment_writeback.handle_delivery_note_submit",
		"on_cancel": "bop_erp.orders.fulfillment_writeback.handle_delivery_note_cancel",
	},
	"Shipment": {
		"before_insert": "bop_erp.attribution.propagate_attribution_to_shipment",
		"validate": [
			"bop_erp.attribution.validate_transaction_attribution",
			"bop_erp.orders.guard.validate_operational_guard",
		],
	},
	"Sales Invoice": {
		"before_insert": "bop_erp.attribution.propagate_attribution_to_sales_invoice",
		"validate": [
			"bop_erp.attribution.validate_transaction_attribution",
			"bop_erp.accounts.isolation.validate_company_accounting_isolation",
		],
	},
	"Payment Entry": {
		"before_insert": "bop_erp.attribution.propagate_attribution_to_payment_entry",
		"validate": [
			"bop_erp.attribution.validate_transaction_attribution",
			"bop_erp.accounts.isolation.validate_company_accounting_isolation",
		],
	},
	"Purchase Invoice": {
		"validate": [
			"bop_erp.accounts.isolation.validate_company_accounting_isolation",
		],
	},
}

# Scheduler Events
# ----------------
scheduler_events = {
	"cron": {
		"*/5 * * * *": [
			"bop_erp.inventory.scheduler.enqueue_inventory_publication_dispatcher",
			"bop_erp.orders.fulfillment_writeback.enqueue_order_fulfillment_writeback_dispatcher",
		],
	},
}
