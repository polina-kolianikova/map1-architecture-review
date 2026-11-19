import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, MapPinned } from "lucide-react";

import { AssetPassport } from "./components/AssetPassport";
import { PanelHeader } from "./components/common";
import { GlobalView } from "./components/GlobalView";
import { MapPanel } from "./components/MapPanel";
import { Sidebar } from "./components/Sidebar";
import { Topbar } from "./components/Topbar";
import { VerificationTable } from "./components/VerificationTable";
import { VerificationToolbar } from "./components/VerificationToolbar";
import { ENABLE_VECTOR_TILES, MAPBOX_TOKEN, VERIFICATION_PAGE_SIZE } from "./config/runtime";
import {
  DEFAULT_WORKSPACE_SCOPE_KEY,
  FULL_PUBLIC_SCOPE_KEY,
  WORKSPACE_SCOPES,
  resolveWorkspaceScope,
  scopeSearchParams,
} from "./config/scopes";
import { fetchAsset, fetchAssetIds, fetchAssetNdviHistory, fetchAssetPage, pickAssetAtLocation } from "./api/stage2";
import { usePersistentState } from "./hooks/usePersistentState";
import { useStage1Data } from "./hooks/useStage1Data";
import { useWorkspaceAvailability } from "./hooks/useWorkspaceAvailability";
import { useI18n } from "./hooks/useI18n";
import { cellLabel, timestamp } from "./lib/formatters";
import { toFeatureCollection } from "./lib/geojson";
import { statusRank } from "./lib/status";
import { layerBObservationCounts } from "./lib/stage4Summary";

const REGISTER_STATUSES = ["anomaly", "watch", "ok", "insufficient_data", "unverified"];

