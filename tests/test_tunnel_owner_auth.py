from __future__ import annotations
import unittest
from haru_life_events.tunnel_owner_auth import SingleOwnerTunnelAuth

class SingleOwnerTests(unittest.TestCase):
    def test_exact_bearer_maps_to_configured_owner(self):
        gate=SingleOwnerTunnelAuth("owner-cat", b"k"*40)
        self.assertEqual(gate({"authorization":"Bearer "+"k"*40}),"owner-cat")
        self.assertIsNone(gate({"authorization":"Bearer "+"q"*40}))
        self.assertIsNone(gate({"authorization":"Bearer "+"k"*39}))
        self.assertIsNone(gate({"authorization":"Basic "+"k"*40}))
        self.assertIsNone(gate({}))
        self.assertIsNone(gate({"x-user-id":"owner-cat"}))
        self.assertIsNone(gate({"authorization":"Bearer "+"k"*40+" "}))
        self.assertIsNone(gate({"authorization":"Bearer "+"k"*40+"\n"}))
    def test_header_owner_spoof_not_accepted(self):
        gate=SingleOwnerTunnelAuth("owner-cat",b"k"*40)
        self.assertEqual(gate({"authorization":"Bearer "+"k"*40,
                               "x-forwarded-user":"another",
                               "x-haru-owner":"another"}),"owner-cat")
    def test_invalid_local_config(self):
        with self.assertRaises(ValueError):
            SingleOwnerTunnelAuth("owner-cat",b"short")
        with self.assertRaises(ValueError):
            SingleOwnerTunnelAuth("",b"k"*40)

if __name__ == "__main__":
    unittest.main()
