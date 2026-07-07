# -*- coding: utf-8 -*-

{
    'name': 'Clover Terminal Integration',
    'version': '18.0.2.0.0',
    'category': 'Point of Sale',
    'summary': 'Clover Flex 4 card payments and Fiserv Transferencias 3.0 QR '
               'displayed on both the Odoo screen and the Clover device',
    'description': """
Clover Terminal Integration (Argentina)
========================================

Integrates Clover Flex 4 payment terminals and the Fiserv QR Estático API
(Transferencias 3.0) with Odoo 18 Point of Sale for the LATAM/Argentina market.

Payment Methods
---------------
* **Card Payment** — chip, contactless, and magstripe via the Clover Flex 4
  through the Remote Pay Cloud SDK.
* **QR Payment** — the same static caja QR is displayed on the Odoo POS screen
  and on the Clover device screen simultaneously. The customer scans either
  one and the payment confirms in Odoo within a few seconds. If the device is
  offline the flow degrades gracefully to Odoo-only QR without failing.

Fiserv QR Estático API
----------------------
* Static QR per caja fetched from the QR Estático API and cached.
* Per-transaction payment orders (POST /payment-order-cashier) with a unique
  reference and configurable expiration.
* Payment status polling (GET /operations-managment/payment-order) with the
  Fiserv status codes (P/A/E/R/C/D/V) mapped to POS states.
* Webhook endpoint (/odoo/fiserv/qr/webhook) with an optional IP allowlist
  system parameter for source validation.
* Refunds via POST /transaction/refund routed through the API when the payment
  came from the Odoo QR side.

Technical Details
-----------------
* Clover Remote Pay Cloud SDK integration (WebSocket via Cloud Pay Display)
  for card and device-side QR payments.
* setDisableReceiptSelection(true) on every SaleRequest so onSaleResponse
  fires immediately after approval, no manual receipt dismissal on the device.
* OAuth 2.0 for Clover; JWT bearer for the Fiserv QR Estático API.
* OWL 2 frontend components (PaymentInterface pattern).
* Transaction audit log (clover.transaction and clover.transaction.log) for
  every REST call, success or error, both Clover and Fiserv sides.
* QR rendered locally via Odoo's built-in /report/barcode endpoint (no
  external QR service).
* Multi-session safety with UUID-based idempotency keys.
    """,
    'author': 'Hiroshi, WolfAIX',
    'website': 'https://www.wolfaix.com',
    'depends': [
        'point_of_sale',
        'account',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/clover_terminal_views.xml',
        'views/clover_transaction_log_views.xml',
        'views/clover_transaction_views.xml',
        'views/pos_payment_method_views.xml',
        'views/menu.xml',
    ],
    'assets': {
        'point_of_sale._assets_pos': [
            'clover_terminal_integration/static/src/lib/clover_sdk.js',
            'clover_terminal_integration/static/src/xml/clover_qr_screen.xml',
            'clover_terminal_integration/static/src/js/clover_qr_screen.js',
            'clover_terminal_integration/static/src/js/clover_payment_interface.js',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
