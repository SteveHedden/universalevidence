export type ApiHealth = {
  ok: boolean;
  status: number | "checking" | "network-error";
  title?: string;
};

export type TaxonomyClass = "state" | "condition" | "intervention" | "outcome" | "region";

export type TaxonomyTerm = {
  uri: string;
  label: string;
  definition?: string;
  broader?: string[];
  collection?: boolean;
};

export type Outcome =
  | string
  | {
      type?: string;
      measure?: string;
      description?: string;
      time_frame?: string;
      summary_statistic?: string;
      state_concept?: string;
      state_concept_uri?: string;
      [key: string]: unknown;
    };

export type ReportedFinding = {
  eligibility?: string;
  id: string;
  status: string;
  state_uri: string;
  measure: string;
  direction?: string;
  comparison: Record<string, unknown>;
  timing: Record<string, unknown>;
  subgroup?: string;
  value?: number | string | null;
  unit?: string;
  result_text?: string;
  uncertainty?: unknown;
  significance?: string;
  citations: { id: string; citation: string; url: string; locator: string; status: string }[];
};

export type SourceXmlProvenance = { record_url: string; xml_path: string; response_sha256: string };
export type OutcomeSpecification = {
  id: string; type: "pre-specified-measure"; role: "primary" | "secondary" | "unspecified";
  source_id: string | null; representation: "structured" | "legacy_prose";
  variable: string | null; method: string | null; timepoints: string | null; legacy_text: string | null;
  provenance: SourceXmlProvenance[];
  legacy_representations?: { text: string; source_id: string | null; provenance: SourceXmlProvenance[] }[];
};
export type IsrctnReportMetadata = {
  schema: "isrctn-document-metadata-1";
  results: { fields: Record<string, string | null>; provenance: SourceXmlProvenance }[];
  outputs: { id: string; attributes: Record<string, string>;
    external_links: { url: string | null; provenance: SourceXmlProvenance }[];
    local_files: { attributes: Record<string, string>; provenance: SourceXmlProvenance }[];
    descriptions: string[]; provenance: SourceXmlProvenance }[];
};

export type StudyResult = {
  state_mappings?: {uri:string; label:string; role:'condition'|'outcome'|'result'}[];
  group_state_roles?: string[];
  outcome_specifications?: OutcomeSpecification[];
  report_metadata?: IsrctnReportMetadata;
  result_availability?: { planned_specification_count: number; results_documents: "typed_metadata_present" | "publication_or_report_text_only" | "not_identified"; structured_numerical_results: "not_extracted"; reason: string };
  study_identity?: string;
  registry_records?: { source: string; record_id: string; url: string }[];
  reported_findings?: ReportedFinding[];
  finding_history?: ReportedFinding[];
  planned_outcomes?: Outcome[];
  source_reported_outcomes?: Outcome[];
  source_unspecified_outcomes?: Outcome[];
  match_provenance?: { basis: string }[];
  source?: string | null;
  study_id?: string | null;
  title?: string | null;
  url?: string | null;
  intervention?: string | null;
  intervention_concept?: string | null;
  intervention_concept_uri?: string | null;
  condition_concept?: string | null;
  condition_concept_uri?: string | null;
  country?: string | null;
  status?: string | null;
  year?: number | string | null;
  outcomes?: Outcome[];
};

export type QueryParams = Partial<Record<TaxonomyClass, string>> & { country?: string };

export type QueryV2Values = Partial<Record<TaxonomyClass, string[]>>;
export type QueryV2Logic = Partial<Record<TaxonomyClass, "and" | "or">>;

export type QueryV2SourceMeta = {
  status: "included" | "excluded" | "unavailable" | "error";
  coverage: "full" | "partial" | "country_only" | "none";
  returned_unique_studies: number;
  truncated: boolean;
  approximate: boolean;
  reason: "unsupported_admin_level" | "budget_exhausted" | "upstream_unavailable" | "upstream_error" | "state_enrichment_unavailable" | null;
};

export type QueryV2Meta = {
  enrichment?: { status: string; limit?: number; truncated?: boolean; reason?: string | null };
  enrichment_version?: string;
  source_counts_overlap?: boolean;
  execution_status?: "complete" | "partial" | "timeout";
  timed_out_sources?: string[];
  completed_sources?: string[];
  response_timeout_stage?: "serialization";
  api_version: "query-v2";
  returned_unique_studies: number;
  limit_per_source_branch: number;
  truncated: boolean;
  approximate: boolean;
  sources: Record<string, QueryV2SourceMeta>;
};

export type QueryV2Response = {
  reviewed_results?: {status: string; mode?: string; partial?: boolean; coverage?: {study:string;source:string;status:string;reason?:string}[]; graph: string; rows: import("../components/InterventionResults").ResultRow[]};
  results: StudyResult[];
  meta: QueryV2Meta;
};

export type SelectedAxes = Partial<Record<TaxonomyClass, TaxonomyTerm>>;

