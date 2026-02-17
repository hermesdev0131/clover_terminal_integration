from odoo import fields, models


class CloverTransactionLog(models.Model):
    _name = 'clover.transaction.log'
    _description = 'Clover API Transaction Log'
    _order = 'create_date desc'
    _rec_name = 'request_id'

    terminal_id = fields.Many2one(
        'clover.terminal',
        string='Terminal',
        ondelete='set null',
        index=True,
    )
    request_id = fields.Char(string='Request ID', index=True)
    endpoint = fields.Char(string='Endpoint')
    http_method = fields.Char(string='Method')
    http_status = fields.Integer(string='HTTP Status')
    request_payload = fields.Text(string='Request')
    response_payload = fields.Text(string='Response')
    error_message = fields.Text(string='Error')
    status = fields.Selection(
        [('pending', 'Pending'),
         ('success', 'Success'),
         ('error', 'Error'),
         ('timeout', 'Timeout')],
        string='Status',
        default='pending',
        index=True,
    )
    # POS references — populated in later phases
    pos_order_id = fields.Many2one('pos.order', string='POS Order', ondelete='set null')
    pos_session_id = fields.Many2one('pos.session', string='POS Session', ondelete='set null')
    clover_payment_id = fields.Char(string='Clover Payment ID', index=True)
