# paynow_gateway/setup.py

import frappe

def after_install():
    create_paynow_modes_of_payment()


def after_migrate():
    create_paynow_modes_of_payment()


def create_paynow_modes_of_payment():
    for mode in ("Ecocash", "Paynow Visa"):
        if not frappe.db.exists("Mode of Payment", mode):
            doc = frappe.get_doc({
                "doctype": "Mode of Payment",
                "mode_of_payment": mode,
                "enabled": 1
            })
            doc.insert(ignore_permissions=True)
    frappe.db.commit()