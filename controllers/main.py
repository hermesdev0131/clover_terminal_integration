import json
import logging

import requests

from odoo import http, _
from odoo.http import request

_logger = logging.getLogger(__name__)


class CloverOAuthController(http.Controller):
    """Handle Clover OAuth callback and exchange code for access token."""

    @http.route('/odoo/clover/oauth/callback', type='http', auth='user',
                website=False, csrf=False)
    def oauth_callback(self, code=None, merchant_id=None, **kw):
        """Clover redirects here after merchant authorizes the app.

        Query params from Clover:
            code         – authorization code (one-time use)
            merchant_id  – merchant that authorized
        """
        if not code or not merchant_id:
            return request.redirect(
                '/odoo/clover/oauth/error?msg=Missing+code+or+merchant_id'
            )

        # Find the terminal record matching this merchant
        terminal = request.env['clover.terminal'].sudo().search([
            ('merchant_id', '=', merchant_id),
            ('company_id', '=', request.env.company.id),
        ], limit=1)

        if not terminal:
            return request.redirect(
                '/odoo/clover/oauth/error?msg=No+terminal+found+for+merchant+'
                + merchant_id
            )

        # Exchange authorization code for access token
        # Clover v1 OAuth uses GET with query params
        oauth_base = terminal._get_oauth_base()
        token_url = f'{oauth_base}/oauth/token'

        try:
            resp = requests.get(token_url, params={
                'client_id': terminal.app_id,
                'client_secret': terminal.app_secret,
                'code': code,
            }, timeout=30, allow_redirects=True)

            if resp.status_code != 200:
                _logger.error(
                    'Clover OAuth token exchange failed: %s %s',
                    resp.status_code, resp.text[:500],
                )
                return request.redirect(
                    '/odoo/clover/oauth/error?msg=Token+exchange+failed:+'
                    + str(resp.status_code)
                )

            data = resp.json()
            access_token = data.get('access_token')

            if not access_token:
                _logger.error('Clover OAuth response missing access_token: %s',
                              json.dumps(data))
                return request.redirect(
                    '/odoo/clover/oauth/error?msg=No+access_token+in+response'
                )

            # Store token on the terminal record
            terminal.write({'api_token': access_token})
            _logger.info(
                'Clover OAuth token acquired for terminal %s (merchant %s)',
                terminal.id, merchant_id,
            )

            # Log the token exchange in transaction log
            request.env['clover.transaction.log'].sudo().create({
                'terminal_id': terminal.id,
                'request_id': 'oauth-token-exchange',
                'endpoint': '/oauth/token',
                'http_method': 'GET',
                'http_status': 200,
                'request_payload': json.dumps({
                    'client_id': terminal.app_id,
                    'code': code[:8] + '...',
                }),
                'response_payload': json.dumps({'access_token': '***acquired***'}),
                'status': 'success',
            })

        except requests.exceptions.RequestException as exc:
            _logger.exception('Clover OAuth token exchange error')
            return request.redirect(
                '/odoo/clover/oauth/error?msg=' + str(exc)[:200]
            )

        # Redirect back to the terminal form view
        return request.redirect(
            f'/web#id={terminal.id}&model=clover.terminal&view_type=form'
        )

    @http.route('/odoo/clover/oauth/error', type='http', auth='user',
                website=False, csrf=False)
    def oauth_error(self, msg='Unknown error', **kw):
        """Display a simple error page for OAuth failures."""
        return (
            '<html><body style="font-family:sans-serif;padding:40px;">'
            '<h2>Clover OAuth Error</h2>'
            f'<p style="color:red;">{msg}</p>'
            '<p><a href="/odoo">Back to Odoo</a></p>'
            '</body></html>'
        )


class FiservQRWebhookController(http.Controller):
    """Receive Fiserv QR payment notifications.

    The POS frontend drives the UI via polling; this webhook updates the
    backend clover.transaction record for audit and reconciliation, and
    serves as a fallback confirmation channel.
    """

    @http.route('/odoo/fiserv/qr/webhook', type='http', auth='public',
                methods=['POST'], website=False, csrf=False)
    def fiserv_qr_webhook(self, **kw):
        """Fiserv POSTs payment notifications here.

        Body contains the payment order UUID and status. We match it to a
        pending clover.transaction and update its state.
        """
        # Optional source IP allowlist. Set the system parameter
        # 'clover_terminal_integration.fiserv_webhook_ips' to a comma-separated
        # list of Fiserv's public IPs (they provide these). If unset, all
        # sources are accepted (so the integration works before IPs are known).
        allowed = request.env['ir.config_parameter'].sudo().get_param(
            'clover_terminal_integration.fiserv_webhook_ips', '')
        if allowed.strip():
            allowed_ips = {ip.strip() for ip in allowed.split(',') if ip.strip()}
            remote_ip = request.httprequest.remote_addr
            if remote_ip not in allowed_ips:
                _logger.warning(
                    'Fiserv webhook rejected from unlisted IP: %s', remote_ip)
                return request.make_json_response({'received': False}, status=403)

        try:
            raw = request.httprequest.get_data(as_text=True)
            data = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            _logger.warning('Fiserv webhook: invalid JSON body')
            return request.make_json_response({'received': False}, status=400)

        order_uuid = (
            data.get('uuid')
            or data.get('paymentOrderUUID')
            or data.get('id')
            or ''
        )
        _logger.info('Fiserv QR webhook received: %s', json.dumps(data, default=str))

        if not order_uuid:
            return request.make_json_response({'received': True})

        tx = request.env['clover.transaction'].sudo().search(
            [('clover_payment_id', '=', order_uuid)], limit=1)
        if tx:
            # Map Fiserv status to our state when present
            status = data.get('status')
            status_id = status.get('id') if isinstance(status, dict) else status
            state_map = {
                'P': 'approved', 'D': 'approved', 'A': 'pending',
                'E': 'expired', 'R': 'rejected', 'C': 'canceled', 'V': 'canceled',
            }
            new_state = state_map.get(status_id, 'approved')
            tx.write({
                'state': new_state,
                'raw_response_payload': json.dumps(data, default=str),
            })

        return request.make_json_response({'received': True})
