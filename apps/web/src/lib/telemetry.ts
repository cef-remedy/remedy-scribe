/**
 * Browser-side correlation and error reporting (Phase 5.2, P0-8).
 *
 * Two jobs, and the PHI rule is the same one the API enforces
 * (`apps/api/app/core/observability.py`): **nothing free-form leaves this
 * machine unless its shape says it cannot be clinical text.**
 *
 * ## 1. Correlation
 *
 * The API mints a correlation ID per request and echoes it in
 * `x-correlation-id`; if the browser sends one, the API adopts it. That is
 * what lets a doctor say "it failed at 10:40" and have one identifier tie
 * the click, the HTTP request, the Celery transcription and the note
 * generation minutes later into a single trace.
 *
 * The header is attached by **wrapping `window.fetch`**, not by editing the
 * API client. That is deliberate, and it is the same argument the backend
 * makes for putting its scrubber in the log-record factory rather than in a
 * handler: a header added at one call site is a convention every future call
 * site can forget, while a header added at the transport is a property of
 * the program. `src/api/client.ts` already goes through `fetch`, so does
 * `uploader.ts`'s presigned-part `PUT`, and so will whatever is written
 * next.
 *
 * Only same-API requests are stamped. A presigned S3 `PUT` goes to object
 * storage, which is a different origin — adding an unexpected header there
 * breaks the signature check, and it would leak our internal trace ID to a
 * host that has no use for it.
 *
 * ## 2. Error reporting
 *
 * There is no `@sentry/react` here. Adding it is a dependency decision that
 * belongs with whoever owns `package.json`, and the SDK's own defaults are
 * the thing Phase 5.2 spends most of its effort switching **off** on the
 * server (request bodies, breadcrumbs, and above all stack-frame locals — a
 * frame holding a note section is a frame holding clinical prose). So this
 * module speaks Sentry's envelope protocol directly, sends a deliberately
 * thin event, and is inert unless `VITE_SENTRY_DSN` is configured.
 *
 * What is sent: error class, a scrubbed message, the *frame lines* of the
 * stack, the correlation ID, and the route path. What is never sent:
 * component props, form state, `localStorage`, IndexedDB (which holds the
 * recorded audio and the queue), request or response bodies, breadcrumbs,
 * DOM snapshots, or the first line of a stack trace (that line is
 * `Name: message`, and the message is the part that can carry text).
 */

/** Reads as "not configured" everywhere except a deploy that sets it. */
const DSN: string = (import.meta.env.VITE_SENTRY_DSN as string | undefined) ?? "";
const API_BASE: string = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";
const ENVIRONMENT: string = (import.meta.env.MODE as string | undefined) ?? "development";
const RELEASE: string | undefined = import.meta.env.VITE_RELEASE as string | undefined;

export const CORRELATION_HEADER = "x-correlation-id";

/**
 * Mirrors the server's cap (`LOG_MAX_FREE_TEXT_CHARS`). Over-length free
 * text is **replaced**, not truncated — the first 200 characters of a
 * generated Assessment is still a generated Assessment.
 */
const MAX_FREE_TEXT_CHARS = 200;

/** A runaway render loop must not turn an error into a network flood. */
const MAX_REPORTS_PER_SESSION = 20;

let reportsSent = 0;
let installed = false;

/* ------------------------------------------------------------------ *
 * Correlation
 * ------------------------------------------------------------------ */

