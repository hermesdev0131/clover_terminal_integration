# -*- coding: utf-8 -*-

{
    'name': 'Clover Terminal Integration',
    'version': '18.0.1.0.0',
    'category': 'Point of Sale',
    'summary': 'Clover Flex 4 terminal integration with directed Card and QR payment modes',
    'description': """
Clover Terminal Integration
============================

This module integrates Clover Flex 4 payment terminals with Odoo 18 Point of Sale,
enabling directed Card and QR payment flows with full device control.

Key Features:
-------------
* Dual payment methods: Clover Card and Clover QR
* Forced payment mode on device (no customer selection screen)
* QR mirroring: same QR displayed on device and POS screen
* Real-time payment status tracking with order locking
* Refund and void support via Clover API
* Transaction audit logging for every API call
* Error recovery dashboard

Technical Details:
------------------
* REST Pay Display API integration (Cloud Pay Display)
* OAuth 2.0 token management
* Per-transaction settings: presentQrcOnly, CardEntryMethods
* OWL 2 frontend components (PaymentInterface pattern)
* Multi-session safety with idempotency keys
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
        'views/pos_payment_method_views.xml',
        'views/menu.xml',
    ],
    'assets': {
        'point_of_sale._assets_pos': [
            # Phase 2: payment interfaces and QR popup will be added here
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
