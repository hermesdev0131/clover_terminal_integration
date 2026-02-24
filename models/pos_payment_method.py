from odoo import api, fields, models, _
from odoo.exceptions import UserError


class PosPaymentMethod(models.Model):
    _inherit = 'pos.payment.method'

    # ------------------------------------------------------------------
    # Terminal selection
    # ------------------------------------------------------------------

    @api.model
    def _get_payment_terminal_selection(self):
        return super()._get_payment_terminal_selection() + [('clover', 'Clover')]

    clover_terminal_id = fields.Many2one(
        'clover.terminal',
        string='Clover Terminal',
        domain="[('state', 'in', ('testing', 'active'))]",
        help='Select the Clover terminal device to use for payments.',
    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_clover_terminal(self):
        """Return the linked clover.terminal record, or raise."""
        self.ensure_one()
        if self.use_payment_terminal != 'clover':
            raise UserError(_('This payment method is not configured for Clover.'))
        if not self.clover_terminal_id:
            raise UserError(_('No Clover terminal selected on this payment method.'))
        return self.clover_terminal_id
