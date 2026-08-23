(() => {
  "use strict";

  // 兜底上下文上限（后端解析失败时才使用；正常由接口返回 context_limit 覆盖）
  const CONTEXT_LIMIT = 1_000_000;
  const COMPRESS_THRESHOLD_PCT = 82;
  const ALERT_THRESHOLD_PCT = 75;
  const POLL_INTERVAL_MS = 10_000;
  const MAIN_PLATFORM_ID = "default";
  const MAIN_USER_ID = "default:FriendMessage:256418297";

  const numberFormatter = new Intl.NumberFormat("zh-CN");
  const state = {
    days: 1,
    trendScope: "global",
    historyScope: "focus",
    focusConvId: null,
    conversations: [],
    turnConfig: null,
    contextLimit: CONTEXT_LIMIT,
    conversationsLoaded: false,
    trendLoaded: false,
    refreshInProgress: false,
    trendRequestId: 0,
    chartPoints: [],
    series: [],
    lastMainToken: null,
    lastMainPercent: null,
    chartGeometry: null,
    hoverIndex: null,
    chartResizeTimer: null,
  };

  const dom = {
    refreshButton: document.querySelector("#refresh-button"),
    refreshStatus: document.querySelector("#refresh-status"),
    errorBanner: document.querySelector("#error-banner"),
    focusTitle: document.querySelector("#focus-title"),
    focusAlertBadge: document.querySelector("#focus-alert-badge"),
    mainTokenUsage: document.querySelector("#main-token-usage"),
    mainUpdatedAt: document.querySelector("#main-updated-at"),
    mainRemaining: document.querySelector("#main-remaining"),
    mainPercent: document.querySelector("#main-percent"),
    mainContextLimit: document.querySelector("#main-context-limit"),
    mainProgress: document.querySelector("#main-progress"),
    mainProgressFill: document.querySelector("#main-progress-fill"),
    mainTurns: document.querySelector("#main-turns"),
    conversationCount: document.querySelector("#conversation-count"),
    conversationEmpty: document.querySelector("#conversation-empty"),
    conversationGrid: document.querySelector("#conversation-grid"),
    rangeButtons: [...document.querySelectorAll("[data-days]")],
    trendScopeButtons: [...document.querySelectorAll("[data-scope]")],
    historyScopeButtons: [...document.querySelectorAll("[data-history-scope]")],
    trendTotalTokens: document.querySelector("#trend-total-tokens"),
    trendTotalCalls: document.querySelector("#trend-total-calls"),
    trendAverage: document.querySelector("#trend-average"),
    chartWrap: document.querySelector("#chart-wrap"),
    chart: document.querySelector("#trend-chart"),
    chartEmpty: document.querySelector("#chart-empty"),
    chartTooltip: document.querySelector("#chart-tooltip"),
    chartLegend: document.querySelector("#chart-legend"),
    providerBreakdown: document.querySelector("#provider-breakdown"),
    historyList: document.querySelector("#history-list"),
    historyEmpty: document.querySelector("#history-empty"),
    historyRefresh: document.querySelector("#history-refresh"),
    bgRing: document.querySelector("#bg-ring"),
    ringPct: document.querySelector("#ring-pct"),
    bgCalls: document.querySelector("#bg-calls"),
    bgToken: document.querySelector("#bg-token"),
  };

  function createElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function asNumber(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function formatNumber(value) {
    return numberFormatter.format(Math.max(0, Math.round(asNumber(value))));
  }

  function animateNumber(element, from, to, format, duration = 600) {
    const safeFrom = Number.isFinite(from) ? from : to;
    if (safeFrom === to || duration <= 0) {
      element.textContent = format(to);
      return;
    }
    const start = performance.now();
    const step = (now) => {
      const progress = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      element.textContent = format(safeFrom + (to - safeFrom) * eased);
      if (progress < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function formatCompact(value) {
    const number = Math.max(0, asNumber(value));
    if (number >= 1_000_000) {
      return `${(number / 1_000_000).toFixed(number >= 10_000_000 ? 1 : 2)}M`;
    }
    if (number >= 1_000) {
      return `${(number / 1_000).toFixed(number >= 100_000 ? 0 : 1)}K`;
    }
    return formatNumber(number);
  }

  function parseDate(value) {
    if (!value) return null;
    let normalized = String(value).trim();
    if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(normalized)) {
      normalized = normalized.replace(" ", "T");
    }
    if (!/[zZ]|[+-]\d{2}:?\d{2}$/.test(normalized)) {
      normalized += "Z";
    }
    const date = new Date(normalized);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function formatDateTime(value) {
    const date = parseDate(value);
    if (!date) return "未知";
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
  }

  function getWaterState(percent) {
    if (percent >= ALERT_THRESHOLD_PCT) return "alert";
    if (percent >= 65) return "caution";
    return "healthy";
  }

  function setErrorState(hasError) {
    dom.errorBanner.hidden = !hasError;
  }

  function setRefreshBusy(busy) {
    dom.refreshButton.disabled = busy;
    dom.refreshButton.classList.toggle("is-loading", busy);
  }

  function markRefreshTime() {
    const time = new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date());
    dom.refreshStatus.textContent = `更新于 ${time}`;
  }

  async function apiGet(endpoint, params) {
    const bridge = window.AstrBotPluginPage;
    if (!bridge || typeof bridge.apiGet !== "function") {
      throw new Error("AstrBot 插件页面桥接尚未就绪");
    }
    return bridge.apiGet(endpoint, params);
  }

  function normalizeConversation(item) {
    const tokenUsage = Math.max(0, asNumber(item?.token_usage));
    const percent = Number.isFinite(Number(item?.percent))
      ? Number(item.percent)
      : (tokenUsage / state.contextLimit) * 100;
    return {
      conversationId: String(item?.conversation_id || ""),
      title: String(item?.title || "未命名会话"),
      displayName: String(item?.display_name || item?.title || "未命名会话"),
      platformId: String(item?.platform_id || "unknown"),
      userId: String(item?.user_id || "unknown"),
      tokenUsage,
      percent: Math.max(0, percent),
      remainingToCompress: Math.max(
        0,
        asNumber(
          item?.remaining_to_compress,
          (state.contextLimit * COMPRESS_THRESHOLD_PCT) / 100 - tokenUsage,
        ),
      ),
      overCompressThreshold:
        Boolean(item?.over_compress_threshold) ||
        percent >= COMPRESS_THRESHOLD_PCT,
      alerting: Boolean(item?.alerting) || percent >= ALERT_THRESHOLD_PCT,
      isMain:
        Boolean(item?.is_main) ||
        (item?.platform_id === MAIN_PLATFORM_ID && item?.user_id === MAIN_USER_ID),
      turns: Math.max(0, asNumber(item?.turns)),
      updatedAt: item?.updated_at,
      status: String(item?.status || "active"),
    };
  }

  function renderTurnLine(conversation) {
    if (!dom.mainTurns) return;
    const turnConfig = state.turnConfig;
    if (!turnConfig || !conversation) {
      dom.mainTurns.textContent = "--";
      return;
    }
    const turns = conversation.turns;
    const maxTurns = asNumber(turnConfig.max_turns, -1);
    if (turnConfig.strategy === "truncate_by_turns" && maxTurns > 0) {
      const left = Math.max(0, maxTurns - turns);
      dom.mainTurns.textContent = `轮数 ${turns} / ${maxTurns} · 还剩 ${left} 轮`;
    } else if (turnConfig.strategy === "truncate_by_turns") {
      dom.mainTurns.textContent = `轮数 ${turns} · 未设轮数上限`;
    } else {
      dom.mainTurns.textContent = `按 Token 压缩 · 当前 ${turns} 轮`;
    }
  }

  function renderFocusConversation(conversation) {
    if (!conversation) {
      dom.focusTitle.textContent = "主会话";
      dom.mainTokenUsage.textContent = "--";
      dom.mainRemaining.textContent = "未找到会话";
      dom.mainPercent.textContent = "--%";
      dom.mainUpdatedAt.textContent = "等待会话数据";
      dom.mainProgressFill.style.width = "0%";
      dom.mainProgressFill.className = "progress-fill";
      dom.mainProgress.setAttribute("aria-valuenow", "0");
      dom.focusAlertBadge.hidden = true;
      updateBgRing(null);
      return;
    }

    const displayedPercent = Math.min(100, conversation.percent);
    const waterState = getWaterState(conversation.percent);
    dom.focusTitle.textContent = conversation.isMain
      ? `⭐ ${conversation.displayName}`
      : conversation.displayName;
    animateNumber(
      dom.mainTokenUsage,
      state.lastMainToken,
      conversation.tokenUsage,
      formatNumber,
    );
    dom.mainRemaining.textContent = conversation.overCompressThreshold
      ? "已达到压缩区间"
      : `${formatNumber(conversation.remainingToCompress)} tokens`;
    animateNumber(
      dom.mainPercent,
      state.lastMainPercent,
      conversation.percent,
      (value) => `${value.toFixed(2)}%`,
    );
    state.lastMainToken = conversation.tokenUsage;
    state.lastMainPercent = conversation.percent;
    dom.mainUpdatedAt.textContent = `最后更新于 ${formatDateTime(conversation.updatedAt)}`;
    dom.mainProgressFill.style.width = `${displayedPercent}%`;
    dom.mainProgressFill.className = `progress-fill ${waterState}`;
    dom.mainProgress.setAttribute("aria-valuenow", String(displayedPercent));
    dom.focusAlertBadge.hidden = !conversation.alerting;
    updateBgRing(conversation);
  }

  function updateBgRing(conversation) {
    if (!dom.bgRing) return;
    if (!conversation) {
      dom.ringPct.textContent = "--";
      return;
    }
    const percent = Math.min(100, conversation.percent);
    const angle = percent * 3.6;
    dom.bgRing.style.background =
      `conic-gradient(from -90deg, rgba(255,122,69,0.75) 0deg, ` +
      `rgba(255,122,69,0.75) ${angle}deg, transparent ${angle}deg 360deg)`;
    dom.ringPct.textContent = `${percent.toFixed(2)}%`;
    if (dom.bgToken) {
      dom.bgToken.innerHTML =
        `TOKEN ${formatNumber(conversation.tokenUsage)}` +
        `<small>${percent.toFixed(2)}% · ${formatCompact(
          conversation.remainingToCompress,
        )} LEFT</small>`;
    }
  }

  function buildConversationCard(conversation) {
    const card = createElement("div", "conv-card");
    card.dataset.conversationId = conversation.conversationId;
    if (conversation.status === "stale") {
      card.classList.add("conv-stale");
    }
    if (conversation.conversationId === state.focusConvId) {
      card.classList.add("focused");
    }

    const waterState = getWaterState(conversation.percent);
    const statusText = conversation.alerting
      ? "⚠️ 警告中"
      : waterState === "caution"
        ? "接近阈值"
        : "正常";

    const top = createElement("div", "card-top");
    const nameWrap = createElement("div");
    const statusPrefix =
      conversation.status === "stale"
        ? "🕓 "
        : conversation.status === "idle"
          ? "💤 "
          : "";
    nameWrap.append(
      createElement(
        "div",
        "card-name",
        conversation.isMain
          ? `⭐ ${conversation.displayName}`
          : `${statusPrefix}${conversation.displayName}`,
      ),
      createElement(
        "div",
        "card-meta",
        `${conversation.platformId} · ${conversation.userId}`,
      ),
    );
    top.append(
      nameWrap,
      createElement(
        "strong",
        `card-percent ${waterState}`,
        `${conversation.percent.toFixed(2)}%`,
      ),
    );

    const bar = createElement("div", "card-bar");
    const barFill = createElement("span", waterState);
    barFill.style.width = `${Math.min(100, conversation.percent)}%`;
    bar.appendChild(barFill);

    const footer = createElement("div", "card-footer");
    if (conversation.status === "stale") {
      footer.append(createElement("span", "conv-flag", "旧会话"));
    }
    footer.append(
      createElement(
        "span",
        "card-remaining",
        conversation.overCompressThreshold
          ? "已达到压缩区间"
          : `剩 ${formatCompact(conversation.remainingToCompress)}`,
      ),
      createElement("span", "card-turns", `${conversation.turns} 轮`),
      createElement("span", `status-badge ${waterState}`, statusText),
    );

    card.append(top, bar, footer);
    return card;
  }

  function renderConversations(payload) {
    const rawConversations = Array.isArray(payload?.conversations)
      ? payload.conversations
      : [];
    const conversations = rawConversations
      .map(normalizeConversation)
      .sort((left, right) => {
        const order = { active: 0, idle: 1, stale: 2 };
        const diff = (order[left.status] ?? 1) - (order[right.status] ?? 1);
        return diff !== 0 ? diff : right.tokenUsage - left.tokenUsage;
      });

    state.turnConfig = payload?.turn_config || null;
    state.contextLimit = asNumber(payload?.context_limit, CONTEXT_LIMIT);
    if (dom.mainContextLimit) {
      dom.mainContextLimit.textContent = `/ ${formatNumber(state.contextLimit)}`;
    }

    dom.conversationCount.textContent = String(conversations.length);
    dom.conversationEmpty.hidden = conversations.length > 0;
    dom.conversationEmpty.textContent = "暂无会话数据";
    dom.conversationGrid.hidden = conversations.length === 0;
    state.conversations = conversations;
    dom.conversationGrid.replaceChildren(
      ...conversations.map(buildConversationCard),
    );

    const mainConversation = conversations.find(
      (conversation) => conversation.isMain,
    );
    let focus = conversations.find(
      (conversation) => conversation.conversationId === state.focusConvId,
    );
    if (!focus) {
      focus = mainConversation;
      if (state.focusConvId === null && mainConversation) {
        state.focusConvId = mainConversation.conversationId;
      }
    }
    renderFocusConversation(focus);
    renderTurnLine(focus);
  }

  async function loadConversations() {
    const payload = await apiGet("conversations");
    renderConversations(payload);
    state.conversationsLoaded = true;
  }

  function normalizeTrend(payload) {
    const rawPoints = Array.isArray(payload?.trend?.total_series)
      ? payload.trend.total_series
      : [];
    const points = rawPoints
      .map((point) => ({
        timestamp: asNumber(point?.[0]),
        tokens: Math.max(0, asNumber(point?.[1])),
      }))
      .filter((point) => point.timestamp > 0)
      .sort((left, right) => left.timestamp - right.timestamp);
    const providers = Array.isArray(payload?.total) ? payload.total : [];
    const series = Array.isArray(payload?.trend?.series)
      ? payload.trend.series.map((entry) => ({
          name: String(entry?.name || "unknown"),
          totalTokens: Math.max(0, asNumber(entry?.total_tokens)),
          points: (Array.isArray(entry?.data) ? entry.data : [])
            .map((point) => ({
              timestamp: asNumber(point?.[0]),
              tokens: Math.max(0, asNumber(point?.[1])),
            }))
            .filter((point) => point.timestamp > 0)
            .sort((left, right) => left.timestamp - right.timestamp),
        }))
      : [];
    return {
      points,
      series,
      providers,
      totalTokens: Math.max(0, asNumber(payload?.range_total_tokens)),
      totalCalls: Math.max(0, asNumber(payload?.range_total_calls)),
    };
  }

  const SERIES_COLORS = [
    "#e11d48",
    "#0ea5e9",
    "#8b5cf6",
    "#f59e0b",
    "#10b981",
    "#ec4899",
    "#14b8a6",
  ];

  function renderChartLegend(series) {
    if (!series.length) {
      dom.chartLegend.hidden = true;
      return;
    }
    dom.chartLegend.replaceChildren(
      ...series.map((entry, index) => {
        const item = createElement("span", "chart-legend-item");
        const dot = createElement("i", "chart-legend-dot");
        dot.style.backgroundColor =
          SERIES_COLORS[index % SERIES_COLORS.length];
        item.append(
          dot,
          document.createTextNode(
            `${entry.name} · ${formatCompact(entry.totalTokens)}`,
          ),
        );
        return item;
      }),
    );
    dom.chartLegend.hidden = false;
  }

  function renderProviderBreakdown(providers) {
    const fragment = document.createDocumentFragment();
    providers.slice(0, 6).forEach((provider) => {
      const item = createElement("span");
      item.append(
        createElement("i"),
        document.createTextNode(
          `${String(provider?.provider_id || "unknown")} ${formatCompact(provider?.tokens)}`,
        ),
      );
      fragment.appendChild(item);
    });
    dom.providerBreakdown.replaceChildren(fragment);
  }

  function renderTrend(payload) {
    const trend = normalizeTrend(payload);
    dom.trendTotalTokens.textContent = formatNumber(trend.totalTokens);
    dom.trendTotalCalls.textContent = formatNumber(trend.totalCalls);
    dom.trendAverage.textContent = trend.totalCalls
      ? formatNumber(trend.totalTokens / trend.totalCalls)
      : "0";
    dom.chartEmpty.hidden = trend.points.some((point) => point.tokens > 0);
    dom.chartEmpty.textContent = "所选范围暂无 Token 用量";
    state.chartPoints = trend.points;
    state.series = trend.series;
    renderChartLegend(trend.series);
    renderProviderBreakdown(trend.providers);
    drawChart();
    updateBgCalls(trend);
  }

  function updateBgCalls(trend) {
    if (!dom.bgCalls || !trend) return;
    const avg = trend.totalCalls
      ? Math.round(trend.totalTokens / trend.totalCalls)
      : 0;
    dom.bgCalls.innerHTML =
      `CALLS ${formatNumber(trend.totalCalls)}` +
      `<small>AVG ${formatCompact(avg)} / CALL</small>`;
  }

  async function loadTrend(days = state.days) {
    const requestId = ++state.trendRequestId;
    const params = { days };
    if (state.trendScope === "conversation" && state.focusConvId) {
      params.conversation_id = state.focusConvId;
    }
    const payload = await apiGet("stats/provider-tokens", params);
    if (requestId !== state.trendRequestId) return;
    renderTrend(payload);
    state.trendLoaded = true;
  }

  function formatAxisTime(timestamp) {
    const options =
      state.days === 1
        ? { hour: "2-digit", minute: "2-digit", hour12: false }
        : { month: "2-digit", day: "2-digit", hour: "2-digit", hour12: false };
    return new Intl.DateTimeFormat("zh-CN", options).format(new Date(timestamp));
  }

  function hexToRgba(hex, alpha) {
    const normalized = hex.replace("#", "");
    if (normalized.length !== 6) return hex;
    const r = parseInt(normalized.slice(0, 2), 16);
    const g = parseInt(normalized.slice(2, 4), 16);
    const b = parseInt(normalized.slice(4, 6), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }

  function traceSmoothPath(context, coordinates) {
    if (coordinates.length < 3) {
      coordinates.forEach((point, index) => {
        if (index === 0) context.moveTo(point.x, point.y);
        else context.lineTo(point.x, point.y);
      });
      return;
    }
    context.moveTo(coordinates[0].x, coordinates[0].y);
    for (let index = 0; index < coordinates.length - 1; index += 1) {
      const p0 = coordinates[Math.max(0, index - 1)];
      const p1 = coordinates[index];
      const p2 = coordinates[index + 1];
      const p3 = coordinates[Math.min(coordinates.length - 1, index + 2)];
      const cp1x = p1.x + (p2.x - p0.x) / 6;
      const cp1y = p1.y + (p2.y - p0.y) / 6;
      const cp2x = p2.x - (p3.x - p1.x) / 6;
      const cp2y = p2.y - (p3.y - p1.y) / 6;
      context.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, p2.x, p2.y);
    }
  }

  function drawChart() {
    const canvas = dom.chart;
    const rect = canvas.getBoundingClientRect();
    if (rect.width < 10 || rect.height < 10) return;

    const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(rect.width * pixelRatio);
    canvas.height = Math.round(rect.height * pixelRatio);
    const context = canvas.getContext("2d");
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    context.clearRect(0, 0, rect.width, rect.height);

    const styles = getComputedStyle(document.documentElement);
    const textMuted = styles.getPropertyValue("--text-muted").trim();
    const border = styles.getPropertyValue("--border").trim();
    const accent =
      styles.getPropertyValue("--chart-accent").trim() ||
      styles.getPropertyValue("--accent").trim();
    const background = styles.getPropertyValue("--accent-soft").trim();
    const padding = { top: 16, right: 12, bottom: 36, left: 58 };
    const plotWidth = Math.max(1, rect.width - padding.left - padding.right);
    const plotHeight = Math.max(1, rect.height - padding.top - padding.bottom);
    const points = state.chartPoints;
    const maximum = Math.max(1, ...points.map((point) => point.tokens));
    const yMaximum = maximum * 1.12;

    context.font = '11px "Microsoft YaHei", sans-serif';
    context.lineWidth = 1;
    context.textBaseline = "middle";
    for (let index = 0; index <= 4; index += 1) {
      const ratio = index / 4;
      const y = padding.top + ratio * plotHeight;
      context.beginPath();
      context.strokeStyle = hexToRgba(border, 0.45);
      context.moveTo(padding.left, y);
      context.lineTo(rect.width - padding.right, y);
      context.stroke();
      context.fillStyle = textMuted;
      context.textAlign = "right";
      context.fillText(
        formatCompact(yMaximum * (1 - ratio)),
        padding.left - 9,
        y,
      );
    }

    if (points.length === 0) {
      state.chartGeometry = null;
      return;
    }

    const xStep = points.length > 1 ? plotWidth / (points.length - 1) : 0;
    const coordinates = points.map((point, index) => ({
      ...point,
      x: padding.left + index * xStep,
      y: padding.top + plotHeight - (point.tokens / yMaximum) * plotHeight,
    }));

    const tickCount = Math.min(5, points.length);
    for (let index = 0; index < tickCount; index += 1) {
      const pointIndex =
        tickCount === 1
          ? 0
          : Math.round((index / (tickCount - 1)) * (points.length - 1));
      const point = coordinates[pointIndex];
      context.fillStyle = textMuted;
      context.textAlign =
        index === 0 ? "left" : index === tickCount - 1 ? "right" : "center";
      context.fillText(formatAxisTime(point.timestamp), point.x, rect.height - 14);
    }

    context.beginPath();
    traceSmoothPath(context, coordinates);
    context.lineTo(coordinates.at(-1).x, padding.top + plotHeight);
    context.lineTo(coordinates[0].x, padding.top + plotHeight);
    context.closePath();
    const areaGradient = context.createLinearGradient(
      0,
      padding.top,
      0,
      padding.top + plotHeight,
    );
    areaGradient.addColorStop(0, hexToRgba(accent, 0.28));
    areaGradient.addColorStop(1, hexToRgba(accent, 0.02));
    context.fillStyle = areaGradient;
    context.fill();

    state.series.forEach((entry, seriesIndex) => {
      if (!entry.points.length) return;
      const tokensByTime = new Map(
        entry.points.map((point) => [point.timestamp, point.tokens]),
      );
      const seriesCoordinates = coordinates.map((point) => ({
        x: point.x,
        y:
          padding.top +
          plotHeight -
          ((tokensByTime.get(point.timestamp) || 0) / yMaximum) *
            plotHeight,
      }));
      if (seriesCoordinates.length < 2) return;
      context.beginPath();
      traceSmoothPath(context, seriesCoordinates);
      context.globalAlpha = 0.65;
      context.strokeStyle =
        SERIES_COLORS[seriesIndex % SERIES_COLORS.length];
      context.lineWidth = 1.5;
      context.lineJoin = "round";
      context.lineCap = "round";
      context.stroke();
      context.globalAlpha = 1;
    });

    context.beginPath();
    traceSmoothPath(context, coordinates);
    context.strokeStyle = accent;
    context.lineWidth = 2.5;
    context.shadowColor = hexToRgba(accent, 0.5);
    context.shadowBlur = 8;
    context.lineJoin = "round";
    context.lineCap = "round";
    context.stroke();
    context.shadowBlur = 0;

    const nonZeroPoints = coordinates.filter((point) => point.tokens > 0);
    if (nonZeroPoints.length <= 24) {
      nonZeroPoints.forEach((point) => {
        context.beginPath();
        context.arc(point.x, point.y, 5, 0, Math.PI * 2);
        context.fillStyle = hexToRgba(accent, 0.18);
        context.fill();
        context.beginPath();
        context.arc(point.x, point.y, 2.6, 0, Math.PI * 2);
        context.fillStyle = accent;
        context.fill();
      });
    }

    if (state.hoverIndex != null && coordinates[state.hoverIndex]) {
      const hover = coordinates[state.hoverIndex];
      context.beginPath();
      context.setLineDash([3, 3]);
      context.strokeStyle = textMuted;
      context.lineWidth = 1;
      context.moveTo(hover.x, padding.top);
      context.lineTo(hover.x, padding.top + plotHeight);
      context.stroke();
      context.setLineDash([]);
      context.beginPath();
      context.arc(hover.x, hover.y, 8, 0, Math.PI * 2);
      context.fillStyle = hexToRgba(accent, 0.2);
      context.fill();
      context.beginPath();
      context.arc(hover.x, hover.y, 4.5, 0, Math.PI * 2);
      context.fillStyle = accent;
      context.fill();
      context.strokeStyle = "#ffffff";
      context.lineWidth = 1.5;
      context.stroke();
    }

    state.chartGeometry = { coordinates, padding, plotWidth, plotHeight };
  }

  function showChartTooltip(event) {
    const geometry = state.chartGeometry;
    if (!geometry?.coordinates.length) return;
    const bounds = dom.chart.getBoundingClientRect();
    const pointerX = event.clientX - bounds.left;
    let nearestIndex = 0;
    let bestDistance = Infinity;
    geometry.coordinates.forEach((point, index) => {
      const distance = Math.abs(point.x - pointerX);
      if (distance < bestDistance) {
        bestDistance = distance;
        nearestIndex = index;
      }
    });
    const nearest = geometry.coordinates[nearestIndex];
    if (Math.abs(nearest.x - pointerX) > 30) {
      dom.chartTooltip.hidden = true;
      if (state.hoverIndex !== null) {
        state.hoverIndex = null;
        drawChart();
      }
      return;
    }
    state.hoverIndex = nearestIndex;
    drawChart();

    const title = new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(new Date(nearest.timestamp));
    const seriesLines = state.series
      .map((entry, index) => {
        const match = entry.points.find(
          (point) => point.timestamp === nearest.timestamp,
        );
        const value = match ? match.tokens : 0;
        if (!value) return null;
        const row = createElement("span", "tooltip-series");
        const dot = createElement("i", "chart-legend-dot");
        dot.style.backgroundColor =
          SERIES_COLORS[index % SERIES_COLORS.length];
        row.append(
          dot,
          document.createTextNode(`${entry.name} · ${formatNumber(value)}`),
        );
        return row;
      })
      .filter(Boolean);

    dom.chartTooltip.replaceChildren(
      createElement("strong", "", title),
      createElement("span", "", `${formatNumber(nearest.tokens)} tokens`),
      ...seriesLines,
    );
    dom.chartTooltip.hidden = false;

    const tooltipWidth = dom.chartTooltip.offsetWidth;
    const tooltipHeight = dom.chartTooltip.offsetHeight;
    const left = Math.min(
      bounds.width - tooltipWidth - 6,
      Math.max(6, nearest.x + 10),
    );
    const top = Math.min(
      bounds.height - tooltipHeight - 6,
      Math.max(6, nearest.y - tooltipHeight - 10),
    );
    dom.chartTooltip.style.left = `${left}px`;
    dom.chartTooltip.style.top = `${top}px`;
  }

  const HISTORY_META = {
    warning: { icon: "⚠️", label: "警告", className: "history-warning" },
    cleared: { icon: "✅", label: "解除", className: "history-cleared" },
    compressed: { icon: "📉", label: "压缩", className: "history-compressed" },
    rollback: { icon: "🌊", label: "回落", className: "history-rollback" },
    turn_warning: {
      icon: "📊",
      label: "轮数",
      className: "history-turn",
    },
  };

  function renderHistory(payload) {
    const items = payload?.history || payload?.data?.history || [];
    dom.historyEmpty.hidden = items.length > 0;
    dom.historyList.hidden = items.length === 0;
    if (!items.length) return;
    dom.historyList.replaceChildren(
      ...items.map((entry) => {
        const meta = HISTORY_META[entry.event_type] || HISTORY_META.warning;
        const row = createElement("div", `history-item ${meta.className}`);
        const titleText =
          entry.title ||
          String(entry.conversation_id || "").slice(0, 8) ||
          "未知会话";
        row.append(
          createElement("span", "history-icon", meta.icon),
          createElement("span", "history-type", meta.label),
          createElement("span", "history-title", titleText),
          createElement(
            "span",
            "history-detail",
            `${formatNumber(entry.token_usage || 0)} · ${(
              entry.percent ?? 0
            ).toFixed(1)}%`,
          ),
          createElement(
            "span",
            "history-time",
            formatDateTime(entry.created_at * 1000),
          ),
        );
        return row;
      }),
    );
  }

  async function loadHistory() {
    const params = { limit: 20 };
    if (state.historyScope === "focus" && state.focusConvId) {
      params.conversation_id = state.focusConvId;
    }
    const payload = await apiGet("stats/alert-history", params);
    renderHistory(payload);
  }

  function setFocus(conversationId) {
    if (conversationId === state.focusConvId) return;
    state.focusConvId = conversationId;
    dom.conversationGrid.querySelectorAll(".conv-card").forEach((card) => {
      card.classList.toggle(
        "focused",
        card.dataset.conversationId === conversationId,
      );
    });
    const focus = state.conversations.find(
      (conversation) => conversation.conversationId === conversationId,
    );
    renderFocusConversation(focus);
    if (state.trendScope === "conversation") {
      dom.chartEmpty.hidden = false;
      dom.chartEmpty.textContent = "正在读取趋势数据…";
      loadTrend(state.days).catch(() => setErrorState(true));
    }
    if (state.historyScope === "focus") {
      loadHistory().catch(() => setErrorState(true));
    }
  }

  async function refreshAll({ manual = false } = {}) {
    if (state.refreshInProgress) return;
    state.refreshInProgress = true;
    if (manual) setRefreshBusy(true);
    if (!state.conversationsLoaded && !state.trendLoaded) {
      dom.refreshStatus.textContent = "正在更新…";
    }

    const results = await Promise.allSettled([
      loadConversations(),
      loadTrend(state.days),
      loadHistory(),
    ]);
    const hasError = results.some((result) => result.status === "rejected");
    const hasSuccess = results.some((result) => result.status === "fulfilled");
    setErrorState(hasError);
    if (hasSuccess) markRefreshTime();
    else dom.refreshStatus.textContent = "等待自动重试";

    state.refreshInProgress = false;
    if (manual) setRefreshBusy(false);
  }

  function positionSegments() {
    document.querySelectorAll(".segmented-control").forEach((container) => {
      const indicator = container.querySelector(".seg-indicator");
      const activeButton = container.querySelector(
        "button.active, button.on",
      );
      if (!indicator) return;
      if (!activeButton) {
        indicator.style.opacity = "0";
        return;
      }
      indicator.style.opacity = "1";
      indicator.style.left = `${activeButton.offsetLeft}px`;
      indicator.style.width = `${activeButton.offsetWidth}px`;
    });
  }

  function selectRange(days) {
    if (![1, 3, 7, 30].includes(days) || days === state.days) return;
    state.days = days;
    dom.rangeButtons.forEach((button) => {
      const active = Number(button.dataset.days) === days;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
      button.disabled = true;
    });
    positionSegments();
    dom.chartEmpty.hidden = false;
    dom.chartEmpty.textContent = "正在读取趋势数据…";
    loadTrend(days)
      .then(() => {
        setErrorState(false);
        markRefreshTime();
      })
      .catch(() => setErrorState(true))
      .finally(() => {
        dom.rangeButtons.forEach((button) => {
          button.disabled = false;
        });
      });
  }

  dom.refreshButton.addEventListener("click", () => refreshAll({ manual: true }));
  dom.historyRefresh.addEventListener("click", () => {
    loadHistory().catch(() => setErrorState(true));
  });
  dom.rangeButtons.forEach((button) => {
    button.addEventListener("click", () => selectRange(Number(button.dataset.days)));
  });
  dom.conversationGrid.addEventListener("click", (event) => {
    const card = event.target.closest(".conv-card");
    if (!card) return;
    setFocus(card.dataset.conversationId);
  });
  dom.trendScopeButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const scope = button.dataset.scope;
      if (scope === state.trendScope) return;
      state.trendScope = scope;
      dom.trendScopeButtons.forEach((item) => {
        const active = item.dataset.scope === scope;
        item.classList.toggle("active", active);
        item.setAttribute("aria-pressed", String(active));
      });
      positionSegments();
      dom.chartEmpty.hidden = false;
      dom.chartEmpty.textContent = "正在读取趋势数据…";
      loadTrend(state.days).catch(() => setErrorState(true));
    });
  });
  dom.historyScopeButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const scope = button.dataset.historyScope;
      if (scope === state.historyScope) return;
      state.historyScope = scope;
      dom.historyScopeButtons.forEach((item) => {
        const active = item.dataset.historyScope === scope;
        item.classList.toggle("active", active);
        item.setAttribute("aria-pressed", String(active));
      });
      positionSegments();
      loadHistory().catch(() => setErrorState(true));
    });
  });
  dom.chart.addEventListener("pointermove", showChartTooltip);
  dom.chart.addEventListener("pointerleave", () => {
    dom.chartTooltip.hidden = true;
    if (state.hoverIndex !== null) {
      state.hoverIndex = null;
      drawChart();
    }
  });

  const resizeObserver = new ResizeObserver(() => {
    window.clearTimeout(state.chartResizeTimer);
    state.chartResizeTimer = window.setTimeout(() => {
      drawChart();
      positionSegments();
    }, 80);
  });
  resizeObserver.observe(dom.chartWrap);

  if (window.AstrBotPluginPage?.onContext) {
    window.AstrBotPluginPage.onContext(() => drawChart());
  }

  positionSegments();
  refreshAll();
  window.setInterval(() => refreshAll(), POLL_INTERVAL_MS);
})();
