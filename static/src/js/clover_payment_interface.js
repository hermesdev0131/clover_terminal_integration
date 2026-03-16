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
        // Don't resolve pending — let onSaleResponse handle it
        // If payment already completed, onSaleResponse will auto-void
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
            console.log("Clover SDK config:", {
                applicationId: cfg.applicationId,
                deviceId: cfg.deviceId,
                deviceSerial: cfg.deviceSerial,
                merchantId: cfg.merchantId,
                cloverServer: cfg.cloverServer,
                hasToken: !!cfg.accessToken,
            });

            // SDK constructor: (applicationId, deviceId, merchantId, accessToken)
            const cloudConfig = new sdk.WebSocketCloudCloverDeviceConfigurationBuilder(
                cfg.applicationId,
                cfg.deviceId,
                cfg.merchantId,
                cfg.accessToken,
            )
                .setCloverServer(cfg.cloverServer)
                .setFriendlyId(cfg.friendlyId)
                .setForceConnect(true)
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
                        this._connector = null;
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
                    onVoidPaymentResponse: (response) => {
                        if (response.getSuccess()) {
                            console.log("Clover void successful");
                        } else {
                            console.warn("Clover void failed:", response.getReason?.());
                        }
                    },
                    onConfirmPaymentRequest: (request) => {
                        connector.acceptPayment(request.getPayment());
                    },
                    onVerifySignatureRequest: (request) => {
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

            // Configure entry methods based on payment type
            if (paymentType === "qr") {
                saleRequest.setPresentQrcOnly(true);
            } else {
                saleRequest.setCardEntryMethods(
                    sdk.CardEntryMethods?.DEFAULT || 15,
                );
            }

            this._pendingLine = line;
            this._pendingResolve = resolve;
            this._pendingOrder = order;
            this._pendingPaymentType = paymentType;

            // Show appropriate status based on payment type
            if (paymentType === "qr") {
                line.set_payment_status("waiting");
            } else {
                line.set_payment_status("waitingCard");
            }

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

        const success = response.getSuccess();
        const payment = response.getPayment();

        if (success && payment) {
            const cloverPaymentId = payment.getId() || "";
            const cardTxn = payment.getCardTransaction?.() || {};
            const cardType = cardTxn.getCardType?.() || "Card";
            const cardLast4 = cardTxn.getLast4?.() || "";

            // If user cancelled but payment completed on device → auto-void
            if (this._cancelled) {
                console.warn("Payment completed after cancel — voiding payment", cloverPaymentId);
                try {
                    const sdk = window.clover;
                    const voidRequest = new sdk.remotepay.VoidPaymentRequest();
                    voidRequest.setPaymentId(cloverPaymentId);
                    voidRequest.setOrderId(payment.getOrder?.()?.getId?.() || "");
                    voidRequest.setVoidReason(sdk.order.VoidReason.USER_CANCEL);
                    this._connector?.voidPayment(voidRequest);
                } catch (_e) {
                    console.error("Auto-void failed:", _e);
                }
                // Log the voided transaction
                this._logTransaction(order, paymentType, 0, cloverPaymentId,
                    "canceled", response, cardType, cardLast4,
                    "Auto-voided: user cancelled during payment");
                if (resolvePayment) resolvePayment(false);
                return;
            }

            if (line) {
                line.transaction_id = cloverPaymentId;
                line.card_type = cardType;
                if (line.set_receipt_info) {
                    line.set_receipt_info(cardType, cardLast4, false);
                }
                line.set_payment_status("done");
            }

            // Log approved transaction
            this._logTransaction(order, paymentType,
                payment.getAmount?.() || 0, cloverPaymentId,
                "approved", response, cardType, cardLast4, "");

            if (resolvePayment) resolvePayment(true);
        } else {
            const reason = response.getReason?.() || response.getMessage?.() ||
                _t("Payment declined.");

            if (line) {
                line.set_payment_status("retry");
            }
            this._showError(reason);

            // Log rejected transaction
            this._logTransaction(order, paymentType, 0, "",
                "rejected", response, "", "", reason);

            if (resolvePayment) resolvePayment(false);
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
    // Transaction Logging
    // ------------------------------------------------------------------

    async _logTransaction(order, paymentType, amount, cloverPaymentId,
                          state, response, cardType, cardLast4, errorMsg) {
        try {
            let rawResponse = "";
            try {
                rawResponse = JSON.stringify(response);
            } catch (_e) {
                rawResponse = String(response);
            }
            await this._rpc("clover_log_transaction", [
                order?.uid || "",
                paymentType || "card",
                amount || 0,
                cloverPaymentId || "",
                state || "error",
                rawResponse,
                cardType || "",
                cardLast4 || "",
                errorMsg || "",
            ]);
        } catch (_e) {
            // non-fatal — don't break payment flow for logging
            console.warn("Failed to log Clover transaction:", _e);
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
