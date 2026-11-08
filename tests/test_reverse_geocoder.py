"""Reverse geocoder tests; all HTTP mocked."""
from haru_life_events.reverse_geocoder import (
    JapanReverseGeocoder, HEARTRAILS_REVERSE_GEOCODER_URL,
    GSI_MUNICIPALITY_URL, GSI_REVERSE_GEOCODER_URL
)

def test_heartrails_primary_and_cache():
    g=JapanReverseGeocoder()
    calls=[]
    def load(url,*,timeout):
        calls.append(url)
        assert url.startswith(HEARTRAILS_REVERSE_GEOCODER_URL)
        return '{"response":{"location":[{"town":"丸の内","city":"千代田区","prefecture":"東京都","distance":14}]}}'
    g._get_text=load
    label=g.resolve(35.6812,139.7671)
    assert label.label=="丸の内, 千代田区, 東京都"
    assert g.resolve(35.6814,139.7674)==label
    assert len(calls)==1

def test_gsi_fallback():
    g=JapanReverseGeocoder()
    def load(url,*,timeout):
        if url.startswith(HEARTRAILS_REVERSE_GEOCODER_URL):
            raise OSError("offline")
        if url==GSI_MUNICIPALITY_URL:
            return 'GSI.MUNI_ARRAY["13101"] = \'13,東京都,13101,千代田区\';'
        assert url.startswith(GSI_REVERSE_GEOCODER_URL)
        return '{"results":{"muniCd":"13101","lv01Nm":"丸の内一丁目"}}'
    g._get_text=load
    assert g.resolve(35.6812,139.7671).label=="丸の内一丁目, 千代田区, 東京都"

def test_japan_bounds_not_international():
    g=JapanReverseGeocoder()
    g._get_text=lambda *_args,**_kwargs: (_ for _ in ()).throw(AssertionError("called"))
    assert g.resolve(51.5074,-0.1278) is None
