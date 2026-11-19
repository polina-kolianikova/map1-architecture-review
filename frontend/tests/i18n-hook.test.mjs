import assert from "node:assert/strict";

import {
  availableLocales,
  createI18nResources,
  mergeLanguagePack,
  messages,
  parseLocaleList,
  resolveLocale,
} from "../src/i18n/messages.js";

assert.deepEqual(availableLocales(messages), ["en"]);
assert.equal(resolveLocale("en-US", ["en"]), "en");
assert.equal(resolveLocale("ru", ["en"]), "en");
assert.deepEqual(parseLocaleList("en, ru, en-US, bad<script>, en-toolongtagpart"), ["en", "ru"]);

const extended = mergeLanguagePack(messages, "ru", {
  assetPassport: "Asset passport",
  global: "Overview",
});
assert.deepEqual(availableLocales(extended), ["en", "ru"]);
assert.equal(resolveLocale("ru-RU", availableLocales(extended)), "ru");
assert.equal(createI18nResources(extended).ru.translation.global, "Overview");

global.window = {
  __MAP1_CONFIG__: {
    availableLocales: ["en"],
    locale: "en-US",
  },
};
const hook = await import(`../src/hooks/useI18n.js?case=${Date.now()}`);
assert.equal(hook.ACTIVE_LOCALE, "en");
assert.equal(hook.translate("assetPassport"), "Asset passport");
assert.equal(hook.translate("score", { score: "91" }), "Score 91");

for (const [count, expected] of [[0, "0 events"], [1, "1 event"], [2, "2 events"], [1000, "1,000 events"]]) {
  assert.equal(hook.translate("carbonAuditEvents", {
    count,
    formattedCount: count.toLocaleString("en-US"),
  }), expected);
}
for (const [key, value] of Object.entries(messages.en)) {
  assert.doesNotMatch(value, /\b(draft|preview)\b/i, `English display text: ${key}`);
}
