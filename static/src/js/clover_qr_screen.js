/** @odoo-module */

import { Component, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";

export class CloverQRScreen extends Component {
    static template = "clover_terminal_integration.CloverQRScreen";
    static components = { Dialog };
    static props = {
        amount: Number,
        orderRef: String,
        qrPayload: { type: String, optional: true },
        onCancel: Function,
        onUpdateReady: { type: Function, optional: true },
        close: Function,
    };

    setup() {
        this.state = useState({
            qrPayload: this.props.qrPayload || "",
            status: "waiting",
        });

        // Expose update callback so parent can push QR data and status
        if (this.props.onUpdateReady) {
            this.props.onUpdateReady((payload, status) => {
                if (payload) this.state.qrPayload = payload;
                if (status) this.state.status = status;
            });
        }
    }

    get amountFormatted() {
        return this.props.amount.toFixed(2);
    }

    get isQrUrl() {
        const p = this.state.qrPayload;
        return p && (p.startsWith("http://") || p.startsWith("https://") || p.startsWith("data:"));
    }

    get hasQrPayload() {
        return !!this.state.qrPayload;
    }

    cancel() {
        this.props.onCancel();
    }
}
