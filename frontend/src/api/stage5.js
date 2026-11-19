import { appendQuery, fetchJson } from "./stage1";
import { scopeSearchParams } from "../config/scopes";

export async function fetchStage5Status(scope) {
  return fetchJson(appendQuery("/stage5/status", scopeSearchParams(scope)));
}
