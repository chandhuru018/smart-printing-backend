document.addEventListener("DOMContentLoaded", () => {
    const uploadForm = document.getElementById("uploadForm");
    const uploadLoading = document.getElementById("uploadLoading");
    const uploadButton = document.getElementById("uploadButton");

    if (uploadForm) {
        uploadForm.addEventListener("submit", () => {
            uploadLoading?.classList.remove("hidden");
            if (uploadButton) {
                uploadButton.disabled = true;
                uploadButton.textContent = "Uploading...";
            }
        });
    }

    const copiesInput = document.getElementById("copies");
    const modeInput = document.getElementById("mode");
    const pageRangesInput = document.getElementById("pageRanges");
    const pageModeSelect = document.getElementById("pageModeSelect");
    const pageStartSelect = document.getElementById("pageStart");
    const pageEndSelect = document.getElementById("pageEnd");
    const toPaymentButton = document.getElementById("toPaymentButton");
    const analyzeColorUrl = document.getElementById("hAnalyzeColorUrl")?.value;
    let hasAnalytics = Number(document.getElementById("hHasAnalytics")?.value || 0) === 1;
    if (copiesInput && modeInput) {
        const parseSelectedPageCount = (inputValue, totalPages) => {
            const raw = (inputValue || "all").trim().toLowerCase();
            if (!raw || raw === "all" || raw === "*") {
                return totalPages;
            }

            const selected = new Set();
            for (const token of raw.split(",")) {
                const part = token.trim();
                if (!part) {
                    continue;
                }
                if (part.includes("-")) {
                    const [a, b] = part.split("-", 2);
                    const start = Number(a);
                    const end = Number(b);
                    if (!Number.isFinite(start) || !Number.isFinite(end)) {
                        continue;
                    }
                    const left = Math.max(1, Math.min(start, end));
                    const right = Math.min(totalPages, Math.max(start, end));
                    for (let page = left; page <= right; page += 1) {
                        selected.add(page);
                    }
                } else {
                    const page = Number(part);
                    if (Number.isFinite(page) && page >= 1 && page <= totalPages) {
                        selected.add(page);
                    }
                }
            }
            return selected.size || totalPages;
        };

        const syncPageRangesFromDropdowns = () => {
            if (!pageRangesInput || !pageModeSelect) {
                return;
            }
            if (pageModeSelect.value === "all") {
                pageRangesInput.value = "all";
                if (pageStartSelect) {
                    pageStartSelect.disabled = true;
                }
                if (pageEndSelect) {
                    pageEndSelect.disabled = true;
                }
                return;
            }

            if (!pageStartSelect || !pageEndSelect) {
                pageRangesInput.value = "all";
                return;
            }

            pageStartSelect.disabled = false;
            pageEndSelect.disabled = false;

            let start = Number(pageStartSelect.value || 1);
            let end = Number(pageEndSelect.value || start);
            if (!Number.isFinite(start)) {
                start = 1;
            }
            if (!Number.isFinite(end)) {
                end = start;
            }
            if (end < start) {
                end = start;
                pageEndSelect.value = String(end);
            }

            pageRangesInput.value = start === end ? String(start) : `${start}-${end}`;
        };

        const initDropdownsFromPageRanges = () => {
            if (!pageRangesInput || !pageModeSelect || !pageStartSelect || !pageEndSelect) {
                return;
            }
            const raw = (pageRangesInput.value || "all").trim().toLowerCase();
            if (!raw || raw === "all" || raw === "*") {
                pageModeSelect.value = "all";
                pageStartSelect.value = pageStartSelect.options[0]?.value || "1";
                pageEndSelect.value = pageEndSelect.options[pageEndSelect.options.length - 1]?.value || pageStartSelect.value;
                syncPageRangesFromDropdowns();
                return;
            }

            const firstRange = raw.split(",", 1)[0].trim();
            if (firstRange.includes("-")) {
                const [a, b] = firstRange.split("-", 2);
                pageModeSelect.value = "range";
                pageStartSelect.value = a || pageStartSelect.value;
                pageEndSelect.value = b || pageStartSelect.value;
            } else {
                pageModeSelect.value = "range";
                pageStartSelect.value = firstRange || pageStartSelect.value;
                pageEndSelect.value = firstRange || pageStartSelect.value;
            }
            syncPageRangesFromDropdowns();
        };

        const updatePricingPreview = () => {
            const pageCount = Number(document.getElementById("hPageCount")?.value || 0);
            const bwPages = Number(document.getElementById("hBwPages")?.value || 0);
            const colorPages = Number(document.getElementById("hColorPages")?.value || 0);
            const colorDensity = Number(document.getElementById("hColorDensity")?.value || 0);

            const copies = Math.max(1, Number(copiesInput.value || 1));
            const mode = modeInput.value;
            const selectedPageCount = parseSelectedPageCount(pageRangesInput?.value, pageCount);

            const bwRate = 2;
            const baseColorRate = 6;
            const densityMultiplier = Math.max(0.5, colorDensity * 8);

            let bwCost = 0;
            let colorCost = 0;
            if (mode === "bw") {
                bwCost = selectedPageCount * copies * bwRate;
            } else {
                const selectedColorByRatio = Math.round(selectedPageCount * (colorPages / Math.max(1, pageCount)));
                const effectiveColorPages = Math.min(selectedPageCount, selectedColorByRatio);
                const effectiveBwPages = Math.max(0, selectedPageCount - effectiveColorPages);

                bwCost = effectiveBwPages * copies * bwRate;
                colorCost = effectiveColorPages * copies * (baseColorRate + densityMultiplier);
            }

            const totalCost = bwCost + colorCost;
            document.getElementById("bwCost").textContent = bwCost.toFixed(2);
            document.getElementById("colorCost").textContent = colorCost.toFixed(2);
            document.getElementById("densityMultiplier").textContent = densityMultiplier.toFixed(2);
            document.getElementById("totalCost").textContent = totalCost.toFixed(2);
        };

        initDropdownsFromPageRanges();
        copiesInput.addEventListener("input", updatePricingPreview);
        modeInput.addEventListener("change", async () => {
            if (modeInput.value === "color" && !hasAnalytics && analyzeColorUrl) {
                const originalText = toPaymentButton?.textContent || "";
                if (toPaymentButton) {
                    toPaymentButton.disabled = true;
                    toPaymentButton.textContent = "Running AI Analysis...";
                }
                try {
                    const response = await fetch(analyzeColorUrl, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            copies: copiesInput.value || "1",
                            page_ranges: pageRangesInput?.value || "all",
                        }),
                    });
                    const payload = await response.json();
                    if (!response.ok || !payload.ok) {
                        throw new Error(payload.error || "Color analysis failed");
                    }
                    hasAnalytics = true;
                    window.location.reload();
                    return;
                } catch (err) {
                    alert(err.message || "Unable to run color analysis");
                    modeInput.value = "bw";
                } finally {
                    if (toPaymentButton) {
                        toPaymentButton.disabled = false;
                        toPaymentButton.textContent = originalText || "Continue to Payment";
                    }
                }
            }
            updatePricingPreview();
        });
        if (pageModeSelect) {
            pageModeSelect.addEventListener("change", () => {
                syncPageRangesFromDropdowns();
                updatePricingPreview();
            });
        }
        if (pageStartSelect) {
            pageStartSelect.addEventListener("change", () => {
                syncPageRangesFromDropdowns();
                updatePricingPreview();
            });
        }
        if (pageEndSelect) {
            pageEndSelect.addEventListener("change", () => {
                syncPageRangesFromDropdowns();
                updatePricingPreview();
            });
        }
        updatePricingPreview();
    }

    const payBtn = document.getElementById("payNowButton");
    const paymentLoading = document.getElementById("paymentLoading");

    if (payBtn && window.__PAYMENT__) {
        payBtn.addEventListener("click", () => {
            payBtn.disabled = true;

            const p = window.__PAYMENT__;
            const options = {
                key: p.keyId,
                amount: p.amount,
                currency: p.currency,
                name: "Smart IoT Printing",
                description: "Document print payment",
                order_id: p.orderId,
                method: {
                    upi: true,
                    card: false,
                    netbanking: false,
                    wallet: false,
                },
                prefill: {
                    name: p.customerName,
                },
                handler: async function (response) {
                    paymentLoading?.classList.remove("hidden");
                    try {
                        const verifyRes = await fetch("/payment/verify", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({
                                job_id: p.jobId,
                                razorpay_order_id: response.razorpay_order_id,
                                razorpay_payment_id: response.razorpay_payment_id,
                                razorpay_signature: response.razorpay_signature,
                            }),
                        });
                        const verifyData = await verifyRes.json();
                        if (!verifyRes.ok || !verifyData.ok) {
                            throw new Error(verifyData.error || "Payment verification failed");
                        }
                        window.location.href = verifyData.redirect_url || "/success";
                    } catch (err) {
                        paymentLoading?.classList.add("hidden");
                        payBtn.disabled = false;
                        alert(err.message || "Payment failed");
                    }
                },
                modal: {
                    ondismiss: function () {
                        payBtn.disabled = false;
                    },
                },
            };

            const instance = new Razorpay(options);
            instance.open();
        });
    }
});
