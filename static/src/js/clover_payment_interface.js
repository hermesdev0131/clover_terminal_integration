/** @odoo-module */

import { PaymentInterface } from "@point_of_sale/app/payment/payment_interface";
import { register_payment_method } from "@point_of_sale/app/store/pos_store";
import { _t } from "@web/core/l10n/translation";

const CONNECT_TIMEOUT_MS = 30000;   // 30s to establish WebSocket
const PAYMENT_TIMEOUT_MS = 120000;  // 2 min hard timeout for payment

export class CloverPaymentInterface extends PaymentInterface {

    setup(...args) {
        super.setup(...args);
        this._cancelled = false;
        this._connector = null;
        this._pendingResolve = null;
        this._pendingLine = null;
        this._sdkConfig = null;
        this.enable_reversals();
    }

    // ------------------------------------------------------------------
    // Public PaymentInterface API
    // ------------------------------------------------------------------

    async send_payment_request(uuid) {
        this._cancelled = false;
        await super.send_payment_request(uuid);

        const order = this.pos.get_order();
        const line = order.get_paymentline_by_uuid(uuid);
        line.set_payment_status("waiting");

        try {
            // 1. Get SDK config from Odoo backend
            if (!this._sdkConfig) {
                this._sdkConfig = await this._fetchSdkConfig();
            }
            if (this._sdkConfig.error) {
                this._showError(this._sdkConfig.error);
                this._sdkConfig = null;
                line.set_payment_status("retry");
                return false;
            }

            // 2. Connect to device via WebSocket
            const connector = await this._getConnector();
            if (!connector) {
                this._showError(_t("Could not connect to Clover device."));
                line.set_payment_status("retry");
                return false;
            }

            // 3. Send sale request
            const amountCents = Math.round(line.amount * 100);
            const paymentType = this._paymentType();

            return await this._executeSale(connector, line, order, amountCents, paymentType);

        } catch (e) {
            const msg = e?.message || _t("Clover payment failed.");
            this._showError(msg);
            line.set_payment_status("retry");
            return false;
        }
    }

