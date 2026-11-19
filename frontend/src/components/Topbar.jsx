import { Globe2, Layers, Maximize2, Minimize2, RefreshCw } from "lucide-react";

import { formatInteger } from "../lib/formatters";
import { useI18n } from "../hooks/useI18n";

export function Topbar({
  cellCount,
  cleanMode,
  mapFocus,
  loading,
  scope,
  scopes = [],
  onOpenGlobal,
  onRefresh,
  onScopeChange,
  onToggleMapFocus,
  onToggleCleanMode,
}) {
  const { t } = useI18n();
  const mapModeLabel = cleanMode ? t("contextMode") : t("cleanMode");
  const focusLabel = mapFocus ? t("exitFocus") : t("focusMap");

  return (
    <header className="topbar">
      <div>
        <p className="eyebrow">{t("privateSystem")}</p>
        <h1>Map 1 Turkey</h1>
        <div className="scope-strip" aria-label={t("projectCoverageScope")}>
          {(scope?.topbarChips || [t("boundedStage1Area"), t("forestOnlyScope")]).map((chip) => (
            <span key={chip}>{t(scopeTextKey(chip), { defaultValue: chip })}</span>
          ))}
          <span>{formatInteger(cellCount)} {t(scopeTextKey(scope?.cellNoun || "cells"), { defaultValue: scope?.cellNoun || "cells" })}</span>
        </div>
      </div>
      <div className="topbar-actions">
        <div className="scope-switcher" aria-label={t("workspaceScope")}>
          {scopes.map((item) => (
            <button
              aria-pressed={scope?.key === item.key}
              className={`scope-option ${scope?.key === item.key ? "is-active" : ""}`}
              key={item.key}
              disabled={item.available === false}
              title={item.available === false ? t("pilotScopeUnavailable") : undefined}
              onClick={() => onScopeChange?.(item.key)}
              type="button"
            >
              {t(scopeTextKey(item.shortLabel), { defaultValue: item.shortLabel })}
            </button>
          ))}
        </div>
        <button className="icon-button" type="button" onClick={onOpenGlobal}>
          <Globe2 size={18} />
          <span>{t("global")}</span>
        </button>
        <button
          aria-label={mapModeLabel}
          aria-pressed={cleanMode}
          className={`icon-button ${cleanMode ? "is-active" : ""}`}
          type="button"
          onClick={onToggleCleanMode}
        >
          <Layers size={18} />
          <span>{mapModeLabel}</span>
        </button>
        <button
          aria-label={focusLabel}
          aria-pressed={mapFocus}
          className={`icon-button ${mapFocus ? "is-active" : ""}`}
          type="button"
          onClick={onToggleMapFocus}
          disabled={!cleanMode}
        >
          {mapFocus ? <Minimize2 size={18} /> : <Maximize2 size={18} />}
          <span>{focusLabel}</span>
        </button>
        <button className="icon-button" type="button" onClick={onRefresh} disabled={loading}>
          <RefreshCw size={18} />
          <span>{loading ? t("refreshing") : t("refresh")}</span>
        </button>
      </div>
    </header>
  );
}

function scopeTextKey(value) {
  return `scope.${String(value || "").trim().replaceAll(/\s+/g, ".").toLowerCase()}`;
}
