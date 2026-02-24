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
        help='Remote Application ID from App Settings (e.g. 4YFRTCTS6SMFT.R9126BVSNOJYY)',
    )
    api_token = fields.Char(
        string='API Access Token',
        groups='point_of_sale.group_pos_manager',
        help='OAuth access token — acquired via Authorize flow or pasted from Merchant Dashboard (Setup → API Tokens)',
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
    active = fields.Boolean(default=True)
    state = fields.Selection(
        [('draft', 'Not Configured'),
         ('testing', 'Testing'),
         ('active', 'Active'),
         ('error', 'Error')],
        string='Status',
        default='draft',
        tracking=True,
    )
    last_ping = fields.Datetime(string='Last Successful Ping', readonly=True)
    last_error = fields.Text(string='Last Error', readonly=True)
    merchant_name = fields.Char(string='Merchant Name', readonly=True)
    device_model = fields.Char(string='Device Model', readonly=True)

    _sql_constraints = [
        ('unique_device', 'unique(merchant_id, device_serial, company_id)',
         'This device is already registered for this merchant.'),
    ]

    # ------------------------------------------------------------------
    # Computed fields
    # ------------------------------------------------------------------

    @api.depends('api_token')
    def _compute_token_acquired(self):
        for rec in self:
            rec.token_acquired = bool(rec.api_token)

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
        if not self.clover_device_id:
            raise UserError(_('Device ID not resolved yet. Run Test Connection first.'))
        headers = {
            'Authorization': f'Bearer {self.api_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-Clover-Device-Id': self.clover_device_id,
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

    def action_open_merchant_tokens(self):
        """Open Clover Developer Dashboard → App Installs page.

        From there click on the merchant to see the API token.
        """
        self.ensure_one()
        if not self.raid:
            raise UserError(_('Fill in the RAID field first.'))
        # RAID format: {developer_id}.{app_id}
        parts = self.raid.split('.', 1)
        if len(parts) != 2:
            raise UserError(_('RAID must be in format: developer_id.app_id'))
        developer_id = parts[0]
        oauth_base = self._get_oauth_base()
        url = (
            f'{oauth_base}/developer-home/{developer_id}'
            f'/apps/{self.app_id}/installs'
        )
        return {
            'type': 'ir.actions.act_url',
            'url': url,
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
        """Fetch merchant info, resolve device, and ping via Connect v1."""
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
                'last_error': False,
                'merchant_name': merchant_name,
                'clover_device_id': clover_device_id,
                'device_model': device_model,
            })

            # 3) Ping device via Connect v1 to validate full connectivity
            self.ping_device_connect()

            self.write({
                'state': 'testing',
                'last_ping': fields.Datetime.now(),
            })
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
        except UserError as exc:
            self.write({'state': 'error', 'last_error': str(exc)})
            raise

    def action_activate(self):
        """Mark terminal ready for production use."""
        self.ensure_one()
        if self.state not in ('testing', 'error'):
            raise UserError(_('Test the connection first.'))
        self.write({'state': 'active', 'last_error': False})

    def action_reset_draft(self):
        """Reset terminal back to draft."""
        self.ensure_one()
        self.write({'state': 'draft', 'last_error': False})

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
        if not self.clover_device_id or not self.api_token:
            return False
        try:
            self.ping_device_connect()
            return True
        except UserError:
            return False
