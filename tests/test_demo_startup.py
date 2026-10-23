"""Check explicit OCR opt-in and actionable missing-runtime errors."""
import pytest
from scripts import serve_demo


def test_runtime_discovery_requires_opt_in(tmp_path, monkeypatch):
    binary=tmp_path/'cloudflared.exe';binary.write_bytes(b'test only')
    monkeypatch.setenv('BILLING_CLOUDFLARED',str(binary))
    assert serve_demo.resolve_cloudflared() is None
    assert serve_demo.resolve_cloudflared(enabled=True)==str(binary.resolve())


def test_explicit_runtime_wins_and_missing_path_fails(tmp_path, monkeypatch):
    binary=tmp_path/'cloudflared.exe';binary.write_bytes(b'test only')
    monkeypatch.setenv('BILLING_CLOUDFLARED','missing-environment-path')
    assert serve_demo.resolve_cloudflared(str(binary))==str(binary.resolve())
    with pytest.raises(ValueError,match='not found'):
        serve_demo.resolve_cloudflared(str(tmp_path/'missing.exe'))


def test_enabled_without_runtime_has_configuration_error(tmp_path, monkeypatch):
    monkeypatch.setattr(serve_demo,'ROOT',tmp_path)
    monkeypatch.delenv('BILLING_CLOUDFLARED',raising=False)
    monkeypatch.setattr(serve_demo.shutil,'which',lambda _:None)
    with pytest.raises(ValueError,match='BILLING_CLOUDFLARED'):
        serve_demo.resolve_cloudflared(enabled=True)
