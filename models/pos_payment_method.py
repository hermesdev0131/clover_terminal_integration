import json
import time
import uuid

from odoo import api, fields, models, _
from odoo.exceptions import UserError

# Clover environment → server URL for SDK WebSocket connection
_CLOVER_SDK_SERVERS = {
    'sandbox': 'https://sandbox.dev.clover.com',
    'production_na': 'https://www.clover.com',
    'production_eu': 'https://www.eu.clover.com',
    'production_la': 'https://www.la.clover.com',
}

# Fiserv QR payment order status id → POS-facing state
_FISERV_STATUS_MAP = {
    'P': 'approved',   # Pagado
    'D': 'approved',   # Acreditado
    'A': 'pending',    # Pendiente
    'E': 'expired',    # Expirada
    'R': 'rejected',   # Rechazada
    'C': 'canceled',   # Cancelada
    'V': 'refunded',   # Devuelto
}


class PosPaymentMethod(models.Model):
    _inherit = 'pos.payment.method'

    # ------------------------------------------------------------------
    # Terminal selection
    # ------------------------------------------------------------------

    def _get_payment_terminal_selection(self):
        return super()._get_payment_terminal_selection() + [('clover', 'Clover')]

    clover_terminal_id = fields.Many2one(
        'clover.terminal',
        string='Clover Terminal',
        domain="[('state', 'in', ('testing', 'active'))]",
        help='Select the Clover terminal device to use for payments.',
    )

    clover_payment_type = fields.Selection(
        [('card', 'Card Payment'),
         ('qr', 'QR Payment')],
        string='Clover Payment Type',
        default='card',
        help='Card payments via Clover terminal, or QR shown on both the '
             'Odoo screen and the Clover device (customer scans either).',
    )

    # ------------------------------------------------------------------
    # POS data loading — expose custom fields to the frontend
    # ------------------------------------------------------------------

    @api.model
    def _load_pos_data_fields(self, config_id):
        fields = super()._load_pos_data_fields(config_id)
        fields += ['clover_terminal_id', 'clover_payment_type']
        return fields

    # ------------------------------------------------------------------
    # Internal helper
    # ------------------------------------------------------------------

    def _get_clover_terminal(self):
        """Return the linked clover.terminal record, or raise."""
        self.ensure_one()
        if self.use_payment_terminal != 'clover':
            raise UserError(_('This payment method is not configured for Clover.'))
        if not self.clover_terminal_id:
            raise UserError(_('No Clover terminal selected on this payment method.'))
        return self.clover_terminal_id

    # ------------------------------------------------------------------
    # RPC endpoints called from POS JS
    # ------------------------------------------------------------------

    def clover_get_sdk_config(self):
        """Return Clover SDK configuration for the JS frontend.

        The JS CloverConnector needs these to establish a WebSocket
        connection to the device via Clover's cloud.
        """
        self.ensure_one()
        terminal = self._get_clover_terminal()
        if not terminal.api_token:
            return {'error': _('No API token. Click Authorize on the terminal first.')}
        if not terminal.clover_device_id:
            return {'error': _('No device ID. Click Test Connection on the terminal first.')}
        return {
            'accessToken': terminal.api_token,
            'merchantId': terminal.merchant_id,
            'deviceId': terminal.clover_device_id,
            'deviceSerial': terminal.device_serial,
            'applicationId': terminal.raid,
            'cloverServer': _CLOVER_SDK_SERVERS.get(terminal.environment, ''),
            'friendlyId': f'odoo-pos-{self.env.company.id}',
        }

    def clover_log_transaction(self, order_uid, payment_type, amount_cents,
                               clover_payment_id, state, raw_response,
                               card_type='', card_last4='', error_message=''):
        """Log a completed transaction from the JS SDK.

        Called after the SDK's onSaleResponse / onRefundPaymentResponse.
        Returns ``{'transaction_id': <int>}``.
        """
        self.ensure_one()
        terminal = self._get_clover_terminal()
        Transaction = self.env['clover.transaction'].sudo()

        tx = Transaction.create({
            'terminal_id': terminal.id,
            'payment_method_id': self.id,
            'company_id': self.env.company.id,
            'amount': amount_cents,
            'payment_type': payment_type,
            'state': state,
            'clover_payment_id': clover_payment_id or '',
            'pos_order_uid': order_uid,
            'raw_response_payload': raw_response or '',
            'error_message': error_message or '',
            'attempt_number': 1,
            'idempotency_key': f'{order_uid}_{payment_type}_{int(time.time())}',
        })
        return {'transaction_id': tx.id}

    # ------------------------------------------------------------------
    # Fiserv QR RPC endpoints (QR shown on Odoo + Clover device screen)
    # ------------------------------------------------------------------

    def _fiserv_webhook_url(self):
        """Public webhook URL for Fiserv payment notifications."""
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        return f'{base}/odoo/fiserv/qr/webhook'

    def fiserv_create_qr_payment(self, order_uid, amount):
        """Create a Fiserv payment order and return the static QR to display.

        :param amount: amount as float (ARS)
        Returns {qr_string, order_uuid, reference} or {error}.
        """
        self.ensure_one()
        terminal = self._get_clover_terminal()
        try:
            qr_string = terminal.fiserv_qr_string or terminal._fiserv_fetch_qr()
            reference = f'{order_uid}-{uuid.uuid4().hex[:12]}'
            order_uuid = terminal._fiserv_create_payment_order(
                amount, reference,
                notification_url=self._fiserv_webhook_url(),
            )
            # Audit row so the webhook can resolve the order later
            self.env['clover.transaction'].sudo().create({
                'terminal_id': terminal.id,
                'payment_method_id': self.id,
                'company_id': self.env.company.id,
                'amount': int(round(amount * 100)),
                'payment_type': 'qr',
                'state': 'pending',
                'clover_payment_id': order_uuid,
                'pos_order_uid': order_uid,
                'idempotency_key': reference,
                'attempt_number': 1,
            })
            return {
                'qr_string': qr_string,
                'order_uuid': order_uuid,
                'reference': reference,
            }
        except Exception as exc:
            return {'error': str(exc)}

    def fiserv_poll_qr_payment(self, order_uuid):
        """Poll a Fiserv payment order status.

        Returns {state, order_uuid} where state is
        pending|approved|rejected|expired|canceled|refunded|error.
        """
        self.ensure_one()
        terminal = self._get_clover_terminal()
        try:
            status = terminal._fiserv_get_order_status(order_uuid)
            status_id = (status.get('status') or {}).get('id', '')
            state = _FISERV_STATUS_MAP.get(status_id, 'pending')
            # Keep the audit row in sync on terminal states
            if state in ('approved', 'rejected', 'canceled', 'expired'):
                self._fiserv_sync_transaction(order_uuid, state)
            return {'state': state, 'order_uuid': order_uuid}
        except Exception as exc:
            return {'state': 'error', 'error': str(exc)}

    def fiserv_cancel_qr_payment(self, order_uuid, reference=''):
        """Cancel/expire a pending Fiserv payment order."""
        self.ensure_one()
        terminal = self._get_clover_terminal()
        try:
            terminal._fiserv_cancel_order(order_uuid, reference)
        except Exception:
            pass
        self._fiserv_sync_transaction(order_uuid, 'canceled')
        return {'success': True}

    def _fiserv_sync_transaction(self, order_uuid, state):
        """Update the matching clover.transaction audit row to ``state``."""
        tx = self.env['clover.transaction'].sudo().search(
            [('clover_payment_id', '=', order_uuid)], limit=1)
        if tx and tx.state != state:
            tx.write({'state': state})

    def fiserv_refund_qr_payment(self, order_uuid, amount=None, reason='Refund'):
        """Refund a paid Fiserv QR transaction."""
        self.ensure_one()
        terminal = self._get_clover_terminal()
        try:
            if amount is None:
                return {'error': _('Refund amount is required.')}
            terminal._fiserv_refund(order_uuid, amount, reason)
            return {'success': True}
        except Exception as exc:
            return {'error': str(exc)}
