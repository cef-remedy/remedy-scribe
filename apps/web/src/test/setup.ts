/**
 * Test environment shims.
 *
 * `fake-indexeddb/auto` installs a real, spec-compliant IndexedDB
 * implementation — not a stub. That matters: the store's behaviour depends
 * on transaction semantics and on compound-index key ranges, and a hand-
 * written fake would let a broken query pass.
 */
import "fake-indexeddb/auto";
import { webcrypto } from "node:crypto";

if (!globalThis.crypto?.subtle) {
  Object.defineProperty(globalThis, "crypto", { value: webcrypto, configurable: true });
}
