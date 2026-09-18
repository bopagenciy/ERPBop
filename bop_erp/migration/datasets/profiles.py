# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class SourceDatasetProfile:
	"""
	Generic, declarative configuration profile for a source dataset.
	Enables tabular datasets (CSV/XLSX) from Prophet 21, SAP, NetSuite, Dynamics,
	or generic formats to be parsed, staged, and validated deterministically.
	"""

	profile_id: str
	source_system: str
	dataset_name: str
	entity_type: str
	file_type: str = "any"  # "xlsx", "csv", "any"
	sheet_name: Optional[str] = None
	header_row: int = 1
	metadata_rows: List[int] = field(default_factory=list)
	data_start_row: int = 2
	example_rows_to_ignore: List[int] = field(default_factory=list)
	key_fields: List[str] = field(default_factory=list)
	required_fields: List[str] = field(default_factory=list)
	optional_fields: List[str] = field(default_factory=list)
	field_types: Dict[str, str] = field(default_factory=dict)
	field_mappings: Dict[str, str] = field(default_factory=dict)
	dependency_profiles: List[str] = field(default_factory=list)
	import_order: int = 100
	allow_extra_columns: bool = True
	active: bool = True
	version: str = "1.0.0"
	description: str = ""
	validation_rules: List[Callable[[Dict[str, Any]], List[str]]] = field(default_factory=list)

	def to_dict(self) -> Dict[str, Any]:
		"""Returns a serializable dictionary representation."""
		return {
			"profile_id": self.profile_id,
			"source_system": self.source_system,
			"dataset_name": self.dataset_name,
			"entity_type": self.entity_type,
			"file_type": self.file_type,
			"sheet_name": self.sheet_name,
			"header_row": self.header_row,
			"metadata_rows": list(self.metadata_rows),
			"data_start_row": self.data_start_row,
			"example_rows_to_ignore": list(self.example_rows_to_ignore),
			"key_fields": list(self.key_fields),
			"required_fields": list(self.required_fields),
			"optional_fields": list(self.optional_fields),
			"field_types": dict(self.field_types),
			"field_mappings": dict(self.field_mappings),
			"dependency_profiles": list(self.dependency_profiles),
			"import_order": self.import_order,
			"allow_extra_columns": self.allow_extra_columns,
			"active": self.active,
			"version": self.version,
			"description": self.description,
		}


