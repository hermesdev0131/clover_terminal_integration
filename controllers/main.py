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
        oauth_base = terminal._get_oauth_base()
        token_url = f'{oauth_base}/oauth/token'

        try:
            resp = requests.post(token_url, params={
                'client_id': terminal.app_id,
                'client_secret': terminal.app_secret,
                'code': code,
            }, timeout=30)

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
                'http_method': 'POST',
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

        # Redirect back to the terminal form
        return request.redirect(
            f'/odoo/action-clover_terminal_integration.action_clover_terminal'
            f'?id={terminal.id}&view_type=form'
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
