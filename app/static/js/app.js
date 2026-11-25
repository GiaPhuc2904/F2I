/* Giao diện Cộng sự Kho Offline cho chủ kho U50–U60 (100% Offline, phân trang mượt, 0 thuật ngữ học thuật) */
(function () {
  "use strict";

  const PAGE_SIZE = 50;
  const PRIVACY_STORAGE_KEY = "misa_companion_hide_sensitive";

  const SEASONAL_LABELS = {
    "1": "Bình thường (0%)",
    "1.15": "Cao điểm / Nắng nóng (+15%)",
    "1.3": "Sắp Lễ Tết (+30%)",
    "0.85": "Mùa chậm (-15%)",
  };

  const state = {
    activeTab: "overview",
    hideSensitive: false,
    uploadMode: "merge",
    overviewPeriod: "all",
    seasonalMult: 1.0,
    selectedWarehouse: "all",
    healthFilter: "all",
    healthQuery: "",
    healthPage: 1,
    forecastQuery: "",
    forecastGroup: "all",
    forecastUnitMode: "base",
    forecastPage: 1,
    creditFilter: "all",
    creditQuery: "",
    creditPage: 1,
    cashflowMonths: 12,
    dashboard: null,
    dirtyTabs: { overview: true, health: true, forecast: true, credit: true },
    setupSelectedCompares: [],
    setupPrimaryModel: null,
    charts: {
      forecast: null,
      cf1: null,
      cf2: null,
      cf3a: null,
    },
  };

  try {
    state.hideSensitive = window.localStorage.getItem(PRIVACY_STORAGE_KEY) === "1";
  } catch (_) {
    state.hideSensitive = false;
  }

  function el(id) {
    return document.getElementById(id);
  }

  function esc(val) {
    return String(val == null ? "" : val)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function foldVi(str) {
    return String(str || "")
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/đ/g, "d")
      .replace(/Đ/g, "D")
      .toLowerCase()
      .trim();
  }

  function fmtInt(n) {
    const v = Number(n || 0);
    return Math.round(v).toLocaleString("vi-VN");
  }

  function fmtQty(n) {
    const v = Number(n || 0);
    if (Math.abs(v) >= 10 || Math.abs(v - Math.round(v)) < 0.05) {
      return Math.round(v).toLocaleString("vi-VN");
    }
    return v.toLocaleString("vi-VN", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  }

  function fmtPct(n) {
    const v = Number(n || 0);
    return v.toLocaleString("vi-VN", { maximumFractionDigits: 1 }) + "%";
  }

  function fmtMoney(vnd, indexFallback) {
    if (state.hideSensitive) {
      if (indexFallback != null && Number.isFinite(Number(indexFallback))) {
        const idx = Number(indexFallback);
        if (Number(vnd || 0) > 0 && idx < 0.1) {
          return "Chỉ số <0,1/100";
        }
        return "Chỉ số " + fmtQty(idx) + "/100";
      }
      return "*** đ";
    }
    if (vnd == null) {
      return "—";
    }
    const num = Number(vnd);
    const abs = Math.abs(num);
    const sign = num < 0 ? "-" : "";
    if (abs >= 1e9) {
      return sign + (abs / 1e9).toLocaleString("vi-VN", { maximumFractionDigits: 2 }) + " tỷ\u00a0đ";
    }
    if (abs >= 1e6) {
      return sign + (abs / 1e6).toLocaleString("vi-VN", { maximumFractionDigits: 1 }) + " triệu\u00a0đ";
    }
    return sign + Math.round(abs).toLocaleString("vi-VN") + "\u00a0đ";
  }

  function fmtDateVi(isoStr) {
    if (!isoStr || String(isoStr).length < 10) return "—";
    const parts = String(isoStr).slice(0, 10).split("-");
    if (parts.length !== 3) return isoStr;
    return parts[2] + "/" + parts[1] + "/" + parts[0];
  }

  function displayProductName(item) {
    if (state.hideSensitive) {
      return "Mặt hàng " + (item.sku_anon || "SKU");
    }
    return item.product_name || item.sku || item.product_id;
  }

  function displayProductCode(item) {
    return state.hideSensitive ? (item.sku_anon || "SKU") : (item.sku || item.product_id);
  }

  function displayWarehouse(item) {
    return state.hideSensitive ? (item.warehouse_anon || "KHO_01") : (item.warehouse_code || "KHO_CHINH");
  }

  function displayWarehouseByKey(whCode, whAnon) {
    return state.hideSensitive ? (whAnon || "KHO_01") : (whCode || "KHO_CHINH");
  }

  function displayCustomerName(cust) {
    return state.hideSensitive ? ("Khách hàng " + (cust.customer_anon || "KH")) : (cust.customer_name || cust.customer_id);
  }

  function displayCustomerCode(cust) {
    return state.hideSensitive ? (cust.customer_anon || "KH") : (cust.customer_code || cust.customer_anon || "");
  }

  function formatStockFlowCell(item, flowMode) {
    if (!flowMode) {
      return item.current_stock_display_vi || "—";
    }
    const uom = item.base_uom || "";
    const lastIn = Number(item.last_inbound_qty || 0);
    const soldNear = Number(item.sold_since_last_inbound || 0);
    if (lastIn > 0) {
      return "Nhập gần nhất: " + fmtQty(lastIn) + " · Đã bán kỳ gần đây: " + fmtQty(soldNear) + (uom ? " " + uom : "");
    }
    if (Number(item.total_out_qty || 0) > 0) {
      return "Đã bán trong kỳ: " + fmtQty(item.total_out_qty) + (uom ? " " + uom : "");
    }
    return "Chưa phát sinh mua bán";
  }

  function computeAdjustedItem(item, flowMode) {
    const mult = state.seasonalMult;
    if (Math.abs(mult - 1.0) < 1e-4) {
      return item;
    }
    const copy = Object.assign({}, item);
    const fc1 = Math.round(Number(item.forecast_next_1w || 0) * mult * 10) / 10;
    const fc4 = Math.round(Number(item.forecast_next_4w || 0) * mult * 10) / 10;
    copy.forecast_next_1w = fc1;
    copy.forecast_next_4w = fc4;

    const upb = Math.max(1.0, Number(item.units_per_box || 1.0));
    const uom = item.base_uom || "ĐV";
    const boxUom = item.suggested_box_uom || item.box_uom || "Thùng";
    const blocked = ["dead_stock", "promo_surplus", "inactive_zero", "no_stock_book"].indexOf(item.status_code) !== -1;

    if (!blocked && fc1 > 0) {
      const avail = flowMode ? Number(item.est_batch_remaining_qty || 0) : Number(item.current_stock_qty || 0);
      if (item.suggested_boxes > 0 || avail < fc1 * 1.2) {
        const shortage = Math.max(fc1, fc1 * 2.0 - Math.max(0, avail));
        const boxes = Math.max(1, Math.ceil(shortage / upb));
        const units = Math.round(boxes * upb);
        copy.suggested_boxes = boxes;
        copy.suggested_units = units;
        copy.estimated_order_cost_vnd = Math.round(units * Number(item.latest_purchase_cost || 0));
        copy.action_vi = upb > 1
          ? ("Nên đặt thêm " + fmtInt(boxes) + " " + String(boxUom).toLowerCase() + " (" + fmtQty(units) + " " + String(uom).toLowerCase() + " cho 2 tuần tới)")
          : ("Nên đặt thêm " + fmtQty(units) + " " + String(uom).toLowerCase() + " (đủ bán 2 tuần tới)");
        if (copy.status_code === "healthy") {
          copy.status_code = "low_stock";
          copy.status_vi = flowMode ? "Sắp cạn đợt nhập" : "Sắp hết hàng";
          copy.badge_tone = "danger";
        }
      } else {
        copy.suggested_boxes = 0;
        copy.suggested_units = 0;
        copy.estimated_order_cost_vnd = 0;
        copy.status_code = "healthy";
        copy.status_vi = "Đủ bán ổn định";
        copy.badge_tone = "success";
        copy.action_vi = "Đủ bán ổn định · Chưa cần đặt thêm";
      }
    }
    return copy;
  }

  function getAdjustedItems() {
    if (!state.dashboard || !state.dashboard.m2) return [];
    const flowMode = Boolean(state.dashboard.m2.flow_mode_active);
    return (state.dashboard.m2.all_items || []).map(function (it) {
      return computeAdjustedItem(it, flowMode);
    });
  }

  function renderBadge(label, tone) {
    let t = tone || "neutral";
    if (t === "critical") t = "danger";
    const badgeCls = "badge-" + t;
    return '<span class="status-badge ' + badgeCls + '"><span class="status-dot"></span>' + esc(label) + "</span>";
  }

  function destroyChart(key) {
    if (state.charts[key]) {
      state.charts[key].destroy();
      state.charts[key] = null;
    }
  }

  function syncPrivacyButtonUI() {
    const btn = el("btn-privacy-toggle");
    const lbl = el("privacy-toggle-label");
    if (!btn || !lbl) return;
    btn.classList.toggle("is-active", state.hideSensitive);
    btn.setAttribute("aria-pressed", state.hideSensitive ? "true" : "false");
    lbl.textContent = state.hideSensitive ? "Đang che tên & tiền" : "Che tên & tiền: Đang tắt";
  }

  function markAllTabsDirty() {
    state.dirtyTabs = { overview: true, health: true, forecast: true, credit: true };
  }

  function triggerExportCsv() {
    const primary = state.dashboard && state.dashboard.model_settings
      ? state.dashboard.model_settings.primary_model_id
      : "";
    const params = new URLSearchParams();
    if (primary) params.set("primary_model", primary);
    if (state.hideSensitive) params.set("hide_sensitive", "true");
    if (state.activeTab === "health" && state.selectedWarehouse !== "all") {
      params.set("warehouse", state.selectedWarehouse);
    }
    if (Math.abs(state.seasonalMult - 1.0) > 1e-4) {
      params.set("demand_mult", String(state.seasonalMult));
    }
    window.location.href = "/api/export/reorder-csv?" + params.toString();
  }

  /* ==================== CHUYỂN TAB MƯỢT (CHỈ VẼ TAB ĐANG MỞ) ==================== */

  function renderCurrentTabIfNeeded() {
    const d = state.dashboard;
    if (!d || !d.has_data) return;
    const t = state.activeTab;
    if (t === "overview" && state.dirtyTabs.overview) {
      renderOverviewTab(d);
      state.dirtyTabs.overview = false;
    } else if (t === "health" && state.dirtyTabs.health) {
      renderHealthTab(d);
      state.dirtyTabs.health = false;
    } else if (t === "forecast") {
      if (state.dirtyTabs.forecast) {
        renderForecastTab(d);
        state.dirtyTabs.forecast = false;
      } else if (!state.charts.forecast) {
        renderForecastCharts();
      }
    } else if (t === "credit") {
      if (state.dirtyTabs.credit) {
        renderCreditTab(d);
        state.dirtyTabs.credit = false;
      } else if (!state.charts.cf1) {
        renderCashflowCharts();
      }
    }
  }

  function switchTab(tabId) {
    state.activeTab = tabId;
    ["overview", "health", "forecast", "credit"].forEach(function (t) {
      const sec = el("tab-" + t);
      if (sec) sec.classList.toggle("is-hidden", t !== tabId);
    });
    document.querySelectorAll("[data-tab]").forEach(function (btn) {
      btn.classList.toggle("is-active", btn.getAttribute("data-tab") === tabId);
    });
    renderCurrentTabIfNeeded();
  }

  function togglePrivacyMode() {
    state.hideSensitive = !state.hideSensitive;
    try {
      window.localStorage.setItem(PRIVACY_STORAGE_KEY, state.hideSensitive ? "1" : "0");
    } catch (_) {}
    syncPrivacyButtonUI();
    if (state.dashboard && state.dashboard.has_data) {
      destroyChart("cf1");
      destroyChart("cf2");
      populateWarehouseSelect();
      markAllTabsDirty();
      renderCurrentTabIfNeeded();
    }
  }

  /* ==================== KHUNG CHUNG & MỤC 1 (TỔNG QUAN + BỘ LỌC KỲ) ==================== */

  function populateWarehouseSelect() {
    const sel = el("select-warehouse-health");
    if (!sel || !state.dashboard || !state.dashboard.m2) return;
    const seen = {};
    const list = [];
    (state.dashboard.m2.all_items || []).forEach(function (x) {
      const code = x.warehouse_code || "KHO_CHINH";
      if (!seen[code]) {
        seen[code] = true;
        list.push({ code: code, anon: x.warehouse_anon || "KHO_01" });
      }
    });
    list.sort(function (a, b) { return a.code.localeCompare(b.code); });
    const prev = state.selectedWarehouse;
    let html = '<option value="all">Tất cả kho / hãng (' + fmtInt(list.length) + ")</option>";
    list.forEach(function (w) {
      const label = displayWarehouseByKey(w.code, w.anon);
      html += '<option value="' + esc(w.code) + '">' + esc(label) + "</option>";
    });
    sel.innerHTML = html;
    sel.value = seen[prev] ? prev : "all";
    state.selectedWarehouse = sel.value;
  }

  function updateSidebarSummary() {
    const d = state.dashboard;
    if (!d || !d.has_data) return;
    const adjItems = getAdjustedItems();
    const reordCnt = adjItems.filter(function (x) { return x.suggested_boxes > 0; }).length;
    const cards = (d.overview && d.overview.cards) || {};

    el("nav-badge-health").textContent = fmtInt(reordCnt);
    el("nav-badge-forecast").textContent = "11";
    el("nav-badge-credit").textContent = fmtInt(cards.call_now_customers || 0);
    el("sidebar-active-model").textContent = d.model_settings.primary_model_label_vi || "—";
    const mKey = String(state.seasonalMult);
    el("sidebar-seasonal-note").textContent = "Hệ số mùa vụ: " + (SEASONAL_LABELS[mKey] || "Bình thường (0%)");
  }

  function renderAllDashboard() {
    const d = state.dashboard;
    if (!d || !d.has_data) return;

    el("app-shell").classList.remove("is-hidden");
    el("mobile-nav").classList.remove("is-hidden");

    const ov = d.overview || {};
    const dr = ov.date_range || {};
    el("header-subtitle").textContent =
      "Kỳ sổ MISA: " + fmtDateVi(dr.min_date) + " đến " + fmtDateVi(dr.max_date) +
      " · Cách dự báo: " + (d.model_settings.primary_model_label_vi || "");

    populateWarehouseSelect();
    updateSidebarSummary();
    markAllTabsDirty();
    renderCurrentTabIfNeeded();
  }

  function renderOverviewTab(d) {
    const ov = d.overview || {};
    const cards = ov.cards || {};
    const dr = ov.date_range || {};
    const flowMode = Boolean(cards.flow_mode_active);
    const presets = ov.period_presets || {};
    const pData = presets[state.overviewPeriod] || {
      start_date: dr.min_date,
      end_date: dr.max_date,
      net_revenue_vnd: cards.net_revenue_vnd,
      gross_profit_vnd: cards.gross_profit_vnd,
      gross_margin_pct: cards.gross_margin_pct,
      gift_cogs_vnd: cards.gift_cogs_vnd,
      total_purchase_net_vnd: cards.total_purchase_net_vnd,
    };

    document.querySelectorAll("#overview-period-pills [data-period]").forEach(function (b) {
      b.classList.toggle("is-active", b.getAttribute("data-period") === state.overviewPeriod);
    });

    const periodNames = { "30d": "30 ngày gần nhất", "90d": "90 ngày gần nhất", "365d": "12 tháng gần nhất", "all": "Toàn bộ kỳ sổ" };
    const pLabel = periodNames[state.overviewPeriod] || "Toàn bộ kỳ sổ";
    el("overview-active-range-label").textContent =
      "Đang xem: " + fmtDateVi(pData.start_date) + " → " + fmtDateVi(pData.end_date) + " (" + pLabel + ")";
    el("overview-period-note").textContent =
      "Tóm tắt kết quả kinh doanh, hàng cần đặt nhập và khách nợ lâu cần gọi nhắc.";

    el("kpi-profit-label").textContent = "Lãi gộp thực tế (" + pLabel.toLowerCase() + ")";
    const gpEl = el("kpi-gross-profit");
    const gpVal = Number(pData.gross_profit_vnd || 0);
    gpEl.textContent = fmtMoney(gpVal);
    gpEl.classList.toggle("text-red-700", gpVal < 0);
    gpEl.classList.toggle("text-emerald-700", gpVal >= 0);
    const uncostedCnt = pData.uncosted_skus_count != null ? pData.uncosted_skus_count : (cards.uncosted_skus_count || 0);
    const uncostedNote = uncostedCnt > 0
      ? (" · Tách riêng " + fmtInt(uncostedCnt) + " mã chưa có giá vốn")
      : "";
    const negNote = gpVal < 0
      ? " · Thấp hơn giá vốn xuất kho (xem quà tặng KM hoặc thưởng hãng trả sau)"
      : "";
    el("kpi-gross-profit-sub").textContent = state.hideSensitive
      ? ("Tỷ suất lãi gộp thực: " + fmtPct(pData.gross_margin_pct) + uncostedNote + negNote)
      : ("Doanh thu: " + fmtMoney(pData.net_revenue_vnd) + " · Lãi " + fmtPct(pData.gross_margin_pct) + " (trừ " + fmtMoney(pData.gift_cogs_vnd) + " vốn quà tặng)" + uncostedNote + negNote);

    const cov = ov.stock_coverage || {};
    if (flowMode) {
      el("kpi-stock-label").textContent = "Tiền nhập hàng (" + pLabel.toLowerCase() + ")";
      el("kpi-stock-value").textContent = fmtMoney(pData.total_purchase_net_vnd);
      el("kpi-stock-sub").textContent =
        (cov.label_vi || "—") + " · " + fmtInt(cards.slow_or_overstock_skus || 0) + " mã bán chậm/nằm im";
      el("th-urgent-stock").textContent = "Nhịp lô nhập cuối";
    } else {
      el("kpi-stock-label").textContent = "Giá trị hàng đang nằm trong kho";
      el("kpi-stock-value").textContent = fmtMoney(cards.stock_value_vnd);
      el("kpi-stock-sub").textContent =
        (cov.label_vi || "—") + " · " + fmtInt(cards.slow_or_overstock_skus || 0) + " mã nằm im/tồn nhiều";
      el("th-urgent-stock").textContent = "Tồn hiện tại";
    }

    const adjItems = getAdjustedItems();
    const reordList = adjItems.filter(function (x) { return x.suggested_boxes > 0; });
    const totBoxes = reordList.reduce(function (s, x) { return s + Number(x.suggested_boxes || 0); }, 0);
    const totEstCost = reordList.reduce(function (s, x) { return s + Number(x.estimated_order_cost_vnd || 0); }, 0);

    el("kpi-reorder-count").textContent = fmtInt(reordList.length) + " món (" + fmtInt(totBoxes) + " thùng)";
    const multTag = Math.abs(state.seasonalMult - 1.0) > 1e-4 ? (" · " + SEASONAL_LABELS[String(state.seasonalMult)]) : "";
    el("kpi-reorder-sub").textContent = state.hideSensitive
      ? ("Tính theo dự báo tuần tới" + multTag + " · Đã ẩn tiền nhập")
      : ("Ước tính tiền nhập khoảng " + fmtMoney(totEstCost) + multTag);

    el("kpi-debt-total").textContent = fmtMoney(cards.total_outstanding_vnd);
    el("kpi-debt-sub").textContent = state.hideSensitive
      ? ("Có " + fmtInt(cards.call_now_customers || 0) + " khách quá hạn thực sự cần gọi nhắc")
      : ("Trong đó " + fmtInt(cards.call_now_customers || 0) + " khách cần gọi ngay (" + fmtMoney(cards.call_now_outstanding_vnd) + ")");

    const rep = ov.import_report || {};
    const warns = rep.warnings_vi || [];
    el("overview-warnings").innerHTML = warns.map(function (w) {
      return '<div class="u60-banner-warn">' + esc(w) + "</div>";
    }).join("");

    const urgItems = reordList.slice(0, 6);
    const tbReord = el("tbody-urgent-reorder");
    if (!urgItems.length) {
      tbReord.innerHTML = '<tr><td colspan="4" class="u60-sub">Hiện tại các mặt hàng chủ lực đều còn đủ bán ổn định.</td></tr>';
    } else {
      tbReord.innerHTML = urgItems.map(function (it) {
        return (
          "<tr>" +
          '<td><div class="font-semibold">' + esc(displayProductName(it)) + '</div><div class="u60-sub">' + esc(displayProductCode(it)) + " · " + esc(displayWarehouse(it)) + "</div></td>" +
          '<td class="u60-sub">' + esc(formatStockFlowCell(it, flowMode)) + "</td>" +
          '<td class="col-num font-semibold">' + fmtQty(it.forecast_next_1w) + " " + esc(it.base_uom) + "</td>" +
          '<td class="col-wrap"><span class="font-semibold text-blue-700">' + esc(it.action_vi) + "</span></td>" +
          "</tr>"
        );
      }).join("");
    }

    const urgCusts = ov.urgent_debt_customers || [];
    const tbDebt = el("tbody-urgent-debt");
    if (!urgCusts.length) {
      tbDebt.innerHTML = '<tr><td colspan="4" class="u60-sub">Không có khách nào nợ quá hạn bất thường.</td></tr>';
    } else {
      tbDebt.innerHTML = urgCusts.map(function (c) {
        return (
          "<tr>" +
          '<td><div class="font-semibold">' + esc(displayCustomerName(c)) + '</div><div class="u60-sub">' + esc(displayCustomerCode(c)) + "</div></td>" +
          '<td class="col-num font-semibold text-red-700">' + fmtInt(c.oldest_unpaid_days) + " ngày · " + fmtInt(c.open_invoices) + " đơn</td>" +
          '<td class="col-num font-semibold">' + esc(fmtMoney(c.outstanding_vnd, c.outstanding_index)) + "</td>" +
          '<td class="col-wrap"><div class="font-semibold text-slate-900">' + esc(c.reason_vi) + '</div><div class="u60-sub">' + esc(c.action_vi) + "</div></td>" +
          "</tr>"
        );
      }).join("");
    }

    const syncSum = (rep.sync_summary && rep.sync_summary.summary_vi) ? rep.sync_summary.summary_vi : "Đã nạp dữ liệu vào máy cục bộ.";
    const arChk = rep.ar_checksum || {};
    const arTxt = arChk.checked
      ? ("Đối chiếu dòng Cộng sổ công nợ MISA: khớp " + fmtInt(arChk.blocks_matched) + "/" + fmtInt(arChk.blocks_total) + " khách hàng.")
      : "Chưa kiểm tra dòng Cộng sổ công nợ.";
    const breakdown = (rep.files_breakdown_vi || []).map(function (line) {
      return "<div>• " + esc(line) + "</div>";
    }).join("");
    el("overview-file-check").innerHTML =
      "<div>• <strong>Đồng bộ chống trùng:</strong> " + esc(syncSum) + "</div>" +
      breakdown +
      "<div>• <strong>Kiểm chứng sổ công nợ &amp; dòng tiền:</strong> " + esc(arTxt) + "</div>";
  }

  /* ==================== MỤC 2: KHÁM BỆNH KHO & LỌC THEO KHO/HÃNG ==================== */

  function renderHealthTab(d) {
    const m2 = d.m2 || {};
    const flowMode = Boolean(m2.flow_mode_active);
    const multSuffix = Math.abs(state.seasonalMult - 1.0) > 1e-4 ? (" · Hệ số: " + SEASONAL_LABELS[String(state.seasonalMult)]) : "";

    el("health-active-model-note").textContent =
      "Dự kiến tuần tới & số thùng đề xuất đang tính theo: " +
      (d.model_settings.primary_model_label_vi || "") + multSuffix;
    el("th-health-stock").textContent = flowMode ? "Đợt nhập cuối & Đã bán" : "Tồn hiện tại";

    // Bảng 1: Bán chạy nhất
    const sellers = m2.top_sellers || [];
    el("tbody-top-sellers").innerHTML = sellers.length
      ? sellers.map(function (x) {
          return (
            "<tr>" +
            '<td><div class="font-semibold">' + esc(displayProductName(x)) + '</div><div class="u60-sub">' + esc(displayProductCode(x)) + " · " + esc(displayWarehouse(x)) + "</div></td>" +
            '<td class="col-num">' + fmtQty(x.paid_qty) + " " + esc(x.base_uom) + "</td>" +
            '<td class="col-num">' + (x.gift_qty > 0 ? (fmtQty(x.gift_qty) + " " + esc(x.base_uom)) : "0") + "</td>" +
            '<td class="col-num font-semibold">' + esc(fmtMoney(x.net_revenue_vnd, x.revenue_index)) + "</td>" +
            "</tr>"
          );
        }).join("")
      : '<tr><td colspan="4" class="u60-sub">Chưa có dữ liệu bán hàng.</td></tr>';

    // Bảng 2: Kiếm lời nhiều nhất
    const profits = m2.top_profit || [];
    el("tbody-top-profit").innerHTML = profits.length
      ? profits.map(function (x) {
          return (
            "<tr>" +
            '<td><div class="font-semibold">' + esc(displayProductName(x)) + '</div><div class="u60-sub">' + esc(displayProductCode(x)) + " · " + esc(displayWarehouse(x)) + "</div></td>" +
            '<td class="col-num text-slate-700">' + (x.gift_cogs_vnd > 0 ? esc(fmtMoney(x.gift_cogs_vnd)) : "0 đ") + "</td>" +
            '<td class="col-num font-semibold text-emerald-700">' + esc(fmtMoney(x.gross_profit_vnd, x.profit_index)) + "</td>" +
            '<td class="col-num">' + esc(x.gross_margin_display_vi || fmtPct(x.gross_margin_pct)) + "</td>" +
            "</tr>"
          );
        }).join("")
      : '<tr><td colspan="4" class="u60-sub">Chưa có dữ liệu lãi gộp đã tính giá vốn.</td></tr>';

    // Bảng 3: Sắp cạn đợt nhập hoặc Hay bị hết hàng
    const adjItems = getAdjustedItems();
    const stockouts = flowMode
      ? adjItems.filter(function (x) { return x.suggested_boxes > 0; }).slice(0, 10)
      : (m2.frequent_stockouts || []).map(function (x) { return computeAdjustedItem(x, flowMode); });

    if (flowMode) {
      el("title-table-3").textContent = "Bảng 3 — Hàng bán mạnh cần đặt nhập đợt mới";
      el("sub-table-3").textContent = "Đối chiếu từ Sổ mua hàng & Sổ bán hàng: đợt nhập gần nhất đã tiêu thụ mạnh";
      el("thead-table-3").innerHTML =
        '<th>Mặt hàng</th><th>Nhịp nhập &amp; bán gần đây</th><th class="col-num">Dự kiến tuần tới</th><th>Đề xuất đặt tới</th>';
      el("tbody-frequent-stockouts").innerHTML = stockouts.length
        ? stockouts.map(function (x) {
            return (
              "<tr>" +
              '<td><div class="font-semibold">' + esc(displayProductName(x)) + '</div><div class="u60-sub">' + esc(displayProductCode(x)) + " · " + esc(displayWarehouse(x)) + "</div></td>" +
              '<td class="u60-sub">' + esc(formatStockFlowCell(x, true)) + "</td>" +
              '<td class="col-num font-semibold text-blue-700">' + fmtQty(x.forecast_next_1w) + " " + esc(x.base_uom) + "</td>" +
              '<td class="col-wrap font-semibold text-blue-700">' + esc(x.action_vi) + "</td>" +
              "</tr>"
            );
          }).join("")
        : '<tr><td colspan="4" class="u60-sub">Các đợt nhập hàng gần đây vẫn còn đủ bán.</td></tr>';
    } else {
      el("title-table-3").textContent = "Bảng 3 — Hàng hay bị hết hàng trong kỳ";
      el("sub-table-3").textContent = "Phát hiện từ những ngày chạm 0 trên sổ tồn + gợi ý số thùng nhập";
      el("thead-table-3").innerHTML =
        '<th>Mặt hàng</th><th class="col-num">Số ngày hết</th><th class="col-num">Ước tính hụt thu</th><th>Đề xuất đặt tới</th>';
      el("tbody-frequent-stockouts").innerHTML = stockouts.length
        ? stockouts.map(function (x) {
            const boxHint = x.suggested_boxes > 0
              ? x.action_vi
              : (x.stockout_ref_boxes > 0
                  ? ("Lần tới nên nhập ~" + fmtInt(x.stockout_ref_boxes) + " " + String(x.suggested_box_uom || "thùng").toLowerCase())
                  : "Đang còn đủ bán");
            return (
              "<tr>" +
              '<td><div class="font-semibold">' + esc(displayProductName(x)) + '</div><div class="u60-sub">' + fmtInt(x.stockout_events) + " đợt chạm 0</div></td>" +
              '<td class="col-num font-semibold text-red-700">' + fmtInt(x.stockout_days) + " ngày</td>" +
              '<td class="col-num">' + esc(fmtMoney(x.estimated_lost_revenue_vnd)) + "</td>" +
              '<td class="col-wrap font-semibold text-blue-700">' + esc(boxHint) + "</td>" +
              "</tr>"
            );
          }).join("")
        : '<tr><td colspan="4" class="u60-sub">Không phát hiện mặt hàng nào bị hết sạch trong kỳ.</td></tr>';
    }

    // Bảng 4: Nằm im lâu / Bán chậm / Hàng KM dư
    const slows = m2.slow_overstock_promo || [];
    el("tbody-slow-promo").innerHTML = slows.length
      ? slows.map(function (x) {
          return (
            "<tr>" +
            '<td><div class="font-semibold">' + esc(displayProductName(x)) + '</div><div class="u60-sub">' + esc(displayProductCode(x)) + " · " + esc(displayWarehouse(x)) + "</div></td>" +
            '<td class="u60-sub">' + esc(formatStockFlowCell(x, flowMode)) + "</td>" +
            '<td class="col-num">' + fmtInt(x.days_since_last_sale) + " ngày</td>" +
            '<td class="col-wrap">' + renderBadge(x.status_vi, x.badge_tone) + '<div class="u60-sub mt-1">' + esc(x.action_vi) + "</div></td>" +
            "</tr>"
          );
        }).join("")
      : '<tr><td colspan="4" class="u60-sub">Không có mặt hàng nằm im quá 60 ngày hoặc hàng KM dư.</td></tr>';

    renderAllHealthItemsTable();
  }

  function renderAllHealthItemsTable() {
    if (!state.dashboard || !state.dashboard.m2) return;
    const flowMode = Boolean(state.dashboard.m2.flow_mode_active);
    const items = getAdjustedItems();
    const wh = state.selectedWarehouse;
    const q = foldVi(state.healthQuery);
    const f = state.healthFilter;

    // 1. Lọc theo Kho / Hãng trước để tính dòng tổng hợp đặt hàng cho hãng đó
    const whItems = items.filter(function (x) {
      return wh === "all" || (x.warehouse_code || "KHO_CHINH") === wh;
    });
    const whReorder = whItems.filter(function (x) { return x.suggested_boxes > 0; });
    const whBoxes = whReorder.reduce(function (s, x) { return s + Number(x.suggested_boxes || 0); }, 0);
    const whCost = whReorder.reduce(function (s, x) { return s + Number(x.estimated_order_cost_vnd || 0); }, 0);
    const whSample = whItems[0] || {};
    const whTitle = wh === "all"
      ? "Toàn bộ kho"
      : ("Kho / Hãng " + displayWarehouseByKey(wh, whSample.warehouse_anon));

    el("warehouse-order-summary").innerHTML =
      "<div><strong>" + esc(whTitle) + ":</strong> " +
      '<span class="font-bold text-blue-700">' + fmtInt(whReorder.length) + " món cần đặt</span> · " +
      "Tổng <strong>" + fmtInt(whBoxes) + " thùng</strong> · " +
      "Ước tính tiền nhập: <strong>" + esc(fmtMoney(whCost)) + "</strong></div>" +
      '<div class="u60-sub">Nút Xuất file Excel (.csv) góc trên phải sẽ xuất đúng kho đang chọn</div>';

    // 2. Lọc tiếp theo trạng thái & từ khóa tìm kiếm
    const filtered = whItems.filter(function (x) {
      if (f === "reorder" && !(x.suggested_boxes > 0)) return false;
      if (f === "slow" && ["dead_stock", "overstock"].indexOf(x.status_code) === -1) return false;
      if (f === "promo" && !x.is_promo_sku && x.status_code !== "promo_surplus") return false;
      if (f === "inactive" && ["inactive_zero", "no_stock_book"].indexOf(x.status_code) === -1) return false;
      if (q) {
        const hay = foldVi(displayProductName(x) + " " + displayProductCode(x) + " " + displayWarehouse(x));
        if (hay.indexOf(q) === -1) return false;
      }
      return true;
    });

    const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
    if (state.healthPage > totalPages) state.healthPage = totalPages;
    if (state.healthPage < 1) state.healthPage = 1;
    const startIdx = (state.healthPage - 1) * PAGE_SIZE;
    const pageRows = filtered.slice(startIdx, startIdx + PAGE_SIZE);

    el("pager-info-health").textContent =
      "Hiển thị " + (filtered.length ? (startIdx + 1) : 0) + "–" + Math.min(filtered.length, startIdx + PAGE_SIZE) +
      " trên tổng " + fmtInt(filtered.length) + " mặt hàng (Trang " + state.healthPage + "/" + totalPages + ")";
    el("btn-prev-health").disabled = state.healthPage <= 1;
    el("btn-next-health").disabled = state.healthPage >= totalPages;

    const tb = el("tbody-all-health");
    if (!pageRows.length) {
      tb.innerHTML = '<tr><td colspan="8" class="u60-sub">Không có mặt hàng nào khớp bộ lọc đang chọn.</td></tr>';
      return;
    }

    tb.innerHTML = pageRows.map(function (x) {
      return (
        "<tr>" +
        '<td><div class="font-semibold">' + esc(displayProductName(x)) + '</div><div class="u60-sub">' + esc(displayProductCode(x)) + (x.is_promo_sku ? " · Hàng KM" : "") + "</div></td>" +
        "<td>" + esc(displayWarehouse(x)) + "</td>" +
        '<td class="u60-sub font-medium text-slate-800">' + esc(formatStockFlowCell(x, flowMode)) + "</td>" +
        '<td class="col-num">' + fmtQty(x.avg_weekly_qty) + " " + esc(x.base_uom) + "</td>" +
        '<td class="col-num font-semibold text-blue-700">' + fmtQty(x.forecast_next_1w) + " " + esc(x.base_uom) + "</td>" +
        '<td class="u60-sub">' + esc(x.days_of_supply_vi) + "</td>" +
        "<td>" + renderBadge(x.status_vi, x.badge_tone) + "</td>" +
        '<td class="col-wrap"><div class="font-semibold text-slate-900">' + esc(x.action_vi) + '</div><div class="u60-sub mt-1">' + esc(x.forecast_reason_vi) + "</div></td>" +
        "</tr>"
      );
    }).join("");
  }

  /* ==================== MỤC 3: DỰ BÁO NHẬP HÀNG & HỆ SỐ MÙA VỤ ==================== */

  function renderForecastTab(d) {
    const m1 = d.m1 || {};
    const adviceEl = el("forecast-advice-banner");
    if (adviceEl) {
      adviceEl.textContent = m1.warehouse_forecast_advice_vi || "Chọn cách dự báo phù hợp với đặc thù mặt hàng và độ dày sổ hiện tại của kho.";
    }

    document.querySelectorAll("#seasonal-mult-pills [data-mult]").forEach(function (b) {
      b.classList.toggle("is-active", Math.abs(Number(b.getAttribute("data-mult")) - state.seasonalMult) < 1e-4);
    });

    const models = m1.models || [];
    const byId = {};
    models.forEach(function (m) {
      byId[m.model_id] = m;
    });

    const leaders = m1.leaders || {};
    const mAcc = byId[leaders.most_accurate] || {};
    const mSo = byId[leaders.least_stockout_risk] || {};
    const mOv = byId[leaders.least_overstock_risk] || {};

    el("leader-accurate-name").textContent = mAcc.label_vi || "—";
    el("leader-accurate-sub").textContent = mAcc.accuracy_label_vi || "—";

    el("leader-stockout-name").textContent = mSo.label_vi || "—";
    el("leader-stockout-sub").textContent = mSo.under_forecast_risk_pct != null
      ? ("Rủi ro hụt hàng thấp nhất (~" + fmtPct(mSo.under_forecast_risk_pct) + ")")
      : "—";

    el("leader-overstock-name").textContent = mOv.label_vi || "—";
    el("leader-overstock-sub").textContent = mOv.over_forecast_risk_pct != null
      ? ("Rủi ro đoán dư thấp nhất (~" + fmtPct(mOv.over_forecast_risk_pct) + ")")
      : "—";

    const primaryId = d.model_settings.primary_model_id;
    const compareIds = d.model_settings.compare_model_ids || [];

    el("tbody-arena-models").innerHTML = models.map(function (m) {
      const isPrimary = m.model_id === primaryId;
      const isComp = compareIds.indexOf(m.model_id) !== -1;
      const styleBadge = renderBadge(m.demand_style_vi || "Đa dụng", m.style_badge_tone || "neutral");
      const readyBadge = m.recommended_for_current_data
        ? renderBadge(m.data_readiness_vi, "success")
        : renderBadge(m.data_readiness_vi, "warning");
      const accTxt = m.accuracy_est_pct != null ? ("~" + fmtPct(m.accuracy_est_pct)) : "Chưa đủ tuần";
      const underTxt = m.under_forecast_risk_pct != null ? fmtPct(m.under_forecast_risk_pct) : "—";
      const overTxt = m.over_forecast_risk_pct != null ? fmtPct(m.over_forecast_risk_pct) : "—";

      const isRec = m.model_id === m1.recommended_model_id;
      const winAcc = m.model_id === leaders.most_accurate && m.accuracy_est_pct != null;
      const winSo = m.model_id === leaders.least_stockout_risk && m.under_forecast_risk_pct != null;
      const winOv = m.model_id === leaders.least_overstock_risk && m.over_forecast_risk_pct != null;

      const namePrefix = isRec ? '<span title="Cách dự báo tối ưu nhất cho kho hiện tại">🏆 </span>' : "";
      const accHtml = (winAcc ? '<span title="Dẫn đầu độ chính xác chu kỳ">🥇 </span>' : "") + esc(accTxt);
      const underHtml = (winSo ? '<span title="Rủi ro đoán thiếu thấp nhất">🥇 </span>' : "") + esc(underTxt);
      const overHtml = (winOv ? '<span title="Rủi ro đoán dư thấp nhất">🥇 </span>' : "") + esc(overTxt);

      const accCls = winAcc ? "col-num font-bold text-emerald-700" : "col-num font-semibold text-slate-900";
      const soCls = winSo ? "col-num font-bold text-emerald-700" : "col-num";
      const ovCls = winOv ? "col-num font-bold text-emerald-700" : "col-num";

      const actionHtml = isPrimary
        ? renderBadge("Đang dùng làm cách chính", "success")
        : (
            '<div class="flex items-center gap-2 flex-wrap">' +
            '<button type="button" class="btn-secondary u60-btn-sm js-set-primary" data-model="' + esc(m.model_id) + '">Chọn làm chính</button>' +
            '<label class="inline-flex items-center gap-1 u60-sub cursor-pointer select-none">' +
            '<input type="checkbox" class="accent-slate-900 js-toggle-compare" data-model="' + esc(m.model_id) + '"' + (isComp ? " checked" : "") + " />" +
            "<span>So sánh</span></label></div>"
          );

      return (
        "<tr" + (isPrimary ? ' class="row-selected"' : "") + ">" +
        '<td><div class="font-bold text-slate-900">' + namePrefix + esc(m.label_vi) + '</div><div class="u60-sub mt-1">' + esc(m.short_desc_vi) + "</div></td>" +
        '<td class="col-wrap">' + styleBadge + '<div class="u60-sub mt-1">' + esc(m.best_for_goods_vi || "") + "</div></td>" +
        '<td class="col-wrap">' + readyBadge + "</td>" +
        '<td class="' + accCls + '">' + accHtml + "</td>" +
        '<td class="' + soCls + '">' + underHtml + "</td>" +
        '<td class="' + ovCls + '">' + overHtml + "</td>" +
        "<td>" + actionHtml + "</td>" +
        "</tr>"
      );
    }).join("");

    renderSkuForecastTable();
    renderForecastCharts();
  }

  function renderSkuForecastTable() {
    if (!state.dashboard || !state.dashboard.m2) return;
    const d = state.dashboard;
    const models = (d.m1 && d.m1.models) || [];
    const labelMap = {};
    models.forEach(function (m) { labelMap[m.model_id] = m.label_vi; });

    const primaryLabel = d.model_settings.primary_model_label_vi || "Cách chính";
    const compIds = d.model_settings.compare_model_ids || [];
    const mult = state.seasonalMult;

    const shortTags = {
      ma4: "TB 4 tuần",
      ma8: "TB 8 tuần",
      median8: "Trung vị 8T",
      ses: "Làm mượt",
      holt: "Theo đà",
      seasonal_naive: "Cùng kỳ",
      croston_sba: "Bán thưa",
      tsb: "Rất thưa",
      ridge_lag: "Hồi quy tuần",
      lgbm_lag: "Học máy cây",
      best_overall: "Tổ hợp chuẩn",
    };

    let headHtml =
      "<tr><th>Mặt hàng</th><th>Nhịp bán</th>" +
      '<th class="col-num">TB 4 tuần</th>' +
      '<th class="col-num" title="Cách chính: ' + esc(primaryLabel) + '">Tuần tới</th>' +
      '<th class="col-num">4 tuần tới</th>';
    compIds.forEach(function (cid) {
      const fullLbl = labelMap[cid] || cid;
      const shortLbl = shortTags[cid] || fullLbl.split("(")[0].trim().slice(0, 14);
      headHtml += '<th class="col-num" title="Tham khảo (1 tuần tới): ' + esc(fullLbl) + '">TK: ' + esc(shortLbl) + "</th>";
    });
    headHtml += "<th>Lý do dự báo</th></tr>";
    el("thead-sku-forecasts").innerHTML = headHtml;

    const allAdj = getAdjustedItems();
    const groupCounts = {};
    allAdj.forEach(function (x) {
      const g = x.demand_group_vi || "Khác";
      groupCounts[g] = (groupCounts[g] || 0) + 1;
    });
    const groupOrder = [
      "Bán đều ổn định",
      "Bán đều · Có đợt tăng vọt",
      "Bán thưa theo đợt",
      "Hàng bán thưa",
      "Chưa phát sinh",
    ];
    const groupKeys = Object.keys(groupCounts).sort(function (a, b) {
      const ia = groupOrder.indexOf(a);
      const ib = groupOrder.indexOf(b);
      return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    });
    if (state.forecastGroup !== "all" && !groupCounts[state.forecastGroup]) {
      state.forecastGroup = "all";
    }
    const grpPillsEl = el("forecast-group-pills");
    if (grpPillsEl) {
      let pillsHtml =
        '<button type="button" class="filter-pill' + (state.forecastGroup === "all" ? " is-active" : "") + '" data-fc-group="all">Tất cả (' + fmtInt(allAdj.length) + ")</button>";
      groupKeys.forEach(function (gk) {
        const activeCls = state.forecastGroup === gk ? " is-active" : "";
        pillsHtml +=
          '<button type="button" class="filter-pill' + activeCls + '" data-fc-group="' + esc(gk) + '">' + esc(gk) + " (" + fmtInt(groupCounts[gk]) + ")</button>";
      });
      grpPillsEl.innerHTML = pillsHtml;
    }
    document.querySelectorAll("#forecast-unit-pills [data-fc-unit]").forEach(function (b) {
      b.classList.toggle("is-active", b.getAttribute("data-fc-unit") === state.forecastUnitMode);
    });

    const isBoxMode = state.forecastUnitMode === "box";
    function fmtFcCell(rawQty, item) {
      const qVal = Number(rawQty || 0);
      if (!isBoxMode) {
        return fmtQty(qVal);
      }
      const upb = Math.max(1, Number(item.units_per_box || 1));
      const boxLbl = String(item.suggested_box_uom || item.box_uom || "Thùng").toLowerCase();
      if (upb > 1) {
        const boxes = qVal <= 0 ? 0 : Math.ceil(qVal / upb);
        return fmtInt(boxes) + "\u00a0" + boxLbl;
      }
      const rounded = qVal <= 0 ? 0 : Math.ceil(qVal);
      return fmtInt(rounded) + "\u00a0" + String(item.base_uom || "đv").toLowerCase();
    }

    const q = foldVi(state.forecastQuery);
    const items = allAdj.filter(function (x) {
      if (state.forecastGroup !== "all" && (x.demand_group_vi || "Khác") !== state.forecastGroup) {
        return false;
      }
      if (!q) return true;
      return foldVi(displayProductName(x) + " " + displayProductCode(x)).indexOf(q) !== -1;
    });

    const totalPages = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
    if (state.forecastPage > totalPages) state.forecastPage = totalPages;
    if (state.forecastPage < 1) state.forecastPage = 1;
    const startIdx = (state.forecastPage - 1) * PAGE_SIZE;
    const pageRows = items.slice(startIdx, startIdx + PAGE_SIZE);

    el("pager-info-forecast").textContent =
      "Hiển thị " + (items.length ? (startIdx + 1) : 0) + "–" + Math.min(items.length, startIdx + PAGE_SIZE) +
      " trên tổng " + fmtInt(items.length) + " mặt hàng (Trang " + state.forecastPage + "/" + totalPages + ")";
    el("btn-prev-forecast").disabled = state.forecastPage <= 1;
    el("btn-next-forecast").disabled = state.forecastPage >= totalPages;

    if (!pageRows.length) {
      el("tbody-sku-forecasts").innerHTML =
        '<tr><td colspan="' + (6 + compIds.length) + '" class="u60-sub">Không tìm thấy mặt hàng phù hợp.</td></tr>';
      return;
    }

    el("tbody-sku-forecasts").innerHTML = pageRows.map(function (x) {
      const upb = Math.max(1, Number(x.units_per_box || 1));
      const boxLbl = x.suggested_box_uom || x.box_uom || "Thùng";
      const uomMeta = (isBoxMode && upb > 1)
        ? (fmtInt(upb) + " " + x.base_uom + "/" + boxLbl)
        : x.base_uom;
      let rowHtml =
        "<tr>" +
        '<td><div class="font-semibold">' + esc(displayProductName(x)) + '</div><div class="u60-sub">' + esc(displayProductCode(x)) + " (" + esc(uomMeta) + ")</div></td>" +
        "<td>" + esc(x.demand_group_vi) + "</td>" +
        '<td class="col-num" title="' + esc(fmtQty(x.avg_weekly_qty) + " " + x.base_uom) + '">' + esc(fmtFcCell(x.avg_weekly_qty, x)) + "</td>" +
        '<td class="col-num font-bold text-blue-700" title="' + esc(fmtQty(x.forecast_next_1w) + " " + x.base_uom) + '">' + esc(fmtFcCell(x.forecast_next_1w, x)) + "</td>" +
        '<td class="col-num font-semibold" title="' + esc(fmtQty(x.forecast_next_4w) + " " + x.base_uom) + '">' + esc(fmtFcCell(x.forecast_next_4w, x)) + "</td>";
      compIds.forEach(function (cid) {
        const cObj = (x.comparisons && x.comparisons[cid]) || null;
        const cVal = cObj ? (cObj.next_1w * mult) : null;
        rowHtml += '<td class="col-num text-slate-700"' + (cVal != null ? (' title="' + esc(fmtQty(cVal) + " " + x.base_uom) + '"') : "") + ">" + (cVal != null ? esc(fmtFcCell(cVal, x)) : "—") + "</td>";
      });
      const reasonTxt = Math.abs(mult - 1.0) > 1e-4
        ? (x.forecast_reason_vi + " · Đã nhân hệ số " + SEASONAL_LABELS[String(mult)])
        : x.forecast_reason_vi;
      rowHtml += '<td class="col-wrap u60-sub">' + esc(reasonTxt) + "</td></tr>";
      return rowHtml;
    }).join("");
  }

  function renderForecastCharts() {
    destroyChart("forecast");
    if (!window.Chart || !state.dashboard || !state.dashboard.m1) return;
    const series = state.dashboard.m1.eval_weekly_series || [];
    const canvas = el("chart-forecast-compare");
    if (!canvas || !series.length) return;

    const models = state.dashboard.m1.models || [];
    const labelMap = {};
    models.forEach(function (m) { labelMap[m.model_id] = m.label_vi; });

    const pId = state.dashboard.model_settings.primary_model_id;
    const compIds = state.dashboard.model_settings.compare_model_ids || [];
    const labels = series.map(function (r) { return fmtDateVi(r.week_start); });

    const datasets = [
      {
        label: "Thực bán của kho",
        data: series.map(function (r) { return r.actual; }),
        borderColor: "#0f172a",
        backgroundColor: "rgba(15, 23, 42, 0.08)",
        borderWidth: 3,
        tension: 0.2,
      },
      {
        label: "Cách chính: " + (labelMap[pId] || pId),
        data: series.map(function (r) { return r[pId] != null ? r[pId] : 0; }),
        borderColor: "#059669",
        borderWidth: 2.5,
        borderDash: [4, 3],
        tension: 0.2,
      },
    ];

    const palette = ["#2563eb", "#d97706"];
    compIds.forEach(function (cid, idx) {
      datasets.push({
        label: "So sánh: " + (labelMap[cid] || cid),
        data: series.map(function (r) { return r[cid] != null ? r[cid] : 0; }),
        borderColor: palette[idx % palette.length],
        borderWidth: 2,
        borderDash: [2, 2],
        tension: 0.2,
      });
    });

    state.charts.forecast = new window.Chart(canvas.getContext("2d"), {
      type: "line",
      data: { labels: labels, datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: { legend: { position: "top" } },
        scales: {
          y: { beginAtZero: true, grace: "12%", title: { display: true, text: "Tổng số lượng xuất bán toàn kho" } },
        },
      },
    });
  }

  /* ==================== MỤC 4: CÔNG NỢ & BIỂU ĐỒ 12 THÁNG + THANH TỶ LỆ ==================== */

  function renderCreditTab(d) {
    const m3 = d.m3 || {};
    el("credit-style-banner").textContent = m3.warehouse_style_note_vi || "Chưa có dữ liệu công nợ.";

    const groups = m3.groups_summary || {};
    ["call_now", "slow_but_pays", "new_customer", "trusted"].forEach(function (gid) {
      const g = groups[gid] || { customers_count: 0, outstanding_vnd: 0 };
      const cntEl = el("grp-count-" + gid);
      const amtEl = el("grp-amt-" + gid);
      if (cntEl) cntEl.textContent = fmtInt(g.customers_count || 0) + " khách";
      if (amtEl) {
        amtEl.textContent = state.hideSensitive
          ? ("Chiếm " + fmtPct(g.outstanding_share_pct || 0) + " tổng nợ")
          : ("Đang nợ: " + fmtMoney(g.outstanding_vnd || 0));
      }
    });

    document.querySelectorAll("[data-credit-group]").forEach(function (card) {
      card.classList.toggle("is-selected", card.getAttribute("data-credit-group") === state.creditFilter);
    });
    document.querySelectorAll("[data-credit-filter]").forEach(function (pill) {
      pill.classList.toggle("is-active", pill.getAttribute("data-credit-filter") === state.creditFilter);
    });
    document.querySelectorAll("#cashflow-range-pills [data-cf-months]").forEach(function (btn) {
      btn.classList.toggle("is-active", Number(btn.getAttribute("data-cf-months")) === state.cashflowMonths);
    });

    renderCreditCustomersTable();

    const cf = m3.cashflow || {};
    const hl = cf.highlights_vi || [];
    el("cashflow-highlights").innerHTML = hl.length
      ? hl.map(function (line) { return "<li>• " + esc(line) + "</li>"; }).join("")
      : "<li>Chưa có dữ liệu dòng tiền theo tháng.</li>";

    renderCashflowCharts();
  }

  function renderCreditCustomersTable() {
    if (!state.dashboard || !state.dashboard.m3) return;
    const custs = state.dashboard.m3.customers || [];
    const q = foldVi(state.creditQuery);
    const f = state.creditFilter;

    const filtered = custs.filter(function (c) {
      if (f !== "all" && c.group_id !== f) return false;
      if (q) {
        const hay = foldVi(displayCustomerName(c) + " " + displayCustomerCode(c));
        if (hay.indexOf(q) === -1) return false;
      }
      return true;
    });

    const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
    if (state.creditPage > totalPages) state.creditPage = totalPages;
    if (state.creditPage < 1) state.creditPage = 1;
    const startIdx = (state.creditPage - 1) * PAGE_SIZE;
    const pageRows = filtered.slice(startIdx, startIdx + PAGE_SIZE);

    el("pager-info-credit").textContent =
      "Hiển thị " + (filtered.length ? (startIdx + 1) : 0) + "–" + Math.min(filtered.length, startIdx + PAGE_SIZE) +
      " trên tổng " + fmtInt(filtered.length) + " khách hàng (Trang " + state.creditPage + "/" + totalPages + ")";
    el("btn-prev-credit").disabled = state.creditPage <= 1;
    el("btn-next-credit").disabled = state.creditPage >= totalPages;

    const tb = el("tbody-credit-customers");
    if (!pageRows.length) {
      tb.innerHTML = '<tr><td colspan="7" class="u60-sub">Không có khách hàng nào thuộc nhóm đang lọc.</td></tr>';
      return;
    }

    tb.innerHTML = pageRows.map(function (c) {
      return (
        "<tr>" +
        '<td><div class="font-semibold">' + esc(displayCustomerName(c)) + '</div><div class="u60-sub">' + esc(displayCustomerCode(c)) + "</div></td>" +
        "<td>" + renderBadge(c.group_label_vi, c.badge_tone) + "</td>" +
        '<td class="col-num">' + fmtInt(c.open_invoices) + " / " + fmtInt(c.total_invoices) + " đơn</td>" +
        '<td class="col-num">~' + fmtQty(c.usual_pay_days) + " ngày</td>" +
        '<td class="col-num font-semibold ' + (c.group_id === "call_now" ? "text-red-700" : "") + '">' + (c.open_invoices > 0 ? (fmtInt(c.oldest_unpaid_days) + " ngày") : "Đã trả hết") + "</td>" +
        '<td class="col-num font-bold">' + esc(fmtMoney(c.outstanding_vnd, c.outstanding_index)) + "</td>" +
        '<td class="col-wrap"><div class="font-semibold text-slate-900">' + esc(c.reason_vi) + '</div><div class="u60-sub mt-1">' + esc(c.action_vi) + "</div></td>" +
        "</tr>"
      );
    }).join("");
  }

  function renderPaymentMixBars(mix) {
    const wrap = el("payment-mix-bars");
    if (!wrap) return;
    if (!mix || !mix.length) {
      wrap.innerHTML = '<div class="u60-sub">Chưa có dữ liệu phương thức thu tiền.</div>';
      return;
    }
    const segClasses = ["u60-mix-seg-0", "u60-mix-seg-1", "u60-mix-seg-2", "u60-mix-seg-3"];
    const segs = mix.map(function (m, idx) {
      const w = Math.max(2, Number(m.share_pct || 0));
      const segCls = segClasses[idx % 4];
      return '<div class="' + segCls + '" style="width:' + w + '%" title="' + esc(m.label_vi + ": " + fmtPct(m.share_pct)) + '"></div>';
    }).join("");

    const rows = mix.map(function (m, idx) {
      const segCls = segClasses[idx % 4];
      const amtPart = state.hideSensitive
        ? ("Chỉ số " + fmtQty(m.amount_index || 0) + "/100")
        : fmtMoney(m.amount_vnd);
      return (
        '<div class="u60-mix-row">' +
        '<div class="flex items-center gap-2"><span class="u60-mix-swatch ' + segCls + '"></span><span class="font-semibold text-slate-900">' + esc(m.label_vi) + "</span></div>" +
        '<div class="num"><span class="font-bold text-slate-900">' + esc(fmtPct(m.share_pct)) + '</span> <span class="u60-sub">(' + esc(amtPart) + ")</span></div>" +
        "</div>"
      );
    }).join("");

    wrap.innerHTML = '<div class="u60-mix-bar">' + segs + '</div><div class="u60-mix-legend">' + rows + "</div>";
  }

  function renderCashflowCharts() {
    destroyChart("cf1");
    destroyChart("cf2");
    destroyChart("cf3a");

    if (!state.dashboard || !state.dashboard.m3) return;
    const cf = state.dashboard.m3.cashflow || {};
    const allMonthly = cf.monthly_cashflow || [];
    const monthly = state.cashflowMonths > 0 ? allMonthly.slice(-state.cashflowMonths) : allMonthly;
    const hist = cf.payment_speed_histogram || [];
    const mix = cf.payment_method_mix || [];

    renderPaymentMixBars(mix);

    if (!window.Chart) return;
    const hide = state.hideSensitive;
    const unitLabel = hide ? "Chỉ số chuẩn hóa (Đỉnh = 100)" : "Triệu đồng (VNĐ)";
    const labelsYm = monthly.map(function (r) { return r.ym; });

    if (monthly.length && el("chart-cf-1")) {
      state.charts.cf1 = new window.Chart(el("chart-cf-1").getContext("2d"), {
        type: "bar",
        data: {
          labels: labelsYm,
          datasets: [
            {
              label: "Tiền thu khách hàng",
              data: monthly.map(function (r) { return hide ? r.cash_in_index : Math.round((r.cash_in_total_vnd / 1e6) * 10) / 10; }),
              backgroundColor: "rgba(5, 150, 105, 0.85)",
            },
            {
              label: "Tiền chi nhập hàng",
              data: monthly.map(function (r) { return hide ? r.cash_out_index : Math.round((r.cash_out_purchase_vnd / 1e6) * 10) / 10; }),
              backgroundColor: "rgba(220, 38, 38, 0.80)",
            },
            {
              type: "line",
              label: "Chênh lệch ròng (Thu trừ Nhập)",
              data: monthly.map(function (r) { return hide ? r.net_index : Math.round((r.net_cashflow_vnd / 1e6) * 10) / 10; }),
              borderColor: "#0f172a",
              backgroundColor: "#0f172a",
              borderWidth: 2.3,
              tension: 0.2,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { position: "top" } },
          scales: { y: { grace: "10%", title: { display: true, text: unitLabel } } },
        },
      });
    }

    if (monthly.length && el("chart-cf-2")) {
      state.charts.cf2 = new window.Chart(el("chart-cf-2").getContext("2d"), {
        type: "bar",
        data: {
          labels: labelsYm,
          datasets: [
            {
              label: "Tiền bán hàng ghi sổ",
              data: monthly.map(function (r) { return hide ? r.sales_index : Math.round((r.sales_invoiced_vnd / 1e6) * 10) / 10; }),
              backgroundColor: "rgba(37, 99, 235, 0.82)",
            },
            {
              label: "Tiền thu thực tế về túi",
              data: monthly.map(function (r) { return hide ? r.cash_in_index : Math.round((r.cash_in_total_vnd / 1e6) * 10) / 10; }),
              backgroundColor: "rgba(16, 185, 129, 0.85)",
            },
            {
              type: "line",
              label: "Tiền khách còn nợ cuối tháng",
              data: monthly.map(function (r) { return hide ? r.ending_ar_index : Math.round((r.ending_ar_vnd / 1e6) * 10) / 10; }),
              borderColor: "#d97706",
              backgroundColor: "#d97706",
              borderWidth: 2.4,
              tension: 0.2,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { position: "top" } },
          scales: { y: { grace: "10%", title: { display: true, text: unitLabel } } },
        },
      });
    }

    if (hist.length && el("chart-cf-3a")) {
      const maxPct = Math.max.apply(null, [10].concat(hist.map(function (b) { return Number(b.share_pct || 0); })));
      const sugMax = Math.min(100, Math.ceil(maxPct * 1.18));
      state.charts.cf3a = new window.Chart(el("chart-cf-3a").getContext("2d"), {
        type: "bar",
        data: {
          labels: hist.map(function (b) { return b.label_vi; }),
          datasets: [
            {
              label: "Tỷ lệ số đơn (%)",
              data: hist.map(function (b) { return b.share_pct; }),
              backgroundColor: ["#059669", "#10b981", "#2563eb", "#f59e0b", "#ea580c", "#dc2626"],
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            y: {
              beginAtZero: true,
              suggestedMax: sugMax,
              grace: "15%",
              title: { display: true, text: "Tỷ lệ đơn (%)" },
            },
          },
        },
      });
    }
  }

  /* ==================== ĐỔI MÔ HÌNH DỰ BÁO TỨC THÌ ==================== */

  async function saveAndApplyModels(primaryId, compareIds) {
    const resp = await fetch("/api/settings/models", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        primary_model_id: primaryId,
        compare_model_ids: compareIds.slice(0, 2),
      }),
    });
    const data = await resp.json();
    if (data && data.ok && data.dashboard) {
      state.dashboard = data.dashboard;
      renderAllDashboard();
    }
  }

  /* ==================== HỘP THOẠI NẠP FILE MISA (.ZIP) & TIẾN TRÌNH ==================== */

  function openUploadModal(canClose) {
    el("upload-modal").classList.remove("is-hidden");
    el("btn-close-upload").classList.toggle("is-hidden", !canClose);
    el("setup-step-select").classList.remove("is-hidden");
    el("setup-step-progress").classList.add("is-hidden");
    el("progress-import-summary").classList.add("is-hidden");
    el("setup-step-done").classList.add("is-hidden");
    el("upload-error-box").classList.add("is-hidden");
  }

  function closeUploadModal() {
    el("upload-modal").classList.add("is-hidden");
  }

  function renderImportSummaryBox(rep) {
    if (!rep) return;
    const box = el("progress-import-summary");
    box.classList.remove("is-hidden");
    const syncTxt = (rep.sync_summary && rep.sync_summary.summary_vi) || "";
    const lines = (rep.files_breakdown_vi || []).map(function (l) {
      return "<div>• " + esc(l) + "</div>";
    }).join("");
    box.innerHTML =
      '<div class="font-semibold text-slate-900">Kết quả đọc tệp ' + esc(rep.zip_filename || ".zip") + ":</div>" +
      "<div>• " + esc(syncTxt) + "</div>" +
      lines;
  }

  async function pollJobUntilDone(jobId) {
    while (true) {
      const resp = await fetch("/api/job/" + encodeURIComponent(jobId));
      const j = await resp.json();
      if (!resp.ok || !j.ok) {
        throw new Error((j && j.detail) || "Không kiểm tra được tiến độ phân tích.");
      }

      const pct = Number(j.progress_pct || 0);
      el("progress-bar-fill").style.width = Math.max(8, Math.min(100, pct)) + "%";
      el("progress-step-text").textContent = j.step_message_vi || "Đang xử lý...";
      el("progress-pct-label").textContent = "Hoàn thành " + Math.round(pct) + "%";

      if (j.status === "failed") {
        throw new Error(j.error || "Có lỗi khi phân tích dữ liệu.");
      }
      if (j.status === "completed") {
        await showSetupDoneStep(j);
        return;
      }
      await new Promise(function (r) { setTimeout(r, 350); });
    }
  }

  async function showSetupDoneStep(jobDone) {
    const dashResp = await fetch("/api/dashboard");
    const dash = await dashResp.json();
    state.dashboard = dash;

    el("setup-step-progress").classList.add("is-hidden");
    el("setup-step-done").classList.remove("is-hidden");
    el("btn-close-upload").classList.remove("is-hidden");

    const recId = (dash.overview && dash.overview.recommended_model_id) || jobDone.recommended_model_id || "best_overall";
    const recLabel = (dash.overview && dash.overview.recommended_model_label_vi) || jobDone.recommended_model_label_vi || "";
    state.setupPrimaryModel = recId;
    state.setupSelectedCompares = (dash.model_settings && dash.model_settings.compare_model_ids) || [];

    const models = (dash.m1 && dash.m1.models) || [];
    const recObj = models.find(function (m) { return m.model_id === recId; }) || {};
    const accPart = recObj.accuracy_label_vi ? (" (" + recObj.accuracy_label_vi + ")") : "";
    el("setup-done-rec-text").innerHTML =
      "Đã tìm ra cách dự báo hợp nhất với kho mình: <strong>" + esc(recLabel) + "</strong>" + esc(accPart) + ".";

    const cands = models.filter(function (m) { return m.model_id !== recId; }).slice(0, 5);
    el("setup-compare-options").innerHTML = cands.map(function (m) {
      const checked = state.setupSelectedCompares.indexOf(m.model_id) !== -1;
      return (
        '<label class="flex items-center justify-between p-2 border border-slate-200 rounded-lg cursor-pointer">' +
        '<div><span class="font-semibold text-sm text-slate-900">' + esc(m.label_vi) + '</span> <span class="u60-sub">· ' + esc(m.accuracy_label_vi || "") + "</span></div>" +
        '<input type="checkbox" class="accent-slate-900 js-setup-compare" value="' + esc(m.model_id) + '"' + (checked ? " checked" : "") + " />" +
        "</label>"
      );
    }).join("");
  }

  async function handleZipFileUpload(file) {
    if (!file) return;
    const errBox = el("upload-error-box");
    errBox.classList.add("is-hidden");

    if (!String(file.name || "").toLowerCase().endsWith(".zip")) {
      errBox.textContent = "Vui lòng chọn tệp nén có đuôi .zip (nén các file Excel xuất từ MISA).";
      errBox.classList.remove("is-hidden");
      return;
    }

    el("setup-step-select").classList.add("is-hidden");
    el("setup-step-progress").classList.remove("is-hidden");
    el("progress-import-summary").classList.add("is-hidden");

    let fakePct = 6;
    el("progress-bar-fill").style.width = fakePct + "%";
    el("progress-step-text").textContent = "Bước 1/4: Đang giải nén & đọc các sổ Excel MISA (vui lòng đợi vài giây)...";
    el("progress-pct-label").textContent = "Hoàn thành " + fakePct + "%";

    const tickTimer = setInterval(function () {
      if (fakePct < 26) {
        fakePct += 2;
        el("progress-bar-fill").style.width = fakePct + "%";
        el("progress-pct-label").textContent = "Hoàn thành " + fakePct + "%";
      }
    }, 650);

    try {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("mode", state.uploadMode);

      const resp = await fetch("/api/upload-zip", { method: "POST", body: fd });
      clearInterval(tickTimer);
      const res = await resp.json();
      if (!resp.ok || !res.ok) {
        throw new Error((res && res.detail) || "Không đọc được tệp .zip này.");
      }

      renderImportSummaryBox(res.import_report);
      await pollJobUntilDone(res.job_id);
    } catch (err) {
      clearInterval(tickTimer);
      el("setup-step-progress").classList.add("is-hidden");
      el("setup-step-select").classList.remove("is-hidden");
      errBox.textContent = err.message || String(err);
      errBox.classList.remove("is-hidden");
    }
  }

  async function handleLoadDemo() {
    const errBox = el("upload-error-box");
    errBox.classList.add("is-hidden");
    el("setup-step-select").classList.add("is-hidden");
    el("setup-step-progress").classList.remove("is-hidden");
    el("progress-import-summary").classList.add("is-hidden");
    el("progress-bar-fill").style.width = "12%";
    el("progress-step-text").textContent = "Đang tạo và nạp kho mẫu MISA 11 tháng...";
    el("progress-pct-label").textContent = "Hoàn thành 12%";

    try {
      const resp = await fetch("/api/load-demo", { method: "POST" });
      const res = await resp.json();
      if (!resp.ok || !res.ok) {
        throw new Error((res && res.detail) || "Không tạo được dữ liệu mẫu.");
      }
      renderImportSummaryBox(res.import_report);
      await pollJobUntilDone(res.job_id);
    } catch (err) {
      el("setup-step-progress").classList.add("is-hidden");
      el("setup-step-select").classList.remove("is-hidden");
      errBox.textContent = err.message || String(err);
      errBox.classList.remove("is-hidden");
    }
  }

  /* ==================== GẮN SỰ KIỆN & KHỞI ĐỘNG ==================== */

  function bindEvents() {
    syncPrivacyButtonUI();
    el("btn-privacy-toggle").addEventListener("click", togglePrivacyMode);
    el("btn-open-upload").addEventListener("click", function () {
      openUploadModal(Boolean(state.dashboard && state.dashboard.has_data));
    });
    el("btn-close-upload").addEventListener("click", closeUploadModal);

    document.querySelectorAll("[data-tab]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        switchTab(btn.getAttribute("data-tab"));
      });
    });

    document.querySelectorAll(".js-jump-tab").forEach(function (btn) {
      btn.addEventListener("click", function () {
        switchTab(btn.getAttribute("data-target"));
      });
    });

    ["btn-overview-export", "btn-health-export", "btn-forecast-export"].forEach(function (id) {
      const b = el(id);
      if (b) b.addEventListener("click", triggerExportCsv);
    });

    // Bộ lọc mốc thời gian Mục 1
    el("overview-period-pills").addEventListener("click", function (ev) {
      const btn = ev.target.closest("[data-period]");
      if (!btn || !state.dashboard) return;
      state.overviewPeriod = btn.getAttribute("data-period") || "all";
      renderOverviewTab(state.dashboard);
    });

    // Hệ số mùa vụ Mục 3 (đồng bộ sang số thùng gợi ý đặt Mục 1 & Mục 2)
    el("seasonal-mult-pills").addEventListener("click", function (ev) {
      const btn = ev.target.closest("[data-mult]");
      if (!btn || !state.dashboard) return;
      state.seasonalMult = Number(btn.getAttribute("data-mult") || 1.0);
      updateSidebarSummary();
      state.dirtyTabs.overview = true;
      state.dirtyTabs.health = true;
      state.dirtyTabs.forecast = true;
      renderCurrentTabIfNeeded();
    });

    document.querySelectorAll("[data-upload-mode]").forEach(function (card) {
      card.addEventListener("click", function () {
        state.uploadMode = card.getAttribute("data-upload-mode") || "merge";
        document.querySelectorAll("[data-upload-mode]").forEach(function (c) {
          c.classList.toggle("is-selected", c.getAttribute("data-upload-mode") === state.uploadMode);
        });
      });
    });

    const dz = el("zip-dropzone");
    const fileInput = el("input-zip-file");
    dz.addEventListener("click", function () { fileInput.click(); });
    dz.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        fileInput.click();
      }
    });
    fileInput.addEventListener("change", function () {
      if (fileInput.files && fileInput.files[0]) {
        handleZipFileUpload(fileInput.files[0]);
        fileInput.value = "";
      }
    });
    dz.addEventListener("dragover", function (ev) {
      ev.preventDefault();
      dz.classList.add("is-dragover");
    });
    dz.addEventListener("dragleave", function () {
      dz.classList.remove("is-dragover");
    });
    dz.addEventListener("drop", function (ev) {
      ev.preventDefault();
      dz.classList.remove("is-dragover");
      if (ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0]) {
        handleZipFileUpload(ev.dataTransfer.files[0]);
      }
    });

    el("btn-load-demo").addEventListener("click", handleLoadDemo);

    el("btn-finish-setup").addEventListener("click", async function () {
      const checked = Array.from(document.querySelectorAll(".js-setup-compare:checked")).map(function (inp) {
        return inp.value;
      }).slice(0, 2);
      if (state.setupPrimaryModel) {
        await saveAndApplyModels(state.setupPrimaryModel, checked);
      } else if (state.dashboard) {
        renderAllDashboard();
      }
      closeUploadModal();
      switchTab("overview");
    });

    // Bộ lọc Kho/Hãng, trạng thái, tìm kiếm & phân trang Mục 2
    el("select-warehouse-health").addEventListener("change", function (ev) {
      state.selectedWarehouse = ev.target.value || "all";
      state.healthPage = 1;
      renderAllHealthItemsTable();
    });
    el("health-filter-pills").addEventListener("click", function (ev) {
      const btn = ev.target.closest("[data-filter]");
      if (!btn) return;
      state.healthFilter = btn.getAttribute("data-filter");
      state.healthPage = 1;
      el("health-filter-pills").querySelectorAll("[data-filter]").forEach(function (b) {
        b.classList.toggle("is-active", b === btn);
      });
      renderAllHealthItemsTable();
    });
    el("input-search-health").addEventListener("input", function (ev) {
      state.healthQuery = ev.target.value || "";
      state.healthPage = 1;
      renderAllHealthItemsTable();
    });
    el("btn-prev-health").addEventListener("click", function () {
      if (state.healthPage > 1) {
        state.healthPage -= 1;
        renderAllHealthItemsTable();
      }
    });
    el("btn-next-health").addEventListener("click", function () {
      state.healthPage += 1;
      renderAllHealthItemsTable();
    });

    // Sự kiện đổi mô hình & phân trang Mục 3
    el("tbody-arena-models").addEventListener("click", function (ev) {
      const btn = ev.target.closest(".js-set-primary");
      if (!btn || !state.dashboard) return;
      const newPrimary = btn.getAttribute("data-model");
      const curComps = (state.dashboard.model_settings.compare_model_ids || []).filter(function (c) {
        return c !== newPrimary;
      });
      saveAndApplyModels(newPrimary, curComps);
    });
    el("tbody-arena-models").addEventListener("change", function (ev) {
      const chk = ev.target.closest(".js-toggle-compare");
      if (!chk || !state.dashboard) return;
      const mid = chk.getAttribute("data-model");
      const curPrimary = state.dashboard.model_settings.primary_model_id;
      let comps = (state.dashboard.model_settings.compare_model_ids || []).slice();
      if (chk.checked) {
        if (comps.indexOf(mid) === -1) comps.push(mid);
        if (comps.length > 2) comps = comps.slice(comps.length - 2);
      } else {
        comps = comps.filter(function (c) { return c !== mid; });
      }
      saveAndApplyModels(curPrimary, comps);
    });
    el("forecast-group-pills").addEventListener("click", function (ev) {
      const btn = ev.target.closest("[data-fc-group]");
      if (!btn) return;
      state.forecastGroup = btn.getAttribute("data-fc-group") || "all";
      state.forecastPage = 1;
      renderSkuForecastTable();
    });
    el("forecast-unit-pills").addEventListener("click", function (ev) {
      const btn = ev.target.closest("[data-fc-unit]");
      if (!btn) return;
      state.forecastUnitMode = btn.getAttribute("data-fc-unit") || "base";
      renderSkuForecastTable();
    });
    el("input-search-forecast").addEventListener("input", function (ev) {
      state.forecastQuery = ev.target.value || "";
      state.forecastPage = 1;
      renderSkuForecastTable();
    });
    el("btn-prev-forecast").addEventListener("click", function () {
      if (state.forecastPage > 1) {
        state.forecastPage -= 1;
        renderSkuForecastTable();
      }
    });
    el("btn-next-forecast").addEventListener("click", function () {
      state.forecastPage += 1;
      renderSkuForecastTable();
    });

    // Bộ lọc nhóm khách, khung tháng biểu đồ, tìm kiếm & phân trang Mục 4
    document.querySelectorAll("[data-credit-group]").forEach(function (card) {
      card.addEventListener("click", function () {
        const g = card.getAttribute("data-credit-group");
        state.creditFilter = state.creditFilter === g ? "all" : g;
        state.creditPage = 1;
        renderCreditTab(state.dashboard);
      });
    });
    el("credit-filter-pills").addEventListener("click", function (ev) {
      const btn = ev.target.closest("[data-credit-filter]");
      if (!btn || !state.dashboard) return;
      state.creditFilter = btn.getAttribute("data-credit-filter");
      state.creditPage = 1;
      renderCreditTab(state.dashboard);
    });
    el("cashflow-range-pills").addEventListener("click", function (ev) {
      const btn = ev.target.closest("[data-cf-months]");
      if (!btn || !state.dashboard) return;
      state.cashflowMonths = Number(btn.getAttribute("data-cf-months") || 12);
      document.querySelectorAll("#cashflow-range-pills [data-cf-months]").forEach(function (b) {
        b.classList.toggle("is-active", b === btn);
      });
      renderCashflowCharts();
    });
    el("input-search-credit").addEventListener("input", function (ev) {
      state.creditQuery = ev.target.value || "";
      state.creditPage = 1;
      renderCreditCustomersTable();
    });
    el("btn-prev-credit").addEventListener("click", function () {
      if (state.creditPage > 1) {
        state.creditPage -= 1;
        renderCreditCustomersTable();
      }
    });
    el("btn-next-credit").addEventListener("click", function () {
      state.creditPage += 1;
      renderCreditCustomersTable();
    });
  }

  async function bootstrap() {
    bindEvents();
    try {
      const stResp = await fetch("/api/status");
      const st = await stResp.json();
      if (st.active_job && st.active_job.job_id) {
        openUploadModal(false);
        el("setup-step-select").classList.add("is-hidden");
        el("setup-step-progress").classList.remove("is-hidden");
        await pollJobUntilDone(st.active_job.job_id);
        return;
      }
      if (!st.has_data) {
        openUploadModal(false);
        return;
      }
      const dashResp = await fetch("/api/dashboard");
      const dash = await dashResp.json();
      if (dash && dash.has_data) {
        state.dashboard = dash;
        renderAllDashboard();
      } else {
        openUploadModal(false);
      }
    } catch (_) {
      openUploadModal(false);
    }
  }

  document.addEventListener("DOMContentLoaded", bootstrap);
})();
