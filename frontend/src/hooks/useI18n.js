import { useMemo } from "react";
import i18next from "i18next";

import { APP_AVAILABLE_LOCALES, APP_LOCALE } from "../config/runtime.js";
import {
  FALLBACK_LOCALE,
  availableLocales,
  createI18nResources,
  messages,
  parseLocaleList,
  resolveLocale,
} from "../i18n/messages.js";

const catalogLocales = availableLocales(messages);
const configuredLocales = parseLocaleList(APP_AVAILABLE_LOCALES).filter((locale) =>
  catalogLocales.includes(locale)
);
const activeLocales = configuredLocales.length ? configuredLocales : catalogLocales;
export const ACTIVE_LOCALE = resolveLocale(APP_LOCALE, activeLocales);

const i18n = i18next.createInstance();
i18n.init({
  fallbackLng: FALLBACK_LOCALE,
  initImmediate: false,
  interpolation: { escapeValue: false },
  lng: ACTIVE_LOCALE,
  resources: createI18nResources(messages),
  showSupportNotice: false,
  supportedLngs: catalogLocales,
});

export function translate(key, params = {}) {
  return i18n.getFixedT(ACTIVE_LOCALE)(key, params);
}

export function useI18n() {
  return useMemo(
    () => ({
      availableLocales: activeLocales,
      fallbackLocale: FALLBACK_LOCALE,
      locale: ACTIVE_LOCALE,
      t: translate,
    }),
    []
  );
}
