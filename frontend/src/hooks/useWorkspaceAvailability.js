import { useEffect, useMemo, useState } from "react";

import { fetchJson, scopedPath } from "../api/stage1";
import { FULL_PUBLIC_SCOPE_KEY } from "../config/scopes";

export function useWorkspaceAvailability(scopes) {
  const [availability, setAvailability] = useState({});

  useEffect(() => {
    let cancelled = false;
    for (const scope of scopes) {
      if (scope.key === FULL_PUBLIC_SCOPE_KEY) continue;
      fetchJson(scopedPath("/stage1/regions?limit=1", scope), { timeoutMs: 8000 })
        .then((regions) => {
          if (!cancelled && Array.isArray(regions)) {
            setAvailability((current) => ({
              ...current,
              [scope.key]: regions.some((region) => region.code === scope.regionCode),
            }));
          }
        })
        .catch(() => {});
    }
    return () => { cancelled = true; };
  }, [scopes]);

  return useMemo(() => scopes.map((scope) => ({
    ...scope, available: availability[scope.key],
  })), [availability, scopes]);
}
