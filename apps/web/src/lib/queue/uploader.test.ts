/**
 * `uploadSession` — the function that moves a consultation off the laptop.
 *
 * It had **no test at all** until a bug written during the Google Drive work
 * (decision 0040) went out with `tsc` reporting clean, because a stale
 * `tsconfig.app.tsbuildinfo` let the typecheck skip the file. The reference
 * that broke — reading `init.data` on the line *above* `const init = …` — is
 * a temporal dead zone error, so it does not fail on Drive, or on a long
 * consultation, or under load. It fails on **every upload, on every backend,
 * always**, with `Cannot access 'init' before initialization`.
 *
 * `planParts` was tested. The state machine around it was tested. The 40
 * lines that actually talk to storage were covered only by an end-to-end
 * smoke test needing Postgres, MinIO, a Celery worker and a real Chromium —
 * which is a real test, and not one anybody runs while editing a header.
 *
 * So these drive the whole function with `api` and `fetch` stubbed. Cheap
 * enough to run on every save, which is the point.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

// `vi.hoisted` because `vi.mock` is hoisted above every `const` in the file,
// so a plain top-level object is in its own temporal dead zone by the time
// the factory runs — the same class of error these tests exist to catch,
// which is a reasonable advertisement for them.
const apiMock = vi.hoisted(() => ({
  GET: vi.fn(),
  POST: vi.fn(),
}));

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual<typeof import("../../api/client")>("../../api/client");
  return { api: apiMock, OfflineError: actual.OfflineError };
});

// The recorder's crypto and IndexedDB layers have their own tests (2.2). What
// matters here is the shape of what comes back, not how it was stored.
vi.mock("../recorder/store", () => ({
  readSessionChunks: vi.fn(),
}));
vi.mock("../recorder/crypto", () => ({
  getAudioKey: vi.fn(async () => "test-key"),
  decryptChunk: vi.fn(async (_key: unknown, c: { ciphertext: ArrayBuffer }) => c.ciphertext),
}));

import { OfflineError } from "../../api/client";
import { decryptChunk, getAudioKey } from "../recorder/crypto";
import { readSessionChunks, type StoredChunk } from "../recorder/store";
import { MIN_PART_SIZE_BYTES, PermanentUploadError, uploadSession } from "./uploader";

const ENCOUNTER = "enc-1";
const SESSION = "sess-1";

/** n chunks of `size` bytes, shaped like what `readSessionChunks` returns. */
function chunks(count: number, size: number): StoredChunk[] {
  return Array.from({ length: count }, (_, i) => ({
    sessionId: SESSION,
    seq: i,
    offsetMs: i * 5000, // the recorder's ~5s cadence
    byteLength: size,
    ciphertext: new ArrayBuffer(size),
    iv: new Uint8Array(12) as Uint8Array<ArrayBuffer>,
    mimeType: "audio/webm;codecs=opus",
  }));
}

type FetchCall = { url: string; init: RequestInit };
let fetchCalls: FetchCall[] = [];

/** Every PUT answers with `status`; 308 is Drive's "stored, send the next". */
function stubFetch(status = 200): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      fetchCalls.push({ url, init });
      return { ok: status >= 200 && status < 300, status } as Response;
    }),
  );
}

/** The happy path for the three `api` calls, with a configurable part size. */
function stubApi(minPartSize: number = MIN_PART_SIZE_BYTES): void {
  apiMock.POST.mockImplementation(async (path: string) => {
    if (path.endsWith("/upload/init")) {
      return {
        data: { object_key: "audio/enc-1.webm", min_part_size_bytes: minPartSize },
        error: undefined,
        response: { status: 200 },
      };
    }
    if (path.includes("/upload/parts/")) {
      return { data: { url: "https://storage.example/put" }, error: undefined, response: { status: 200 } };
    }
    return { data: { object_key: "audio/enc-1.webm" }, error: undefined, response: { status: 200 } };
  });
  apiMock.GET.mockResolvedValue({ data: { parts: [] }, error: undefined, response: { status: 200 } });
}

beforeEach(() => {
  vi.clearAllMocks();
  fetchCalls = [];
  vi.mocked(getAudioKey).mockResolvedValue("test-key" as unknown as CryptoKey);
  vi.mocked(decryptChunk).mockImplementation(
    async (_k: unknown, c: { ciphertext: ArrayBuffer }) => c.ciphertext,
  );
});

