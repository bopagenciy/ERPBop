# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
import bop_erp
from bop_erp import hooks

class TestFoundation(unittest.TestCase):
	def test_version_exists(self):
		self.assertTrue(hasattr(bop_erp, "__version__"))
		self.assertEqual(bop_erp.__version__, "0.0.1")

	def test_hooks_metadata(self):
		self.assertEqual(hooks.app_name, "bop_erp")
		self.assertEqual(hooks.app_title, "Bop ERP")
		self.assertEqual(hooks.app_publisher, "Bop Agency")
