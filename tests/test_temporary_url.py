import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from billing_agent.temporary_url import TemporaryURL, file_type


@pytest.mark.parametrize('data,expected',[
    (b'\x89PNG\r\n\x1a\n',('png','image/png')),
    (b'\xff\xd8\xff',('jpg','image/jpeg')),
    (b'II*\x00',('tiff','image/tiff')),
    (b'MM\x00*',('tiff','image/tiff'))])
def test_image_signature_controls_url_and_mime(data,expected):
    assert file_type(data)==expected


def test_html_not_published_as_image():
    with pytest.raises(ValueError,match='signatures'):
        file_type(b'<html>not a PNG</html>')


def provider(tmp_path,monkeypatch,bad=False):
    data=b'%PDF-1.4 test content'
    source=tmp_path/'bill.pdf';source.write_bytes(data)
    binary=tmp_path/'cloudflared.exe';binary.touch()
    class FakeProcess:
        def __init__(self,*args,**kwargs):
            kwargs['stdout'].write('https://test-domain.trycloudflare.com Registered tunnel connection\n')
            kwargs['stdout'].flush();self.done=False
        def poll(self):return 0 if self.done else None
        def terminate(self):self.done=True
        def wait(self,**kwargs):self.done=True
    monkeypatch.setattr('billing_agent.temporary_url.subprocess.Popen',FakeProcess)
    monkeypatch.setattr('billing_agent.temporary_url.time.sleep',lambda _:None)
    monkeypatch.setattr('billing_agent.temporary_url.requests.get',lambda *a,**kw:SimpleNamespace(
        status_code=200,content=b'<html>' if bad else data,headers={'Content-Type':'application/pdf'},history=[]))
    return TemporaryURL(binary,[source],tmp_path/'artifacts',interval=0),data


def test_success_and_consumer_failure_both_cleanup(tmp_path,monkeypatch):
    service,data=provider(tmp_path,monkeypatch)
    with service.publish(data,'bill.pdf') as (url,out):
        assert url.endswith('/bill.pdf')
    assert json.loads((out/'cleanup.json').read_text())=={'server_closed':True,'tunnel_stopped':True}
    assert len(json.loads((out/'download-checks.json').read_text()))==4
    with pytest.raises(RuntimeError,match='injected'):
        with service.publish(data,'bill.pdf') as (_,failed):raise RuntimeError('injected OCR timeout')
    assert json.loads((failed/'cleanup.json').read_text())['tunnel_stopped']


def test_html_200_blocks_ocr_and_cleans_up(tmp_path,monkeypatch):
    service,data=provider(tmp_path,monkeypatch,bad=True)
    with pytest.raises(RuntimeError,match='OCR not called'):
        with service.publish(data,'bill.pdf'):pytest.fail('Should never submit OCR')
    out=next((tmp_path/'artifacts').iterdir())
    assert json.loads((out/'cleanup.json').read_text())['server_closed']


def test_unauthorized_file_blocked_before_publishing(tmp_path,monkeypatch):
    service,_=provider(tmp_path,monkeypatch)
    with pytest.raises(ValueError,match='not explicitly allowed'):
        with service.publish(b'%PDF-other company file','other.pdf'):pytest.fail('Unauthorized publication')
    assert not service.artifacts.exists()


def test_startup_transport_error_retried_without_weakening_tls(tmp_path,monkeypatch):
    import requests
    service,data=provider(tmp_path,monkeypatch)
    calls=[]
    def download(*args,**kwargs):
        calls.append(kwargs)
        if len(calls)==1:raise requests.exceptions.SSLError('injected startup EOF')
        return SimpleNamespace(status_code=200,content=data,headers={},history=[])
    monkeypatch.setattr('billing_agent.temporary_url.requests.get',download)
    with service.publish(data,'bill.pdf') as (_,out):pass
    assert len(calls)==5 and all('verify' not in c for c in calls)
    assert json.loads((out/'cleanup.json').read_text())['tunnel_stopped']


def test_parser_ocr_timeout_closes_public_service(tmp_path,monkeypatch):
    from billing_agent.parsers import Parser
    service,data=provider(tmp_path,monkeypatch)
    def timeout(*args,**kwargs):raise TimeoutError('injected OCR timeout')
    monkeypatch.setattr('billing_agent.parsers.urllib.request.urlopen',timeout)
    parser=Parser(pdf_mode='ocr',temporary_url=service)
    parser.parse(data,'bill.pdf')
    assert not parser.evidence and any('injected OCR timeout' in x.get('error','') for x in parser.issues)
    out=next(service.artifacts.iterdir())
    assert json.loads((out/'cleanup.json').read_text())['tunnel_stopped']
    assert (out/'ocr-error.json').exists()
