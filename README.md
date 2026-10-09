````markdown
# 💳 Payment Gateway (Paynow + EcoCash) – Frappe App

A modular payment gateway system for ERPNext/Frappe supporting:

- 💵 Paynow (USD payments)
- 🇿🇼 EcoCash / ZWG mobile money
- 🧾 Payment Entry integration
- 🧪 Test mode simulation (Paynow sandbox logic)
- 🔴 Live production payments

---

# 🚀 Features

## 💳 Payment Processing

- Create payments from Sales Invoice
- Auto-generate Payment Entry
- Support for USD and ZWG flows

## 🧪 Test Mode Support

- Paynow sandbox integration mode
- Simulated responses using:
  - Test EcoCash numbers
  - Test tokens
- No real money movement

## 🔴 Live Mode

- Real Paynow + EcoCash integrations
- Webhook confirmation handling
- Production-ready transaction flow

## 🔁 Webhook Handling

- Automatic payment status updates
- Invoice reconciliation
- Failed / pending / success tracking

---

# 🧱 Architecture

```text
Payment Gateway App
│
├── Settings
│   ├── Paynow USD Settings
│   ├── EcoCash ZWG Settings
│   └── System Mode (Test / Live)
│
├── API Layer
│   ├── Paynow Service
│   ├── EcoCash Service
│   └── Payment Router
│
├── UI Layer
│   ├── Invoice Payment Buttons
│   └── Frappe Dialogs
│
└── Core Logic
    ├── Payment Entry Creation
    ├── Webhook Processor
    └── Status Updater
````

---

# ⚙️ Payment Flow

## Visa/Mastercard hosted checkout

```text
Invoice → Customer selects Visa → Paynow hosted checkout → Signed callback → Payment Entry
```

## Mobile money

```text
Invoice → Customer selects mobile money → EcoCash prompt → Signed callback → Payment Entry
```

---

# 🧪 Test Mode (Paynow)

When system is in TEST mode:

## Mobile Money Test Numbers

| Scenario           | Number     |
| ------------------ | ---------- |
| Success            | 0771111111 |
| Delayed Success    | 0772222222 |
| Cancelled          | 0773333333 |
| Insufficient Funds | 0774444444 |

Use Paynow's current test-mode integration credentials and test payment methods. No real money should be processed in test mode. Saved-card tokens are not used by this one-time hosted checkout flow.

---

# 🔐 Settings Structure

## 🌐 System Mode

* Test
* Live

## 💵 Paynow USD Settings

* Integration ID (Test & Live)
* Integration Key (Test & Live)
* Enabled

## 🇿🇼 EcoCash Settings

* API Key (Test & Live)
* API Secret (Test & Live)
* Shortcode
* Enabled

---

# 🧠 Design Principles

* No sandbox/live UI buttons
* No per-transaction environment switching
* No mixing currencies in same flow
* System-wide mode control
* Backend-controlled payment logic
* UI only triggers payment actions
* Full audit trail through Payment Entry

---

# 🖥️ UI Behavior

* Customers choose Mobile Money or Visa/Mastercard for an outstanding invoice.
* Mobile Money uses the EcoCash prompt; Visa/Mastercard uses Paynow hosted checkout.

* System automatically:

  * chooses correct gateway
  * uses correct credentials
  * handles test/live behavior

---

# 🔥 Security Features

* Prevents accidental live payments in test mode
* Webhook validation
* Duplicate payment protection
* Invoice state validation

---

# 🛠️ Tech Stack

* Frappe Framework
* ERPNext
* Paynow API
* EcoCash API
* Python
* JavaScript

---

# 🚀 Future Improvements

* Multi-gateway routing
* OneMoney / InnBucks support
* Payment retry system
* Real-time payment dashboard
* Mobile app support

---

## Visa/Mastercard Hosted Checkout

The gateway sends first-time card payments to Paynow's hosted checkout. Card numbers are entered on Paynow and are never sent to this app. The customer returns to the shop while Paynow posts a signed result to the configured result URL.

Before enabling Visa:

1. Ask Paynow to activate Visa/Mastercard for the integration ID configured in Paynow Settings.
2. Set `result_url` to `https://<your-site>/api/method/paynow_gateway.paynow_gateway.api.paynow_webhook` and set the HTTPS `return_url` to `https://<your-site>/orders2` so the shop resumes payment-status polling when the customer returns.
3. Deploy the gateway and run `bench --site <site_name> migrate`, then restart the web workers.
4. In Paynow Settings, enable Paynow and then enable Visa/Mastercard separately for each approved USD or ZWG integration. Both Visa switches are off by default.
5. Test the card redirect and signed callback using Paynow test credentials before enabling the live integration.

Do not set `tokenize=true` for one-time checkout. Saving cards for recurring payments requires separate Paynow approval.

# 📌 Summary

This system provides a clean separation of concerns:

> UI triggers payments
> Backend processes logic
> Payment gateways move money
> Webhooks reconcile transactions

```
```