type ViteEnv = {
  VITE_API_BASE_URL?: string;
  [key: string]: unknown;
};

export function readApiBaseUrl(env: ViteEnv = import.meta.env): string {
  return (env.VITE_API_BASE_URL ?? "").trim().replace(/\/+$/, "");
}

export function getConfiguredApiBaseUrl(): string {
  return readApiBaseUrl();
}

export function resolveApiUrl(path: string, baseUrl = getConfiguredApiBaseUrl()): string {
  if (/^https?:\/\//i.test(path)) {
    return path;
  }

  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return baseUrl ? `${baseUrl}${normalizedPath}` : normalizedPath;
}

async function fetchOnce(path: string, init: RequestInit): Promise<Response> {
  return fetch(resolveApiUrl(path), {
    ...init,
    method: "GET",
    headers: {
      Accept: "application/json",
      ...init.headers,
    },
  });
}

export async function apiGet<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetchOnce(path, init);
  } catch {
    // A stale keep-alive connection (idle longer than the server's
    // keepalive_timeout) fails on first reuse with a raw network error
    // ("Failed to fetch"), not an HTTP status -- the browser doesn't
    // discover the connection is dead until it tries to use it. One
    // retry always succeeds on a fresh connection, so retry silently
    // before surfacing anything to the user.
    response = await fetchOnce(path, init);
  }

  if (!response.ok) {
    let detail = `GET ${path} failed with ${response.status}`;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) {
        detail = payload.detail;
      }
    } catch {
      // Keep the HTTP status fallback when the response is not JSON.
    }
    throw new Error(detail);
  }

  return response.json() as Promise<T>;
}

export function buildQueryPath(params: QueryParams): string {
  const searchParams = new URLSearchParams();
  (["state", "condition", "intervention", "outcome", "region"] as TaxonomyClass[]).forEach((axis) => {
    const value = params[axis]?.trim();
    if (value) {
      searchParams.set(axis, value);
    }
  });
  if (params.country?.trim()) {
    searchParams.set("country", params.country.trim());
  }
  const queryString = searchParams.toString();
  return queryString ? `/query?${queryString}` : "/query";
}

export function buildQueryV2Path(values: QueryV2Values, logic: QueryV2Logic = {}): string {
  const searchParams = new URLSearchParams();
  (["state", "condition", "intervention", "outcome", "region"] as TaxonomyClass[]).forEach((axis) => {
    const axisValues = values[axis] ?? [];
    axisValues.forEach((value) => {
      const cleaned = value.trim();
      if (cleaned) searchParams.append(axis, cleaned);
    });
    if (axisValues.length > 0) {
      searchParams.set(`${axis}_logic`, logic[axis] ?? "or");
    }
  });
  const queryString = searchParams.toString();
  return queryString ? `/query/v2?${queryString}` : "/query/v2";
}

export type TaxonomyNode = {
  uri: string;
  label: string;
  definition?: string;
  altLabels?: string[];
  children: TaxonomyNode[];
  collection?: boolean;
};

export function getTaxonomyTree(cls: TaxonomyClass): Promise<TaxonomyNode[]> {
  const taxonomyClass = cls === "state" ? "condition" : cls;
  return apiGet<TaxonomyNode[]>(`/taxonomy/${taxonomyClass}/tree`);
}

export function searchTaxonomy(cls: TaxonomyClass, q: string): Promise<TaxonomyTerm[]> {
  const taxonomyClass = cls === "state" ? "condition" : cls;
  return apiGet<TaxonomyTerm[]>(
    `/taxonomy/${taxonomyClass}?${new URLSearchParams({ q, limit: "10" }).toString()}`,
  );
}

export function queryStudies(params: QueryParams): Promise<StudyResult[]> {
  return apiGet<StudyResult[]>(buildQueryPath(params));
}

export function queryStudiesV2(
  values: QueryV2Values,
  logic: QueryV2Logic = {},
): Promise<QueryV2Response> {
  return apiGet<QueryV2Response>(buildQueryV2Path(values, logic));
}

export async function queryStudiesForState(params: QueryParams): Promise<StudyResult[]> {
  const stateUri = params.condition;
  if (!stateUri) {
    return queryStudies(params);
  }
  return queryStudies({ ...params, condition: undefined, state: stateUri });
}

export type Stats = {
  studies: number;
  reachable: number;
  sources: number;
  countries: number;
};

export function getStats(): Promise<Stats> {
  return apiGet<Stats>("/stats");
}

export async function getApiHealth(): Promise<ApiHealth> {
  try {
    const response = await fetch(resolveApiUrl("/openapi.json"), {
      headers: { Accept: "application/json" },
    });

    if (!response.ok) {
      return { ok: false, status: response.status };
    }

    const payload = (await response.json()) as { info?: { title?: string } };
    return {
      ok: true,
      status: response.status,
      title: payload.info?.title,
    };
  } catch {
    return { ok: false, status: "network-error" };
  }
}
