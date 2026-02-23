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
    },
    'production_na': {
        'api_base': 'https://api.clover.com',
        'web_base': 'https://www.clover.com',
    },
    'production_eu': {
        'api_base': 'https://api.eu.clover.com',
        'web_base': 'https://www.eu.clover.com',
    },
    'production_la': {
        'api_base': 'https://api.la.clover.com',
        'web_base': 'https://www.la.clover.com',
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
        help='Clover Merchant ID (from dashboard URL)',
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
    api_token = fields.Char(
        string='API Access Token',
        required=True,
        groups='point_of_sale.group_pos_manager',
        help='OAuth access token from Clover Developer Dashboard',
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
    # URL / header helpers
    # ------------------------------------------------------------------

    def _get_api_base(self):
        self.ensure_one()
        return CLOVER_ENV[self.environment]['api_base']

    def _get_headers(self):
        self.ensure_one()
        return {
            'Authorization': f'Bearer {self.api_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

    # ------------------------------------------------------------------
    # Core API caller  (all Clover HTTP traffic goes through here)
    # ------------------------------------------------------------------

    def _api_request(self, method, endpoint, payload=None, timeout=30):
        """
        Authenticated request to Clover API.
        Every call is written to clover.transaction.log for auditing.

        Returns parsed JSON on success.
        Raises UserError on any failure.
        """
        self.ensure_one()
        url = f'{self._get_api_base()}{endpoint}'
        request_id = uuid.uuid4().hex[:16]

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
                headers=self._get_headers(),
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
        """Fetch merchant info + resolve device by serial number."""
        self.ensure_one()
        try:
            # 1) Verify merchant
            merchant = self._api_request(
                'GET', f'/v3/merchants/{self.merchant_id}',
            )
            merchant_name = merchant.get('name', '?')

            # 2) Find device by serial number → get UUID
            device = self._resolve_device_by_serial()
            clover_device_id = device.get('id')
            device_model = device.get('productName', device.get('model', ''))

            self.write({
                'state': 'testing',
                'last_ping': fields.Datetime.now(),
                'last_error': False,
                'merchant_name': merchant_name,
                'clover_device_id': clover_device_id,
                'device_model': device_model,
            })
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Connection OK'),
                    'message': _(
                        'Merchant: %(merchant)s — Device: %(serial)s (%(model)s)',
                        merchant=merchant_name,
                        serial=self.device_serial,
                        model=device_model or 'Flex',
                    ),
                    'type': 'success',
                    'sticky': False,
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
    # Device status check  (can be called from frontend later)
    # ------------------------------------------------------------------

    def check_device_online(self):
        """Return True/False whether device responds."""
        self.ensure_one()
        if not self.clover_device_id:
            return False
        try:
            self._api_request(
                'GET',
                f'/v3/merchants/{self.merchant_id}/devices/{self.clover_device_id}',
                timeout=10,
            )
            self.last_ping = fields.Datetime.now()
            return True
        except UserError:
            return False