def get_initial_p21_profiles() -> List[SourceDatasetProfile]:
	"""
	Initial six Prophet 21 dataset profiles, derived strictly from local client
	sample workbooks. These are client-specific profile versions, NOT globally
	authoritative P21 schemas.
	"""
	return [
		SourceDatasetProfile(
			profile_id="P21_ITEM_MASTER",
			source_system="PROPHET_21",
			dataset_name="ItemMaster",
			entity_type="ITEM_MASTER",
			file_type="xlsx",
			sheet_name="Sheet1",
			header_row=1,
			metadata_rows=[2, 3, 4],
			data_start_row=5,  # Evidence from sample: Row 5 is real data (AB28400), no example row
			example_rows_to_ignore=[],
			key_fields=["Item ID"],
			required_fields=["Item ID", "Item Description"],
			field_mappings={
				"Item ID": "item_code",
				"Item Description": "item_name",
				"UPC ID": "upc_code",
				"Weight": "weight",
				"Net Weight": "net_weight",
				"Default Product Group": "item_group",
				"Default Selling Unit": "sales_uom",
				"Default Purchasing Unit": "purchase_uom",
				"Base Unit": "stock_uom",
				"Serialized": "has_serial_no",
				"Track Lots": "has_batch_no",
				"Hazardous Material": "hazardous_material",
				"Item Type": "source_item_type",
			},
			dependency_profiles=[],
			import_order=10,
			allow_extra_columns=True,
			active=True,
			version="1.0.0-client-sample",
			description="Prophet 21 Item Master export profile.",
		),
		SourceDatasetProfile(
			profile_id="P21_INVENTORY_LOCATION",
			source_system="PROPHET_21",
			dataset_name="InventoryLocation",
			entity_type="INVENTORY_LOCATION",
			file_type="xlsx",
			sheet_name="Sheet1",
			header_row=1,
			metadata_rows=[2, 3, 4],
			data_start_row=6,  # Row 5 is example (TS/MILES)
			example_rows_to_ignore=[5],
			key_fields=["Item ID", "Company ID", "Location ID"],
			required_fields=["Item ID", "Company ID", "Location ID"],
			field_mappings={
				"Item ID": "item_code",
				"Company ID": "company_id",
				"Location ID": "location_id",
				"Quantity On Hand": "qty_on_hand",
				"Quantity in Process": "qty_in_process",
				"Quantity Allocated": "qty_allocated",
				"Quantity Backordered": "qty_backordered",
				"Quantity In Transit": "qty_in_transit",
				"Safety Stock": "safety_stock",
				"Inventory Minimum": "min_qty",
				"Inventory Maximum": "max_qty",
				"Moving Average Cost": "moving_avg_cost",
				"Standard Cost": "standard_cost",
				"Primary Bin": "primary_bin",
				"Sellable": "is_sellable",
				"Stockable": "is_stockable",
				"Track Bins": "track_bins",
				"Buy": "is_buy",
				"Make": "is_make",
				"Discontinued": "is_discontinued",
			},
			dependency_profiles=["P21_ITEM_MASTER"],
			import_order=20,
			allow_extra_columns=True,
			active=True,
			version="1.0.0-client-sample",
			description="Prophet 21 Inventory Location quantities and configuration profile.",
		),
		SourceDatasetProfile(
			profile_id="P21_INVENTORY_SUPPLIER",
			source_system="PROPHET_21",
			dataset_name="InventorySupplier",
			entity_type="INVENTORY_SUPPLIER",
			file_type="xlsx",
			sheet_name="Sheet1",
			header_row=1,
			metadata_rows=[2, 3, 4],
			data_start_row=6,  # Row 5 is example (TS/102144)
			example_rows_to_ignore=[5],
			key_fields=["Item ID", "Supplier ID"],
			required_fields=["Item ID", "Supplier ID"],
			field_mappings={
				"Item ID": "item_code",
				"Supplier ID": "supplier_id",
				"Supplier Name": "supplier_name",
				"Division ID": "division_id",
				"Division Name": "division_name",
				"UPC Code": "supplier_upc",
				"Supplier Part No": "supplier_part_no",
				"List Price": "list_price",
				"Cost": "cost",
			},
			dependency_profiles=["P21_ITEM_MASTER"],
			import_order=30,
			allow_extra_columns=True,
			active=True,
			version="1.0.0-client-sample",
			description="Prophet 21 general Item-to-Supplier relationship profile.",
		),
		SourceDatasetProfile(
			profile_id="P21_ITEM_UOM",
			source_system="PROPHET_21",
			dataset_name="ItemUnitofMeasure",
			entity_type="ITEM_UOM",
			file_type="xlsx",
			sheet_name="Sheet1",
			header_row=1,
			metadata_rows=[2, 3, 4],
			data_start_row=6,  # Row 5 is example (TS/EACH)
			example_rows_to_ignore=[5],
			key_fields=["Item ID", "Unit of Measure"],
			required_fields=["Item ID", "Unit of Measure", "Unit Size"],
			field_mappings={
				"Item ID": "item_code",
				"Unit of Measure": "uom",
				"Unit Size": "conversion_factor",
				"Selling Unit": "is_selling",
				"Purchasing Unit": "is_purchasing",
			},
			dependency_profiles=["P21_ITEM_MASTER"],
			import_order=40,
			allow_extra_columns=True,
			active=True,
			version="1.0.0-client-sample",
			description="Prophet 21 Item Unit of Measure conversions profile.",
		),
		SourceDatasetProfile(
			profile_id="P21_ITEM_DESCRIPTION",
			source_system="PROPHET_21",
			dataset_name="ItemDescription",
			entity_type="ITEM_DESCRIPTION",
			file_type="xlsx",
			sheet_name="Sheet1",
			header_row=1,
			metadata_rows=[2, 3, 4],
			data_start_row=6,  # Row 5 is example (TS/Product Name)
			example_rows_to_ignore=[5],
			key_fields=["Item ID"],
			required_fields=["Item ID", "Extended Description"],
			field_mappings={
				"Item ID": "item_code",
				"Extended Description": "extended_description",
			},
			dependency_profiles=["P21_ITEM_MASTER"],
			import_order=50,
			allow_extra_columns=True,
			active=True,
			version="1.0.0-client-sample",
			description="Prophet 21 Extended Item Description profile.",
		),
		SourceDatasetProfile(
			profile_id="P21_ITEM_SUPPLIER_BY_LOCATION",
			source_system="PROPHET_21",
			dataset_name="ItemSupplierByLocation",
			entity_type="ITEM_SUPPLIER_BY_LOCATION",
			file_type="xlsx",
			sheet_name="Sheet1",
			header_row=1,
			metadata_rows=[2, 3, 4],
			data_start_row=6,  # Row 5 is example (DRILLBIT/Set of 6 bits)
			example_rows_to_ignore=[5],
			key_fields=["Item ID", "Location ID", "Supplier ID"],
			required_fields=["Item ID", "Location ID", "Supplier ID"],
			field_mappings={
				"Item ID": "item_code",
				"Item Description": "item_description",
				"Location ID": "location_id",
				"Location Name": "location_name",
				"Supplier ID": "supplier_id",
				"Supplier Name": "supplier_name",
				"Division ID": "division_id",
				"Primary Supplier": "is_primary",
				"Average Lead Time": "lead_time_days",
				"Location Cost": "location_cost",
				"Location List Price": "location_list_price",
			},
			dependency_profiles=["P21_ITEM_MASTER", "P21_INVENTORY_LOCATION", "P21_INVENTORY_SUPPLIER"],
			import_order=60,
			allow_extra_columns=True,
			active=True,
			version="1.0.0-client-sample",
			description="Prophet 21 location-specific supplier, cost, and lead-time override profile.",
		),
	]
