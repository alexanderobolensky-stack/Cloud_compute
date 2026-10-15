// Small dependency-free SVG charts: sparklines for lanes and line charts with a
// crosshair tooltip for the detail views. Labels are inserted via textContent.

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function niceMax(value) {
  if (!(value > 0)) return 1;
  const exp = Math.pow(10, Math.floor(Math.log10(value)));
  for (const step of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) {
    if (value <= step * exp) return step * exp;
  }
  return 10 * exp;
}

export function compact(value) {
  if (value == null || Number.isNaN(value)) return "–";
  const abs = Math.abs(value);
  if (abs >= 1e6) return (value / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (abs >= 1e3) return (value / 1e3).toFixed(1).replace(/\.0$/, "") + "K";
  if (abs >= 100 || Number.isInteger(value)) return String(Math.round(value));
  return value.toFixed(1);
}

// Area sparkline that stretches to its container (preserveAspectRatio="none").
export function sparkline(values, { max = null, color = "var(--series-1)", height = 40 } = {}) {
  const width = 300;
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none",
    class: "spark", "aria-hidden": "true" });
  if (!values.length) return svg;
  const top = max ?? niceMax(Math.max(...values, 1));
  const n = Math.max(values.length, 2);
  const x = (i) => (i / (n - 1)) * width;
  const y = (v) => height - 2 - (Math.min(v, top) / top) * (height - 4);
  let d = "";
  values.forEach((v, i) => { d += `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`; });
  const last = values.length - 1;
  svg.append(
    svgEl("path", { d: `${d}L${x(last).toFixed(1)},${height}L0,${height}Z`, fill: color,
      "fill-opacity": "0.12", stroke: "none" }),
    svgEl("path", { d, fill: "none", stroke: color, "stroke-width": "1.5",
      "vector-effect": "non-scaling-stroke", "stroke-linejoin": "round", "stroke-linecap": "round" }),
  );
  return svg;
}

