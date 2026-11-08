"""Owner-owned sqlite import must be secured without trusting foreign files."""
from pathlib import Path
import os
import pytest
from haru_life_events.telegram_location import HaruTelegramLocationAdapter
from haru_life_events.reverse_geocoder import ResolvedPlace


def adapter(p):
    return HaruTelegramLocationAdapter(
        allowed_user_id=42,allowed_chat_id=42,sqlite_path=p,
        place_resolver=lambda *a:ResolvedPlace("東京都","HeartRails"))


def test_existing_owner_db_mode_640_automatically_tightened(tmp_path):
    path=tmp_path/"loc.sqlite"
    path.touch(mode=0o640)
    path.chmod(0o640)
    adapter(path)
    assert path.stat().st_mode & 0o777==0o600


def test_symlink_is_rejected(tmp_path):
    real=tmp_path/"real.sqlite"
    real.touch(mode=0o600)
    link=tmp_path/"loc.sqlite"
    link.symlink_to(real)
    with pytest.raises(OSError):
        adapter(link)
    assert real.stat().st_mode & 0o777==0o600
