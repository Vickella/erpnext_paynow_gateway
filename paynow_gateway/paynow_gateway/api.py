import hashlib
import hmac
import uuid
from urllib.parse import parse_qs, parse_qsl, urlparse

import frappe
from frappe import _
from frappe.utils import flt, now
from paynow import Paynow
from paynow_gateway.paynow_gateway.zoho_client import (
	extract_zoho_invoice_fields,
	record_zoho_invoice_payment,
)


def get_paynow_credentials(settings, currency):
    currency = (currency or "").strip().upper()

    if currency == "USD":
        integration_id = settings.usd_integration_id
        integration_key = settings.usd_integration_key
        label = "USD"
    elif currency in {"ZIG", "ZWG"}:
        integration_id = settings.zwg_integration_id
        integration_key = settings.zwg_integration_key
        label = "ZWG"
    else:
        frappe.throw(_("Paynow is not configured for currency: {0}").format(currency or _("Unknown")))

    if not integration_id or not integration_key:
        frappe.throw(_("{0} Paynow credentials are not configured").format(label))

    return integration_id, integration_key


def verify_paynow_hash(fields, integration_key):
    try:
        received_hashes = [value for key, value in fields if str(key).lower() == "hash"]
    except (TypeError, ValueError):
        return False
    if len(received_hashes) != 1 or not integration_key:
        return False

    try:
        message = "".join(str(value) for key, value in fields if str(key).lower() != "hash")
        expected_hash = hashlib.sha512(f"{message}{integration_key.lower()}".encode("utf-8")).hexdigest().upper()
        return hmac.compare_digest(str(received_hashes[0]).upper(), expected_hash)
    except (TypeError, ValueError):
        return False


def normalize_paynow_status(status, current_status):
    status_map = {
        "created": "Initiated",
        "sent": "Pending",
        "paid": "Paid",
        "awaiting delivery": "Paid",
        "delivered": "Paid",
        "cancelled": "Cancelled",
        "refunded": "Failed",
        "disputed": "Failed",
        "error": "Failed",
    }
    return status_map.get((status or "").strip().lower(), current_status)


def verify_paynow_response(response, integration_key):
    response_data = getattr(response, "data", None)
    if isinstance(response_data, str):
        fields = parse_qsl(response_data, keep_blank_values=True)
    elif isinstance(response_data, dict):
        fields = list(response_data.items())
    else:
        return False

    return verify_paynow_hash(fields, integration_key)


def get_paynow_callback_payload():
    request = frappe.request
    raw_body = request.get_data(as_text=True) if request else ""
    fields = parse_qsl(raw_body, keep_blank_values=True)
    if not fields and request:
        fields = list(request.form.items(multi=True))

    payload = {}
    for key, value in fields:
        payload[key.lower()] = value
    return fields, payload


def get_payable_invoice(invoice_name):
    user = frappe.session.user
    if not user or user == "Guest":
        frappe.throw(_("Please log in before paying an invoice."), frappe.PermissionError)

    invoice = frappe.get_doc("Sales Invoice", invoice_name)
    if invoice.docstatus != 1:
        frappe.throw(_("Only submitted invoices can be paid."))

    permitted = frappe.has_permission("Sales Invoice", "read", doc=invoice)
    user_email = str(frappe.db.get_value("User", user, "email") or user).strip().lower()
    customer_email = frappe.db.get_value("Customer", invoice.customer, "email_id")
    invoice_email = getattr(invoice, "contact_email", None)
    invoice_emails = {
        str(email).strip().lower()
        for email in (customer_email, invoice_email)
        if email
    }
    linked_customers = set(frappe.get_all("Customer", filters={"email_id": user_email}, pluck="name"))
    contacts = frappe.get_all("Contact Email", filters={"email_id": user_email}, pluck="parent")
    if contacts:
        linked_customers.update(frappe.get_all(
            "Dynamic Link",
            filters={
                "parenttype": "Contact",
                "parent": ["in", contacts],
                "link_doctype": "Customer",
            },
            pluck="link_name",
        ))

    if not permitted and invoice.owner != user and user_email not in invoice_emails and invoice.customer not in linked_customers:
        frappe.throw(_("You are not allowed to pay this invoice."), frappe.PermissionError)

    amount = flt(invoice.outstanding_amount)
    if amount <= 0:
        frappe.throw(_("This invoice has no outstanding balance."))

    return invoice, amount


def save_paynow_transaction(invoice, transaction_id, amount, response, payment_method, phone=None):
    poll_url = response.poll_url
    parsed = urlparse(poll_url or "")
    guid = parse_qs(parsed.query).get("guid", [None])[0]
    payment_txn = frappe.get_doc({
        "doctype": "Paynow Transaction",
        "transaction_id": transaction_id,
        "payment_gateway": "Paynow",
        "payment_method": payment_method,
        "sales_invoice": invoice.name,
        "amount": amount,
        "currency": invoice.currency,
        "status": "Initiated",
        "phone_number": phone,
        "poll_url": poll_url,
        "guid": guid,
    })
    payment_txn.insert(ignore_permissions=True)
    frappe.db.commit()
    return payment_txn