    async send_payment_cancel(order, uuid) {
        this._cancelled = true;
        if (this._connector) {
            try {
                this._connector.resetDevice();
            } catch (_e) {
                // best-effort
            }
        }
        if (this._pendingResolve) {
            this._pendingResolve({ success: false, cancelled: true });
            this._pendingResolve = null;
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
            const result = await this._rpc("clover_refund_payment",
                [String(line.transaction_id)]);
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
        this._disposeConnector();
    }

    // ------------------------------------------------------------------
    // SDK Connection Management
    // ------------------------------------------------------------------

    async _fetchSdkConfig() {
        return this._rpc("clover_get_sdk_config", []);
    }

    _getConnector() {
        return new Promise((resolve) => {
            if (this._connector && this._connectorReady) {
                resolve(this._connector);
                return;
            }

            this._disposeConnector();

            const sdk = window.clover;
            if (!sdk) {
                console.error("Clover SDK not loaded");
                resolve(null);
                return;
            }

            const cfg = this._sdkConfig;

            const cloudConfig = new sdk.WebSocketCloudCloverDeviceConfigurationBuilder(
                cfg.applicationId,
                cfg.deviceId,
                cfg.merchantId,
                cfg.accessToken,
            )
                .setCloverServer(cfg.cloverServer)
                .setFriendlyId(cfg.friendlyId)
                .build();

            const builderConfig = {};
            builderConfig[sdk.CloverConnectorFactoryBuilder.FACTORY_VERSION] =
                sdk.CloverConnectorFactoryBuilder.VERSION_12;

            const factory = sdk.CloverConnectorFactoryBuilder
                .createICloverConnectorFactory(builderConfig);

            const connector = factory.createICloverConnector(cloudConfig);

            this._connectorReady = false;
            let resolved = false;

            const timeout = setTimeout(() => {
                if (!resolved) {
                    resolved = true;
                    console.error("Clover SDK connection timeout");
                    resolve(null);
                }
            }, CONNECT_TIMEOUT_MS);

            const listener = Object.assign(
                {},
                sdk.remotepay.ICloverConnectorListener.prototype,
                {
                    onDeviceReady: () => {
                        if (!resolved) {
                            resolved = true;
                            clearTimeout(timeout);
                            this._connectorReady = true;
                            this._connector = connector;
                            resolve(connector);
                        }
                    },
                    onDeviceDisconnected: () => {
                        console.warn("Clover device disconnected");
                        this._connectorReady = false;
                        if (!resolved) {
                            resolved = true;
                            clearTimeout(timeout);
                            resolve(null);
                        }
                    },
                    onDeviceConnected: () => {
                        console.log("Clover device connected, waiting for ready...");
                    },
                    onDeviceError: (deviceErrorEvent) => {
                        console.error("Clover device error:", deviceErrorEvent);
                    },
                    onSaleResponse: (response) => {
                        this._handleSaleResponse(response);
                    },
                    onRefundPaymentResponse: (response) => {
                        this._handleRefundResponse(response);
                    },
                    onConfirmPaymentRequest: (request) => {
                        // Auto-accept payment challenges (e.g. duplicate check)
                        connector.acceptPayment(request.getPayment());
                    },
                    onVerifySignatureRequest: (request) => {
                        // Auto-accept signature
                        connector.acceptSignature(request);
                    },
                },
            );

            connector.addCloverConnectorListener(listener);
            connector.initializeConnection();
        });
    }

    _disposeConnector() {
        if (this._connector) {
            try {
                this._connector.dispose();
            } catch (_e) {
                // ignore
            }
            this._connector = null;
            this._connectorReady = false;
        }
    }

    // ------------------------------------------------------------------
    // Sale Execution
    // ------------------------------------------------------------------

    _executeSale(connector, line, order, amountCents, paymentType) {
        return new Promise((resolve) => {
            const sdk = window.clover;

            const saleRequest = new sdk.remotepay.SaleRequest();
            saleRequest.setExternalId(sdk.CloverID.getNewId());
            saleRequest.setAmount(amountCents);

            // Argentina regional extras
            const extras = {};
            extras["currency"] = "ARS";
            saleRequest.setRegionalExtras(extras);

            // Card entry methods
            if (paymentType === "qr") {
                // QR-only: disable card entry methods
                saleRequest.setCardEntryMethods(0);
            } else {
                // Card: allow all entry methods
                saleRequest.setCardEntryMethods(
                    sdk.CardEntryMethods?.DEFAULT || 15,
                );
            }

            this._pendingLine = line;
            this._pendingResolve = resolve;
            this._pendingOrder = order;
            this._pendingPaymentType = paymentType;

            line.set_payment_status("waitingCard");

            // Hard timeout
            this._paymentTimeout = setTimeout(() => {
                if (this._pendingResolve) {
                    this._pendingResolve(false);
                    this._pendingResolve = null;
                    this._pendingLine = null;
                    line.set_payment_status("retry");
                    this._showError(_t("Payment timed out."));
                }
            }, PAYMENT_TIMEOUT_MS);

            connector.sale(saleRequest);
        });
    }

    // ------------------------------------------------------------------
    // SDK Response Handlers
    // ------------------------------------------------------------------

    async _handleSaleResponse(response) {
        clearTimeout(this._paymentTimeout);

        const line = this._pendingLine;
        const resolvePayment = this._pendingResolve;
        const order = this._pendingOrder;
        const paymentType = this._pendingPaymentType;
        this._pendingLine = null;
        this._pendingResolve = null;
        this._pendingOrder = null;
        this._pendingPaymentType = null;

        if (!line || !resolvePayment) return;

        const success = response.getSuccess();
        const payment = response.getPayment();

        if (success && payment) {
            const cloverPaymentId = payment.getId() || "";
            const cardTxn = payment.getCardTransaction?.() || {};
            const cardType = cardTxn.getCardType?.() || "Card";
            const cardLast4 = cardTxn.getLast4?.() || "";

            line.transaction_id = cloverPaymentId;
            line.card_type = cardType;
            if (line.set_receipt_info) {
                line.set_receipt_info(cardType, cardLast4, false);
            }
            line.set_payment_status("done");

            // Log transaction to Odoo backend
            try {
                await this._rpc("clover_log_transaction", [
                    order?.uid || "",
                    paymentType || "card",
                    payment.getAmount?.() || 0,
                    cloverPaymentId,
                    "approved",
                    JSON.stringify(response),
                    cardType,
                    cardLast4,
                    "",
                ]);
            } catch (_e) {
                // non-fatal — payment already succeeded
            }

            resolvePayment(true);
        } else {
            const reason = response.getReason?.() || _t("Payment declined.");
            line.set_payment_status("retry");
            this._showError(reason);

            // Log failed transaction
            try {
                await this._rpc("clover_log_transaction", [
                    order?.uid || "",
                    paymentType || "card",
                    0,
                    "",
                    "rejected",
                    JSON.stringify(response),
                    "",
                    "",
                    reason,
                ]);
            } catch (_e) {
                // non-fatal
            }

            resolvePayment(false);
        }
    }

    _handleRefundResponse(response) {
        if (response.getSuccess()) {
            console.log("Clover refund successful");
        } else {
            console.warn("Clover refund failed:", response.getReason?.());
        }
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    _paymentType() {
        return this.payment_method_id.clover_payment_type || "card";
    }

    _rpc(method, extraArgs = []) {
        return this.env.services.orm.call(
            "pos.payment.method",
            method,
            [[this.payment_method_id.id], ...extraArgs],
        );
    }

    _showError(msg) {
        this.env.services.notification.add(msg, {
            type: "danger",
            sticky: false,
        });
    }
}

register_payment_method("clover", CloverPaymentInterface);
