/** @odoo-module */

import { Component, onMounted, useRef } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";

export class CloverQRScreen extends Component {
    static template = "clover_terminal_integration.CloverQRScreen";
    static components = { Dialog };
    static props = {
        amount: Number,
        orderRef: String,
        qrPayload: { type: String, optional: true },
        manualConfirm: { type: Boolean, optional: true },
        onConfirm: { type: Function, optional: true },
        onCancel: Function,
        close: Function,
    };

    setup() {
        this.qrContainer = useRef("qrContainer");
        onMounted(() => this._renderQR());
    }

    get amountFormatted() {
        return this.props.amount.toFixed(2);
    }

    get hasQrPayload() {
        return !!this.props.qrPayload;
    }

    get isManualConfirm() {
        return !!this.props.manualConfirm;
    }

    confirm() {
        if (this.props.onConfirm) {
            this.props.onConfirm();
        }
    }

    cancel() {
        this.props.onCancel();
    }

    _renderQR() {
        const container = this.qrContainer.el;
        if (!container || !this.props.qrPayload) return;

        const payload = encodeURIComponent(this.props.qrPayload);
        const img = document.createElement("img");
        img.src = `https://api.qrserver.com/v1/create-qr-code/?size=240x240&data=${payload}`;
        img.alt = "QR Code";
        img.style.width = "240px";
        img.style.height = "240px";
        img.className = "border rounded p-2 bg-white";
        container.innerHTML = "";
        container.appendChild(img);
    }
}
