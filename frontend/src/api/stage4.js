import { appendQuery, fetchJson } from "./stage1";
import { scopeSearchParams } from "../config/scopes";

export async function fetchStage4Status(scope) {
  return fetchJson(appendQuery("/stage4/status", scopeSearchParams(scope)));
}
