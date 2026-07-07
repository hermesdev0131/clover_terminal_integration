# Clover Terminal Integration

Odoo 18 module for Clover Flex 4 card payments and Fiserv Transferencias 3.0 QR
payments in Argentina. The QR is displayed on both the Odoo POS screen and the
Clover device screen at the same time — the customer scans whichever is closer.

## Requirements

- Odoo 18 with `point_of_sale` and `account` installed
- Clover Flex 4 with Cloud Pay Display active
- Fiserv QR Estático API access (JWT token from `integraciones_qr@fiserv.com`)

## Install

Copy the module into your Odoo `addons` path, restart Odoo, then in Apps search
for **Clover Terminal Integration** and install.

## Configure

1. Go to **Clover → Terminals → New**
2. Fill in the Clover section: environment, merchant ID, device serial, app ID,
   app secret, RAID
3. Click **Authorize** → complete OAuth in Clover
4. Click **Test Connection** to verify the device is online
5. Fill in the Fiserv QR section: environment (cert/prod), JWT token,
   sucursal ID, caja ID
6. Click **Fetch Static QR** to cache the QR

Then create a payment method (POS → Configuration → Payment Methods):

- **Integrate with:** Clover
- **Clover Terminal:** the terminal you configured
- **Clover Payment Type:** `Card Payment` or `QR Payment`

## Use

Ring up an order in the POS, hit Validate, pick the payment method. Cards are
processed on the terminal. QR appears simultaneously on Odoo and the Clover
device — customer scans either. Odoo confirms the payment within a few seconds
and the other side auto-cancels.

## Refunds

Refund a paid payment line from Odoo. Card refunds go through the Clover SDK.
QR refunds go through the Fiserv API.

## Environments

- **Certification:** `connect-cert.latam.fiservapis.com` — test wallet only, no
  real money
- **Production:** `connect.latam.fiservapis.com` — requires a separate
  production JWT token from Fiserv

## Optional Webhook IP Allowlist

Set the system parameter `clover_terminal_integration.fiserv_webhook_ips` to a
comma-separated list of Fiserv's public IPs to restrict webhook sources.

## License

LGPL-3
