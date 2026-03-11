import json

from odoo import api, fields, models, _
from odoo.exceptions import UserError

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

    def clover_create_payment(self, amount_cents, order_uid, payment_type):
        """Initiate a payment on the Clover terminal.

        Called from CloverPaymentInterface in the POS JS.
        Returns ``{'clover_transaction_id': <int>, 'qr_payload': <str>}``
        or ``{'error': <msg>}`` on failure.
        """
        self.ensure_one()
        terminal = self._get_clover_terminal()
        Transaction = self.env['clover.transaction'].sudo()

        # Guard: one approved payment per order per payment method
        if Transaction.search_count([
            ('pos_order_uid', '=', order_uid),
            ('payment_method_id', '=', self.id),
            ('state', '=', 'approved'),
        ]):
            return {'error': _('This order already has an approved payment.')}

        # Determine attempt number
        last = Transaction.search(
            [('pos_order_uid', '=', order_uid), ('payment_method_id', '=', self.id)],
            order='attempt_number desc', limit=1,
        )
        attempt = (last.attempt_number + 1) if last else 1
        idempotency_key = f'{order_uid}_{attempt}_{payment_type}'

        tx = Transaction.create({
            'terminal_id': terminal.id,
            'payment_method_id': self.id,
            'company_id': self.env.company.id,
            'amount': amount_cents,
            'payment_type': payment_type,
            'state': 'created',
            'idempotency_key': idempotency_key,
            'attempt_number': attempt,
            'pos_order_uid': order_uid,
        })

        try:
            clover_order_id = terminal._payment_create_clover_order(amount_cents, order_uid)
            tx.write({'clover_order_id': clover_order_id, 'state': 'pending'})

            qr_payload = ''
            if payment_type == 'qr':
                clover_payment_id, qr_payload = terminal._payment_send_qr(
                    clover_order_id, amount_cents, idempotency_key,
                )
            else:
                clover_payment_id = terminal._payment_send_card(
                    clover_order_id, amount_cents, idempotency_key,
                )

            tx.write({'clover_payment_id': clover_payment_id, 'qr_payload': qr_payload})
            return {'clover_transaction_id': tx.id, 'qr_payload': qr_payload}

        except Exception as exc:
            tx.write({'state': 'error', 'error_message': str(exc)})
            return {'error': str(exc)}

    def clover_get_payment_status(self, clover_transaction_id):
        """Poll current status of an in-progress Clover payment.

        Called by the polling loop in CloverPaymentInterface (card) and
        CloverQRScreen (QR).  Returns ``{'state': <str>, 'clover_payment_id': <str>}``.
        """
        self.ensure_one()
        tx = self.env['clover.transaction'].sudo().browse(clover_transaction_id)
        if not tx.exists():
            return {'state': 'error', 'clover_payment_id': ''}

        # Already terminal — no need to query Clover again
        if tx.state in ('approved', 'rejected', 'canceled', 'expired', 'error'):
            card_info = self._extract_card_info(tx)
            return {
                'state': tx.state,
                'clover_payment_id': tx.clover_payment_id or '',
                **card_info,
            }

        terminal = self._get_clover_terminal()
        try:
            new_state = 'pending'
            resolved_payment_id = tx.clover_payment_id or ''

            if tx.payment_type == 'qr' and not tx.clover_payment_id:
                # QR async: poll the Clover order for any settled payment
                if tx.clover_order_id:
                    for pmt in terminal._payment_get_order_payments(tx.clover_order_id):
                        mapped = _CLOVER_RESULT_MAP.get(pmt.get('result', ''), '')
                        if mapped:
                            new_state = mapped
                            resolved_payment_id = pmt.get('id', '')
                            tx.write({'raw_response_payload': json.dumps(pmt)})
                            break
            elif tx.clover_payment_id:
                # Card (or QR with a payment ID): poll the specific payment
                clover_pmt = terminal._payment_get_status(tx.clover_payment_id)
                new_state = _CLOVER_RESULT_MAP.get(clover_pmt.get('result', ''), 'pending')
                tx.write({'raw_response_payload': json.dumps(clover_pmt)})

            if new_state != 'pending':
                tx.write({'state': new_state, 'clover_payment_id': resolved_payment_id})

            card_info = self._extract_card_info(tx) if new_state == 'approved' else {}
            return {'state': new_state, 'clover_payment_id': resolved_payment_id, **card_info}

        except Exception:
            # Don't surface polling errors — keep the JS retrying
            return {'state': 'pending', 'clover_payment_id': tx.clover_payment_id or ''}

    def _extract_card_info(self, tx):
        """Extract card type and last 4 digits from the stored Clover response."""
        try:
            if tx.raw_response_payload:
                data = json.loads(tx.raw_response_payload)
                card_txn = data.get('cardTransaction', {})
                return {
                    'card_type': card_txn.get('cardType', ''),
                    'card_last4': card_txn.get('last4', ''),
                }
        except (json.JSONDecodeError, AttributeError):
            pass
        return {'card_type': '', 'card_last4': ''}

    def clover_cancel_payment(self, clover_transaction_id):
        """Cancel a pending Clover payment.

        Called from CloverPaymentInterface.send_payment_cancel() or
        CloverQRScreen cancel button.
        """
        self.ensure_one()
        tx = self.env['clover.transaction'].sudo().browse(clover_transaction_id)
        if not tx.exists() or tx.state in ('approved', 'canceled'):
            return {'success': True}

        terminal = self._get_clover_terminal()
        terminal._payment_cancel_on_terminal()
        tx.write({'state': 'canceled'})
        return {'success': True}

    def clover_refund_payment(self, clover_payment_id, amount_cents=None):
        """Refund an approved Clover payment.

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
