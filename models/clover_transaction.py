from odoo import api, fields, models


class CloverTransaction(models.Model):
    """Business-level payment transaction record (one per payment attempt).

    Distinct from ``clover.transaction.log`` which is a raw API audit trail.
    This model tracks the full lifecycle of a single cashier payment attempt.
    """

    _name = 'clover.transaction'
    _description = 'Clover Payment Transaction'
    _order = 'create_date desc'
    _rec_name = 'idempotency_key'

    # ------------------------------------------------------------------
    # Relations
    # ------------------------------------------------------------------

    terminal_id = fields.Many2one(
        'clover.terminal', required=True, ondelete='restrict', string='Terminal',
        index=True,
    )
    payment_method_id = fields.Many2one(
        'pos.payment.method', ondelete='set null', string='Payment Method',
    )
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
    )
    pos_order_id = fields.Many2one(
        'pos.order', ondelete='set null', string='POS Order',
    )
    pos_config_id = fields.Many2one(
        'pos.config', ondelete='set null', string='POS Config',
    )

    # ------------------------------------------------------------------
    # Amount
    # ------------------------------------------------------------------

    amount = fields.Integer(
        string='Amount (cents)', required=True,
        help='Payment amount expressed in the smallest currency unit (cents).',
    )
    amount_display = fields.Float(
        string='Amount', compute='_compute_amount_display', digits=(10, 2), store=False,
    )
    currency_id = fields.Many2one('res.currency', string='Currency')

    # ------------------------------------------------------------------
    # Type & state
    # ------------------------------------------------------------------

    payment_type = fields.Selection(
        [('card', 'Card'), ('qr', 'QR')],
        required=True, index=True, string='Type',
    )
    state = fields.Selection(
        [('created', 'Created'),
         ('pending', 'Pending'),
         ('approved', 'Approved'),
         ('rejected', 'Rejected'),
         ('canceled', 'Canceled'),
         ('expired', 'Expired'),
         ('error', 'Error')],
        default='created', required=True, index=True, string='State',
    )

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------

    idempotency_key = fields.Char(index=True, string='Idempotency Key')
    attempt_number = fields.Integer(default=1, string='Attempt #')

    # ------------------------------------------------------------------
    # POS references
    # ------------------------------------------------------------------

    pos_order_uid = fields.Char(
        index=True, string='POS Order UID',
        help='Transient UID from POS (available before the order is saved to DB).',
    )

    # ------------------------------------------------------------------
    # Clover references
    # ------------------------------------------------------------------

    clover_order_id = fields.Char(index=True, string='Clover Order ID')
    clover_payment_id = fields.Char(index=True, string='Clover Payment ID')
    qr_payload = fields.Text(string='QR Payload / Code')

    # ------------------------------------------------------------------
    # Payload storage
    # ------------------------------------------------------------------

    raw_response_payload = fields.Text(string='Raw Response')
    error_message = fields.Text(string='Error Message')

    # ------------------------------------------------------------------
    # Computed
    # ------------------------------------------------------------------

    @api.depends('amount')
    def _compute_amount_display(self):
        for rec in self:
            rec.amount_display = rec.amount / 100.0
