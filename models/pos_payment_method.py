import json

from odoo import api, fields, models, _
from odoo.exceptions import UserError

# Clover environment → server URL for SDK WebSocket connection
_CLOVER_SDK_SERVERS = {
    'sandbox': 'https://sandbox.dev.clover.com',
    'production_na': 'https://www.clover.com',
    'production_eu': 'https://www.eu.clover.com',
    'production_la': 'https://www.la.clover.com',
}

# Clover payment `result` field → our transaction state
_CLOVER_RESULT_MAP = {
    'SUCCESS': 'approved',
    'AUTH': 'approved',
    'OFFLINE_SUCCESS': 'approved',
    'FAIL': 'rejected',
    'VOIDED': 'canceled',
    'TIMEDOUT': 'expired',
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
        [('card', 'Card Payment'), ('qr', 'QR Payment')],
        string='Clover Payment Type',
        default='card',
        help='Whether this method triggers a card or QR payment on the terminal.',
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
            'idempotency_key': f'{order_uid}_{payment_type}',
        })
        return {'transaction_id': tx.id}

    def clover_cancel_payment(self, clover_transaction_id):
        """Cancel a pending Clover payment."""
        self.ensure_one()
        tx = self.env['clover.transaction'].sudo().browse(clover_transaction_id)
        if not tx.exists() or tx.state in ('approved', 'canceled'):
            return {'success': True}
        tx.write({'state': 'canceled'})
        return {'success': True}

    def clover_refund_payment(self, clover_payment_id, amount_cents=None):
        """Refund an approved Clover payment via REST v3.

        Called from CloverPaymentInterface.send_payment_reversal().
        If ``amount_cents`` is None, performs a full refund.
        Returns ``{'success': True}`` or ``{'error': <msg>}``.
        """
        self.ensure_one()
        terminal = self._get_clover_terminal()
        try:
            terminal._payment_refund(clover_payment_id, amount_cents)
            return {'success': True}
        except Exception as exc:
            return {'error': str(exc)}