def get_enabled_paynow_settings(currency):
    settings = frappe.get_single("Paynow Settings")
    if not settings.enabled:
        frappe.throw(_("Paynow payments are currently disabled."))
    integration_id, integration_key = get_paynow_credentials(settings, currency)
    return settings, integration_id, integration_key


def is_paynow_configured(settings, currency):
    currency = (currency or "").strip().upper()
    if currency == "USD":
        integration_id, integration_key = settings.usd_integration_id, settings.usd_integration_key
    elif currency in {"ZIG", "ZWG"}:
        integration_id, integration_key = settings.zwg_integration_id, settings.zwg_integration_key
    else:
        return False
    return bool(settings.enabled and integration_id and integration_key)


def is_visa_checkout_enabled(settings, currency):
    currency = (currency or "").strip().upper()
    if currency == "USD":
        return bool(settings.usd_visa_checkout_enabled)
    if currency in {"ZIG", "ZWG"}:
        return bool(settings.zwg_visa_checkout_enabled)
    return False


def get_paynow_customer_email():
    user = frappe.session.user
    return frappe.db.get_value("User", user, "email") or user


@frappe.whitelist()
def process_paynow_payment(invoice, phone, amount=None):
    try:
        invoice_doc, payable_amount = get_payable_invoice(invoice)
        if not phone or not str(phone).strip():
            frappe.throw(_("A mobile money number is required."))

        settings, integration_id, integration_key = get_enabled_paynow_settings(invoice_doc.currency)
        transaction_id = str(uuid.uuid4())
        paynow = Paynow(integration_id, integration_key, settings.return_url, settings.result_url)
        payment = paynow.create_payment(transaction_id, get_paynow_customer_email())
        payment.add(f"Payment for Invoice {invoice_doc.name}", payable_amount)
        response = paynow.send_mobile(payment, str(phone).strip(), "ecocash")

        if not response.success or not verify_paynow_response(response, integration_key):
            frappe.throw(_("Paynow did not return a valid mobile payment response."))

        save_paynow_transaction(invoice_doc, transaction_id, payable_amount, response, "Mobile Money", str(phone).strip())
        invoice_doc.db_set("custom_paynow_poll_url", response.poll_url)
        return {
            "status": "success",
            "message": _("Payment prompt sent to {0}").format(phone),
            "poll_url": response.poll_url,
        }
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "Paynow Mobile Payment Error")
        raise


@frappe.whitelist()
def process_paynow_card_payment(invoice):
    try:
        invoice_doc, payable_amount = get_payable_invoice(invoice)
        settings, integration_id, integration_key = get_enabled_paynow_settings(invoice_doc.currency)
        if not is_visa_checkout_enabled(settings, invoice_doc.currency):
            frappe.throw(_("Visa checkout is not enabled for this merchant account."))

        transaction_id = str(uuid.uuid4())
        paynow = Paynow(integration_id, integration_key, settings.return_url, settings.result_url)
        payment = paynow.create_payment(transaction_id, get_paynow_customer_email())
        payment.add(f"Payment for Invoice {invoice_doc.name}", payable_amount)
        response = paynow.send(payment)

        if not response.success or not verify_paynow_response(response, integration_key):
            frappe.throw(_("Paynow did not return a valid card checkout response."))

        redirect_url = response.redirect_url
        parsed_url = urlparse(redirect_url or "")
        hostname = (parsed_url.hostname or "").lower()
        if parsed_url.scheme != "https" or not (hostname == "paynow.co.zw" or hostname.endswith(".paynow.co.zw")):
            frappe.throw(_("Paynow returned an invalid checkout URL."))

        save_paynow_transaction(invoice_doc, transaction_id, payable_amount, response, "Visa/Mastercard")
        invoice_doc.db_set("custom_paynow_poll_url", response.poll_url)
        return {
            "status": "success",
            "redirect_url": redirect_url,
            "poll_url": response.poll_url,
        }
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "Paynow Card Checkout Error")
        raise


@frappe.whitelist()
def get_checkout_options(invoice):
    invoice_doc, amount = get_payable_invoice(invoice)
    settings = frappe.get_single("Paynow Settings")
    configured = is_paynow_configured(settings, invoice_doc.currency)

    return {
        "amount": amount,
        "mobile_money": configured,
        "visa_card": configured and is_visa_checkout_enabled(settings, invoice_doc.currency),
    }

@frappe.whitelist(allow_guest=True)
def paynow_webhook():
    fields, data = get_paynow_callback_payload()
    reference = data.get("reference")
    transaction = frappe.db.get_value(
        "Paynow Transaction",
        {"transaction_id": reference},
        ["name", "currency", "sales_invoice"],
        as_dict=True,
    ) if reference else None
    if not transaction:
        frappe.log_error(str(data), "Paynow Callback Transaction Not Found")
        frappe.throw(_("Paynow transaction was not found."))

    settings = frappe.get_single("Paynow Settings")
    transaction_currency = transaction.get("currency") if isinstance(transaction, dict) else transaction.currency
    sales_invoice = transaction.get("sales_invoice") if isinstance(transaction, dict) else transaction.sales_invoice
    if not transaction_currency and sales_invoice:
        transaction_currency = frappe.db.get_value("Sales Invoice", sales_invoice, "currency")
    integration_id, integration_key = get_paynow_credentials(settings, transaction_currency)
    if not verify_paynow_hash(fields, integration_key):
        frappe.log_error(str(data), "Paynow Callback Hash Validation Failed")
        frappe.throw(_("Paynow callback signature is invalid."), frappe.PermissionError)

    frappe.set_user("Administrator")
    process_paynow_webhook(data)

    return "ok"

