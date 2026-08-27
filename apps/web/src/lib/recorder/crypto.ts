/**
 * On-device audio encryption (P0-2: "audio is recorded and encrypted
 * on-device before any network activity is required").
 *
 * AES-GCM with a **non-extractable** CryptoKey held in IndexedDB. Two
 * properties matter and are easy to lose:
 *
 * 1. `extractable: false` means the raw key bytes cannot be read back out
 *    of the browser at all — not by this code, not by an XSS payload. The
 *    key object can be *used* to encrypt and decrypt, never exported. A key
 *    stored as a base64 string in IndexedDB would be trivially exfiltrated;
 *    this is the difference between "encrypted at rest" as a checkbox and
 *    as a property.
 * 2. GCM is authenticated, so a tampered chunk fails to decrypt rather than
 *    decrypting to garbage that then gets transcribed as if it were speech.
 *
 * **The honest limitation, stated because the checklist demands it be
 * stated:** a browser cannot seal this key in hardware the way iOS
 * Keychain or Android Keystore could. `extractable: false` is enforced by
 * the browser, not by a secure element — someone with the device, the OS
 * user session, and local code execution can still *use* the key. If Legal
 * requires hardware-sealed key custody for on-device PHI, this is the item
 * that forces decision 0024's Electron option (`safeStorage` → DPAPI /
 * Keychain). Recorded in the Phase 2.2 writeup as an open follow-up rather
 * than papered over.
 */

const DB_NAME = "remedy-scribe";
const DB_VERSION = 1;
const KEY_STORE = "keys";
const CHUNK_STORE = "chunks";
const KEY_ID = "audio-aes-gcm-v1";

/** GCM's recommended IV length. A fresh one per chunk, never reused. */
const IV_BYTES = 12;

export type EncryptedBlob = {
  /** GCM ciphertext including the auth tag. */
  ciphertext: ArrayBuffer;
  /**
   * Unique per chunk. Storing it alongside the ciphertext is standard and
   * not a secret — GCM requires the IV to decrypt and only requires that it
   * never repeat under the same key.
   *
   * Typed `Uint8Array<ArrayBuffer>` rather than plain `Uint8Array`: as of
   * TS 5.8 that type is generic over its backing buffer, and the default
   * `ArrayBufferLike` admits `SharedArrayBuffer`, which `BufferSource`
   * rejects. Pinning it here beats casting at every crypto call site.
   */
  iv: Uint8Array<ArrayBuffer>;
};

/* ------------------------------------------------------------------ *
 * IndexedDB plumbing. Shared with store.ts so both live in one
 * database and one version — two `open` calls with different
 * `onupgradeneeded` handlers on the same name is a classic way to get a
 * VersionError at runtime on a second tab.
 * ------------------------------------------------------------------ */
export function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(KEY_STORE)) {
        db.createObjectStore(KEY_STORE);
      }
      if (!db.objectStoreNames.contains(CHUNK_STORE)) {
        const chunks = db.createObjectStore(CHUNK_STORE, { keyPath: "id", autoIncrement: true });
        // Every read is "all chunks for this recording, in order".
        chunks.createIndex("bySession", ["sessionId", "seq"]);
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

export const CHUNK_STORE_NAME = CHUNK_STORE;
export const KEY_STORE_NAME = KEY_STORE;

/* ------------------------------------------------------------------ *
 * Key management
 * ------------------------------------------------------------------ */

async function readStoredKey(db: IDBDatabase): Promise<CryptoKey | null> {
  return new Promise((resolve, reject) => {
    const request = db.transaction(KEY_STORE, "readonly").objectStore(KEY_STORE).get(KEY_ID);
    request.onsuccess = () => resolve((request.result as CryptoKey | undefined) ?? null);
    request.onerror = () => reject(request.error);
  });
}

async function writeStoredKey(db: IDBDatabase, key: CryptoKey): Promise<void> {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(KEY_STORE, "readwrite");
    // A non-extractable CryptoKey is structured-cloneable, which is what
    // makes this work: IndexedDB stores the *handle*, and the raw bytes
    // never exist in JS. This is the whole mechanism.
    tx.objectStore(KEY_STORE).put(key, KEY_ID);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

/**
 * Get-or-create the device's audio key. Idempotent: a second call returns
 * the same key, so chunks written across separate recordings (or separate
 * page loads) all remain decryptable.
 */
export async function getAudioKey(db?: IDBDatabase): Promise<CryptoKey> {
  // Close only what we opened. Leaking a connection here is not cosmetic:
  // an open connection blocks `onupgradeneeded`, so a future DB_VERSION
  // bump would hang indefinitely for any user with the tab open — and it
  // accumulates one connection per recording over a clinic day. Found by a
  // test hook timing out on deleteDatabase, which is the same blocking
  // behaviour a real upgrade would hit.
  const owned = db === undefined;
  const database = db ?? (await openDb());
  try {
    const existing = await readStoredKey(database);
    if (existing) return existing;

    const key = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, false, [
      "encrypt",
      "decrypt",
    ]);
    await writeStoredKey(database, key);
    return key;
  } finally {
    if (owned) database.close();
  }
}

/**
 * Destroys the device key, rendering every stored chunk permanently
 * unreadable. This is the "crypto-shred" path for a consent withdrawal
 * (P0-1: audio queued for deletion without undue delay) — faster and more
 * complete than deleting rows, because it cannot leave a recoverable
 * fragment behind.
 *
 * Note the blast radius: the key is per-device, not per-encounter, so this
 * shreds *all* locally-queued audio. Correct for "wipe this laptop", wrong
 * as a per-encounter withdrawal primitive — for that, delete the
 * encounter's chunks (see store.ts) and let the server-side deletion path
 * handle anything already uploaded.
 */
export async function destroyAudioKey(db?: IDBDatabase): Promise<void> {
  const owned = db === undefined;
  const database = db ?? (await openDb());
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = database.transaction(KEY_STORE, "readwrite");
      tx.objectStore(KEY_STORE).delete(KEY_ID);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  } finally {
    if (owned) database.close();
  }
}

/* ------------------------------------------------------------------ *
 * Encrypt / decrypt
 * ------------------------------------------------------------------ */

export async function encryptChunk(key: CryptoKey, data: ArrayBuffer): Promise<EncryptedBlob> {
  const iv = new Uint8Array(new ArrayBuffer(IV_BYTES));
  crypto.getRandomValues(iv);
  const ciphertext = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, data);
  return { ciphertext, iv };
}

export async function decryptChunk(key: CryptoKey, blob: EncryptedBlob): Promise<ArrayBuffer> {
  // Throws on a tampered or truncated chunk rather than returning garbage —
  // that is GCM's authentication doing its job, and the caller should treat
  // a failure here as data loss, not as a decode hiccup to retry.
  return crypto.subtle.decrypt({ name: "AES-GCM", iv: blob.iv }, key, blob.ciphertext);
}
