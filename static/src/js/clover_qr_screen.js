/** @odoo-module */

import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { useService } from "@web/core/utils/hooks";

const POLL_INTERVAL_MS = 3000;

const STATUS_LABELS = {
    created: "Initializing...",
    pending: "Waiting for payment...",
    waiting: "Waiting for payment...",
    approved: "Payment approved!",
    rejected: "Payment rejected",
    canceled: "Payment canceled",
    expired: "QR code expired",
    error: "An error occurred",
};

export class CloverQRScreen extends Component {
    static template = "clover_terminal_integration.CloverQRScreen";
    static components = { Dialog };
    static props = {
        transactionId: Number,
        paymentMethodId: Number,
        qrPayload: String,
        amount: Number,
        orderRef: String,
        onComplete: Function,
        close: Function,  // injected by the dialog service
    };

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            status: "pending",
            label: STATUS_LABELS["pending"],
        });
        this._pollInterval = null;

        onMounted(() => this._startPolling());
        onWillUnmount(() => this._stopPolling());
    }

    // ------------------------------------------------------------------
    // Computed
    // ------------------------------------------------------------------

    get amountFormatted() {
        // props.amount is the POS line amount in the session currency (e.g. 12.50)
        return this.props.amount.toFixed(2);
    }

    get isDone() {
        return ["approved", "rejected", "canceled", "expired", "error"].includes(
            this.state.status
        );
    }

    get isApproved() {
        return this.state.status === "approved";
    }

    get isQrUrl() {
        const p = this.props.qrPayload;
        return p && (p.startsWith("http://") || p.startsWith("https://") || p.startsWith("data:"));
    }

    // ------------------------------------------------------------------
    // Polling
    // ------------------------------------------------------------------

    _startPolling() {
        this._pollInterval = setInterval(async () => {
            try {
                const result = await this.orm.call(
                    "pos.payment.method",
                    "clover_get_payment_status",
                    [[this.props.paymentMethodId]],
                    { clover_transaction_id: this.props.transactionId }
                );

                const status = result.state || "pending";
                this.state.status = status;
                this.state.label = STATUS_LABELS[status] || status;

                if (this.isDone) {
                    this._stopPolling();
                    // Brief pause so the cashier sees the final status
                    await this._sleep(1500);
                    this.props.onComplete(this.isApproved);
                    this.props.close();
                }
            } catch (_e) {
                // Network hiccup — keep polling silently
            }
        }, POLL_INTERVAL_MS);
    }

    _stopPolling() {
        if (this._pollInterval) {
            clearInterval(this._pollInterval);
            this._pollInterval = null;
        }
    }

    _sleep(ms) {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }

    // ------------------------------------------------------------------
    // Actions
    // ------------------------------------------------------------------

    cancel() {
        this._stopPolling();
        this.props.onComplete(false);
        this.props.close();
    }
}
