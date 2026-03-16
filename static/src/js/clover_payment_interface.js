/** @odoo-module */

import { PaymentInterface } from "@point_of_sale/app/payment/payment_interface";
import { register_payment_method } from "@point_of_sale/app/store/pos_store";
import { _t } from "@web/core/l10n/translation";
import { CloverQRScreen } from "./clover_qr_screen";

const CONNECT_TIMEOUT_MS = 30000;   // 30s to establish WebSocket
const PAYMENT_TIMEOUT_MS = 120000;  // 2 min hard timeout for payment
const QR_POLL_INTERVAL_MS = 3000;   // 3s between QR status polls

export class CloverPaymentInterface extends PaymentInterface {

    setup(...args) {
        super.setup(...args);
        this._cancelled = false;
        this._connector = null;
        this._connectorReady = false;
        this._pendingResolve = null;
        this._pendingLine = null;
        this._sdkConfig = null;
        this._qrDialogClose = null;
        this._pendingSaleRequest = null;
        this._retried = false;
        this._qrPollTimer = null;
        this._qrOrderId = null;
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

        const paymentType = this._paymentType();
        console.log(`[Clover] === Payment request: ${paymentType}, amount: ${line.amount}, uuid: ${uuid} ===`);

        try {
            // QR payments use REST API (returns QR code data for display)
            if (paymentType === "qr") {
                return await this._executeQRPayment(line, order);
            }

            // Card payments use SDK WebSocket
            if (!this._sdkConfig) {
                console.log("[Clover] Fetching SDK config...");
                this._sdkConfig = await this._fetchSdkConfig();
            }
            if (this._sdkConfig.error) {
                this._showError(this._sdkConfig.error);
                this._sdkConfig = null;
                line.set_payment_status("retry");
                return false;
            }

            console.log("[Clover] Getting connector...", {
                hasConnector: !!this._connector,
                connectorReady: this._connectorReady,
            });
            const connector = await this._getConnector();
            if (!connector) {
                this._showError(_t("Could not connect to Clover device."));
                line.set_payment_status("retry");
                return false;
            }

            const amountCents = Math.round(line.amount * 100);
            console.log(`[Clover] Connector ready, executing card sale: ${amountCents} cents`);
            return await this._executeSale(connector, line, order, amountCents);

        } catch (e) {
            this._closeQRDialog();
            const msg = e?.message || _t("Clover payment failed.");
            console.error("[Clover] Payment exception:", e);
            this._showError(msg);
            line.set_payment_status("retry");
            return false;
        }
    }

