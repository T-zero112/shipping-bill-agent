import base64
import json
import threading
import urllib.error
import urllib.request

import pytest

from billing_agent.webapp import DemoApp,make_server


@pytest.fixture
def web(tmp_path):
    app=DemoApp(tmp_path)
    server=make_server(app,0)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    yield app,f'http://127.0.0.1:{server.server_port}'
    server.shutdown();server.server_close();app.pool.shutdown(wait=True)


def test_api_requires_token_and_same_origin(web):
    app,url=web
    with urllib.request.urlopen(url+'/api/config') as r:assert json.load(r)['token']==app.token
    with pytest.raises(urllib.error.HTTPError) as error:urllib.request.urlopen(url+'/api/jobs/not-found')
    assert error.value.code==403
    request=urllib.request.Request(url+'/api/jobs',data=b'{}',headers={'X-Demo-Token':app.token,'Origin':'https://other.example'})
    with pytest.raises(urllib.error.HTTPError) as error:urllib.request.urlopen(request)
    assert error.value.code==403


def test_upload_path_is_basename_and_no_remote_without_configuration(web,monkeypatch):
    app,_=web
    monkeypatch.setattr(app.pool,'submit',lambda *args:None)
    identity=app.submit({'entity':'B','filename':'../../bill.txt','content':base64.b64encode(b'synthetic').decode()})
    assert (app.directory/identity/'bill.txt').is_file()
    with pytest.raises(ValueError,match='正在处理'):app.submit({'entity':'B','synthetic':True})
    app.jobs[identity]['state']='done'
    with pytest.raises(ValueError,match='cloudflared'):app.submit({'entity':'B','mode':'remote','filename':'bill.pdf','content':base64.b64encode(b'%PDF-test').decode(),'consent':True})


def test_unknown_extensions_and_invalid_base64_rejected(web):
    app,_=web
    with pytest.raises(ValueError):app.submit({'entity':'B','filename':'script.exe','content':'YQ=='})
    with pytest.raises(ValueError):app.submit({'entity':'B','filename':'bill.txt','content':'!!'})


def test_static_content_and_csp(web):
    _,url=web
    with urllib.request.urlopen(url) as r:
        assert b'upload-form' in r.read()
        assert "script-src 'self'" in r.headers['Content-Security-Policy']
    with pytest.raises(urllib.error.HTTPError):urllib.request.urlopen(url+'/../requirements.txt')
