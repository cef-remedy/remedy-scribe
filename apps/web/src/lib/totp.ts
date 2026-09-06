/**
 * A local, dev-only TOTP generator — RFC 6238, HMAC-SHA1, 30-second step,
 * 6 digits. Exists for exactly one reason: demoing this app without an
 * authenticator app in hand, without weakening the real backend
 * requirement it stands in for.
 *
 * ⚠️ This is not a security bypass. `POST /auth/login` still verifies a
 * real TOTP code server-side, exactly as it does for a real clinician —
 * this only computes that code locally instead of asking a phone for it,
 * and only when `import.meta.env.DEV` is true (never in a production
 * `npm run build`, so a real Netlify deploy always shows the real field)
 * and only when the operator has explicitly put the demo account's own
 * secret in their local, gitignored `apps/web/.env`.
 */

function base32Decode(secret: string): Uint8Array {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  const clean = secret.toUpperCase().replace(/[^A-Z2-7]/g, "");
  const bytes: number[] = [];
  let bits = 0;
  let value = 0;
  for (const char of clean) {
    value = (value << 5) | alphabet.indexOf(char);
    bits += 5;
    if (bits >= 8) {
      bits -= 8;
      bytes.push((value >>> bits) & 0xff);
    }
  }
  return new Uint8Array(bytes);
}

export async function currentTotpCode(secret: string, step = 30): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    base32Decode(secret).slice().buffer as ArrayBuffer,
    { name: "HMAC", hash: "SHA-1" },
    false,
    ["sign"],
  );

  const counter = Math.floor(Date.now() / 1000 / step);
  const counterBytes = new DataView(new ArrayBuffer(8));
  counterBytes.setUint32(4, counter, false); // low 32 bits; high 32 stay zero until the year 2106

  const mac = new Uint8Array(await crypto.subtle.sign("HMAC", key, counterBytes.buffer));
  const offset = mac[mac.length - 1] & 0x0f;
  const binary =
    ((mac[offset] & 0x7f) << 24) |
    ((mac[offset + 1] & 0xff) << 16) |
    ((mac[offset + 2] & 0xff) << 8) |
    (mac[offset + 3] & 0xff);

  return String(binary % 1_000_000).padStart(6, "0");
}