describe("uploadSession", () => {
  it("completes a short consult — the regression guard for the TDZ bug", async () => {
    // Nothing clever. If any reference in the function body runs before its
    // declaration, this throws a ReferenceError and the test fails, which is
    // exactly the coverage that was missing.
    vi.mocked(readSessionChunks).mockResolvedValue(chunks(4, 20 * 1024));
    stubApi();
    stubFetch(200);

    const result = await uploadSession(SESSION, ENCOUNTER);

    expect(result.objectKey).toBe("audio/enc-1.webm");
    expect(result.bytesUploaded).toBe(4 * 20 * 1024);
    expect(fetchCalls).toHaveLength(1); // 80 KB is one part under a 5 MiB floor
  });

  it("takes the part size from the server rather than assuming S3's floor", async () => {
    // Drive's floor is 256 KiB, so the same audio that is one part on S3
    // becomes several. Hard-coding MIN_PART_SIZE_BYTES here would silently
    // produce one oversized part that Drive rejects (decision 0040).
    vi.mocked(readSessionChunks).mockResolvedValue(chunks(8, 128 * 1024)); // 1 MiB
    stubApi(256 * 1024);
    stubFetch(308);

    await uploadSession(SESSION, ENCOUNTER);

    expect(fetchCalls).toHaveLength(4); // 1 MiB / 256 KiB
  });

  it("accepts 308 Resume Incomplete as success", async () => {
    // `response.ok` is false for 308. Treating ok-ness as success failed
    // every multi-part Drive upload at the first chunk, with an error that
    // reads like a server fault and is in fact success.
    vi.mocked(readSessionChunks).mockResolvedValue(chunks(8, 128 * 1024));
    stubApi(256 * 1024);
    stubFetch(308);

    await expect(uploadSession(SESSION, ENCOUNTER)).resolves.toMatchObject({
      bytesUploaded: 8 * 128 * 1024,
    });
  });

  it("sends contiguous Content-Range headers covering the whole file exactly", async () => {
    // A gap or an overlap here corrupts the recording on a resumable backend,
    // and S3 ignores the header entirely — so nothing on the S3 path would
    // ever reveal it.
    vi.mocked(readSessionChunks).mockResolvedValue(chunks(8, 128 * 1024));
    stubApi(256 * 1024);
    stubFetch(308);

    await uploadSession(SESSION, ENCOUNTER);

    const total = 8 * 128 * 1024;
    const ranges = fetchCalls.map((c) => (c.init.headers as Record<string, string>)["Content-Range"]);
    expect(ranges).toEqual([
      `bytes 0-262143/${total}`,
      `bytes 262144-524287/${total}`,
      `bytes 524288-786431/${total}`,
      `bytes 786432-1048575/${total}`,
    ]);
  });

  it("skips parts the server already holds, and still reports them as uploaded", async () => {
    vi.mocked(readSessionChunks).mockResolvedValue(chunks(8, 128 * 1024));
    stubApi(256 * 1024);
    stubFetch(308);
    apiMock.GET.mockResolvedValue({
      data: { parts: [{ part_number: 1 }, { part_number: 2 }] },
      error: undefined,
      response: { status: 200 },
    });

    const result = await uploadSession(SESSION, ENCOUNTER);

    expect(fetchCalls).toHaveLength(2); // parts 3 and 4 only
    expect(result.bytesUploaded).toBe(8 * 128 * 1024); // but the total is whole
  });

  it("does not send credentials to storage", async () => {
    // The presigned URL *is* the authorisation. An Authorization header or a
    // cookie alongside it is rejected by S3, and would put a session token
    // into a third party's logs.
    vi.mocked(readSessionChunks).mockResolvedValue(chunks(4, 20 * 1024));
    stubApi();
    stubFetch(200);

    await uploadSession(SESSION, ENCOUNTER);

    const headers = fetchCalls[0].init.headers as Record<string, string>;
    expect(Object.keys(headers).map((k) => k.toLowerCase())).not.toContain("authorization");
    expect(fetchCalls[0].init.credentials).toBeUndefined();
  });

  it("raises OfflineError when the PUT cannot leave the machine", async () => {
    // Must be distinguishable from a server rejection: the queue's backoff
    // deliberately does not spend the attempt budget while offline.
    vi.mocked(readSessionChunks).mockResolvedValue(chunks(4, 20 * 1024));
    stubApi();
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));

    await expect(uploadSession(SESSION, ENCOUNTER)).rejects.toBeInstanceOf(OfflineError);
  });

  it("treats a 409 on complete as permanent — consent may have been withdrawn", async () => {
    vi.mocked(readSessionChunks).mockResolvedValue(chunks(4, 20 * 1024));
    stubApi();
    stubFetch(200);
    apiMock.POST.mockImplementation(async (path: string) => {
      if (path.endsWith("/upload/init")) {
        return {
          data: { object_key: "audio/enc-1.webm", min_part_size_bytes: MIN_PART_SIZE_BYTES },
          error: undefined,
          response: { status: 200 },
        };
      }
      if (path.includes("/upload/parts/")) {
        return { data: { url: "https://storage.example/put" }, error: undefined, response: { status: 200 } };
      }
      return { data: undefined, error: {}, response: { status: 409 } };
    });

    await expect(uploadSession(SESSION, ENCOUNTER)).rejects.toBeInstanceOf(PermanentUploadError);
  });

  it("refuses to upload a session with no local audio", async () => {
    // Permanent by design: retrying cannot conjure audio, and the likely
    // cause is a withdrawal having already shredded it.
    vi.mocked(readSessionChunks).mockResolvedValue([]);
    stubApi();
    stubFetch(200);

    await expect(uploadSession(SESSION, ENCOUNTER)).rejects.toBeInstanceOf(PermanentUploadError);
    expect(apiMock.POST).not.toHaveBeenCalled();
  });
});
