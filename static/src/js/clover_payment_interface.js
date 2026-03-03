/** @odoo-module */

import { PaymentInterface } from "@point_of_sale/app/payment/payment_interface";
import { registry } from "@web/core/registry";
import { CloverQRScreen } from "./clover_qr_screen";

const POLL_INTERVAL_MS = 3000;      // poll Clover status every 3 s
const PAYMENT_TIMEOUT_MS = 120000;  // hard timeout after 2 min

export class CloverPaymentInterface extends PaymentInterface {

    setup(...args) {
        super.setup(...args);
    }

    // ------------------------------------------------------------------
    // Public PaymentInterface API
    // ------------------------------------------------------------------

    async send_payment_request(cid) {
        await super.send_payment_request(cid);

        if (this._paymentType() === "qr") {
            return this._sendQRPaymentRequest(cid);
        }
        return this._sendCardPaymentRequest(cid);
    }

    async send_payment_cancel(order, cid) {
        const line = order.get_paymentline(cid);
        if (line?.transaction_id) {
            try {
                await this._rpc("clover_cancel_payment", {
                    clover_transaction_id: line.transaction_id,
                });
            } catch (_e) {
                // best-effort — terminal may have already settled
            }
        }
        return super.send_payment_cancel(order, cid);
    }

    // ------------------------------------------------------------------
    // Card payment flow
    // ------------------------------------------------------------------

    async _sendCardPaymentRequest(cid) {
        const order = this.pos.get_order();
        const line = order.get_paymentline(cid);

        line.set_payment_status("waiting");

        const amountCents = Math.round(line.amount * 100);

        let result;
        try {
            result = await this._rpc("clover_create_payment", {
                amount_cents: amountCents,
                order_uid: order.uid,
                payment_type: "card",
            });
        } catch (_e) {
            line.set_payment_status("retry");
            return false;
        }

        if (!result || result.error) {
            line.set_payment_status("retry");
            return false;
        }

        line.transaction_id = result.clover_transaction_id;
        line.set_payment_status("waitingCard");

        return this._pollUntilResolved(line, result.clover_transaction_id);
    }

    // ------------------------------------------------------------------
    // QR payment flow
    // ------------------------------------------------------------------

    async _sendQRPaymentRequest(cid) {
        const order = this.pos.get_order();
        const line = order.get_paymentline(cid);

        line.set_payment_status("waiting");

        const amountCents = Math.round(line.amount * 100);

        let result;
        try {
            result = await this._rpc("clover_create_payment", {
                amount_cents: amountCents,
                order_uid: order.uid,
                payment_type: "qr",
            });
        } catch (_e) {
            line.set_payment_status("retry");
            return false;
        }

        if (!result || result.error) {
            line.set_payment_status("retry");
            return false;
        }

        line.transaction_id = result.clover_transaction_id;

        // Open QR dialog — it handles its own polling and resolves when done
        const approved = await new Promise((resolve) => {
            this.env.services.dialog.add(CloverQRScreen, {
                transactionId: result.clover_transaction_id,
                paymentMethodId: this.payment_method.id,
                qrPayload: result.qr_payload || "",
                amount: line.amount,
                orderRef: order.uid,
                onComplete: (ok) => resolve(ok),
            });
        });

        if (approved) {
            line.set_payment_status("done");
            return true;
        }
        line.set_payment_status("retry");
        return false;
    }

    // ------------------------------------------------------------------
    // Card polling loop
    // ------------------------------------------------------------------

    async _pollUntilResolved(line, cloverTransactionId) {
        const deadline = Date.now() + PAYMENT_TIMEOUT_MS;

        while (Date.now() < deadline) {
            await this._sleep(POLL_INTERVAL_MS);

            // Cashier hit Cancel from the POS UI
            if (line.payment_status === "retry") {
                return false;
            }

            let status;
            try {
                status = await this._rpc("clover_get_payment_status", {
                    clover_transaction_id: cloverTransactionId,
                });
            } catch (_e) {
                continue; // network hiccup — keep polling
            }

            switch (status.state) {
                case "approved":
                    line.transaction_id = status.clover_payment_id || cloverTransactionId;
                    line.set_payment_status("done");
                    return true;
                case "rejected":
                case "canceled":
                case "expired":
                case "error":
                    line.set_payment_status("retry");
                    return false;
                // "created" / "pending" → keep polling
            }
        }

        // 2-minute hard timeout
        line.set_payment_status("retry");
        return false;
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    _paymentType() {
        return this.payment_method.clover_payment_type || "card";
    }

    _rpc(method, kwargs = {}) {
        return this.env.services.orm.call(
            "pos.payment.method",
            method,
            [[this.payment_method.id]],
            kwargs
        );
    }

    _sleep(ms) {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }
}

registry.category("pos_payment_methods").add("clover", CloverPaymentInterface);
