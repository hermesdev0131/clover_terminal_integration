import json
import logging
import uuid

import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Clover environment URLs (regional)
CLOVER_ENV = {
    'sandbox': {
        'api_base': 'https://apisandbox.dev.clover.com',
        'web_base': 'https://sandbox.dev.clover.com',
        'oauth_base': 'https://sandbox.dev.clover.com',
    },
    'production_na': {
        'api_base': 'https://api.clover.com',
        'web_base': 'https://www.clover.com',
        'oauth_base': 'https://www.clover.com',
    },
    'production_eu': {
        'api_base': 'https://api.eu.clover.com',
        'web_base': 'https://www.eu.clover.com',
        'oauth_base': 'https://eu.clover.com',
    },
    'production_la': {
        'api_base': 'https://api.la.clover.com',
        'web_base': 'https://www.la.clover.com',
        'oauth_base': 'https://la.clover.com',
    },
}

# Card entry method bitmask (Clover SDK values)
CARD_ENTRY_MAG_STRIPE = 1
CARD_ENTRY_ICC_CONTACT = 2    # chip
CARD_ENTRY_NFC = 4            # contactless / tap
CARD_ENTRY_MANUAL = 8         # key-in
CARD_ENTRY_ALL = CARD_ENTRY_MAG_STRIPE | CARD_ENTRY_ICC_CONTACT | CARD_ENTRY_NFC | CARD_ENTRY_MANUAL


