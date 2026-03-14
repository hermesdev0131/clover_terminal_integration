/** @odoo-module */

import { PaymentInterface } from "@point_of_sale/app/payment/payment_interface";
import { register_payment_method } from "@point_of_sale/app/store/pos_store";
import { _t } from "@web/core/l10n/translation";
import { CloverQRScreen } from "./clover_qr_screen";

const POLL_INTERVAL_MS = 3000;      // poll Clover status every 3 s
const PAYMENT_TIMEOUT_MS = 120000;  // hard timeout after 2 min

export class CloverPaymentInterface extends PaymentInterface {

    setup(...args) {
        super.setup(...args);
        this._cancelled = false;
        this.enable_reversals();
    }

    // ------------------------------------------------------------------
    // Public PaymentInterface API
    // ------------------------------------------------------------------

    async send_payment_request(uuid) {
        this._cancelled = false;
        await super.send_payment_request(uuid);

        if (this._paymentType() === "qr") {
            return this._sendQRPaymentRequest(uuid);
        }
        return this._sendCardPaymentRequest(uuid);
    }

    async send_payment_cancel(order, uuid) {
        this._cancelled = true;
        const line = order.get_paymentline_by_uuid(uuid);
        if (line?.transaction_id) {
            try {
                await this._rpc("clover_cancel_payment", {
                    clover_transaction_id: line.transaction_id,
                });
            } catch (_e) {
                // best-effort — terminal may have already settled
            }
        }
        return super.send_payment_cancel(order, uuid);
    }

    async send_payment_reversal(uuid) {
        const order = this.pos.get_order();
        const line = order.get_paymentline_by_uuid(uuid);
        if (!line?.transaction_id) {
            return false;
        }
        try {
            const result = await this._rpc("clover_refund_payment", {
                clover_payment_id: String(line.transaction_id),
            });
            if (result?.error) {
                this._showError(result.error);
                return false;
            }
            return true;
        } catch (_e) {
            this._showError(_t("Could not process refund. Check connection."));
            return false;
        }
    }

    close() {
        this._cancelled = true;
    }

    // ------------------------------------------------------------------
    // Card payment flow
    // ------------------------------------------------------------------

    async _sendCardPaymentRequest(uuid) {
        const order = this.pos.get_order();
        const line = order.get_paymentline_by_uuid(uuid);

        line.set_payment_status("waiting");

        const amountCents = Math.round(line.amount * 100);

        let result;
        try {
            result = await this._rpc("clover_create_payment", {
                amount_cents: amountCents,
                order_uid: order.uid,
                payment_type: "card",
            });
        } catch (e) {
            const msg = e?.data?.message || e?.message || _t("Could not reach Clover. Check device/network.");
            this._showError(msg);
            line.set_payment_status("retry");
            return false;
        }

        if (!result || result.error) {
            this._showError(result?.error || _t("Clover payment failed."));
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

    async _sendQRPaymentRequest(uuid) {
        const order = this.pos.get_order();
        const line = order.get_paymentline_by_uuid(uuid);

        line.set_payment_status("waiting");

        const amountCents = Math.round(line.amount * 100);

        let result;
        try {
            result = await this._rpc("clover_create_payment", {
                amount_cents: amountCents,
                order_uid: order.uid,
                payment_type: "qr",
            });
        } catch (e) {
            const msg = e?.data?.message || e?.message || _t("Could not reach Clover. Check device/network.");
            this._showError(msg);
            line.set_payment_status("retry");
            return false;
        }

        if (!result || result.error) {
            this._showError(result?.error || _t("Clover QR payment failed."));
            line.set_payment_status("retry");
            return false;
        }

        line.transaction_id = result.clover_transaction_id;

        // Open QR dialog — it handles its own polling and resolves when done
        const approved = await new Promise((resolve) => {
            this.env.services.dialog.add(CloverQRScreen, {
                transactionId: result.clover_transaction_id,
                paymentMethodId: this.payment_method_id.id,
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

            // Cashier hit Cancel or left the payment screen
            if (this._cancelled || line.payment_status === "retry") {
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
                    line.card_type = status.card_type || "Clover";
                    if (line.set_receipt_info) {
                        line.set_receipt_info(
                            status.card_type || "Card",
                            status.card_last4 || "",
                            false,
                        );
                    }
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
        return this.payment_method_id.clover_payment_type || "card";
    }

    _rpc(method, kwargs = {}) {
        return this.env.services.orm.call(
            "pos.payment.method",
            method,
            [[this.payment_method_id.id]],
            { kwargs },
        );
    }

    _showError(msg) {
        this.env.services.notification.add(msg, {
            type: "danger",
            sticky: false,
        });
    }

    _sleep(ms) {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }
}

register_payment_method("clover", CloverPaymentInterface);
