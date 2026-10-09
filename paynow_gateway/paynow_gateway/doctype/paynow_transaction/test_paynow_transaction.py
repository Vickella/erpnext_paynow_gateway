# Copyright (c) 2026, Munyaradzi Chirove and Contributors
# See license.txt

import hashlib
import inspect
import types
import unittest
from urllib.parse import urlencode
from unittest.mock import Mock, patch

import frappe
from paynow_gateway.paynow_gateway.api import (
	get_checkout_options,
	is_visa_checkout_enabled,
	normalize_paynow_status,
	paynow_webhook,
	process_paynow_card_payment,
	process_paynow_payment,
	verify_paynow_hash,
	verify_paynow_response,
)

class TestPaynowTransaction(unittest.TestCase):
	def setUp(self):
		frappe.local.db = Mock()
		frappe.local.flags = frappe._dict(in_test=True)
		frappe.local.session = frappe._dict(user="customer@example.com")
		self.integration_key = "3e9fed89-60e1-4ce5-ab6e-6b1eb2d4f977"
		self.fields = [
			("status", "Ok"),
			("browserurl", "https://staging.paynow.co.zw/Payment/ConfirmPayment/9510"),
			("pollurl", "https://staging.paynow.co.zw/Interface/CheckPayment/?guid=c7ed41da-0159-46da-b428-69549f770413"),
			("paynowreference", "9510"),
			("hash", "750DD0B0DF374678707BB5AF915AF81C228B9058AD57BB7120569EC68BBB9C2EFC1B26C6375D2BC562AC909B3CD6B2AF1D42E1A5E479FFAC8F4FB3FDCE71DF4D"),
		]

	def test_validates_paynow_official_hash_example(self):
		self.assertTrue(verify_paynow_hash(self.fields, self.integration_key))

	def test_rejects_modified_paynow_callback(self):
		modified_fields = list(self.fields)
		modified_fields[1] = ("browserurl", "https://attacker.example/fake-checkout")

		self.assertFalse(verify_paynow_hash(modified_fields, self.integration_key))

	def test_validates_sdk_response_dictionary(self):
		response = type("PaynowResponse", (), {"data": dict(self.fields)})()

		self.assertTrue(verify_paynow_response(response, self.integration_key))

	def test_hash_validation_normalizes_integration_key_case(self):
		self.assertTrue(verify_paynow_hash(self.fields, self.integration_key.upper()))

	def test_maps_paynow_delivery_status_to_valid_paid_state(self):
		self.assertEqual(normalize_paynow_status("Awaiting Delivery", "Initiated"), "Paid")

	def test_maps_mobile_prompt_and_failure_statuses(self):
		self.assertEqual(normalize_paynow_status("Sent", "Initiated"), "Pending")
		self.assertEqual(normalize_paynow_status("Cancelled", "Pending"), "Cancelled")

	@patch("paynow_gateway.paynow_gateway.api.frappe.get_single")
	@patch("paynow_gateway.paynow_gateway.api.get_payable_invoice")
	def test_visa_option_stays_disabled_until_merchant_setting_is_enabled(self, get_invoice, get_single):
		invoice = types.SimpleNamespace(currency="USD")
		settings = types.SimpleNamespace(
			enabled=1,
			usd_integration_id="12345",
			usd_integration_key=self.integration_key,
			usd_visa_checkout_enabled=0,
			zwg_visa_checkout_enabled=0,
		)
		get_invoice.return_value = (invoice, 42.50)
		get_single.return_value = settings

		options = inspect.unwrap(get_checkout_options)("SINV-TEST-1")

		self.assertTrue(options["mobile_money"])
		self.assertFalse(options["visa_card"])
		settings.usd_visa_checkout_enabled = 1
		self.assertTrue(is_visa_checkout_enabled(settings, "USD"))
		self.assertFalse(is_visa_checkout_enabled(settings, "ZWG"))

	def make_response(self, redirect_url=None):
		fields = [
			("status", "Ok"),
			("browserurl", redirect_url or "https://www.paynow.co.zw/Payment/ConfirmPayment/9510"),
			("pollurl", "https://www.paynow.co.zw/Interface/CheckPayment/?guid=test-guid"),
			("paynowreference", "9510"),
		]
		message = "".join(value for _, value in fields) + self.integration_key.lower()
		hash_value = hashlib.sha512(message.encode("utf-8")).hexdigest().upper()
		return types.SimpleNamespace(
			success=True,
			data=dict(fields + [("hash", hash_value)]),
			redirect_url=fields[1][1],
			poll_url=fields[2][1],
		)

	def payment_mocks(self, response, visa_enabled=True):
		invoice = types.SimpleNamespace(name="SINV-TEST-1", currency="USD", db_set=Mock())
		settings = types.SimpleNamespace(
			return_url="https://shop.example/orders2",
			result_url="https://shop.example/api/method/paynow_gateway.paynow_gateway.api.paynow_webhook",
			email="merchant@example.com",
			usd_visa_checkout_enabled=visa_enabled,
			zwg_visa_checkout_enabled=False,
		)
		paynow = Mock()
		paynow.create_payment.return_value = Mock()
		paynow.create_payment.return_value.add.return_value = None
		paynow.send.return_value = response
		paynow.send_mobile.return_value = response
		return invoice, settings, paynow

	def callback_body(self, signature_valid=True):
		fields = [
			("reference", "TXN-TEST-1"),
			("amount", "42.50"),
			("status", "Paid"),
			("paynowreference", "9510"),
			("pollurl", "https://www.paynow.co.zw/Interface/CheckPayment/?guid=test-guid"),
		]
		message = "".join(value for _, value in fields) + self.integration_key.lower()
		hash_value = hashlib.sha512(message.encode("utf-8")).hexdigest().upper()
		if not signature_valid:
			hash_value = "0" * 128
		return urlencode(fields + [("hash", hash_value)])

	@patch("paynow_gateway.paynow_gateway.api.process_paynow_webhook")
	@patch("paynow_gateway.paynow_gateway.api.frappe.set_user")
	@patch("paynow_gateway.paynow_gateway.api.frappe.get_single")
	def test_valid_signed_callback_sets_admin_and_processes_payment(self, get_single, set_user, process_webhook):
		frappe.local.request = types.SimpleNamespace(get_data=lambda as_text: self.callback_body(), form={})
		frappe.db.get_value.return_value = frappe._dict(name="PAYNOW-TXN-1", currency="USD")
		get_single.return_value = types.SimpleNamespace(
			usd_integration_id="12345",
			usd_integration_key=self.integration_key,
			zwg_integration_id=None,
			zwg_integration_key=None,
		)

		result = inspect.unwrap(paynow_webhook)()

		self.assertEqual(result, "ok")
		set_user.assert_called_once_with("Administrator")
		process_webhook.assert_called_once()

	@patch("paynow_gateway.paynow_gateway.api.process_paynow_webhook")
	@patch("paynow_gateway.paynow_gateway.api.frappe.set_user")
	@patch("paynow_gateway.paynow_gateway.api.frappe.get_single")
	@patch("paynow_gateway.paynow_gateway.api.frappe.log_error")
	@patch("paynow_gateway.paynow_gateway.api.frappe.throw", side_effect=PermissionError("invalid signature"))
	@patch("paynow_gateway.paynow_gateway.api._", side_effect=lambda message: message)
	def test_invalid_callback_signature_is_rejected_before_admin_context(
		self, translate, throw_error, log_error, get_single, set_user, process_webhook
	):
		frappe.local.request = types.SimpleNamespace(get_data=lambda as_text: self.callback_body(False), form={})
		frappe.db.get_value.return_value = frappe._dict(name="PAYNOW-TXN-1", currency="USD")
		get_single.return_value = types.SimpleNamespace(
			usd_integration_id="12345",
			usd_integration_key=self.integration_key,
			zwg_integration_id=None,
			zwg_integration_key=None,
		)

		with self.assertRaises(PermissionError):
			inspect.unwrap(paynow_webhook)()

		set_user.assert_not_called()
		process_webhook.assert_not_called()

	@patch("paynow_gateway.paynow_gateway.api.save_paynow_transaction")
	@patch("paynow_gateway.paynow_gateway.api.get_enabled_paynow_settings")
	@patch("paynow_gateway.paynow_gateway.api.get_payable_invoice")
	@patch("paynow_gateway.paynow_gateway.api.Paynow")
	@patch("paynow_gateway.paynow_gateway.api.get_paynow_customer_email", return_value="customer@example.com")
	@patch("paynow_gateway.paynow_gateway.api._", side_effect=lambda message: message)
	def test_mobile_choice_uses_mobile_prompt_and_outstanding_amount(
		self, translate, customer_email, paynow_class, get_invoice, get_settings, save_transaction
	):
		response = self.make_response()
		invoice, settings, paynow = self.payment_mocks(response)
		paynow_class.return_value = paynow
		get_invoice.return_value = (invoice, 42.50)
		get_settings.return_value = (settings, "12345", self.integration_key)

		result = inspect.unwrap(process_paynow_payment)("SINV-TEST-1", "0771234567", amount=9999)

		self.assertEqual(result["status"], "success")
		paynow.send_mobile.assert_called_once()
		paynow.send.assert_not_called()
		paynow.create_payment.return_value.add.assert_called_once_with("Payment for Invoice SINV-TEST-1", 42.50)
		save_transaction.assert_called_once()

	@patch("paynow_gateway.paynow_gateway.api.save_paynow_transaction")
	@patch("paynow_gateway.paynow_gateway.api.get_enabled_paynow_settings")
	@patch("paynow_gateway.paynow_gateway.api.get_payable_invoice")
	@patch("paynow_gateway.paynow_gateway.api.Paynow")
	@patch("paynow_gateway.paynow_gateway.api.get_paynow_customer_email", return_value="customer@example.com")
	def test_visa_choice_uses_hosted_checkout_and_outstanding_amount(
		self, customer_email, paynow_class, get_invoice, get_settings, save_transaction
	):
		response = self.make_response()
		invoice, settings, paynow = self.payment_mocks(response)
		paynow_class.return_value = paynow
		get_invoice.return_value = (invoice, 42.50)
		get_settings.return_value = (settings, "12345", self.integration_key)

		result = inspect.unwrap(process_paynow_card_payment)("SINV-TEST-1")

		self.assertEqual(result["status"], "success")
		self.assertEqual(result["redirect_url"], response.redirect_url)
		paynow.send.assert_called_once()
		paynow.send_mobile.assert_not_called()
		paynow.create_payment.return_value.add.assert_called_once_with("Payment for Invoice SINV-TEST-1", 42.50)
		save_transaction.assert_called_once()


if __name__ == "__main__":
	unittest.main()
