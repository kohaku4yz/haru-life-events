import os
import stat
import tempfile
import unittest
from pathlib import Path

from haru_life_events.prepare_canary import prepare

IDENT="tunnel_6acc47c5825481919c1432fce67c75b0"


class TestPrepareCanary(unittest.TestCase):
    def test_private_generated_files_stay_private_and_disabled(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root)
            conf=base/"cfg"
            state=base/"state"
            result=prepare(tunnel_id=IDENT,config_dir=conf,data_dir=state,owner="owner-test")
            self.assertEqual(result["enabled"],"false")
            self.assertEqual(result["control_plane_key_installed"],"false")
            files=list(conf.iterdir())
            self.assertEqual(len(files),7)
            for file in files:
                self.assertEqual(file.stat().st_mode & 0o077,0)
            self.assertEqual(conf.stat().st_mode & 0o077,0)
            self.assertEqual(state.stat().st_mode & 0o077,0)
            self.assertEqual((conf/"control-plane-runtime.key").read_bytes(), b"")
            self.assertIn(f'tunnel_id: "{IDENT}"',(conf/"tunnel.yaml").read_text())
            self.assertIn("HARU_EVENTS_BRIDGE_ENABLED=0",(conf/"events.env").read_text())
            token=(conf/"owner.key").read_bytes()
            self.assertEqual((conf/"backend-auth-header.txt").read_bytes(),b"Bearer "+token)
            self.assertNotIn(token.decode(),(conf/"tunnel.yaml").read_text())
            with self.assertRaises(FileExistsError):
                prepare(tunnel_id=IDENT,config_dir=conf,data_dir=state,owner="owner-test")
            self.assertEqual((conf/"owner.key").read_bytes(),token)

    def test_reject_invalid_id_or_owner(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root)
            with self.assertRaises(ValueError):
                prepare(tunnel_id="tunnel_fake",config_dir=base/"cfg",data_dir=base/"data",owner="owner-test")
            with self.assertRaises(ValueError):
                prepare(tunnel_id=IDENT,config_dir=base/"cfg",data_dir=base/"data",owner="a\nb")

    def test_reject_insecure_existing_directory(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root)
            conf=base/"cfg"
            conf.mkdir(mode=0o755)
            conf.chmod(0o755)
            with self.assertRaises(ValueError):
                prepare(tunnel_id=IDENT,config_dir=conf,data_dir=base/"state",owner="owner-test")

if __name__=="__main__":
    unittest.main()
