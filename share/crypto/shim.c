/* Crush share - crypto shim over Monocypher 4.0.2 (CC0 / BSD-2).
 * Only what decryption needs: Argon2id and XChaCha20-Poly1305 open.
 * Built freestanding for wasm32; JS owns the memory layout. */
#include <stdint.h>
#include <stddef.h>
#include "monocypher.h"

#define EXPORT(name) __attribute__((export_name(name)))

extern unsigned char __heap_base;

EXPORT("heap_base") uint32_t heap_base(void) { return (uint32_t)(uintptr_t)&__heap_base; }

/* Argon2id, version 0x13. work must hold m_kib * 1024 bytes, 8-byte aligned. */
EXPORT("argon2id")
void argon2id(uint8_t *out, uint32_t out_len, const uint8_t *pass, uint32_t pass_len,
              const uint8_t *salt, uint32_t salt_len, uint32_t t, uint32_t m_kib,
              uint32_t p, void *work)
{
    crypto_argon2_config cfg = { CRYPTO_ARGON2_ID, m_kib, t, p };
    crypto_argon2_inputs in = { pass, salt, pass_len, salt_len };
    crypto_argon2(out, out_len, work, cfg, in, crypto_argon2_no_extras);
}

/* XChaCha20-Poly1305 (IETF construction, as libsodium and Crush use it).
 * ct is ciphertext || 16-byte tag. Returns 0 if authentic, -1 otherwise;
 * nothing is written to out unless the tag verifies. */
EXPORT("xchacha_open")
int xchacha_open(uint8_t *out, const uint8_t key[32], const uint8_t nonce[24],
                 const uint8_t *ad, uint32_t ad_len, const uint8_t *ct, uint32_t ct_len)
{
    if (ct_len < 16) return -1;
    uint32_t n = ct_len - 16;
    return crypto_aead_unlock(out, ct + n, key, nonce, ad, ad_len, ct, n);
}

EXPORT("wipe") void wipe(void *p, uint32_t n) { crypto_wipe(p, n); }