def process_paynow_webhook(data):
    reference = data.get("reference")
    paynow_reference = data.get("paynowreference")
    status = data.get("status")
    poll_url = data.get("pollurl")

    print("\n===== PROCESSING WEBHOOK =====")
    print("Reference:", reference)
    print("Paynow Ref:", paynow_reference)
    print("Status:", status)

    if not reference:
        print("Missing reference")
        return
    txn_name = frappe.get_value(
        "Paynow Transaction",
        {"transaction_id": reference},
        "name"
    )
    if not txn_name:
        frappe.log_error(str(data), "Paynow - Transaction Not Found")
        return

    txn = frappe.get_doc("Paynow Transaction", txn_name)

    if txn.status == "Completed":
        print("Already processed, skipping")
        return

    callback_amount = flt(data.get("amount"))
    if callback_amount != flt(txn.amount):
        frappe.log_error(str(data), "Paynow Callback Amount Mismatch")
        frappe.throw(_("Paynow callback amount does not match the transaction."))

    txn.status = normalize_paynow_status(status, txn.status)
    txn.paynow_reference = paynow_reference or txn.paynow_reference
    txn.poll_url = poll_url or txn.poll_url
    incoming_invoice_number, incoming_invoice_id = extract_zoho_invoice_fields(data)
    if incoming_invoice_number or not txn.zoho_invoice_number:
        txn.zoho_invoice_number = incoming_invoice_number
    if incoming_invoice_id or not txn.zoho_invoice_id:
        txn.zoho_invoice_id = incoming_invoice_id

    txn.log = (txn.log or "") + f"""
        [{frappe.utils.now()}] WEBHOOK UPDATE:
        Status: {status}
        Paynow Ref: {paynow_reference}
        Poll URL: {poll_url}
        RAW: {data}
        """
    txn.save(ignore_permissions=True)
    frappe.db.commit()
    # 🔥 trigger payment entry logic
    handle_paid_transaction(txn, data)
    print("✅ TRANSACTION UPDATED")

def handle_paid_transaction(txn, data):
    if txn.status == "Completed":
        print("⚠️ Already processed, skipping")
        return

    if (data.get("status") or "").strip().lower() not in {"paid", "awaiting delivery"}:
        return

    print("\n💰 PAYMENT CONFIRMED - CREATING PAYMENT ENTRY")

    if txn.zoho_invoice_number or txn.zoho_invoice_id:
        try:
            record_zoho_invoice_payment(txn, data)
        except Exception:
            txn.db_set("zoho_sync_status", "Failed")
            txn.db_set("zoho_error", frappe.get_traceback())
            frappe.log_error(frappe.get_traceback(), "Zoho Paynow - Payment Sync Failed")
            raise
    elif not txn.sales_invoice:
        txn.db_set("zoho_sync_status", "Failed")
        txn.db_set("zoho_error", "Missing Zoho invoice_number or invoice_id on paid Paynow webhook")
        frappe.log_error(str(data), "Zoho Paynow - Missing Invoice Reference")
        return

    if not txn.sales_invoice:
        txn.status = "Paid"
        txn.save(ignore_permissions=True)
        frappe.db.commit()
        return

    inv = frappe.get_doc("Sales Invoice", txn.sales_invoice)
    settings = frappe.get_single("Paynow Settings")
    if inv.currency == "USD":
        paid_to_account = settings.usd_paid_to_account
    else:
        paid_to_account = settings.zwg_payment_accout

    # 🔥 Create Payment Entry
    payment_entry = frappe.get_doc({
        "doctype": "Payment Entry",
        "payment_type": "Receive",
        "party_type": "Customer",
        "party": inv.customer,
        "paid_amount": txn.amount,
        "received_amount": txn.amount,
        "mode_of_payment": "Paynow Visa" if (txn.payment_method or "").lower().startswith("visa") else "Ecocash",
        "reference_no": txn.paynow_reference or txn.transaction_id,
        "reference_date": now(),
        "paid_to":paid_to_account,  # adjust your account
    })

    payment_entry.append("references", {
        "reference_doctype": "Sales Invoice",
        "reference_name": inv.name,
        "allocated_amount": txn.amount
    })

    payment_entry.insert(ignore_permissions=True)
    payment_entry.submit()

    print("✅ Payment Entry Created:", payment_entry.name)

    # 🔥 update transaction
    txn.status = "Completed"
    txn.payment_entry = payment_entry.name
    txn.save(ignore_permissions=True)

    frappe.db.commit()
