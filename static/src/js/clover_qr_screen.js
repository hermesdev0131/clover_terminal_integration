/** @odoo-module */

import { Component } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";

export class CloverQRScreen extends Component {
    static template = "clover_terminal_integration.CloverQRScreen";
    static components = { Dialog };
    static props = {
        amount: Number,
        orderRef: String,
        qrPayload: { type: String, optional: true },
        onCancel: Function,
        close: Function,
    };

    get amountFormatted() {
        return this.props.amount.toFixed(2);
    }

    get isQrUrl() {
        const p = this.props.qrPayload;
        return p && (p.startsWith("http://") || p.startsWith("https://") || p.startsWith("data:"));
    }

    get hasQrPayload() {
        return !!this.props.qrPayload;
    }

    cancel() {
        this.props.onCancel();
    }
}
