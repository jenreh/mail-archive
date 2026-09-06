/* Base64 to bytes — the one place the archive spells that in a browser.
 *
 * Every viewer in this folder is handed its file as a base64 string on a
 * React prop, because the archive's posture is that the browser never fetches
 * anything: the bytes travel on the socket that is already open. See
 * `mailarc_ui/message_detail/preview.py` for the whole argument.
 */

import { useMemo } from "react";

/* `atob` yields one character per byte, so each character's code *is* the
 * byte. A TextEncoder here would re-encode as UTF-8 and corrupt every byte
 * above 0x7f — which, in a zipped Office file, is most of them. */
export function decodeBase64(encoded) {
  const binary = atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

/* Keyed on the payload itself rather than on a render. Reflex sends a fresh
 * string with every state delta the page happens to make, and decoding eight
 * megabytes on each one is the only expensive thing these components do. */
export function useDecoded(encoded) {
  return useMemo(() => (encoded ? decodeBase64(encoded) : null), [encoded]);
}

/* Some renderers want an ArrayBuffer rather than a view of one. */
export function useBuffer(encoded) {
  const bytes = useDecoded(encoded);
  return useMemo(() => (bytes ? bytes.buffer : null), [bytes]);
}
