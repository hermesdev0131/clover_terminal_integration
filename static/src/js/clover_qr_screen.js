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

    cancel() {
        this.props.onCancel();
    }

    _renderQR() {
        const container = this.qrContainer.el;
        if (!container || !this.props.qrPayload) return;

        // Use a simple canvas-based QR generator (no external dependencies)
        // Encode the URL into a QR code via an img tag pointing to a QR API
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
