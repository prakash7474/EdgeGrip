"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const TOKEN_KEY = "edgegrip-ui-token";
  const BANNER_TEXT = "SIMULATED DATA - NOT A MEASUREMENT";
  let token = sessionStorage.getItem(TOKEN_KEY) || "";
  let authPromptOpen = false;
  let lastLatency = null;
  let lastFrameObjectUrl = null;
  let loading = false;
  let frameLoading = false;

  const authHeaders = (extra = {}) => {
    const headers = { Accept: "application/json", ...extra };
    if (token) headers["X-API-Token"] = token;
    return headers;
  };

  const setConnection = (text, bad = false) => {
    const element = $("connection");
    element.textContent = text;
    element.classList.toggle("bad", bad);
  };

  const showAuth = () => {
    if (authPromptOpen) return;
    authPromptOpen = true;
    $("auth-token").value = "";
    if (typeof $("auth-dialog").showModal === "function") {
      $("auth-dialog").showModal();
      $("auth-token").focus();
    }
    setConnection("token required", true);
  };

  const errorText = async (response) => {
    try {
      const body = await response.json();
      if (Array.isArray(body.detail)) {
        return body.detail.map((item) => `${(item.loc || []).join(".")}: ${item.msg}`).join("; ");
      }
      return body.detail || `HTTP ${response.status}`;
    } catch (_) {
      return `HTTP ${response.status}`;
    }
  };

  const api = async (path, options = {}) => {
    const response = await fetch(path, {
      ...options,
      headers: authHeaders(options.headers || {}),
      cache: "no-store",
    });
    if (response.status === 401) {
      showAuth();
      throw new Error("API token required");
    }
    if (!response.ok) {
      throw new Error(await errorText(response));
    }
    return response.json();
  };

  const postJson = (path, body) => api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  const formatTime = (value) => {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleTimeString([], { hour12: false });
  };

  const formatNumber = (value, digits = 0) => {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    return Number(value).toFixed(digits);
  };

  const addText = (parent, tag, text, className) => {
    const element = document.createElement(tag);
    element.textContent = text;
    if (className) element.className = className;
    parent.appendChild(element);
    return element;
  };

  const ensureDemoBanner = (state) => {
    if (!state.demo) return;
    document.body.dataset.demo = "true";
    if ($("demo-banner")) return;
    const banner = document.createElement("div");
    banner.id = "demo-banner";
    banner.className = "demo-banner";
    banner.setAttribute("role", "status");
    banner.textContent = BANNER_TEXT;
    document.body.insertBefore(banner, $("main"));
  };

  const renderState = (state) => {
    ensureDemoBanner(state);
    $("env-pill").textContent = `environment: ${state.environment}`;
    $("env-pill").className = `pill${state.demo ? " warn" : ""}`;

    const run = state.run || {};
    $("run-pill").textContent = `run: ${run.status || "unknown"}`;
    const running = run.status === "running" || run.status === "stopping";
    $("run-pill").className = `pill${run.status === "failed" ? " bad" : running ? " warn" : ""}`;
    $("run-start").disabled = running;
    $("run-stop").disabled = !running;

    const prediction = state.last_prediction;
    if (prediction && prediction.label) {
      const score = prediction.score === null || prediction.score === undefined
        ? "score n/a" : `score ${formatNumber(prediction.score, 3)}`;
      $("prediction-line").textContent =
        `Prediction: ${prediction.label}  |  ${score}  |  ${formatTime(prediction.at)}`;
    } else {
      $("prediction-line").textContent = "No prediction yet.";
    }

    renderProviders(state.providers || []);
    renderCounts(state.counts || {});
  };

  const renderProviders = (providers) => {
    const container = $("providers");
    container.replaceChildren();
    if (!providers.length) {
      addText(container, "p", "No provider state has been observed.", "muted");
    }
    const select = $("mock-provider");
    const previous = select.value;
    select.replaceChildren();
    for (const provider of providers) {
      const card = document.createElement("article");
      card.className = "provider";
      const head = document.createElement("div");
      head.className = "provider-head";
      addText(head, "span", provider.name || "unknown", "provider-name");
      const state = provider.health || "unknown";
      addText(
        head, "span", state,
        `state state-${["healthy", "degraded", "failed"].includes(state) ? state : "unknown"}`
      );
      card.appendChild(head);

      const metrics = document.createElement("div");
      metrics.className = "provider-metrics";
      const ewma = document.createElement("span");
      ewma.append("EWMA ");
      addText(ewma, "strong", provider.ewma_ms === null || provider.ewma_ms === undefined
        ? "—" : `${formatNumber(provider.ewma_ms, 1)} ms`);
      metrics.appendChild(ewma);
      const circuit = document.createElement("span");
      circuit.append("circuit ");
      addText(circuit, "strong", provider.circuit || "unknown");
      metrics.appendChild(circuit);
      const last = document.createElement("span");
      last.append("last ");
      addText(last, "strong", provider.last_latency_ms === null || provider.last_latency_ms === undefined
        ? "—" : `${formatNumber(provider.last_latency_ms, 1)} ms`);
      metrics.appendChild(last);
      card.appendChild(metrics);

      addText(
        card, "p",
        provider.last_failover ? `Last failover: ${formatTime(provider.last_failover)}` : "No failover observed.",
        "provider-failover"
      );
      container.appendChild(card);

      const option = document.createElement("option");
      option.value = provider.name || "";
      option.textContent = provider.name || "unknown";
      select.appendChild(option);
    }
    if (previous && [...select.options].some((option) => option.value === previous)) {
      select.value = previous;
    }
  };

  const renderCounts = (counts) => {
    const container = $("counts");
    container.replaceChildren();
    const discarded = counts.discarded || {};
    const tiles = [
      ["sent", counts.sent],
      ["acked", counts.acked],
      ["applied", counts.applied],
      ["safe default", counts.safe_default],
      ["outbox depth", counts.outbox_depth],
      ["unconfirmed", counts.unconfirmed],
      ["duplicate", discarded.duplicate],
      ["late", discarded.late],
    ];
    for (const [label, value] of tiles) {
      const tile = document.createElement("div");
      tile.className = `count${["duplicate", "late"].includes(label) ? " count-discard" : ""}`;
      addText(tile, "div", label, "count-label");
      addText(tile, "div", formatNumber(value), "count-value");
      container.appendChild(tile);
    }
  };

  const renderEvents = (events) => {
    const container = $("events");
    container.replaceChildren();
    if (!events.length) {
      addText(container, "p", "No events recorded.", "muted");
      return;
    }
    for (const event of events.slice(0, 40)) {
      const row = document.createElement("div");
      row.className = `event event-severity-${event.severity || "info"}`;
      addText(row, "span", formatTime(event.timestamp), "event-time");
      addText(row, "span", event.kind || "event", "event-kind");
      const message = document.createElement("span");
      message.className = "event-message";
      const provider = event.provider ? `[${event.provider}] ` : "";
      const task = event.task_id ? ` (${event.task_id})` : "";
      message.textContent = `${provider}${event.message || ""}${task}`;
      row.appendChild(message);
      container.appendChild(row);
    }
  };

  const renderLatencySummary = (data) => {
    const summary = $("latency-summary");
    summary.replaceChildren();
    const values = [
      ["p50", data.p50], ["p95", data.p95], ["p99", data.p99],
      ["min", data.min], ["max", data.max], ["samples", data.count],
    ];
    for (const [label, value] of values) {
      const item = document.createElement("span");
      item.append(`${label} `);
      addText(item, "strong", value === null || value === undefined
        ? "—" : formatNumber(value, label === "samples" ? 0 : 1));
      summary.appendChild(item);
    }
    $("latency-chart").setAttribute(
      "aria-label",
      `Latency chart. ${data.count || 0} samples. p50 ${formatNumber(data.p50, 1)} milliseconds, ` +
      `p95 ${formatNumber(data.p95, 1)}, p99 ${formatNumber(data.p99, 1)}.`
    );
  };

  const drawChart = (data) => {
    const canvas = $("latency-chart");
    const rect = canvas.getBoundingClientRect();
    if (rect.width < 10) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);

    const points = data.points || [];
    const width = rect.width;
    const height = rect.height;
    const left = 54;
    const right = 16;
    const top = 18;
    const bottom = 34;
    const plotWidth = Math.max(10, width - left - right);
    const plotHeight = Math.max(10, height - top - bottom);

    ctx.fillStyle = "#0b1118";
    ctx.fillRect(0, 0, width, height);
    ctx.font = "12px 'Segoe UI', system-ui, sans-serif";
    ctx.textBaseline = "middle";

    if (!points.length) {
      ctx.fillStyle = "#9fb0c0";
      ctx.textAlign = "center";
      ctx.fillText("No latency points yet.", width / 2, height / 2);
      return;
    }

    const values = points.map((point) => Number(point.ms));
    const maxValue = Math.max(
      ...values,
      data.p99 || 0,
      1
    ) * 1.12;
    const xFor = (index) => left + (points.length === 1 ? plotWidth / 2 : (index / (points.length - 1)) * plotWidth);
    const yFor = (value) => top + plotHeight - (Math.max(0, value) / maxValue) * plotHeight;

    ctx.strokeStyle = "#29394a";
    ctx.fillStyle = "#9fb0c0";
    ctx.lineWidth = 1;
    ctx.textAlign = "right";
    for (let step = 0; step <= 4; step += 1) {
      const value = (maxValue / 4) * step;
      const y = yFor(value);
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(width - right, y);
      ctx.stroke();
      ctx.fillText(`${value.toFixed(0)}`, left - 8, y);
    }

    ctx.textAlign = "center";
    ctx.fillText("oldest", left, height - 14);
    ctx.fillText("newest", width - right, height - 14);

    const lines = [
      ["p50", data.p50, "#62d99b"],
      ["p95", data.p95, "#f0bd5a"],
      ["p99", data.p99, "#ff7b7b"],
    ];
    ctx.setLineDash([6, 4]);
    for (const [label, value, color] of lines) {
      if (value === null || value === undefined) continue;
      const y = yFor(Number(value));
      ctx.strokeStyle = color;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(width - right, y);
      ctx.stroke();
      ctx.fillStyle = color;
      // Keep p95 and p99 labels apart when their values are close.
      const atLeft = label === "p50";
      const labelY = label === "p99" ? y + 12 : y - 8;
      ctx.textAlign = atLeft ? "left" : "right";
      ctx.fillText(
        `${label} ${Number(value).toFixed(1)}`,
        atLeft ? left + 4 : width - right - 4,
        labelY
      );
    }
    ctx.setLineDash([]);

    const gradient = ctx.createLinearGradient(0, top, 0, top + plotHeight);
    gradient.addColorStop(0, "rgba(110,198,255,.28)");
    gradient.addColorStop(1, "rgba(110,198,255,0)");
    ctx.beginPath();
    points.forEach((point, index) => {
      const x = xFor(index);
      const y = yFor(Number(point.ms));
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.lineTo(xFor(points.length - 1), top + plotHeight);
    ctx.lineTo(xFor(0), top + plotHeight);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    ctx.beginPath();
    points.forEach((point, index) => {
      const x = xFor(index);
      const y = yFor(Number(point.ms));
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = "#6ec6ff";
    ctx.lineWidth = 2;
    ctx.stroke();

    if (points.length <= 40) {
      ctx.fillStyle = "#cfe8fa";
      for (let index = 0; index < points.length; index += 1) {
        ctx.beginPath();
        ctx.arc(xFor(index), yFor(Number(points[index].ms)), 2.2, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  };

  const loadState = async () => {
    const [state, latency, events] = await Promise.all([
      api("/api/state"),
      api("/api/latency"),
      api("/api/events?limit=100"),
    ]);
    renderState(state);
    lastLatency = latency;
    renderLatencySummary(latency);
    drawChart(latency);
    renderEvents(events);
  };

  const loadAll = async () => {
    if (loading) return;
    loading = true;
    try {
      await loadState();
      setConnection(`updated ${formatTime(new Date().toISOString())}`);
    } catch (error) {
      if (error.message !== "API token required") {
        setConnection(`update failed: ${error.message}`, true);
      }
    } finally {
      loading = false;
    }
  };

  const loadFrame = async () => {
    if (frameLoading) return;
    frameLoading = true;
    try {
      const response = await fetch("/stream/frame", {
        headers: authHeaders(),
        cache: "no-store",
      });
      if (response.status === 401) {
        showAuth();
        return;
      }
      if (!response.ok) {
        $("frame-status").textContent = `frame HTTP ${response.status}`;
        return;
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const image = $("live-frame");
      image.onload = () => {
        if (lastFrameObjectUrl) URL.revokeObjectURL(lastFrameObjectUrl);
        lastFrameObjectUrl = url;
      };
      image.src = url;
      $("frame-status").textContent = `frame ${formatTime(new Date().toISOString())}`;
    } catch (error) {
      $("frame-status").textContent = "frame unavailable";
    } finally {
      frameLoading = false;
    }
  };

  const setMessage = (text, kind = "") => {
    const message = $("control-message");
    message.textContent = text;
    message.className = `control-message${kind ? ` ${kind}` : ""}`;
  };

  const runPayload = () => {
    const endpoints = $("run-endpoints").value
      .split(/[\n,]+/)
      .map((value) => value.trim())
      .filter(Boolean);
    return {
      source: $("run-source").value,
      fps: Number($("run-fps").value),
      deadline_ms: Number($("run-deadline").value),
      duration: Number($("run-duration").value),
      camera: Number($("run-camera").value),
      endpoints,
    };
  };

  const bindControls = () => {
    $("run-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      setMessage("Starting run…");
      try {
        const result = await postJson("/api/run/start", runPayload());
        setMessage(`Run ${result.run_id || ""} is ${result.status}.`, "ok");
        await loadAll();
      } catch (error) {
        setMessage(`Start rejected: ${error.message}`, "error");
      }
    });

    $("run-stop").addEventListener("click", async () => {
      if (!window.confirm("Stop the active run? In-flight work may be abandoned.")) return;
      try {
        const result = await postJson("/api/run/stop", {});
        setMessage(`Run is now ${result.status}.`, "ok");
        await loadAll();
      } catch (error) {
        setMessage(`Stop rejected: ${error.message}`, "error");
      }
    });

    const mock = async (action) => {
      const provider = $("mock-provider").value;
      if (!provider) {
        setMessage("No mock provider selected.", "error");
        return;
      }
      const description = action === "slow" ? "force slow responses on" : `${action} on`;
      if (!window.confirm(`Apply ${description} ${provider}? This control is for local mocks only.`)) return;
      try {
        await postJson(`/api/mock/${encodeURIComponent(provider)}/${action}`, {
          confirm: true,
          ms: 1500,
        });
        setMessage(`Mock ${provider}: ${action} applied.`, "ok");
        await loadAll();
      } catch (error) {
        setMessage(`Mock control refused: ${error.message}`, "error");
      }
    };
    $("mock-fail").addEventListener("click", () => mock("fail"));
    $("mock-slow").addEventListener("click", () => mock("slow"));
    $("mock-reset").addEventListener("click", () => mock("reset"));

    $("actuator-stop").addEventListener("click", async () => {
      if (!window.confirm(
        "Send a software stop? This is NOT an emergency stop and does not cut servo power. Continue?"
      )) return;
      try {
        const result = await api("/api/stop", { method: "POST" });
        setMessage(
          `${result.message || "Software stop requested."} ` +
          `(not an emergency stop: ${result.not_an_emergency_stop ? "yes" : "see response"})`,
          result.ok ? "ok" : "error"
        );
        await loadAll();
      } catch (error) {
        setMessage(`Software stop failed: ${error.message}`, "error");
      }
    });

    $("auth-form").addEventListener("submit", (event) => {
      event.preventDefault();
      token = $("auth-token").value.trim();
      if (!token) return;
      sessionStorage.setItem(TOKEN_KEY, token);
      $("auth-token").value = "";
      authPromptOpen = false;
      $("auth-dialog").close();
      $("auth-button").hidden = false;
      loadAll();
      loadFrame();
    });

    $("auth-cancel").addEventListener("click", () => {
      $("auth-token").value = "";
      $("auth-dialog").close();
    });

    $("auth-dialog").addEventListener("close", () => {
      authPromptOpen = false;
      $("auth-token").value = "";
    });

    $("auth-button").addEventListener("click", showAuth);

    window.addEventListener("resize", () => {
      if (lastLatency) drawChart(lastLatency);
    });
  };

  const boot = async () => {
    bindControls();
    try {
      const health = await fetch("/healthz", { cache: "no-store" }).then((response) => response.json());
      if (health.auth_required) {
        $("auth-button").hidden = false;
        if (!token) {
          showAuth();
          return;
        }
      }
    } catch (_) {
      // The API poll below reports the failure.
    }
    await loadAll();
    await loadFrame();
    setInterval(loadAll, 2000);
    setInterval(loadFrame, 1000);
  };

  boot();
})();
