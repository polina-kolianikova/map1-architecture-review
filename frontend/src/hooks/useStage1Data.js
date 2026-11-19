import { useCallback, useEffect, useRef, useState } from "react";

import { fetchAllPages, fetchJson, scopedPath } from "../api/stage1";
import { fetchStage4Status } from "../api/stage4";
import { fetchStage5Status } from "../api/stage5";
import { FULL_PUBLIC_SCOPE_KEY } from "../config/scopes";
import { useI18n } from "./useI18n";
import { sourceQualityWarning } from "../lib/status";
import { withStage5Status } from "../lib/stage5Summary";
import { withLayerBStatus } from "../lib/stage4Summary";

export function useStage1Data(scope) {
  const requestRef = useRef(0);
  const summaryRetryRef = useRef(null);
  const scopeRef = useRef("");
  const { t } = useI18n();
  const [summary, setSummary] = useState(null);
  const [regions, setRegions] = useState([]);
  const [layerA, setLayerA] = useState([]);
  const [gridCells, setGridCells] = useState([]);
  const [observations, setObservations] = useState([]);
  const [verification, setVerification] = useState([]);
  const [auditEvents, setAuditEvents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [dataWarning, setDataWarning] = useState("");

  const loadData = useCallback(async ({ background = false } = {}) => {
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    const scopeKey = scopeIdentity(scope);
    const scopeChanged = scopeRef.current !== scopeKey;
    scopeRef.current = scopeKey;
    const isCurrentRequest = () => requestRef.current === requestId;
    window.clearTimeout(summaryRetryRef.current);
    if (!background) setLoading(true);
    setError("");
    setDataWarning("");
    if (scopeChanged) {
      setSummary(null);
      setRegions([]);
      setLayerA([]);
      setGridCells([]);
      setObservations([]);
      setVerification([]);
      setAuditEvents([]);
    }

    if (scope?.isPreview) {
      try {
        const shouldLoadStage5Status = scope.key === FULL_PUBLIC_SCOPE_KEY;
        const [loadedSummary, stage4Status, stage5Status] = await Promise.all([
          fetchJson(scopedPath("/stage1/summary", scope), { timeoutMs: 45000 }),
          fetchStage4Status(scope).catch(() => null),
          shouldLoadStage5Status ? fetchStage5Status(scope).catch(() => null) : Promise.resolve(null),
        ]);
        if (!isCurrentRequest()) {
          return;
        }
        if (loadedSummary?.status === "preparing") {
          setDataWarning(t("dashboardSummaryPreparing"));
          setLoading(false);
          summaryRetryRef.current = window.setTimeout(() => {
            if (isCurrentRequest()) loadData({ background: true });
          }, 10000);
          return;
        }
        setSummary(withStage5Status(withLayerBStatus(loadedSummary, stage4Status), stage5Status));
        setDataWarning(sourceQualityWarning(loadedSummary?.data_quality, t));
        summaryRetryRef.current = window.setTimeout(() => {
          if (isCurrentRequest()) loadData({ background: true });
        }, loadedSummary.summary_refreshing ? 15000 : 60000);
      } catch {
        if (!isCurrentRequest()) {
          return;
        }
        if (scopeChanged) {
          setSummary(null);
        }
        setDataWarning("");
        setError("Local database or source data unavailable. Private calculations are not distributed.");
      }
      setLoading(false);
      return;
    }

    const requests = [
      ["regions", fetchJson(scopedPath("/stage1/regions", scope)), setRegions, []],
    ];
    requests.push(
      ["summary", fetchJson(scopedPath("/stage1/summary", scope)), setSummary, null],
      ["Layer A", fetchAllPages(scopedPath("/stage1/layer-a", scope)), setLayerA, []],
      ["grid", fetchAllPages(scopedPath("/stage1/grid-cells", scope)), setGridCells, []],
      ["Layer B", fetchAllPages(scopedPath("/stage1/observations", scope), 200), setObservations, []],
      ["verification", fetchAllPages(scopedPath("/stage1/verification", scope), 200), setVerification, []],
      ["audit", fetchAllPages(scopedPath("/stage1/verification-audit", scope), 200, 1000), setAuditEvents, []]
    );

    const results = await Promise.allSettled(requests.map(([, request]) => request));
    if (!isCurrentRequest()) {
      return;
    }
    const failed = [];
    let loadedSummary = null;

    results.forEach((result, index) => {
      const [name, , setter, emptyValue] = requests[index];
      if (result.status === "fulfilled") {
        if (result.value?.rows) {
          setter(result.value.rows);
          if (result.value.truncated) {
            failed.push(`${name} display limit`);
          }
          return;
        }
        setter(result.value);
        if (name === "summary") {
          loadedSummary = result.value;
        }
        return;
      }
      setter(emptyValue);
      failed.push(name);
    });

    const loadFailures = failed.filter((name) => !name.endsWith("display limit"));
    if (loadFailures.length) {
      setError(t("dataLoadFailed", { sources: loadFailures.join(", ") }));
    }
    const qualityWarning = sourceQualityWarning(loadedSummary?.data_quality, t);
    setDataWarning(qualityWarning);
    setLoading(false);
  }, [scope, t]);

  useEffect(() => {
    loadData();
    return () => {
      requestRef.current += 1;
      window.clearTimeout(summaryRetryRef.current);
    };
  }, [loadData]);

  return {
    auditEvents,
    assets: [],
    dataWarning,
    error,
    gridCells,
    layerA,
    loadData,
    loading,
    observations,
    regions,
    summary,
    verification,
  };
}

function scopeIdentity(scope) {
  return [
    scope?.key || "",
    scope?.regionCode || "",
    scope?.gridVersion || "",
    scope?.isPreview ? "preview" : "workspace",
  ].join(":");
}