export function App() {
  const { t } = useI18n();
  const [workspaceScopeKey, setWorkspaceScopeKey] = usePersistentState(
    "map1.workspaceScope.v2",
    DEFAULT_WORKSPACE_SCOPE_KEY
  );
  const workspaceScope = useMemo(
    () => resolveWorkspaceScope(workspaceScopeKey),
    [workspaceScopeKey]
  );
  const workspaceScopes = useWorkspaceAvailability(WORKSPACE_SCOPES);
  const [scopeNotice, setScopeNotice] = useState("");
  useEffect(() => {
    if (workspaceScopes.find((scope) => scope.key === workspaceScope.key)?.available === false) {
      setWorkspaceScopeKey(FULL_PUBLIC_SCOPE_KEY);
      setScopeNotice(t("pilotScopeFallback"));
    }
  }, [workspaceScope.key, workspaceScopes, setWorkspaceScopeKey, t]);
  const {
    auditEvents,
    assets,
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
  } = useStage1Data(workspaceScope);
  const [selectedCellUuid, setSelectedCellUuid] = useState("");
  const [assetFilter, setAssetFilter] = useState("");
  const [assetStatusFilter, setAssetStatusFilter] = useState("all");
  const [assetCarbonFilter, setAssetCarbonFilter] = useState("all");
  const [assetSignalFilter, setAssetSignalFilter] = useState("all");
  const [assetObservedFrom, setAssetObservedFrom] = useState("");
  const [assetObservedTo, setAssetObservedTo] = useState("");
  const [assetPage, setAssetPage] = useState(0);
  const [viewMode, setViewMode] = useState("global");
  const [cleanMode, setCleanMode] = usePersistentState("map1.cleanMode", true);
  const [mapFocus, setMapFocus] = useState(false);
  const [workspaceEntering, setWorkspaceEntering] = useState(false);
  const [registerRefreshTick, setRegisterRefreshTick] = useState(0);
  const [extraAssets, setExtraAssets] = useState([]);
  const [assetLoadError, setAssetLoadError] = useState("");
  const [registerRows, setRegisterRows] = useState([]);
  const [registerResultCount, setRegisterResultCount] = useState(0);
  const [registerHasMore, setRegisterHasMore] = useState(false);
  const [registerExactTotal, setRegisterExactTotal] = useState(false);
  const [registerStatusCounts, setRegisterStatusCounts] = useState(null);
  const [registerLoading, setRegisterLoading] = useState(true);
  const [registerError, setRegisterError] = useState("");
  const [assetDetailLoading, setAssetDetailLoading] = useState(false);
  const [assetNdviHistory, setAssetNdviHistory] = useState([]);
  const [assetNdviHistoryLoading, setAssetNdviHistoryLoading] = useState(false);
  const [assetNdviHistoryError, setAssetNdviHistoryError] = useState("");
  const [assetNdviHistoryMonths, setAssetNdviHistoryMonths] = useState(12);
  const [visibleAssetUuidSet, setVisibleAssetUuidSet] = useState(null);
  const [visibleAssetIdsLoading, setVisibleAssetIdsLoading] = useState(false);
  const [visibleAssetIdsTruncated, setVisibleAssetIdsTruncated] = useState(false);
  const assetPassportRef = useRef(null);
  const assetRegisterRef = useRef(null);
  const assetDetailRequestRef = useRef(0);
  const assetFilterRef = useRef({
    carbon: "all",
    filter: "",
    from: "",
    signal: "all",
    status: "all",
    to: "",
  });
  const registerQueryRef = useRef("");
  const clearAssetSelection = useCallback(() => {
    assetDetailRequestRef.current += 1;
    setSelectedCellUuid("");
    setAssetLoadError("");
    setAssetDetailLoading(false);
  }, []);

  useEffect(() => {
    assetDetailRequestRef.current += 1;
    setSelectedCellUuid("");
    setExtraAssets([]);
    setAssetLoadError("");
    setAssetDetailLoading(false);
    setAssetNdviHistory([]);
    setAssetPage(0);
  }, [workspaceScope.key]);

  const latestVerificationByCell = useMemo(() => {
    const byCell = new Map();
    verification.forEach((row) => {
      const current = byCell.get(row.cell_uuid);
      const nextTime = timestamp(row.created_at);
      const currentTime = timestamp(current?.created_at);
      if (
        !current ||
        nextTime > currentTime ||
        (nextTime === currentTime && statusRank(row.anomaly_type) > statusRank(current.anomaly_type))
      ) {
        byCell.set(row.cell_uuid, {
          ...row,
          rank: statusRank(row.anomaly_type),
        });
      }
    });
    return byCell;
  }, [verification]);

  const latestObservationByCell = useMemo(() => {
    const byCell = new Map();
    observations.forEach((row) => {
      const current = byCell.get(row.cell_uuid);
      if (!current || timestamp(row.acquisition_date) >= timestamp(current.acquisition_date)) {
        byCell.set(row.cell_uuid, row);
      }
    });
    return byCell;
  }, [observations]);

  const auditByCell = useMemo(() => {
    const byCell = new Map();
    auditEvents.forEach((event) => {
      const current = byCell.get(event.cell_uuid) || {
        audit_event_count: 0,
        latest_event_at: "",
      };
      current.audit_event_count += 1;
      if (timestamp(event.created_at) >= timestamp(current.latest_event_at)) {
        current.latest_event_at = event.created_at;
      }
      byCell.set(event.cell_uuid, current);
    });
    return byCell;
  }, [auditEvents]);

  const joinedAssetRows = useMemo(() => {
    return gridCells
      .map((cell) => {
        const result = latestVerificationByCell.get(cell.uuid);
        const observation = latestObservationByCell.get(cell.uuid);
        const audit = auditByCell.get(cell.uuid) || {};
        return {
          id: result?.id || `cell-${cell.id}`,
          cell_id: cell.id,
          cell_uuid: cell.uuid,
          region_code: cell.region_code,
          area_ha: cell.area_ha,
          area_coefficient: cell.area_coefficient ?? null,
          grid_version: cell.grid_version,
          verification_result_id: result?.id || null,
          anomaly_type: result?.anomaly_type || "unverified",
          score: result?.score ?? null,
          explanation: result?.explanation || t("noVerificationResultRecorded"),
          ndvi: result?.ndvi ?? observation?.ndvi ?? null,
          acquisition_date: result?.acquisition_date || observation?.acquisition_date || "",
          previous_ndvi: result?.previous_ndvi ?? null,
          previous_acquisition_date: result?.previous_acquisition_date || "",
          previous_observation_id: result?.previous_observation_id ?? null,
          layer_a_relation: result?.layer_a_relation || "",
          layer_a_intersection_area_ha: result?.layer_a_intersection_area_ha ?? null,
          layer_a_coverage_ratio: result?.layer_a_coverage_ratio ?? null,
          layer_a_source_name: result?.layer_a_source_name || "",
          layer_a_source_quality: result?.layer_a_source_quality || "",
          layer_a_official_status: result?.layer_a_official_status || "",
          layer_a_source_checked_at: result?.layer_a_source_checked_at || "",
          layer_a_latest_check_at: result?.layer_a_latest_check_at || "",
          layer_a_latest_check_status: result?.layer_a_latest_check_status || "",
          layer_a_latest_check_feature_count: result?.layer_a_latest_check_feature_count ?? null,
          layer_a_data_age_days: result?.layer_a_data_age_days ?? null,
          observation_status: result?.observation_status || observation?.status || "",
          observation_cloud_coverage:
            result?.observation_cloud_coverage ?? observation?.cloud_coverage ?? null,
          observation_statistics_source:
            result?.observation_statistics_source || observation?.statistics_source || "",
          observation_valid_pixel_count:
            result?.observation_valid_pixel_count ?? observation?.valid_pixel_count ?? null,
          observation_valid_pixel_ratio:
            result?.observation_valid_pixel_ratio ?? observation?.valid_pixel_ratio ?? null,
          observation_sample_count:
            result?.observation_sample_count ?? observation?.sample_count ?? null,
          audit_event_count: audit.audit_event_count || 0,
          latest_event_at: audit.latest_event_at || "",
          created_at: result?.created_at || "",
          rank: result?.rank || 0,
        };
      })
      .sort(
        (left, right) =>
          statusRank(right.anomaly_type) - statusRank(left.anomaly_type) ||
          timestamp(right.latest_event_at || right.created_at) -
            timestamp(left.latest_event_at || left.created_at) ||
          left.cell_uuid.localeCompare(right.cell_uuid)
      );
  }, [auditByCell, gridCells, latestObservationByCell, latestVerificationByCell, t]);

  const assetRows = useMemo(() => {
    const sourceRows = assets.length ? assets.map(normalizeAssetRow) : joinedAssetRows;
    const rowsByUuid = new Map(sourceRows.map((row) => [row.cell_uuid, row]));
    registerRows.forEach((row) => {
      rowsByUuid.set(row.cell_uuid, normalizeAssetRow({ ...rowsByUuid.get(row.cell_uuid), ...row }));
    });
    extraAssets.forEach((row) => {
      rowsByUuid.set(row.cell_uuid, normalizeAssetRow({ ...rowsByUuid.get(row.cell_uuid), ...row }));
    });
    return Array.from(rowsByUuid.values()).sort(
      (left, right) =>
        statusRank(right.anomaly_type) - statusRank(left.anomaly_type) ||
        timestamp(right.latest_event_at || right.created_at) -
          timestamp(left.latest_event_at || left.created_at) ||
        left.cell_uuid.localeCompare(right.cell_uuid)
    );
  }, [assets, extraAssets, joinedAssetRows, registerRows]);

  const pageCount = registerExactTotal
    ? Math.max(1, Math.ceil(registerResultCount / VERIFICATION_PAGE_SIZE))
    : Math.max(1, assetPage + (registerHasMore ? 2 : 1));
  const currentPage = Math.min(assetPage, pageCount - 1);
  const visibleAssetRows = registerRows;

  const selectedAsset = useMemo(() => {
    if (!selectedCellUuid) {
      return null;
    }
    return (
      assetRows.find((row) => row.cell_uuid === selectedCellUuid) ||
      null
    );
  }, [assetRows, selectedCellUuid]);

  const selectedAuditEvents = useMemo(() => {
    if (!selectedAsset?.cell_uuid) {
      return [];
    }
    return auditEvents.filter((event) => event.cell_uuid === selectedAsset.cell_uuid);
  }, [auditEvents, selectedAsset]);
  const hasActiveAssetFilters = Boolean(
    assetFilter.trim() ||
      assetStatusFilter !== "all" ||
      assetCarbonFilter !== "all" ||
      assetSignalFilter !== "all" ||
      assetObservedFrom ||
      assetObservedTo
  );
  const hasInvalidObservedRange = Boolean(
    assetObservedFrom &&
      assetObservedTo &&
      assetObservedFrom > assetObservedTo
  );
  const shouldApplyMapAssetFilter =
    hasActiveAssetFilters && !hasInvalidObservedRange;
  const assetTileFilterQuery = useMemo(() => {
    const params = scopeSearchParams(workspaceScope);
    if (shouldApplyMapAssetFilter) {
      params.set("status", assetStatusFilter);
      params.set("carbon_status", assetCarbonFilter);
      params.set("signal_status", assetSignalFilter);
      const search = assetFilter.trim();
      if (search) {
        params.set("search", search);
      }
      if (assetObservedFrom) {
        params.set("observed_from", assetObservedFrom);
      }
      if (assetObservedTo) {
        params.set("observed_to", assetObservedTo);
      }
    }
    return params.toString();
  }, [
    assetCarbonFilter,
    assetFilter,
    assetObservedFrom,
    assetObservedTo,
    assetSignalFilter,
    assetStatusFilter,
    shouldApplyMapAssetFilter,
    workspaceScope,
  ]);
  const auditEmptyText = useMemo(() => {
    if (loading) {
      return t("loadingData");
    }
    if (!selectedCellUuid) {
      return t("selectAssetAudit");
    }
    if (selectedAsset?.audit_event_count > 0) {
      return t("latestEventCountInPassport");
    }
    if (hasActiveAssetFilters && !registerResultCount) {
      return t("noMatchingAssetSelected");
    }
    if (hasActiveAssetFilters) {
      return t("noAuditEventsCurrentAsset");
    }
    return t("noAuditEventsYet");
  }, [hasActiveAssetFilters, loading, registerResultCount, selectedAsset, selectedCellUuid, t]);

  const baseObservationStatusCounts = useMemo(() => {
    const counts = new Map();
    observations.forEach((row) => {
      const status = row.status || "unknown";
      counts.set(status, (counts.get(status) || 0) + 1);
    });
    return Array.from(counts.entries())
      .map(([status, count]) => ({ status, count }))
      .sort((left, right) => statusRank(right.status) - statusRank(left.status) || left.status.localeCompare(right.status));
  }, [observations]);

  useEffect(() => {
    const filterChanged =
      assetFilterRef.current.filter !== assetFilter ||
      assetFilterRef.current.status !== assetStatusFilter ||
      assetFilterRef.current.carbon !== assetCarbonFilter ||
      assetFilterRef.current.signal !== assetSignalFilter ||
      assetFilterRef.current.from !== assetObservedFrom ||
      assetFilterRef.current.to !== assetObservedTo;
    assetFilterRef.current = {
      carbon: assetCarbonFilter,
      filter: assetFilter,
      from: assetObservedFrom,
      signal: assetSignalFilter,
      status: assetStatusFilter,
      to: assetObservedTo,
    };
    if (filterChanged) {
      clearAssetSelection();
      setAssetPage(0);
    }
  }, [
    assetCarbonFilter,
    assetFilter,
    assetObservedFrom,
    assetObservedTo,
    assetSignalFilter,
    assetStatusFilter,
    clearAssetSelection,
  ]);

  useEffect(() => {
    let ignore = false;
    const offset = assetPage * VERIFICATION_PAGE_SIZE;
    const registerQueryKey = [
      workspaceScope.key,
      workspaceScope.regionCode,
      workspaceScope.gridVersion,
      offset,
      assetFilter,
      assetStatusFilter,
      assetCarbonFilter,
      assetSignalFilter,
      assetObservedFrom,
      assetObservedTo,
      hasInvalidObservedRange ? "invalid" : "valid",
    ].join("\u001f");
    const registerQueryChanged = registerQueryRef.current !== registerQueryKey;
    registerQueryRef.current = registerQueryKey;
    setRegisterLoading(true);
    setRegisterError("");
    if (registerQueryChanged) {
      setRegisterRows([]);
      setRegisterResultCount(0);
      setRegisterHasMore(false);
      setRegisterExactTotal(false);
    }
    if (hasInvalidObservedRange) {
      setRegisterError(t("observedDateRangeInvalid"));
      setRegisterLoading(false);
      setRegisterExactTotal(true);
      return () => {
        ignore = true;
      };
    }
    fetchAssetPage({
      carbonStatus: assetCarbonFilter,
      limit: VERIFICATION_PAGE_SIZE,
      offset,
      observedFrom: assetObservedFrom,
      observedTo: assetObservedTo,
      search: assetFilter,
      signalStatus: assetSignalFilter,
      status: assetStatusFilter,
      scope: workspaceScope,
    })
      .then((page) => {
        if (ignore) {
          return;
        }
        const rows = page.rows || [];
        const total = Number(page.total || 0);
        setRegisterRows(rows.map(normalizeAssetRow));
        setRegisterResultCount(total);
        setRegisterHasMore(Boolean(page.has_more));
        setRegisterExactTotal(Boolean(page.exact_total));
      })
      .catch(() => {
        if (!ignore) {
          if (registerQueryChanged) {
            setRegisterRows([]);
            setRegisterResultCount(0);
            setRegisterHasMore(false);
            setRegisterExactTotal(false);
          }
          setRegisterError(t("assetRegisterUnavailable"));
        }
      })
      .finally(() => {
        if (!ignore) {
          setRegisterLoading(false);
        }
      });
    return () => {
      ignore = true;
    };
  }, [
    assetCarbonFilter,
    assetFilter,
    assetObservedFrom,
    assetObservedTo,
    assetPage,
    assetSignalFilter,
    assetStatusFilter,
    hasInvalidObservedRange,
    registerRefreshTick,
    workspaceScope,
  ]);

  useEffect(() => {
    let ignore = false;
    if (hasInvalidObservedRange) {
      setRegisterStatusCounts(null);
      return () => {
        ignore = true;
      };
    }
    if (workspaceScope.isPreview) {
      setRegisterStatusCounts(summary?.verification_status || null);
      return () => {
        ignore = true;
      };
    }

    Promise.all(
      REGISTER_STATUSES.map((status) =>
        fetchAssetPage({
          carbonStatus: assetCarbonFilter,
          limit: 1,
          offset: 0,
          observedFrom: assetObservedFrom,
          observedTo: assetObservedTo,
          search: assetFilter,
          signalStatus: assetSignalFilter,
          status,
          scope: workspaceScope,
        }).then((page) => ({
          anomaly_type: status,
          result_count: Number(page.total || 0),
        }))
      )
    )
      .then((counts) => {
        if (!ignore) {
          setRegisterStatusCounts(counts);
        }
      })
      .catch(() => {
        if (!ignore) {
          setRegisterStatusCounts(null);
        }
      });

    return () => {
      ignore = true;
    };
  }, [
    assetCarbonFilter,
    assetFilter,
    assetObservedFrom,
    assetObservedTo,
    assetSignalFilter,
    hasInvalidObservedRange,
    registerRefreshTick,
    workspaceScope,
    summary,
  ]);

  useEffect(() => {
    let ignore = false;
    if (!hasActiveAssetFilters || hasInvalidObservedRange || workspaceScope.isPreview) {
      setVisibleAssetUuidSet(null);
      setVisibleAssetIdsLoading(false);
      setVisibleAssetIdsTruncated(false);
      return () => {
        ignore = true;
      };
    }
    setVisibleAssetUuidSet(null);
    setVisibleAssetIdsTruncated(false);
    setVisibleAssetIdsLoading(true);
    fetchAssetIds({
      carbonStatus: assetCarbonFilter,
      observedFrom: assetObservedFrom,
      observedTo: assetObservedTo,
      search: assetFilter,
      signalStatus: assetSignalFilter,
      status: assetStatusFilter,
      scope: workspaceScope,
    })
      .then((payload) => {
        if (!ignore) {
          const ids = new Set(payload.ids || []);
          setVisibleAssetUuidSet(ids);
          setVisibleAssetIdsTruncated(Boolean(payload.truncated));
        }
      })
      .catch(() => {
        if (!ignore) {
          setVisibleAssetUuidSet(new Set());
          setVisibleAssetIdsTruncated(false);
        }
      })
      .finally(() => {
        if (!ignore) {
          setVisibleAssetIdsLoading(false);
        }
      });
    return () => {
      ignore = true;
    };
  }, [
    assetCarbonFilter,
    assetFilter,
    assetObservedFrom,
    assetObservedTo,
    assetSignalFilter,
    assetStatusFilter,
    hasInvalidObservedRange,
    hasActiveAssetFilters,
    workspaceScope,
  ]);

  useEffect(() => {
    let ignore = false;
    setAssetNdviHistory([]);
    setAssetNdviHistoryError("");
    if (!selectedCellUuid) {
      setAssetNdviHistoryLoading(false);
      return () => {
        ignore = true;
      };
    }
    setAssetNdviHistoryLoading(true);
    fetchAssetNdviHistory(selectedCellUuid, assetNdviHistoryMonths, workspaceScope)
      .then((payload) => {
        if (!ignore) {
          setAssetNdviHistory(payload.rows || []);
        }
      })
      .catch(() => {
        if (!ignore) {
          setAssetNdviHistory([]);
          setAssetNdviHistoryError(t("ndviHistoryUnavailable"));
        }
      })
      .finally(() => {
        if (!ignore) {
          setAssetNdviHistoryLoading(false);
        }
      });
    return () => {
      ignore = true;
    };
  }, [assetNdviHistoryMonths, selectedCellUuid, workspaceScope]);

  useEffect(() => {
    if (!cleanMode && mapFocus) {
      setMapFocus(false);
    }
  }, [cleanMode, mapFocus]);

  useEffect(() => {
    if (!selectedCellUuid || workspaceScope.isPreview) {
      return;
    }
    if (visibleAssetIdsLoading) {
      return;
    }
    if (visibleAssetIdsTruncated) {
      return;
    }
    const selectedStillVisible =
      !shouldApplyMapAssetFilter ||
      (visibleAssetUuidSet
        ? visibleAssetUuidSet.has(selectedCellUuid)
        : registerRows.some((row) => row.cell_uuid === selectedCellUuid));
    if (!selectedStillVisible) {
      clearAssetSelection();
    }
  }, [
    clearAssetSelection,
    registerRows,
    selectedCellUuid,
    shouldApplyMapAssetFilter,
    visibleAssetIdsLoading,
    visibleAssetIdsTruncated,
    visibleAssetUuidSet,
    workspaceScope.isPreview,
  ]);

  const changeAssetPage = useCallback(
    (nextPage) => {
      const boundedPage = Math.max(0, Math.min(nextPage, pageCount - 1));
      setAssetPage(boundedPage);
    },
    [pageCount]
  );

  const scrollPassportIntoView = useCallback(() => {
    if (typeof window === "undefined" || !window.matchMedia("(max-width: 900px)").matches) {
      return;
    }
    window.setTimeout(() => {
      assetPassportRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 80);
  }, []);

  const loadAssetByUuid = useCallback((cellUuid) => {
    const requestId = assetDetailRequestRef.current + 1;
    assetDetailRequestRef.current = requestId;
    setAssetLoadError("");
    setAssetDetailLoading(true);
    fetchAsset(cellUuid, workspaceScope)
      .then((row) => {
        if (assetDetailRequestRef.current !== requestId) {
          return;
        }
        setExtraAssets((current) => {
          const rowsByUuid = new Map(current.map((item) => [item.cell_uuid, item]));
          rowsByUuid.set(row.cell_uuid, row);
          return Array.from(rowsByUuid.values());
        });
      })
      .catch(() => {
        if (assetDetailRequestRef.current === requestId) {
          setAssetLoadError(t("assetDetailsUnavailable"));
        }
      })
      .finally(() => {
        if (assetDetailRequestRef.current === requestId) {
          setAssetDetailLoading(false);
        }
      });
  }, [workspaceScope]);

  useEffect(() => {
    if (selectedAsset) {
      scrollPassportIntoView();
    }
  }, [scrollPassportIntoView, selectedAsset]);

  const selectAssetCell = useCallback(
    (cellUuid) => {
      if (cellUuid === selectedCellUuid) {
        clearAssetSelection();
        return;
      }
      setSelectedCellUuid(cellUuid);
      loadAssetByUuid(cellUuid);
      scrollPassportIntoView();
    },
    [assetRows, clearAssetSelection, loadAssetByUuid, registerRows, scrollPassportIntoView, selectedCellUuid]
  );

  const pickAssetByLocation = useCallback(
    ({ lng, lat }) => {
      const requestId = assetDetailRequestRef.current + 1;
      assetDetailRequestRef.current = requestId;
      setAssetLoadError("");
      setAssetDetailLoading(true);
      pickAssetAtLocation({ lng, lat, scope: workspaceScope })
        .then((payload) => {
          if (assetDetailRequestRef.current !== requestId) {
            return;
          }
          if (!payload?.cell_uuid) {
            setAssetLoadError(t("noAssetAtLocation"));
            setAssetDetailLoading(false);
            return;
          }
          setSelectedCellUuid(payload.cell_uuid);
          loadAssetByUuid(payload.cell_uuid);
          scrollPassportIntoView();
        })
        .catch(() => {
          if (assetDetailRequestRef.current === requestId) {
            setAssetLoadError(t("noAssetAtLocation"));
            setAssetDetailLoading(false);
          }
        });
    },
    [loadAssetByUuid, scrollPassportIntoView, workspaceScope]
  );

  const layerAFeatureCollection = useMemo(
    () => toFeatureCollection(layerA, (item) => ({
      id: item.id,
      sourceName: item.source_name,
      status: item.official_status,
      forestType: item.forest_type,
      quality: item.source_quality,
    })),
    [layerA]
  );

  const regionFeatureCollection = useMemo(
    () => toFeatureCollection(regions, (item) => ({
      id: item.id,
      code: item.code,
      name: item.name,
    })),
    [regions]
  );

  const gridFeatureCollection = useMemo(
    () => workspaceScope.isPreview && shouldApplyMapAssetFilter
      ? toFeatureCollection(registerRows, (item) => ({
          id: item.cell_id ?? item.id,
          uuid: item.cell_uuid,
          areaHa: item.area_ha,
          regionCode: item.region_code,
          verificationStatus: item.anomaly_type || "unverified",
          verificationScore: item.score ?? null,
          ndvi: item.ndvi ?? null,
        }))
      : toFeatureCollection(gridCells, (item) => ({
      id: item.id,
      uuid: item.uuid,
      areaHa: item.area_ha,
      regionCode: item.region_code,
      verificationStatus: latestVerificationByCell.get(item.uuid)?.anomaly_type || "unverified",
      verificationScore: latestVerificationByCell.get(item.uuid)?.score ?? null,
      ndvi: latestObservationByCell.get(item.uuid)?.ndvi ?? null,
    })),
    [gridCells, latestObservationByCell, latestVerificationByCell, registerRows,
      shouldApplyMapAssetFilter, workspaceScope.isPreview]
  );
  const selectedAssetFeatureCollection = useMemo(() => {
    if (!selectedAsset?.geometry) {
      return { type: "FeatureCollection", features: [] };
    }
    return toFeatureCollection([selectedAsset], (item) => ({
      id: item.id,
      uuid: item.cell_uuid,
      areaHa: item.area_ha,
      regionCode: item.region_code,
      verificationStatus: item.anomaly_type || "unverified",
      verificationScore: item.score ?? null,
      ndvi: item.ndvi ?? null,
    }));
  }, [selectedAsset]);
  const visibleGridFeatureCollection = useMemo(() => {
    const selectedFeature = selectedAssetFeatureCollection.features[0];
    const baseFeatures = shouldApplyMapAssetFilter && !workspaceScope.isPreview
      ? gridFeatureCollection.features.filter((feature) => {
          const uuid = feature.properties?.uuid;
          const visibleCellUuids = visibleAssetUuidSet || new Set();
          return visibleCellUuids.has(uuid) || uuid === selectedCellUuid;
        })
      : gridFeatureCollection.features;
    const selectedAlreadyLoaded = !selectedFeature || baseFeatures.some(
      (feature) => feature.properties?.uuid === selectedFeature.properties?.uuid
    );
    return {
      ...gridFeatureCollection,
      features: selectedAlreadyLoaded ? baseFeatures : [...baseFeatures, selectedFeature],
    };
  }, [
    gridFeatureCollection,
    selectedAssetFeatureCollection,
    selectedCellUuid,
    shouldApplyMapAssetFilter,
    visibleAssetUuidSet,
    workspaceScope.isPreview,
  ]);
  const fallbackStatusCounts = useMemo(() => {
    const counts = new Map();
    assetRows.forEach((row) => {
      counts.set(row.anomaly_type, (counts.get(row.anomaly_type) || 0) + 1);
    });
    return Array.from(counts.entries())
      .map(([anomaly_type, result_count]) => ({ anomaly_type, result_count }))
      .sort((left, right) => statusRank(right.anomaly_type) - statusRank(left.anomaly_type));
  }, [assetRows]);
  const fallbackLayerASources = useMemo(() => {
    const sources = new Map();
    layerA.forEach((row) => {
      const key = `${row.source_name || t("sourceLayerA")}|${row.source_quality || ""}|${row.official_status || ""}`;
      const current = sources.get(key) || {
        source_name: row.source_name || t("sourceLayerA"),
        source_quality: row.source_quality,
        official_status: row.official_status,
        latest_check_status: null,
        last_successful_refresh_at: row.fetched_at,
        data_age_days: ageInDays(row.fetched_at),
        feature_count: 0,
      };
      current.feature_count += 1;
      sources.set(key, current);
    });
    return Array.from(sources.values());
  }, [layerA, t]);
  const verificationRows = useMemo(() => Array.from(latestVerificationByCell.values()), [latestVerificationByCell]);
  const summaryMatchesScope = matchesWorkspaceScope(summary, workspaceScope);
  const scopedSummary = summaryMatchesScope ? summary : null;
  const fallbackSummary = scopedSummary || {
    grid_cells: gridCells.length,
    ogm_parcels: layerA.length,
    real_sentinel_observations: observations.filter((row) => row.ndvi !== null && row.ndvi !== undefined).length,
    ndvi_coverage_ratio: fallbackNdviCoverage(observations, gridCells.length),
    verification_results: verificationRows.length,
    verification_audit_events: auditEvents.length,
    layer_a_sources: fallbackLayerASources,
    verification_status: fallbackStatusCounts,
  };
  const activePreviewSummary = workspaceScope.isPreview && summaryMatchesScope ? summary : null;
  const emptyPreviewSummary = {
    grid_cells: 0,
    ogm_parcels: 0,
    real_sentinel_observations: 0,
    ndvi_coverage_ratio: 0,
    verification_results: 0,
    verification_audit_events: 0,
    layer_a_sources: [],
    verification_status: [],
  };
  const displaySummary = workspaceScope.isPreview ? activePreviewSummary || emptyPreviewSummary : fallbackSummary;
  const layerBObservationStatusCounts = useMemo(() => {
    return layerBObservationCounts(displaySummary.stage4_layer_b);
  }, [displaySummary.stage4_layer_b]);
  const observationStatusCounts = baseObservationStatusCounts.length
    ? baseObservationStatusCounts
    : layerBObservationStatusCounts;
  const portfolioRegions = useMemo(
    () =>
      regions.map((region) => ({
        id: region.id,
        code: region.code,
        countryCode: region.country_code || "TR",
        name: region.name || region.code || t("turkeyRegion"),
      })),
    [regions, t]
  );
  const statusCounts = registerStatusCounts || displaySummary.verification_status || [];
  const sidebarStatusCounts = useMemo(() => {
    const byStatus = new Map(statusCounts.map((row) => [row.anomaly_type, row.result_count]));
    const unverifiedCount = registerStatusCounts
      ? byStatus.get("unverified") || 0
      : Math.max(0, Number(displaySummary.grid_cells || 0) - Number(displaySummary.verification_results || 0));
    return REGISTER_STATUSES.map((status) => ({
      anomaly_type: status,
      result_count: status === "unverified" ? unverifiedCount : byStatus.get(status) || 0,
    }));
  }, [displaySummary.grid_cells, displaySummary.verification_results, registerStatusCounts, statusCounts]);
  const visibleMapCellCount =
    ENABLE_VECTOR_TILES && workspaceScope.isPreview && !shouldApplyMapAssetFilter
      ? displaySummary.grid_cells
      : ENABLE_VECTOR_TILES
        ? registerResultCount || visibleGridFeatureCollection.features.length
        : visibleGridFeatureCollection.features.length;
  const globalStatusCounts = sidebarStatusCounts;
  const chooseStatusFilter = useCallback((status) => {
    setAssetPage(0);
    setAssetStatusFilter((current) => (current === status ? "all" : status));
    setAssetFilter("");
    window.setTimeout(() => {
      assetRegisterRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 0);
  }, []);
  const clearAssetFilters = useCallback(() => {
    setAssetPage(0);
    setAssetFilter("");
    setAssetStatusFilter("all");
    setAssetCarbonFilter("all");
    setAssetSignalFilter("all");
    setAssetObservedFrom("");
    setAssetObservedTo("");
  }, []);
  const selectWorkspaceScope = useCallback(
    (nextScopeKey) => {
      if (nextScopeKey === workspaceScope.key) {
        return;
      }
      assetDetailRequestRef.current += 1;
      setWorkspaceScopeKey(nextScopeKey);
      setSelectedCellUuid("");
      setExtraAssets([]);
      setAssetLoadError("");
      setAssetDetailLoading(false);
      setAssetNdviHistory([]);
      setRegisterRows([]);
      setRegisterResultCount(0);
      setRegisterHasMore(false);
      setRegisterExactTotal(false);
      setRegisterStatusCounts(null);
      setVisibleAssetUuidSet(null);
      setVisibleAssetIdsTruncated(false);
      clearAssetFilters();
      setRegisterRefreshTick((current) => current + 1);
    },
    [clearAssetFilters, setWorkspaceScopeKey, workspaceScope.key]
  );
  const updateAssetFilter = useCallback((value) => {
    setAssetPage(0);
    setAssetFilter(value);
  }, []);
  const updateAssetStatusFilter = useCallback((value) => {
    setAssetPage(0);
    setAssetStatusFilter(value);
  }, []);
  const updateAssetCarbonFilter = useCallback((value) => {
    setAssetPage(0);
    setAssetCarbonFilter(value);
  }, []);
  const updateAssetSignalFilter = useCallback((value) => {
    setAssetPage(0);
    setAssetSignalFilter(value);
  }, []);
  const updateAssetObservedFrom = useCallback((value) => {
    setAssetPage(0);
    setAssetObservedFrom(value);
  }, []);
  const updateAssetObservedTo = useCallback((value) => {
    setAssetPage(0);
    setAssetObservedTo(value);
  }, []);
  const previewGridCellCount = Number(activePreviewSummary?.grid_cells || 0);
  const workspaceDataReady = workspaceScope.isPreview
    ? Boolean(activePreviewSummary) && previewGridCellCount > 0
    : displaySummary.grid_cells > 0 || gridCells.length > 0;
  const enterWorkspace = useCallback(() => {
    if (!workspaceDataReady) {
      return;
    }
    setWorkspaceEntering(true);
    window.setTimeout(() => {
      setViewMode("workspace");
      setWorkspaceEntering(false);
    }, 360);
  }, [workspaceDataReady]);

  const refreshWorkspace = useCallback(() => {
    loadData();
    setRegisterRefreshTick((current) => current + 1);
  }, [loadData]);

  if (viewMode === "global") {
    return (
      <div className={`stage2-shell ${workspaceEntering ? "is-entering" : ""}`}>
        {scopeNotice && <div className="global-notice" role="status">{scopeNotice}</div>}
        {error && (
          <div className="global-warning" role="alert">
            <AlertTriangle size={17} />
            <span>{t("dataRefreshFailed", { error })}</span>
            <button className="icon-button" type="button" onClick={refreshWorkspace} disabled={loading}>
              {t("retryLoading")}
            </button>
          </div>
        )}
        {dataWarning && !error && (
          <div className="global-notice" role="status">
            <AlertTriangle size={17} />
            <span>{dataWarning}</span>
          </div>
        )}
        <GlobalView
          cellCount={displaySummary.grid_cells}
          carbonSummary={displaySummary.stage5_carbon}
          dataQuality={displaySummary.data_quality}
          layerCount={displaySummary.ogm_parcels}
          ndviCoverageRatio={displaySummary.ndvi_coverage_ratio}
          regions={portfolioRegions}
          resultCount={displaySummary.verification_results}
          loading={loading && !workspaceDataReady}
          dataReady={workspaceDataReady}
          loadError={Boolean(error)}
          summaryUpdatedAt={summary?.summary_updated_at}
          statusCounts={globalStatusCounts}
          scope={workspaceScope}
          scopes={workspaceScopes}
          onEnter={enterWorkspace}
          onScopeChange={selectWorkspaceScope}
          onSelectStatus={(status) => {
            setAssetPage(0);
            setAssetFilter("");
            setAssetCarbonFilter("all");
            setAssetSignalFilter("all");
            setAssetObservedFrom("");
            setAssetObservedTo("");
            setAssetStatusFilter(status);
            enterWorkspace();
          }}
        />
      </div>
    );
  }

  return (
    <div className={`app-shell theme-dark ${cleanMode ? "is-clean-mode" : "is-context-mode"} ${mapFocus ? "is-map-focus" : ""}`}>
      {scopeNotice && <div className="global-notice" role="status">{scopeNotice}</div>}
      <Topbar
        cellCount={displaySummary.grid_cells}
        cleanMode={cleanMode}
        mapFocus={mapFocus}
        loading={loading}
        onOpenGlobal={() => setViewMode("global")}
        onRefresh={refreshWorkspace}
        onScopeChange={selectWorkspaceScope}
        onToggleMapFocus={() => setMapFocus((current) => !current)}
        onToggleCleanMode={() => setCleanMode((current) => !current)}
        scope={workspaceScope}
        scopes={workspaceScopes}
      />

      {error && (
        <div className="global-warning" role="alert">
          <AlertTriangle size={17} />
          <span>{t("dataRefreshFailed", { error })}</span>
            <button className="icon-button" type="button" onClick={refreshWorkspace} disabled={loading}>
              {t("retryLoading")}
            </button>
        </div>
      )}

      {dataWarning && !error && (
        <div className="global-notice" role="status">
          <AlertTriangle size={17} />
          <span>{dataWarning}</span>
        </div>
      )}

      {visibleAssetIdsTruncated && (
        <div className="global-notice" role="status">
          <AlertTriangle size={17} />
          <span>{t("assetRegisterFilteredNotice")}</span>
        </div>
      )}
      {workspaceScope.isPreview && (
        <div className="global-notice" role="status">
          <AlertTriangle size={17} />
          <span>{t(scopeTextKey(workspaceScope.globalNote), { defaultValue: workspaceScope.globalNote })}</span>
        </div>
      )}
      {hasInvalidObservedRange && (
        <div className="global-warning" role="alert">
          <AlertTriangle size={17} />
          <span>{t("observedDateRangeInvalidLong")}</span>
        </div>
      )}

      <main className="workspace">
        <Sidebar
          auditEmptyText={auditEmptyText}
          displaySummary={displaySummary}
          loading={loading}
          observationStatusCounts={observationStatusCounts}
          onChooseStatusFilter={chooseStatusFilter}
          selectedAuditEvents={selectedAuditEvents}
          statusCounts={sidebarStatusCounts}
          summary={summary}
          scope={workspaceScope}
          verificationStatusFilter={assetStatusFilter}
        />

        <section className={`map-area ${mapFocus && !selectedAsset ? "is-focus-empty" : ""}`}>
          <div className="map-viewport">
            <MapPanel
              layerA={layerAFeatureCollection}
              regions={regionFeatureCollection}
              grid={visibleGridFeatureCollection}
              cleanMode={cleanMode}
              hasToken={Boolean(MAPBOX_TOKEN)}
              onPickLocation={pickAssetByLocation}
              onSelectCell={selectAssetCell}
              selectedCellUuid={selectedCellUuid}
              summary={displaySummary}
              visibleCellCount={visibleMapCellCount}
              loading={loading || (shouldApplyMapAssetFilter && registerLoading)}
              error={error}
              hasActiveFilters={shouldApplyMapAssetFilter}
              hasMatchingResults={
                registerLoading ||
                visibleAssetIdsLoading ||
                hasInvalidObservedRange ||
                registerResultCount > 0
              }
              layoutRevision={mapFocus ? "focus" : "standard"}
              onClearSelection={clearAssetSelection}
              selectedResult={selectedAsset}
              scope={workspaceScope}
              tileFilterQuery={assetTileFilterQuery}
              filterPage={currentPage}
            />
          </div>
          <aside className={`asset-drawer ${selectedAsset ? "has-selection" : "is-empty"}`} ref={assetPassportRef}>
            {assetLoadError && (
              <div className="asset-load-error" role="status">{assetLoadError}</div>
            )}
            {assetDetailLoading && !selectedAsset ? (
              <section className="asset-passport asset-passport-empty is-loading" aria-label={t("assetPassport")}>
                <span>{t("assetPassport")}</span>
                <strong>{t("loadingSelectedAsset")}</strong>
                <p>{t("assetIdentityLoading")}</p>
              </section>
            ) : selectedAsset ? (
              <AssetPassport
                ndviHistory={assetNdviHistory}
                ndviHistoryError={assetNdviHistoryError}
                ndviHistoryLoading={assetNdviHistoryLoading}
                ndviHistoryMonths={assetNdviHistoryMonths}
                onClearSelection={clearAssetSelection}
                onNdviHistoryMonthsChange={setAssetNdviHistoryMonths}
                row={selectedAsset}
              />
            ) : (
              <section className="asset-passport asset-passport-empty" aria-label={t("assetPassport")}>
                <span>{t("assetPassport")}</span>
                <strong>{t("selectAssetPassport")}</strong>
                <p>{t("assetPassportEmptyCopy")}</p>
              </section>
            )}
          </aside>
          <div className="bottom-panel" ref={assetRegisterRef}>
            <PanelHeader icon={MapPinned} title={t("assetRegister")} />
            <VerificationToolbar
              carbonStatus={assetCarbonFilter}
              filter={assetFilter}
              hasActiveFilters={hasActiveAssetFilters}
              observedFrom={assetObservedFrom}
              observedTo={assetObservedTo}
              page={currentPage}
              pageCount={pageCount}
              hasMore={registerHasMore}
              hasExactTotal={registerExactTotal}
              resultCount={registerResultCount}
              signalStatus={assetSignalFilter}
              visibleCount={visibleAssetRows.length}
              status={assetStatusFilter}
              onCarbonStatusChange={updateAssetCarbonFilter}
              onClearFilters={clearAssetFilters}
              onFilterChange={updateAssetFilter}
              onNextPage={() => changeAssetPage(currentPage + 1)}
              onObservedFromChange={updateAssetObservedFrom}
              onObservedToChange={updateAssetObservedTo}
              onPreviousPage={() => changeAssetPage(currentPage - 1)}
              onSignalStatusChange={updateAssetSignalFilter}
              onStatusChange={updateAssetStatusFilter}
            />
            <div className="verification-content">
              <div className="verification-list">
                <VerificationTable
                  hasFilters={hasActiveAssetFilters}
                  rows={visibleAssetRows}
                  selectedCellUuid={selectedCellUuid}
                  loading={loading || registerLoading}
                  loadFailed={Boolean(error || registerError)}
                  emptyText={undefined}
                  onSelect={selectAssetCell}
                />
              </div>
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}

function ageInDays(value) {
  const time = Date.parse(value || "");
  if (!Number.isFinite(time)) {
    return null;
  }
  return Math.max(0, Math.floor((Date.now() - time) / 86400000));
}

function fallbackNdviCoverage(observations, gridCellCount) {
  if (!gridCellCount) {
    return null;
  }
  const coveredCells = new Set();
  observations.forEach((row) => {
    if (row.cell_uuid && row.ndvi !== null && row.ndvi !== undefined) {
      coveredCells.add(row.cell_uuid);
    }
  });
  return coveredCells.size / gridCellCount;
}

function normalizeAssetRow(row) {
  return {
    ...row,
    area_coefficient: row.area_coefficient ?? null,
    rank: statusRank(row.anomaly_type),
  };
}

function scopeTextKey(value) {
  return `scope.${String(value || "").trim().replaceAll(/\s+/g, ".").toLowerCase()}`;
}

function matchesWorkspaceScope(summary, scope) {
  if (!summary) {
    return false;
  }
  if (!scope?.regionCode || !scope?.gridVersion) {
    return true;
  }
  return summary.region_code === scope.regionCode && summary.grid_version === scope.gridVersion;
}