// Line chart. series: [{name, color, values}] aligned with times (unix seconds).
// opts: {yMax, unit, format, threshold: {value, label}, height}
export function lineChart(container, times, series, opts = {}) {
  container.textContent = "";
  container.classList.add("chart");
  const height = opts.height ?? 180;
  const width = Math.max(container.clientWidth || 600, 240);
  const pad = { l: 40, r: series.length > 1 ? 64 : 16, t: 12, b: 24 };
  const iw = width - pad.l - pad.r;
  const ih = height - pad.t - pad.b;
  const format = opts.format ?? ((v) => compact(v) + (opts.unit ?? ""));

  const all = series.flatMap((s) => s.values.filter((v) => v != null));
  if (opts.threshold) all.push(opts.threshold.value);
  const yMax = opts.yMax ?? niceMax(Math.max(...all, 1) * 1.1);
  const n = times.length;
  const t0 = n ? times[0] : 0;
  const t1 = n > 1 ? times[n - 1] : t0 + 1;
  const x = (t) => pad.l + ((t - t0) / (t1 - t0 || 1)) * iw;
  const y = (v) => pad.t + ih - (Math.min(v, yMax) / yMax) * ih;

  const svg = svgEl("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img" });
  if (opts.label) svg.setAttribute("aria-label", opts.label);

  for (let i = 0; i <= 4; i++) {
    const v = (yMax / 4) * i;
    const yy = y(v);
    svg.append(svgEl("line", { x1: pad.l, x2: pad.l + iw, y1: yy, y2: yy,
      class: i === 0 ? "axis" : "grid" }));
    const label = svgEl("text", { x: pad.l - 6, y: yy + 4, "text-anchor": "end", class: "tick" });
    label.textContent = format(v);
    svg.append(label);
  }
  if (n > 1) {
    const span = t1 - t0;
    for (let i = 0; i <= 3; i++) {
      const t = t0 + (span / 3) * i;
      const label = svgEl("text", { x: x(t), y: height - 6, class: "tick",
        "text-anchor": i === 0 ? "start" : i === 3 ? "end" : "middle" });
      label.textContent = new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
      svg.append(label);
    }
  }
  if (opts.threshold) {
    const ty = y(opts.threshold.value);
    svg.append(svgEl("line", { x1: pad.l, x2: pad.l + iw, y1: ty, y2: ty, class: "threshold" }));
    const tl = svgEl("text", { x: pad.l + iw, y: ty - 5, "text-anchor": "end", class: "tick threshold-label" });
    tl.textContent = opts.threshold.label;
    svg.append(tl);
  }

  // Direct end labels only when they separate; otherwise the legend carries identity.
  const ends = series.map((s) => s.values[s.values.length - 1]).filter((v) => v != null).map(y).sort((a, b) => a - b);
  const labelsCollide = ends.some((v, i) => i > 0 && v - ends[i - 1] < 14);

  for (const s of series) {
    let d = "";
    let pen = false;
    s.values.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(times[i]).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    if (series.length === 1 && d) {
      const first = s.values.findIndex((v) => v != null);
      svg.append(svgEl("path", { d: `${d}L${x(times[n - 1]).toFixed(1)},${y(0)}L${x(times[first]).toFixed(1)},${y(0)}Z`,
        fill: s.color, "fill-opacity": "0.1" }));
    }
    svg.append(svgEl("path", { d, fill: "none", stroke: s.color, "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round" }));
    const lastIdx = s.values.length - 1;
    const last = s.values[lastIdx];
    if (last != null && n) {
      svg.append(svgEl("circle", { cx: x(times[lastIdx]), cy: y(last), r: 4, fill: s.color, class: "end-dot" }));
      if (series.length > 1 && !labelsCollide) {
        const label = svgEl("text", { x: x(times[lastIdx]) + 8, y: y(last) + 4, class: "end-label" });
        label.textContent = `${s.name} ${format(last)}`;
        svg.append(label);
      }
    }
  }

  const cross = svgEl("line", { y1: pad.t, y2: pad.t + ih, class: "crosshair", visibility: "hidden" });
  const dots = series.map((s) => svgEl("circle", { r: 4, fill: s.color, class: "end-dot", visibility: "hidden" }));
  svg.append(cross, ...dots);
  const hit = svgEl("rect", { x: pad.l, y: pad.t, width: iw, height: ih, fill: "transparent", tabindex: 0 });
  svg.append(hit);
  container.append(svg);

  if (series.length > 1) {
    const legend = document.createElement("div");
    legend.className = "legend";
    for (const s of series) {
      const item = document.createElement("span");
      const key = document.createElement("i");
      key.style.background = s.color;
      item.append(key, document.createTextNode(s.name));
      legend.append(item);
    }
    container.prepend(legend);
  }

  const tip = document.createElement("div");
  tip.className = "tooltip";
  tip.hidden = true;
  container.append(tip);

  const show = (i) => {
    if (i < 0 || i >= n) return;
    const cx = x(times[i]);
    cross.setAttribute("x1", cx);
    cross.setAttribute("x2", cx);
    cross.setAttribute("visibility", "visible");
    tip.textContent = "";
    const head = document.createElement("div");
    head.className = "tooltip-time";
    head.textContent = new Date(times[i] * 1000).toLocaleTimeString([], { hourCycle: "h23" });
    tip.append(head);
    series.forEach((s, k) => {
      const v = s.values[i];
      if (v == null) { dots[k].setAttribute("visibility", "hidden"); return; }
      dots[k].setAttribute("cx", cx);
      dots[k].setAttribute("cy", y(v));
      dots[k].setAttribute("visibility", "visible");
      const row = document.createElement("div");
      row.className = "tooltip-row";
      const key = document.createElement("i");
      key.style.background = s.color;
      const val = document.createElement("strong");
      val.textContent = format(v);
      const name = document.createElement("span");
      name.textContent = s.name;
      row.append(key, val, name);
      tip.append(row);
    });
    tip.hidden = false;
    const fitsRight = cx + 12 + tip.offsetWidth <= width;
    tip.style.left = `${Math.max(0, fitsRight ? cx + 12 : cx - tip.offsetWidth - 12)}px`;
    tip.style.top = `${pad.t}px`;
  };
  const hide = () => {
    cross.setAttribute("visibility", "hidden");
    dots.forEach((d) => d.setAttribute("visibility", "hidden"));
    tip.hidden = true;
  };
  const nearest = (clientX) => {
    const rect = svg.getBoundingClientRect();
    const px = ((clientX - rect.left) / rect.width) * width;
    const t = t0 + ((px - pad.l) / iw) * (t1 - t0);
    let best = 0;
    for (let i = 1; i < n; i++) if (Math.abs(times[i] - t) < Math.abs(times[best] - t)) best = i;
    return best;
  };
  let focusIdx = n - 1;
  hit.addEventListener("pointermove", (e) => show((focusIdx = nearest(e.clientX))));
  hit.addEventListener("pointerleave", hide);
  hit.addEventListener("focus", () => show(focusIdx));
  hit.addEventListener("blur", hide);
  hit.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") show((focusIdx = Math.max(0, focusIdx - 1)));
    if (e.key === "ArrowRight") show((focusIdx = Math.min(n - 1, focusIdx + 1)));
  });
}