    async send_payment_cancel(order, uuid) {
        console.log("[Clover] Cancel requested, uuid:", uuid);
        this._cancelled = true;
        this._closeQRDialog();
        this._stopQRPolling();

        // QR cancel via REST API
        if (this._qrOrderId) {
            try {
                await this._rpc("clover_cancel_qr_payment", [this._qrOrderId]);
            } catch (_e) {
                // best-effort
            }
            this._qrOrderId = null;
        }

        // Card cancel via SDK — reset device, keep connector alive
        if (this._connector && this._connectorReady) {
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
        this._closeQRDialog();
        this._stopQRPolling();
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
            // Reuse existing healthy connection
            if (this._connector && this._connectorReady) {
                resolve(this._connector);
                return;
            }

            // Dispose stale connector before creating new one
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
                        console.log("[Clover] Device ready");
                        this._connectorReady = true;
                        if (!resolved) {
                            resolved = true;
                            clearTimeout(timeout);
                            this._connector = connector;
                            resolve(connector);
                        }
                    },
                    onDeviceDisconnected: () => {
                        console.warn("Clover device disconnected");
                        this._connectorReady = false;
                        // During initial connection, don't resolve null —
                        // forceConnect causes a brief disconnect→reconnect cycle.
                        // The timeout will catch if reconnection never happens.
                        if (resolved) {
                            // Already connected and now lost connection
                            this._connector = null;
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
                    onResetDeviceResponse: () => {
                        console.log("[Clover] Device reset acknowledged");
                        // Retry sale after SECURE_PAY reset
                        if (this._retried && this._pendingSaleRequest && this._connector) {
                            const req = this._pendingSaleRequest;
                            this._pendingSaleRequest = null;
                            console.log("[Clover] Retrying sale after reset...");
                            this._connector.sale(req);
                        }
                    },
                    onDeviceActivityStart: (event) => {
                        this._handleDeviceActivity(event);
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

    _executeSale(connector, line, order, amountCents) {
        return new Promise((resolve) => {
            const sdk = window.clover;

            const saleRequest = new sdk.remotepay.SaleRequest();
            saleRequest.setExternalId(sdk.CloverID.getNewId());
            saleRequest.setAmount(amountCents);

            // Argentina regional extras
            const extras = {};
            extras["currency"] = "ARS";
            saleRequest.setRegionalExtras(extras);

            saleRequest.setCardEntryMethods(
                sdk.CardEntryMethods?.DEFAULT || 15,
            );

            this._pendingLine = line;
            this._pendingResolve = resolve;
            this._pendingOrder = order;
            this._pendingPaymentType = "card";

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

            // Store sale request for potential SECURE_PAY retry
            this._pendingSaleRequest = saleRequest;
            this._retried = false;
            console.log("[Clover] Sending card sale request...");
            connector.sale(saleRequest);
        });
    }

    // ------------------------------------------------------------------
    // QR Payment (REST API + polling)
    // ------------------------------------------------------------------

    async _executeQRPayment(line, order) {
        const amountCents = Math.round(line.amount * 100);
        console.log(`[Clover] Creating QR payment via REST: ${amountCents} cents`);

        // 1. Create QR payment on backend → get QR code data
        const result = await this._rpc("clover_create_qr_payment", [
            order.uid || "", amountCents,
        ]);

        if (result.error) {
            this._showError(result.error);
            line.set_payment_status("retry");
            return false;
        }

        const { clover_order_id, clover_payment_id, qr_payload } = result;
        this._qrOrderId = clover_order_id;
        console.log("[Clover] QR payment created:", { clover_order_id, clover_payment_id, qr_payload: qr_payload ? "yes" : "no" });

        // 2. Open QR dialog with the actual QR payload
        this._openQRDialog(line, order, qr_payload);
        line.set_payment_status("waiting");

        // 3. Poll for payment completion
        return new Promise((resolve) => {
            const startTime = Date.now();

            this._qrPollTimer = setInterval(async () => {
                if (this._cancelled) {
                    this._stopQRPolling();
                    this._closeQRDialog();
                    resolve(false);
                    return;
                }

                // Hard timeout
                if (Date.now() - startTime > PAYMENT_TIMEOUT_MS) {
                    this._stopQRPolling();
                    this._closeQRDialog();
                    line.set_payment_status("retry");
                    this._showError(_t("QR payment timed out."));
                    this._logTransaction(order, "qr", 0, clover_payment_id,
                        "expired", "", "", "", "Timeout");
                    resolve(false);
                    return;
                }

                try {
                    const status = await this._rpc("clover_poll_qr_payment", [
                        clover_order_id, clover_payment_id,
                    ]);
                    console.log("[Clover] QR poll:", status.state);

                    if (status.state === "approved") {
                        this._stopQRPolling();
                        this._closeQRDialog();
                        line.transaction_id = status.clover_payment_id || clover_payment_id;
                        line.card_type = status.card_type || "QR";
                        if (line.set_receipt_info) {
                            line.set_receipt_info(status.card_type || "QR", status.card_last4 || "", false);
                        }
                        line.set_payment_status("done");
                        this._logTransaction(order, "qr", amountCents,
                            status.clover_payment_id || clover_payment_id,
                            "approved", "", status.card_type || "", status.card_last4 || "", "");
                        resolve(true);
                    } else if (status.state === "rejected" || status.state === "canceled") {
                        this._stopQRPolling();
                        this._closeQRDialog();
                        line.set_payment_status("retry");
                        this._showError(status.error || _t("QR payment was declined."));
                        this._logTransaction(order, "qr", 0, clover_payment_id,
                            status.state, "", "", "", status.error || "");
                        resolve(false);
                    } else if (status.state === "error") {
                        this._stopQRPolling();
                        this._closeQRDialog();
                        line.set_payment_status("retry");
                        this._showError(status.error || _t("QR payment error."));
                        resolve(false);
                    }
                    // "pending" → keep polling
                } catch (_e) {
                    console.warn("[Clover] QR poll error:", _e);
                    // Keep polling on transient errors
                }
            }, QR_POLL_INTERVAL_MS);
        });
    }

    _stopQRPolling() {
        if (this._qrPollTimer) {
            clearInterval(this._qrPollTimer);
            this._qrPollTimer = null;
        }
    }

    // ------------------------------------------------------------------
    // SDK Response Handlers
    // ------------------------------------------------------------------

    async _handleSaleResponse(response) {
        clearTimeout(this._paymentTimeout);

        const success = response.getSuccess();
        const message = response.getMessage?.() || "";
        console.log("[Clover] Sale response:", {
            success,
            result: response.getResult?.(),
            reason: response.getReason?.(),
            message,
            hasPayment: !!response.getPayment(),
        });

        // SECURE_PAY retry: device stuck from previous session → reset and retry once
        if (!success && message.includes("SECURE_PAY") && !this._retried) {
            console.log("[Clover] Device stuck in SECURE_PAY, resetting and retrying...");
            this._retried = true;
            this._connector?.resetDevice();
            // onResetDeviceResponse will trigger the retry
            return;
        }

        const line = this._pendingLine;
        const resolvePayment = this._pendingResolve;
        const order = this._pendingOrder;
        const paymentType = this._pendingPaymentType;
        this._pendingLine = null;
        this._pendingResolve = null;
        this._pendingOrder = null;
        this._pendingPaymentType = null;
        this._retried = false;

        // Close QR dialog if open
        this._closeQRDialog();

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

    _handleDeviceActivity(event) {
        const state = event.getEventState?.() || "";
        const message = event.getMessage?.() || "";
        console.log("Clover device activity:", state, message);
    }

    // ------------------------------------------------------------------
    // QR Dialog Management
    // ------------------------------------------------------------------

    _openQRDialog(line, order, qrPayload = "") {
        this._closeQRDialog();
        this._qrDialogClose = this.env.services.dialog.add(CloverQRScreen, {
            amount: line.amount,
            orderRef: order.uid || "",
            qrPayload: qrPayload,
            onCancel: () => {
                this.send_payment_cancel(order, line.uuid);
            },
        });
    }

    _closeQRDialog() {
        if (this._qrDialogClose) {
            this._qrDialogClose();
            this._qrDialogClose = null;
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
