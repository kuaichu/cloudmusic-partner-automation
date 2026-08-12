import base64
import unittest

from Crypto.Cipher import AES

import netease_utils as utils


class EncryptionUtilityTests(unittest.TestCase):
    def test_aes_encrypt_round_trips_with_pkcs7_padding(self):
        plaintext = '{"hello":"world"}'
        encrypted = utils.aes_encrypt(plaintext, utils.NONCE, utils.IV)

        cipher = AES.new(utils.to_16(utils.NONCE), AES.MODE_CBC, utils.to_16(utils.IV))
        padded = cipher.decrypt(base64.b64decode(encrypted))
        padding = padded[-1]

        self.assertEqual(padded[:-padding].decode(), plaintext)

    def test_rsa_encrypt_has_fixed_hex_output(self):
        encrypted = utils.rsa_encrypt("0123456789abcdef")

        self.assertEqual(len(encrypted), 256)
        self.assertTrue(all(character in "0123456789abcdef" for character in encrypted))

    def test_random_key_and_redaction_are_safe(self):
        key = utils.random_key()
        redacted = utils.redact_sensitive(
            "POST https://example.invalid/?csrf_token=secret Cookie: MUSIC_U=private"
        )

        self.assertEqual(len(key), 16)
        self.assertTrue(key.isalnum())
        self.assertNotIn("secret", redacted)
        self.assertNotIn("private", redacted)
        self.assertNotIn("example.invalid", redacted)


if __name__ == "__main__":
    unittest.main()