function randomHex(bytes: number): string {
  const buffer = new Uint8Array(bytes);
  crypto.getRandomValues(buffer);
  return Array.from(buffer, (b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * One ID per browser tab session, refreshed per request below. The prefix
 * says where it came from, matching the server's `req-` / `sweep-` /
 * `monitor-` convention, so a log line's origin is readable without a
 * lookup.
 */
let sessionCorrelationId = `web-${randomHex(8)}`;

/** The ID the server last confirmed. This is the one worth showing a user. */
let lastServerCorrelationId: string | null = null;

export function currentCorrelationId(): string {
  return lastServerCorrelationId ?? sessionCorrelationId;
}

/**
 * A fresh ID for a new unit of work — call it when starting a recording or
 * an upload so one consultation's requests group together rather than a
 * whole tab session's.
 */
export function newTrace(): string {
  sessionCorrelationId = `web-${randomHex(8)}`;
  lastServerCorrelationId = null;
  return sessionCorrelationId;
}

function isApiRequest(url: string): boolean {
  try {
    return new URL(url, window.location.href).origin === new URL(API_BASE, window.location.href).origin;
  } catch {
    return false;
  }
}

function installFetchCorrelation(): void {
  const original = window.fetch.bind(window);

  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (!isApiRequest(url)) return original(input, init);

    // A Request is immutable, so the header goes on a clone. Building it
    // this way rather than mutating `init.headers` also covers the callers
    // that pass a fully-formed Request (openapi-fetch does).
    const request = new Request(input as RequestInfo, init);
    if (!request.headers.has(CORRELATION_HEADER)) {
      request.headers.set(CORRELATION_HEADER, sessionCorrelationId);
    }

    const response = await original(request);
    const echoed = response.headers.get(CORRELATION_HEADER);
    if (echoed) lastServerCorrelationId = echoed;
    return response;
  };
}

/* ------------------------------------------------------------------ *
 * Scrubbing
 * ------------------------------------------------------------------ */

/**
 * FNV-1a. Explicitly **not** a cryptographic hash and not used as one: its
 * only job is to let two occurrences of the same suppressed message be
 * recognised as the same message during an incident. `crypto.subtle` is
 * async and cannot be called from a synchronous error handler.
 */
function fingerprint(text: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
}

const REDACTIONS: Array<[RegExp, string]> = [
  [/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g, "<redacted-email>"],
  [/(?<![\w:-])\d{4}-\d{2}-\d{2}(?![\w:-])/g, "<redacted-date>"],
  [/(?<![\w.-])\+?\d[\d ]{6,}\d(?![\w.-])/g, "<redacted-digits>"],
];

export function redactText(text: string): string {
  let cleaned = text.replace(/[\r\n\t]+/g, " ");
  for (const [pattern, replacement] of REDACTIONS) cleaned = cleaned.replace(pattern, replacement);
  if (cleaned.length > MAX_FREE_TEXT_CHARS) {
    return `<redacted ${cleaned.length} chars fnv:${fingerprint(cleaned)}>`;
  }
  return cleaned;
}

/**
 * Frame locations only.
 *
 * `error.stack` begins with `Name: message` — the one line of a stack trace
 * that can hold arbitrary text, and therefore the one line that can hold a
 * patient's name or a note section. Every line after it is a function name
 * and a bundle URL, which is exactly the part worth reporting.
 */
function stackFrames(stack: string | undefined): string {
  if (!stack) return "";
  return stack
    .split("\n")
    .slice(1)
    .map((line) => line.trim())
    .filter((line) => line.startsWith("at "))
    .slice(0, 20)
    .join("\n");
}

/* ------------------------------------------------------------------ *
 * Transport
 * ------------------------------------------------------------------ */

type ParsedDsn = { url: string; publicKey: string };

function parseDsn(dsn: string): ParsedDsn | null {
  try {
    const parsed = new URL(dsn);
    const projectId = parsed.pathname.replace(/^\//, "");
    if (!parsed.username || !projectId) return null;
    return {
      url: `${parsed.protocol}//${parsed.host}/api/${projectId}/envelope/?sentry_key=${parsed.username}&sentry_version=7`,
      publicKey: parsed.username,
    };
  } catch {
    return null;
  }
}

export type ErrorReport = {
  type: string;
  message: string;
  frames: string;
  path: string;
};

function buildReport(error: unknown, fallbackType: string): ErrorReport {
  const asError = error instanceof Error ? error : undefined;
  return {
    type: asError?.name ?? fallbackType,
    message: redactText(asError?.message ?? String(error ?? "")),
    frames: stackFrames(asError?.stack),
    // The path, never the query string or the hash — a URL is the one part
    // of a page a client controls freely, and `?q=Maria+Santos` is a patient
    // name. The API's request logger drops the query string for the same
    // reason.
    path: window.location.pathname,
  };
}

function send(report: ErrorReport): void {
  if (reportsSent >= MAX_REPORTS_PER_SESSION) return;
  reportsSent += 1;

  // Always visible locally. With no DSN configured — the normal state in
  // development and, until someone provisions a Sentry project, in the
  // pilot too — this is the whole of error tracking on the client, and the
  // runbook says so rather than implying a pipeline exists.
  // eslint-disable-next-line no-console
  console.error("[telemetry]", { ...report, correlation_id: currentCorrelationId() });

  const dsn = parseDsn(DSN);
  if (!dsn) return;

  const eventId = randomHex(16);
  const payload = {
    event_id: eventId,
    timestamp: Date.now() / 1000,
    platform: "javascript",
    level: "error",
    environment: ENVIRONMENT,
    release: RELEASE,
    tags: { correlation_id: currentCorrelationId(), path: report.path },
    exception: { values: [{ type: report.type, value: report.message }] },
    // The frames go in `extra` rather than a structured `stacktrace`
    // because a structured stacktrace is the field Sentry's UI will happily
    // enrich with source context and variables. There is nothing to enrich
    // here, and that is on purpose.
    extra: { frames: report.frames },
  };

  const envelope =
    `${JSON.stringify({ event_id: eventId, sent_at: new Date().toISOString() })}\n` +
    `${JSON.stringify({ type: "event" })}\n` +
    `${JSON.stringify(payload)}\n`;

  // `keepalive` so a report raised during an unload still leaves. Failures
  // are swallowed: telemetry must never surface an error of its own to a
  // doctor mid-consultation, and it must never recurse into this handler.
  void fetch(dsn.url, {
    method: "POST",
    body: envelope,
    headers: { "content-type": "application/x-sentry-envelope" },
    keepalive: true,
    mode: "cors",
  }).catch(() => {});
}

/** Report an error caught by application code (e.g. an ErrorBoundary). */
export function reportError(error: unknown, fallbackType = "Error"): void {
  try {
    send(buildReport(error, fallbackType));
  } catch {
    /* telemetry never throws into the app */
  }
}

/**
 * Install the fetch correlation wrapper and the two global error channels.
 * Idempotent, because React 19 StrictMode mounts twice in development.
 */
export function installTelemetry(): void {
  if (installed) return;
  installed = true;

  installFetchCorrelation();

  window.addEventListener("error", (event) => {
    reportError(event.error ?? event.message, "WindowError");
  });

  // The channel that catches a failed `await` nobody handled — which in
  // this app is most of the interesting failures, since the API client,
  // the recorder and the upload queue are all promise-based.
  window.addEventListener("unhandledrejection", (event) => {
    reportError(event.reason, "UnhandledRejection");
  });
}
