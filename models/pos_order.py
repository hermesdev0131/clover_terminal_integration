import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class PosOrder(models.Model):
    _inherit = 'pos.order'

    @api.model
    def sync_from_ui(self, orders):
        """Link Clover transactions to the created POS orders after sync."""
        result = super().sync_from_ui(orders)

        try:
            self._link_clover_transactions(result)
        except Exception:
            _logger.exception("Failed to link Clover transactions to POS orders")

        return result

    def _link_clover_transactions(self, result):
        """Best-effort linking — must never break order sync."""
        for order_data in result.get('pos.order', []):
            order_id = order_data.get('id')
            if not order_id:
                continue

            order = self.browse(order_id)
            if not order.exists():
                continue

            for payment in order.payment_ids:
                if (
                    payment.payment_method_id.use_payment_terminal == 'clover'
                    and payment.transaction_id
                ):
                    txs = self.env['clover.transaction'].sudo().search([
                        '|',
                        ('clover_payment_id', '=', payment.transaction_id),
                        ('id', '=', self._safe_int(payment.transaction_id)),
                    ], limit=1)
                    if txs:
                        txs.write({
                            'pos_order_id': order.id,
                            'pos_config_id': order.config_id.id,
                        })

    @staticmethod
    def _safe_int(val):
        """Try to parse val as int; return 0 on failure."""
        try:
            return int(val)
        except (ValueError, TypeError):
            return 0
