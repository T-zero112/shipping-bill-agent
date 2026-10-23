"""Local LibreOffice conversion in an isolated, temporary profile."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def soffice_path():
    candidates = [os.environ.get('BILLING_SOFFICE'), shutil.which('soffice'),
                  r'C:\Program Files\LibreOffice\program\soffice.exe',
                  r'C:\Program Files (x86)\LibreOffice\program\soffice.exe']
    for path in candidates:
        if path and Path(path).is_file():
            return str(Path(path).resolve())
    raise RuntimeError('DOC conversion requires LibreOffice; set BILLING_SOFFICE to soffice executable')


def convert(data, source_suffix, target='docx'):
    executable = soffice_path()
    with tempfile.TemporaryDirectory(prefix='billing-convert-') as directory:
        root = Path(directory)
        profile = root/'profile'
        (profile/'user').mkdir(parents=True)
        (profile/'user'/'registrymodifications.xcu').write_text('''<?xml version="1.0"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
</oor:items>''', encoding='utf-8')
        source = root/('source'+source_suffix)
        source.write_bytes(data)
        output = root/'converted'
        output.mkdir()
        command = [executable, '-env:UserInstallation='+profile.as_uri(), '--headless',
                   '--nologo', '--nodefault', '--norestore', '--convert-to', target,
                   '--outdir', str(output), str(source)]
        result = subprocess.run(command, capture_output=True, timeout=60,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        expected = output/('source.'+target.split(':')[0])
        if result.returncode or not expected.exists():
            raise RuntimeError('LibreOffice conversion failed or produced no output')
        return expected.read_bytes()
