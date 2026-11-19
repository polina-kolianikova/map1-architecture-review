export function shortId(value) {
  if (!value) {
    return "none";
  }
  return value.slice(0, 8);
}

export function cellLabel(value) {
  return `Cell ${shortId(value)}`;
}

export function formatNumber(value) {
  if (value === null || value === undefined) {
    return "n/a";
  }
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "n/a";
  }
  return number.toFixed(2);
}

export function formatInteger(value) {
  if (value === null || value === undefined) {
    return "n/a";
  }
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "n/a";
  }
  return Math.round(number).toLocaleString("en-US");
}

export function formatPercent(value) {
  if ((typeof value !== "number" && typeof value !== "string") || (typeof value === "string" && !value.trim())) {
    return "n/a";
  }
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "n/a";
  }
  return `${Math.round(number * 100)}%`;
}

export function formatPercentFromWhole(value) {
  if ((typeof value !== "number" && typeof value !== "string") || (typeof value === "string" && !value.trim())) {
    return "n/a";
  }
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "n/a";
  }
  return `${Math.round(number)}%`;
}

export function formatDate(value) {
  if (!value) {
    return "none";
  }
  return String(value).slice(0, 10);
}

export function timestamp(value) {
  const time = Date.parse(value || "");
  return Number.isFinite(time) ? time : 0;
}