class CloverTerminal(models.Model):
    _name = 'clover.terminal'
    _description = 'Clover Terminal Device'
    _inherit = ['mail.thread']
    _order = 'name'

    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------

    name = fields.Char(
        string='Terminal Name',
        required=True,
        tracking=True,
        help='Friendly name (e.g. "Front Counter Flex 4")',
    )
    environment = fields.Selection(
        [('sandbox', 'Sandbox'),
         ('production_na', 'Production (North America)'),
         ('production_eu', 'Production (Europe)'),
         ('production_la', 'Production (Latin America)')],
        string='Environment',
        required=True,
        default='sandbox',
        tracking=True,
    )
    merchant_id = fields.Char(
        string='Merchant ID',
        required=True,
        tracking=True,
        help='Clover Merchant ID (Account & Setup → Merchants)',
    )
    device_serial = fields.Char(
        string='Device Serial',
        required=True,
        tracking=True,
        help='Serial number printed on device (e.g. C046LT52640523)',
    )
    clover_device_id = fields.Char(
        string='Clover Device ID',
        readonly=True,
        help='UUID resolved automatically from serial during connection test',
    )

    # OAuth credentials
    app_id = fields.Char(
        string='App ID',
        required=True,
        tracking=True,
        help='Clover App ID from Developer Dashboard (App Settings)',
    )
    app_secret = fields.Char(
        string='App Secret',
        required=True,
        groups='point_of_sale.group_pos_manager',
        help='Clover App Secret from Developer Dashboard (App Settings)',
    )
    raid = fields.Char(
        string='RAID',
        required=True,
        tracking=True,
        help='Remote Application ID from App Settings (e.g. 4YFRTCTS6SMFT.R9126BVSN0JYY)',
    )
    api_token = fields.Char(
        string='API Access Token',
        readonly=True,
        groups='point_of_sale.group_pos_manager',
        help='OAuth access token — acquired automatically via Authorize flow',
    )
    token_acquired = fields.Boolean(
        string='Token Acquired',
        compute='_compute_token_acquired',
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        required=True,
        default=lambda self: self.env.company,
    )
    state = fields.Selection(
        [('draft', 'Not Configured'),
         ('testing', 'Testing'),
         ('active', 'Active'),
         ('inactive', 'Inactive'),
         ('error', 'Error')],
        string='Status',
        default='draft',
        tracking=True,
    )
    last_ping = fields.Datetime(string='Last Successful Ping', readonly=True)
    last_error = fields.Text(string='Last Error', readonly=True)
    merchant_name = fields.Char(string='Merchant Name', readonly=True)
    device_model = fields.Char(string='Device Model', readonly=True)

    # Reverse relation to payment methods
    payment_method_ids = fields.One2many(
        'pos.payment.method', 'clover_terminal_id',
        string='Payment Methods',
    )
    payment_method_count = fields.Integer(
        compute='_compute_payment_method_count',
    )

    _sql_constraints = [
        ('unique_device', 'unique(merchant_id, device_serial, company_id)',
         'This device is already registered for this merchant.'),
    ]

    def init(self):
        """Clean up partial index from previous version if it exists."""
        self.env.cr.execute("""
            DROP INDEX IF EXISTS clover_terminal_unique_active_device;
        """)

    # ------------------------------------------------------------------
    # Computed fields
    # ------------------------------------------------------------------

    @api.depends('api_token')
    def _compute_token_acquired(self):
        for rec in self:
            rec.token_acquired = bool(rec.api_token)

    def _compute_payment_method_count(self):
        for rec in self:
            rec.payment_method_count = len(rec.payment_method_ids)

    # ------------------------------------------------------------------
    # URL / header helpers
    # ------------------------------------------------------------------

    def _get_api_base(self):
        self.ensure_one()
        return CLOVER_ENV[self.environment]['api_base']

    def _get_oauth_base(self):
        self.ensure_one()
        return CLOVER_ENV[self.environment]['oauth_base']

    def _get_headers(self):
        """Headers for Clover REST API v3 (merchant/device management)."""
        self.ensure_one()
        return {
            'Authorization': f'Bearer {self.api_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

    def _get_connect_headers(self, idempotency_key=None):
        """Headers for Connect v1 REST Pay Display API (device control)."""
        self.ensure_one()
        if not self.device_serial:
            raise UserError(_('Device serial not set.'))
        headers = {
            'Authorization': f'Bearer {self.api_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-Clover-Device-Id': self.clover_device_id or self.device_serial,
            'X-POS-Id': self.raid,
        }
        if idempotency_key:
            headers['Idempotency-Key'] = idempotency_key
        return headers

    # ------------------------------------------------------------------
    # Core API caller  (all Clover HTTP traffic goes through here)
    # ------------------------------------------------------------------

    def _api_request(self, method, endpoint, payload=None, timeout=30,
                     connect=False, idempotency_key=None):
        """
        Authenticated request to Clover API.
        Every call is written to clover.transaction.log for auditing.

        :param connect: if True, use Connect v1 headers (X-Clover-Device-Id,
                        X-POS-Id) instead of plain v3 headers.
        :param idempotency_key: optional idempotency key for financial ops.
        Returns parsed JSON on success.
        Raises UserError on any failure.
        """
        self.ensure_one()
        url = f'{self._get_api_base()}{endpoint}'
        request_id = uuid.uuid4().hex[:16]
        headers = (self._get_connect_headers(idempotency_key)
                   if connect else self._get_headers())

        log_vals = {
            'terminal_id': self.id,
            'request_id': request_id,
            'endpoint': endpoint,
            'http_method': method.upper(),
            'request_payload': json.dumps(payload) if payload else '',
            'status': 'pending',
        }

        try:
            resp = requests.request(
                method=method,
                url=url,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            body = resp.json() if resp.content else {}
            log_vals['response_payload'] = json.dumps(body, default=str)
            log_vals['http_status'] = resp.status_code

            if resp.status_code in (200, 201):
                log_vals['status'] = 'success'
                self.env['clover.transaction.log'].sudo().create(log_vals)
                return body

            error_msg = body.get('message') or resp.text[:500]
            log_vals.update(status='error', error_message=error_msg)
            self.env['clover.transaction.log'].sudo().create(log_vals)
            raise UserError(_(
                'Clover API %(status)s: %(msg)s',
                status=resp.status_code,
                msg=error_msg,
            ))

        except requests.exceptions.Timeout:
            log_vals.update(status='timeout', error_message='Request timed out')
            self.env['clover.transaction.log'].sudo().create(log_vals)
            raise UserError(_('Clover request timed out. Check device/network.'))

        except requests.exceptions.ConnectionError:
            log_vals.update(status='error', error_message='Connection refused')
            self.env['clover.transaction.log'].sudo().create(log_vals)
            raise UserError(_('Cannot reach Clover API. Check network.'))

        except UserError:
            raise

        except Exception as exc:
            _logger.exception('Clover API unexpected error')
            log_vals.update(status='error', error_message=str(exc))
            self.env['clover.transaction.log'].sudo().create(log_vals)
            raise UserError(_('Clover error: %s', exc))

    # ------------------------------------------------------------------
    # OAuth authorization
    # ------------------------------------------------------------------

    def action_authorize(self):
        """Open Clover OAuth page in a new browser tab.

        After merchant approval Clover redirects back to
        ``/odoo/clover/oauth/callback`` where the code is exchanged
        for an access token.
        """
        self.ensure_one()
        if not self.app_id or not self.app_secret:
            raise UserError(_('Fill in App ID and App Secret first.'))
        if not self.merchant_id:
            raise UserError(_('Fill in Merchant ID first.'))

        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        callback = f'{base_url}/odoo/clover/oauth/callback'
        oauth_base = self._get_oauth_base()
        authorize_url = (
            f'{oauth_base}/oauth/authorize'
            f'?client_id={self.app_id}'
            f'&merchant_id={self.merchant_id}'
            f'&redirect_uri={callback}'
            f'&response_type=code'
        )
        return {
            'type': 'ir.actions.act_url',
            'url': authorize_url,
            'target': 'new',
        }

    # ------------------------------------------------------------------
    # Connection testing  (Phase 1 deliverable)
    # ------------------------------------------------------------------

    def _resolve_device_by_serial(self):
        """Find Clover device UUID by serial number."""
        self.ensure_one()
        devices = self._api_request(
            'GET', f'/v3/merchants/{self.merchant_id}/devices',
        )
        for dev in devices.get('elements', []):
            if dev.get('serial') == self.device_serial:
                return dev
        raise UserError(_(
            'No device with serial "%(serial)s" found for this merchant.',
            serial=self.device_serial,
        ))

    def action_test_connection(self):
        """Fetch merchant info, resolve device, and optionally ping via Connect v1."""
        self.ensure_one()
        if not self.api_token:
            raise UserError(_('No API token. Click Authorize first.'))
        try:
            # 1) Verify merchant via REST v3
            merchant = self._api_request(
                'GET', f'/v3/merchants/{self.merchant_id}',
            )
            merchant_name = merchant.get('name', '?')

            # 2) Find device by serial number → get UUID
            device = self._resolve_device_by_serial()
            clover_device_id = device.get('id')
            device_model = device.get('productName', device.get('model', ''))

            self.write({
                'merchant_name': merchant_name,
                'clover_device_id': clover_device_id,
                'device_model': device_model,
            })

            # 3) Ping device via Connect v1 (non-fatal — device may be offline)
            ping_ok = False
            ping_error = ''
            try:
                self.ping_device_connect()
                ping_ok = True
            except UserError as ping_exc:
                ping_error = str(ping_exc)
                _logger.warning(
                    'Clover terminal %s: API OK but device ping failed: %s',
                    self.name, ping_error,
                )

            # Steps 1-2 passed → terminal is valid regardless of ping
            write_vals = {
                'state': 'testing',
                'last_error': ping_error if not ping_ok else False,
            }
            if ping_ok:
                write_vals['last_ping'] = fields.Datetime.now()
            self.write(write_vals)

            if ping_ok:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Connection OK'),
                        'message': _(
                            'Merchant: %(merchant)s — Device: %(serial)s (%(model)s) — Ping OK',
                            merchant=merchant_name,
                            serial=self.device_serial,
                            model=device_model or 'Flex',
                        ),
                        'type': 'success',
                        'sticky': False,
                        'next': {
                            'type': 'ir.actions.act_window',
                            'res_model': 'clover.terminal',
                            'res_id': self.id,
                            'views': [(False, 'form')],
                            'target': 'current',
                        },
                    },
                }
            else:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('API OK — Device Offline'),
                        'message': _(
                            'Merchant: %(merchant)s — Device: %(serial)s (%(model)s) verified. '
                            'Device ping failed — start Cloud Pay Display on the terminal.',
                            merchant=merchant_name,
                            serial=self.device_serial,
                            model=device_model or 'Flex',
                        ),
                        'type': 'warning',
                        'sticky': True,
                        'next': {
                            'type': 'ir.actions.act_window',
                            'res_model': 'clover.terminal',
                            'res_id': self.id,
                            'views': [(False, 'form')],
                            'target': 'current',
                        },
                    },
                }

        except UserError as exc:
            self.write({'state': 'error', 'last_error': str(exc)})
            raise

    def action_view_payment_methods(self):
        """Open the list of payment methods linked to this terminal."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Payment Methods'),
            'res_model': 'pos.payment.method',
            'view_mode': 'list,form',
            'domain': [('clover_terminal_id', '=', self.id)],
        }

    def action_activate(self):
        """Mark terminal ready for production use."""
        self.ensure_one()
        if self.state not in ('testing', 'error', 'inactive'):
            raise UserError(_('Test the connection first.'))
        self.write({'state': 'active', 'last_error': False})

    def action_deactivate(self):
        """Deactivate terminal — keeps record visible but unusable."""
        self.ensure_one()
        self.write({'state': 'inactive'})

    def action_reset_draft(self):
        """Reset terminal back to draft."""
        self.ensure_one()
        self.write({
            'state': 'draft',
            'last_error': False,
            'api_token': False,
            'clover_device_id': False,
        })

    # ------------------------------------------------------------------
    # Connect v1 device operations
    # ------------------------------------------------------------------

    def ping_device_connect(self):
        """Ping device via Connect v1 REST Pay Display API."""
        self.ensure_one()
        if not self.api_token:
            raise UserError(_('No API token. Run Authorize first.'))
        result = self._api_request(
            'POST', '/connect/v1/device/ping',
            connect=True, timeout=15,
        )
        self.last_ping = fields.Datetime.now()
        return result

    def reset_device(self):
        """Reset device to idle via Connect v1."""
        self.ensure_one()
        return self._api_request(
            'PUT', '/connect/v1/device/reset',
            connect=True, timeout=15,
        )

    def check_device_online(self):
        """Return True/False whether device responds via Connect v1 ping."""
        self.ensure_one()
        if not self.device_serial or not self.api_token:
            return False
        try:
            self.ping_device_connect()
            return True
        except UserError:
            return False

    # ------------------------------------------------------------------
    # Payment API  (Phase 3 — Cloud Pay Display)
    # ------------------------------------------------------------------

    def _payment_create_clover_order(self, amount_cents, order_uid):
        """Create a Clover order for the payment. Returns clover_order_id (str)."""
        self.ensure_one()
        result = self._api_request(
            'POST',
            f'/v3/merchants/{self.merchant_id}/orders',
            payload={'currency': 'USD', 'state': 'open'},
        )
        clover_order_id = result.get('id')
        if not clover_order_id:
            raise UserError(_('Clover did not return an order ID.'))
        # Attach a line item so the order has a description on the device
        self._api_request(
            'POST',
            f'/v3/merchants/{self.merchant_id}/orders/{clover_order_id}/line_items',
            payload={
                'name': f'POS {order_uid}',
                'price': amount_cents,
                'unitQty': 1,
            },
        )
        return clover_order_id

    def _payment_send_card(self, clover_order_id, amount_cents, idempotency_key):
        """Send a SALE request to the terminal via Connect v1.

        Returns the Clover payment ID string (used to poll status).
        """
        self.ensure_one()
        result = self._api_request(
            'POST',
            '/connect/v1/payments',
            payload={
                'amount': amount_cents,
                'externalPaymentId': idempotency_key,
                'cardEntryMethods': CARD_ENTRY_ALL,
                'orderId': clover_order_id,
            },
            connect=True,
            idempotency_key=idempotency_key,
            timeout=15,
        )
        return result.get('paymentId') or result.get('id') or ''

    def _payment_send_qr(self, clover_order_id, amount_cents, idempotency_key):
        """Send a QR-only payment request to the terminal via Connect v1.

        The terminal displays the QR; the customer scans it independently.
        Returns (clover_payment_id, qr_payload) where qr_payload is the
        QR code URL / data returned by Clover.
        """
        self.ensure_one()
        result = self._api_request(
            'POST',
            '/connect/v1/payments',
            payload={
                'amount': amount_cents,
                'externalPaymentId': idempotency_key,
                'orderId': clover_order_id,
                'presentQrcOnly': True,
            },
            connect=True,
            idempotency_key=idempotency_key,
            timeout=15,
        )
        clover_payment_id = result.get('paymentId') or result.get('id') or ''
        # Clover returns the QR code under various key names depending on version
        qr_payload = (
            result.get('qrPaymentCode')
            or result.get('qrCode')
            or result.get('qr')
            or ''
        )
        return clover_payment_id, qr_payload

    def _payment_get_status(self, clover_payment_id):
        """Fetch a specific payment from Clover REST v3. Returns raw dict."""
        self.ensure_one()
        return self._api_request(
            'GET',
            f'/v3/merchants/{self.merchant_id}/payments/{clover_payment_id}',
        )

    def _payment_get_order_payments(self, clover_order_id):
        """List payments attached to a Clover order (used for async QR polling)."""
        self.ensure_one()
        result = self._api_request(
            'GET',
            f'/v3/merchants/{self.merchant_id}/orders/{clover_order_id}/payments',
        )
        return result.get('elements', [])

    def _payment_refund(self, clover_payment_id, amount_cents=None):
        """Refund a completed Clover payment via REST v3.

        If ``amount_cents`` is None, performs a full refund.
        Otherwise, performs a partial refund for the given amount.
        Returns the refund dict from Clover.
        """
        self.ensure_one()
        payload = {}
        if amount_cents is not None:
            payload['amount'] = amount_cents
        return self._api_request(
            'POST',
            f'/v3/merchants/{self.merchant_id}/payments/{clover_payment_id}/refunds',
            payload=payload if payload else None,
        )

    def _payment_void(self, clover_payment_id):
        """Void an auth-only payment via REST v3 (before settlement).

        Returns the updated payment dict from Clover.
        """
        self.ensure_one()
        return self._api_request(
            'POST',
            f'/v3/merchants/{self.merchant_id}/payments/{clover_payment_id}/void',
        )

    def _payment_cancel_on_terminal(self):
        """Reset the terminal to dismiss any in-progress payment screen.

        Best-effort — terminal may already be idle.
        """
        self.ensure_one()
        try:
            self._api_request('PUT', '/connect/v1/device/reset', connect=True, timeout=15)
        except UserError:
            pass
