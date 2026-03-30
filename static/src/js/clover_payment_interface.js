/** @odoo-module */

import { PaymentInterface } from "@point_of_sale/app/payment/payment_interface";
import { register_payment_method } from "@point_of_sale/app/store/pos_store";
import { _t } from "@web/core/l10n/translation";
import { CloverQRScreen } from "./clover_qr_screen";

const CONNECT_TIMEOUT_MS = 30000;   // 30s to establish WebSocket
const PAYMENT_TIMEOUT_MS = 120000;  // 2 min hard timeout for payment
const QR_POLL_INTERVAL_MS = 3000;   // 3s between QR status polls
const RESET_RETRY_DELAY_MS = 3000;  // 3s wait after reset before retry

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
        this._retryCount = 0;
        this._qrPollTimer = null;
        this._qrOrderId = null;
        this._pendingRefundResolve = null;
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
            if (this._cancelled) return false;
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

        // Cancel SDK — reset device if connected, dispose if still connecting
        if (this._connector && this._connectorReady) {
            try {
                this._connector.resetDevice();
            } catch (_e) {
                // best-effort
            }
        } else if (!this._connectorReady) {
            this._disposeConnector();
        }

        // Resolve pending promise so Odoo doesn't stay stuck
        if (this._pendingResolve) {
            const line = this._pendingLine;
            this._pendingResolve(false);
            this._pendingResolve = null;
            this._pendingLine = null;
            this._pendingOrder = null;
            this._pendingPaymentType = null;
            clearTimeout(this._paymentTimeout);
            if (line) {
                line.set_payment_status("retry");
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
            // Use SDK WebSocket for refund (REST v3 not available on LATAM)
            if (!this._sdkConfig) {
                this._sdkConfig = await this._fetchSdkConfig();
            }
            if (this._sdkConfig.error) {
                this._showError(this._sdkConfig.error);
                return false;
            }
            const connector = await this._getConnector();
            if (!connector) {
                this._showError(_t("Could not connect to Clover device for refund."));
                return false;
            }
            const sdk = window.clover;
            const refundRequest = new sdk.remotepay.RefundPaymentRequest();
            refundRequest.setPaymentId(String(line.transaction_id));
            refundRequest.setFullRefund(true);
            const amountCents = Math.round(line.amount * 100);
            refundRequest.setAmount(amountCents);

            return new Promise((resolve) => {
                this._pendingRefundResolve = resolve;
                connector.refundPayment(refundRequest);

                // Timeout for refund
                setTimeout(() => {
                    if (this._pendingRefundResolve) {
                        this._pendingRefundResolve(false);
                        this._pendingRefundResolve = null;
                        this._showError(_t("Refund timed out."));
                    }
                }, PAYMENT_TIMEOUT_MS);
            });
        } catch (_e) {
            console.error("[Clover] Refund exception:", _e);
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

            // Store resolve so _disposeConnector can abort this promise
            this._connectResolve = resolve;

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

            this._connectTimeout = setTimeout(() => {
                if (!resolved) {
                    resolved = true;
                    this._connectResolve = null;
                    console.error("Clover SDK connection timeout");
                    resolve(null);
                }
            }, CONNECT_TIMEOUT_MS);
            const timeout = this._connectTimeout;

            const listener = Object.assign(
                {},
                sdk.remotepay.ICloverConnectorListener.prototype,
                {
                    onDeviceReady: () => {
                        console.log("[Clover] Device ready");
                        this._connector = connector;
                        this._connectorReady = true;
                        if (!resolved) {
                            resolved = true;
                            this._connectResolve = null;
                            clearTimeout(timeout);
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

                        // Retry sale after SECURE_PAY reset (with delay for device to settle)
                        if (this._retryCount > 0 && this._pendingSaleRequest && this._connector) {
                            console.log(`[Clover] Waiting ${RESET_RETRY_DELAY_MS}ms before retry...`);
                            setTimeout(() => {
                                if (this._pendingSaleRequest && this._connector) {
                                    console.log("[Clover] Retrying sale after reset...");
                                    this._connector.sale(this._pendingSaleRequest);
                                }
                            }, RESET_RETRY_DELAY_MS);
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
        if (this._connectTimeout) {
            clearTimeout(this._connectTimeout);
            this._connectTimeout = null;
        }
        // Resolve orphaned _getConnector promise
        if (this._connectResolve) {
            this._connectResolve(null);
            this._connectResolve = null;
        }
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
            this._retryCount = 0;
            console.log("[Clover] Sending card sale request...");
            connector.sale(saleRequest);
        });
    }

    // ------------------------------------------------------------------
    // QR Payment (SDK WebSocket for device + checkout URL for Odoo)
    // ------------------------------------------------------------------

    async _executeQRPayment(line, order) {
        const amountCents = Math.round(line.amount * 100);
        console.log(`[Clover] QR payment: ${amountCents} cents`);

        // 1. Create Clover order via backend + get EMVCo QR (if template configured)
        const result = await this._rpc("clover_create_qr_payment", [
            order.uid || "", amountCents,
        ]);
        if (result.error) {
            this._showError(result.error);
            line.set_payment_status("retry");
            return false;
        }
        const { clover_order_id, qr_data } = result;
        this._qrOrderId = clover_order_id;
        console.log("[Clover] QR order created:", clover_order_id,
            "EMVCo QR:", qr_data ? `${qr_data.length} chars` : "none");

        // 2. Show QR dialog — with EMVCo QR if available, otherwise "scan on terminal"
        this._openQRDialog(line, order, qr_data || "");

        // 3. Try to connect SDK and send to device (non-blocking — don't fail if offline)
        this._sendQRToDevice(amountCents);

        // 4. Set up dual detection: SDK onSaleResponse + REST v3 polling
        return new Promise((resolve) => {
            this._pendingLine = line;
            this._pendingResolve = resolve;
            this._pendingOrder = order;
            this._pendingPaymentType = "qr";
            line.set_payment_status("waiting");

            // Hard timeout — show manual confirm instead of failing
            this._paymentTimeout = setTimeout(() => {
                this._stopQRPolling();
                if (this._pendingResolve) {
                    console.log("[Clover] QR payment timeout — showing manual confirm");
                    this._showManualConfirmDialog(line, order, clover_order_id);
                }
            }, PAYMENT_TIMEOUT_MS);

            // Start REST v3 polling to detect payment from Odoo QR scan
            this._startQRPolling(clover_order_id, line, order);
        });
    }

    async _sendQRToDevice(amountCents) {
        // Best-effort: send SaleRequest to device for QR display
        try {
            if (!this._sdkConfig) {
                this._sdkConfig = await this._fetchSdkConfig();
            }
            if (this._sdkConfig.error) {
                console.warn("[Clover] SDK config error, device QR skipped:", this._sdkConfig.error);
                return;
            }
            const connector = await this._getConnector();
            if (this._cancelled || !connector) {
                console.warn("[Clover] Device not available, Odoo QR only");
                return;
            }

            const sdk = window.clover;
            const saleRequest = new sdk.remotepay.SaleRequest();
            saleRequest.setExternalId(sdk.CloverID.getNewId());
            saleRequest.setAmount(amountCents);
            saleRequest.setAllowOfflinePayment(false);
            saleRequest.setApproveOfflinePaymentWithoutPrompt(false);

            if (typeof saleRequest.setPresentQrcOnly === "function") {
                saleRequest.setPresentQrcOnly(true);
                console.log("[Clover] Using setPresentQrcOnly(true)");
            } else {
                saleRequest.setCardEntryMethods(0);
                console.log("[Clover] Fallback: setCardEntryMethods(0)");
            }

            const extras = {};
            extras["currency"] = "ARS";
            saleRequest.setRegionalExtras(extras);

            this._pendingSaleRequest = saleRequest;
            this._retryCount = 0;
            console.log("[Clover] Sending QR-only sale request to device...");
            connector.sale(saleRequest);
        } catch (e) {
            console.warn("[Clover] Device QR failed, Odoo QR only:", e);
        }
    }

    _startQRPolling(cloverOrderId, line, order) {
        this._qrPollPaymentId = "";
        this._qrPollTimer = setInterval(async () => {
            if (this._cancelled || !this._pendingResolve) {
                this._stopQRPolling();
                return;
            }
            try {
                const result = await this._rpc("clover_poll_qr_payment", [
                    cloverOrderId, this._qrPollPaymentId || "",
                ]);
                console.log("[Clover] QR poll:", result.state);

                if (result.state === "approved") {
                    this._stopQRPolling();
                    clearTimeout(this._paymentTimeout);
                    console.log("[Clover] QR payment detected via polling!");

                    const resolvePayment = this._pendingResolve;
                    this._pendingLine = null;
                    this._pendingResolve = null;
                    this._pendingOrder = null;
                    this._pendingPaymentType = null;

                    this._closeQRDialog();

                    line.transaction_id = result.clover_payment_id || "";
                    line.card_type = result.card_type || "QR";
                    if (line.set_receipt_info) {
                        line.set_receipt_info(result.card_type || "QR", result.card_last4 || "", false);
                    }
                    line.set_payment_status("done");

                    this._logTransaction(order, "qr",
                        line.amount * 100, result.clover_payment_id || "",
                        "approved", result, result.card_type || "QR",
                        result.card_last4 || "", "Detected via REST polling");

                    if (resolvePayment) resolvePayment(true);
                }
            } catch (e) {
                console.warn("[Clover] QR poll error:", e);
            }
        }, QR_POLL_INTERVAL_MS);
    }

    _showManualConfirmDialog(line, order, cloverOrderId) {
        // Replace QR dialog with manual confirmation option
        this._closeQRDialog();
        this._qrDialogClosedByCode = false;

        const doConfirm = () => {
            this._qrDialogClosedByCode = true;
            if (this._qrDialogClose) {
                this._qrDialogClose();
                this._qrDialogClose = null;
            }
            clearTimeout(this._paymentTimeout);

            const resolvePayment = this._pendingResolve;
            this._pendingLine = null;
            this._pendingResolve = null;
            this._pendingOrder = null;
            this._pendingPaymentType = null;

            line.transaction_id = `manual-qr-${cloverOrderId}`;
            line.card_type = "QR";
            line.set_payment_status("done");

            this._logTransaction(order, "qr",
                line.amount * 100, `manual-qr-${cloverOrderId}`,
                "approved", {}, "QR", "",
                "Manually confirmed by cashier");

            if (resolvePayment) resolvePayment(true);
        };

        const doCancel = () => {
            this.send_payment_cancel(order, line.uuid);
        };

        this._qrDialogClose = this.env.services.dialog.add(CloverQRScreen, {
            amount: line.amount,
            orderRef: order.uid || "",
            qrPayload: "",
            manualConfirm: true,
            onConfirm: doConfirm,
            onCancel: doCancel,
        }, {
            onClose: () => {
                if (!this._qrDialogClosedByCode) {
                    doCancel();
                }
            },
        });
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

        // SECURE_PAY: device has an orphaned payment in progress — try one reset
        if (!success && message.includes("SECURE_PAY") && this._retryCount === 0) {
            this._retryCount = 1;
            console.log("[Clover] Device stuck in SECURE_PAY, attempting reset...");
            this._connector?.resetDevice();
            // onResetDeviceResponse will trigger the delayed retry
            return;
        }
        // If retry also failed, the device needs physical intervention
        if (!success && message.includes("SECURE_PAY") && this._retryCount > 0) {
            console.warn("[Clover] Device still stuck after reset — needs manual intervention");
        }

        const line = this._pendingLine;
        const resolvePayment = this._pendingResolve;
        const order = this._pendingOrder;
        const paymentType = this._pendingPaymentType;
        this._pendingLine = null;
        this._pendingResolve = null;
        this._pendingOrder = null;
        this._pendingPaymentType = null;
        this._retryCount = 0;

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

            // Late success: Odoo timed out but device completed payment
            if (!line) {
                console.warn("[Clover] Late payment success — Odoo timed out but device charged. Payment ID:", cloverPaymentId);
                this._logTransaction(order, paymentType,
                    payment.getAmount?.() || 0, cloverPaymentId,
                    "approved", response, cardType, cardLast4,
                    "Late success: Odoo timed out before device completed");
                this.env.services.notification.add(
                    _t("Payment was charged on the device (ID: %s). Please verify and reconcile manually.", cloverPaymentId),
                    { type: "warning", sticky: true },
                );
                return;
            }

            line.transaction_id = cloverPaymentId;
            line.card_type = cardType;
            if (line.set_receipt_info) {
                line.set_receipt_info(cardType, cardLast4, false);
            }
            line.set_payment_status("done");

            this._logTransaction(order, paymentType,
                payment.getAmount?.() || 0, cloverPaymentId,
                "approved", response, cardType, cardLast4, "");

            if (resolvePayment) resolvePayment(true);
        } else {
            const isStuckDevice = message.includes("SECURE_PAY");
            const reason = isStuckDevice
                ? _t("Device is busy with a previous payment. Cancel it on the device screen or restart the terminal, then try again.")
                : (response.getReason?.() || response.getMessage?.() ||
                    _t("Payment declined."));

            if (line) {
                line.set_payment_status("retry");
            }
            if (isStuckDevice) {
                // Sticky notification so cashier sees the device instruction
                this.env.services.notification.add(reason, {
                    type: "warning",
                    sticky: true,
                });
            } else {
                this._showError(reason);
            }

            this._logTransaction(order, paymentType, 0, "",
                "rejected", response, "", "", reason);

            if (resolvePayment) resolvePayment(false);
        }
    }

    _handleRefundResponse(response) {
        const success = response.getSuccess();
        console.log("[Clover] Refund response:", { success, reason: response.getReason?.() });
        if (this._pendingRefundResolve) {
            const resolve = this._pendingRefundResolve;
            this._pendingRefundResolve = null;
            if (success) {
                resolve(true);
            } else {
                this._showError(response.getReason?.() || _t("Refund was declined."));
                resolve(false);
            }
        }
    }

    _handleDeviceActivity(event) {
        const state = String(event.getEventState?.() || "");
        const message = String(event.getMessage?.() || "");
        console.log("[Clover] Device activity:", state, message);

        // Log QR mode transitions
        if (state === "START_QR_CODE_MODE") {
            console.log("[Clover] Device entered QR mode");
        }

        // Reset Odoo hard timeout while device is still active
        if (this._pendingResolve && this._pendingLine) {
            const line = this._pendingLine;
            clearTimeout(this._paymentTimeout);
            this._paymentTimeout = setTimeout(() => {
                if (this._pendingResolve) {
                    this._pendingResolve(false);
                    this._pendingResolve = null;
                    this._pendingLine = null;
                    this._closeQRDialog();
                    line.set_payment_status("retry");
                    this._showError(_t("Payment timed out."));
                }
            }, PAYMENT_TIMEOUT_MS);
        }
    }

    // ------------------------------------------------------------------
    // QR Dialog Management
    // ------------------------------------------------------------------

    _openQRDialog(line, order, qrPayload = "") {
        this._closeQRDialog();
        this._qrDialogClosedByCode = false;
        const doCancel = () => {
            this.send_payment_cancel(order, line.uuid);
        };
        this._qrDialogClose = this.env.services.dialog.add(CloverQRScreen, {
            amount: line.amount,
            orderRef: order.uid || "",
            qrPayload: qrPayload,
            onCancel: doCancel,
        }, {
            onClose: () => {
                if (!this._qrDialogClosedByCode) {
                    doCancel();
                }
            },
        });
    }

    _closeQRDialog() {
        if (this._qrDialogClose) {
            this._qrDialogClosedByCode = true;
            this._qrDialogClose();
            this._qrDialogClose = null;
        }
    }

    _stopQRPolling() {
        if (this._qrPollTimer) {
            clearInterval(this._qrPollTimer);
            this._qrPollTimer = null;
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
