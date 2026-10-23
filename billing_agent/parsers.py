import hashlib
import json
import urllib.request
from email import policy
from email.parser import BytesParser
from io import BytesIO
from pathlib import Path
from .models import Evidence


def attachment_payloads(message):
    """Include named inline resources inside multipart/related, not just top-level attachments."""
    for index,part in enumerate(message.iter_parts() if message.is_multipart() else []):
        name=part.get_filename()
        if name or part.get_content_disposition()=='attachment':
            payload=part.get_payload(decode=True)
            if payload is None and part.get_content_type()=='message/rfc822':
                messages=part.get_payload()
                if messages:payload=messages[0].as_bytes()
            if payload:
                yield name or f'attachment-{index}.eml',payload
        elif part.is_multipart():
            yield from attachment_payloads(part)
        elif part.get_content_maintype()=='image' and part.get('Content-ID'):
            payload=part.get_payload(decode=True)
            if payload:
                extension='.jpg' if part.get_content_subtype() in {'jpeg','jpg'} else '.'+part.get_content_subtype()
                yield f'inline-{index}{extension}',payload


class Parser:
    def __init__(self, pdf_mode='local', url_map=None, ocr_cache=None, image_mode='remote', local_ocr_cache=None, temporary_url=None):
        self.pdf_mode = pdf_mode
        self.url_map = url_map or {}
        self.ocr_cache = ocr_cache or {}
        self.evidence = []
        self.issues = []
        self.seen = set()
        self.image_mode=image_mode
        self.local_ocr_cache=local_ocr_cache
        self.local_ocr_engine=None
        self.ocr_results=[]
        self.sources=set()
        self.temporary_url=temporary_url
        self.remote_adapter_events=[]

    def add(self, source, location, text, method='parser', **metadata):
        if text and text.strip():
            key = hashlib.sha256(f'{source}:{location}:{text}'.encode()).hexdigest()[:20]
            item = Evidence(id=key, source=source, location=location, text=text, method=method, **metadata)
            self.evidence.append(item)
            return item

    def parse(self, data, name, depth=0):
        self.sources.add(name)
        if depth > 5 or len(data) > 30 * 1024 * 1024:
            self.issues.append({'source': name, 'error': 'File size or nesting limit exceeded'})
            return
        digest = hashlib.sha256(data).hexdigest()
        if digest in self.seen:
            self.issues.append({'source': name, 'status': 'duplicate_file', 'sha256': digest})
            return
        self.seen.add(digest)
        ext = Path(name).suffix.lower()
        # PDF magic overrides unhelpful MIME types or attachment names.
        if data.startswith(b'%PDF-'):
            ext = '.pdf'
        try:
            if ext == '.eml':
                m = BytesParser(policy=policy.default).parsebytes(data)
                self.add(name, 'headers', '\n'.join(f'{k}: {m.get(k, "")}' for k in ['Subject', 'From', 'To', 'Date','Message-ID']),source_digest=digest)
                body = m.get_body(preferencelist=('plain', 'html'))
                if body:
                    text = body.get_content()
                    if body.get_content_type()=='text/html':
                        from .tabular import html_text
                        text=html_text(text)
                    parent = self.add(name, 'body', text,source_digest=digest)
                    quoted=False
                    for line_no, line in enumerate(text.splitlines(), 1):
                        import re
                        if re.search(r'^\s*(?:-{2,}\s*(?:Original Message|Forwarded)|On .+wrote:|发件人[:：])',line,re.I):
                            quoted=True
                        if line.strip():
                            self.add(name, f'body:line:{line_no}', line, parent_id=parent.id,
                                     source_digest=digest,structured={'kind': 'email_statement', 'line_number': line_no,
                                         'quoted':quoted or line.lstrip().startswith('>')})
                for filename,payload in attachment_payloads(m):
                    self.parse(payload,name+'::'+filename,depth+1)
            elif ext == '.pdf' and self.pdf_mode == 'local':
                from pypdf import PdfReader
                reader=PdfReader(BytesIO(data))
                if len(reader.pages)>50:
                    raise ValueError('PDF page limit exceeded')
                scanned=set()
                for i, page in enumerate(reader.pages):
                    text=page.extract_text(extraction_mode='layout') or ''
                    if len(''.join(c for c in text if c.isalnum()))<40:
                        scanned.add(i)
                        self.scan_pdf_page(data,name,i,digest)
                    else:
                        self.add(name, f'page:{i+1}', text, 'local_pdf_unverified')
                import pdfplumber
                from .pdf_layout import charge_rows
                from .party_layout import party_blocks
                with pdfplumber.open(BytesIO(data)) as document:
                    for i, page in enumerate(document.pages):
                        if i in scanned:
                            continue
                        parent = next((e for e in self.evidence if e.source==name and e.location==f'page:{i+1}'), None)
                        self.hybrid_regions(data,name,i,digest,page,parent)
                        for j, party in enumerate(party_blocks(page)):
                            self.add(name, f'page:{i+1}/party:{j+1}', party['quote'], 'local_pdf_roles_unverified',
                                parent_id=parent.id if parent else None,bbox=party['bbox'],
                                structured={'kind':'party_role','role':party['role'],'entity':party['entity']})
                        for j, row in enumerate(charge_rows(page)):
                            text = row['quote']
                            if row['currency_header']:
                                text += '\n'+row['currency_header']['text']
                            self.add(name, f'page:{i+1}/charge-row:{j+1}', text, 'local_pdf_columns_unverified',
                                parent_id=parent.id if parent else None, bbox=row['bbox'],
                                structured={'kind': 'pdf_charge_row', **row})
                self.issues.append({'source': name, 'warning': 'Local PDF inspection only; designated OCR not verified'})
            elif ext in {'.png','.jpg','.jpeg','.tif','.tiff'} and self.image_mode=='local':
                self.local_image(data,name)
            elif ext in {'.pdf', '.png', '.jpg', '.jpeg', '.tif', '.tiff'}:
                self.ocr(data, name)
            elif ext in {'.doc','.docx'}:
                from .tabular import word_blocks
                if ext=='.doc':
                    if not (data.startswith(bytes.fromhex('d0cf11e0a1b11ae1')) or data.lstrip().startswith(b'{\\rtf')):
                        raise ValueError('Invalid legacy DOC signature; expected binary DOC or RTF')
                    from .conversion import convert
                    data=convert(data,'.doc')
                    self.issues.append({'source':name,'status':'converted_doc_to_docx','converter':'LibreOffice'})
                for location,text,metadata in word_blocks(data):
                    self.add(name,location,text,source_digest=digest,structured={**metadata,'converted_from_doc':ext=='.doc'})
            elif ext in {'.xlsx', '.xls'}:
                if ext == '.xlsx':
                    from .tabular import xlsx_rows
                    for sheet,number,text,metadata in xlsx_rows(data):
                        self.add(name,f'sheet:{sheet}/row:{number}',text,source_digest=digest,structured=metadata)
                        for reason in metadata['review_reasons']:
                            self.issues.append({'source':name,'location':f'{sheet}:{number}','warning':reason})
                else:
                    import xlrd
                    book = xlrd.open_workbook(file_contents=data)
                    from .tabular import is_header,charge_cells,business_cells
                    for sheet in book.sheets():
                        headers=[]
                        if sheet.nrows*sheet.ncols>200_000:
                            raise ValueError('Spreadsheet cell budget exceeded')
                        for i in range(sheet.nrows):
                            values=sheet.row_values(i)
                            if is_header(values):headers=values
                            self.add(name, f'sheet:{sheet.name}/row:{i+1}', ' | '.join(map(str, values)),
                                structured={'kind':'spreadsheet_row','charge_cells':charge_cells(headers,values),
                                    'business_cells':business_cells(headers,values),
                                    'review_reasons':['Legacy XLS formula provenance unavailable; values require review']})
                    self.issues.append({'source':name,'warning':'Legacy XLS formula provenance unavailable; values require review'})
                    book.release_resources()
            elif ext == '.txt':
                self.add(name, 'text', data.decode('utf-8-sig'))
            else:
                raise ValueError(f'Unsupported format {ext}; legacy DOC requires conversion to DOCX')
        except Exception as exc:
            self.issues.append({'source': name, 'error': f'{type(exc).__name__}: {exc}'})

    def ocr(self, data, name):
        digest = hashlib.sha256(data).hexdigest()
        if digest in self.ocr_cache:
            result = self.ocr_cache[digest]
            method = 'ocr_replay'
        else:
            url = self.url_map.get(digest)
            if not url and self.temporary_url is None:
                raise ValueError(f'OCR needs accessible URL or cached response for SHA256 {digest}; no automatic upload')
            def call(url, artifacts=None):
                import time
                payload={'file': url, 'task': 'orderChange', 'callBack': '', 'taskType': ''}
                if artifacts:
                    (artifacts/'ocr-request.json').write_text(json.dumps(payload),encoding='utf-8')
                request = urllib.request.Request('http://36.103.199.11:5002/process',
                    data=json.dumps(payload).encode(),headers={'Content-Type': 'application/json'})
                start=time.monotonic()
                try:
                    with urllib.request.urlopen(request, timeout=300) as response:
                        content=response.read()
                        if artifacts:
                            (artifacts/'ocr-response.txt').write_bytes(content)
                            (artifacts/'ocr-http.json').write_text(json.dumps({'status':response.status,
                                'elapsed_seconds':round(time.monotonic()-start,2)}),encoding='utf-8')
                        return json.loads(content)
                except Exception as exc:
                    if artifacts:
                        (artifacts/'ocr-error.json').write_text(json.dumps({'error':str(exc)}),encoding='utf-8')
                    raise
            if url:
                result=call(url)
            else:
                with self.temporary_url.publish(data,name) as (url,artifacts):
                    result=call(url,artifacts)
                if (artifacts/'postcheck-warning.json').exists():
                    self.issues.append({'source':name,'status':'unresolved_ocr','warning':'Temporary URL postcheck failed'})
            method = 'ocr_api'
        if not isinstance(result,dict) or result.get('success') is not True:
            raise ValueError('Remote OCR returned invalid or unsuccessful response')
        if result.get('error_code'):
            raise ValueError('Remote OCR returned error_code: '+str(result['error_code']))
        # Response contract is unknown: retain JSON without inventing table/page semantics.
        payload=result.get('data')
        empty=not payload or (isinstance(payload,dict) and not payload.get('list') and not payload.get('pages_split'))
        reason='Remote OCR success response contains no usable bills/pages; not confirmed non-bill'
        raw=self.add(name, 'ocr_response', json.dumps(result, ensure_ascii=False), method,source_digest=digest,
            structured={'kind':'remote_ocr_unresolved' if empty else 'remote_ocr_raw',
                        'remote_context_only':True,
                        'review_reasons':[reason] if empty else []})
        if empty:
            self.issues.append({'source':name,'status':'unresolved_ocr',
                'warning':reason})
        else:
            from .remote_ocr import adapt
            adapt(self,data,name,result,raw)
            transport_warnings=[i['warning'] for i in self.issues if i.get('source')==name
                                and i.get('status')=='unresolved_ocr' and i.get('warning')]
            for e in self.evidence:
                if e.source==name and (e.structured or {}).get('remote_charge'):
                    e.structured['remote_charge']['warnings'].extend(transport_warnings)

    def hybrid_regions(self,data,name,index,digest,page,parent):
        from .hybrid import image_regions,reconcile
        regions=image_regions(page)
        if not regions:return
        import pypdfium2 as pdfium
        from .local_ocr import LocalOCR
        if self.local_ocr_engine is None:self.local_ocr_engine=LocalOCR(self.local_ocr_cache)
        document=pdfium.PdfDocument(data)
        pdf_page=document[index]
        try:
            width,height=pdf_page.get_size()
            if width*height*4>25_000_000:raise ValueError('Hybrid PDF render pixel budget exceeded')
            bitmap=pdf_page.render(scale=2)
            try:image=bitmap.to_pil().copy()
            finally:bitmap.close()
            words=page.extract_words()
            for number,region in enumerate(regions,1):
                location=f'page:{index+1}/region:{number}'
                try:
                    stream=BytesIO()
                    image.crop(tuple(round(v*2) for v in region)).save(stream,format='PNG')
                    raw=self.local_ocr_engine.recognize(stream.getvalue())
                    filtered,suppressed,conflicts=reconcile(raw['frames'][0],words,region)
                    filtered['page']=index+1
                    filtered['width']=page.width;filtered['height']=page.height
                    result={**raw,'frames':[filtered],'raw_pixel_frames':raw['frames'],
                        'source_kind':'hybrid_pdf_region','pdf_sha256':digest,'region_bbox':region,
                        'suppressed_native_duplicates':suppressed,'conflicts':conflicts}
                    if conflicts or not raw['frames'][0]['boxes']:
                        reason='Hybrid OCR conflicts with native text or returned no text; manual region verification required'
                        if parent:
                            parent.structured={**(parent.structured or {}),'review_reasons':list(dict.fromkeys((parent.structured or {}).get('review_reasons',[])+[reason]))}
                        self.add(name,location,'\n'.join(b['text'] for b in raw['frames'][0]['boxes']) or '[Unrecognized image region]',
                            'local_pdf_ocr_unverified',parent_id=parent.id if parent else None,bbox=region,
                            structured={'kind':'hybrid_conflict','review_reasons':[reason]})
                        self.ocr_results.append({'source':name,**result})
                        self.issues.append({'source':name,'page':index+1,'warning':reason})
                    elif filtered['boxes']:
                        self.consume_ocr(result,name,index+1,location,parent.id if parent else None)
                    else:
                        self.ocr_results.append({'source':name,**result})
                except Exception as exc:
                    self.issues.append({'source':name,'page':index+1,'region':number,'error':f'Hybrid OCR: {type(exc).__name__}: {exc}'})
        finally:
            pdf_page.close();document.close()

    def scan_pdf_page(self,data,name,index,digest):
        import pypdfium2 as pdfium
        document=pdfium.PdfDocument(data)
        try:
            page=document[index]
            try:
                width,height=page.get_size()
                if width*height*4>25_000_000:
                    raise ValueError('Rendered PDF pixel budget exceeded')
                bitmap=page.render(scale=2)
                try:
                    stream=BytesIO()
                    bitmap.to_pil().save(stream,format='PNG')
                finally:
                    bitmap.close()
                self.local_image(stream.getvalue(),name,index+1,digest)
            finally:
                page.close()
        finally:
            document.close()

    def local_image(self,data,name,pdf_page=None,pdf_digest=None):
        import re
        from .local_ocr import LocalOCR
        from .image_layout import charge_rows,frame_text,document_totals
        if self.local_ocr_engine is None:
            self.local_ocr_engine=LocalOCR(self.local_ocr_cache)
        result=self.local_ocr_engine.recognize(data)
        if pdf_page is not None:
            result={**result,'frames':[{**frame,'page':pdf_page} for frame in result['frames']],
                    'pdf_sha256':pdf_digest,'render_scale':2,'source_kind':'scanned_pdf_page'}
        self.consume_ocr(result,name,pdf_page)

    def consume_ocr(self,result,name,pdf_page=None,region_location=None,parent_id=None):
        import re
        from .image_layout import charge_rows,frame_text,document_totals
        self.ocr_results.append({'source':name,**result})
        for frame in result['frames']:
            text=frame_text(frame)
            if not text.strip():
                self.issues.append({'source':name,'page':frame['page'],'error':'Local OCR returned no text; manual inspection required'})
                continue
            monetary=bool(re.search(r'invoice|debit\s*note|credit\s*note|amount|total|charge|账单|发票|金额|费用|\b(?:USD|EUR|CNY)\b|[$€£¥]\s*\d',text,re.I))
            method='local_pdf_ocr_unverified' if pdf_page is not None else 'local_image_ocr_unverified'
            location=region_location or f'page:{frame["page"]}'
            parent=self.add(name,location,text,method,parent_id=parent_id,
                source_digest=result.get('pdf_sha256',result['sha256']),structured={'kind':'ocr_page','engine':result['engine'],
                    'pdf_sha256':result.get('pdf_sha256'),'region_bbox':result.get('region_bbox'),
                    'coordinate_space':frame.get('coordinate_space','image_pixels'),
                    'document_kind':'bill_candidate' if monetary else 'non_bill_candidate',
                    'width':frame['width'],'height':frame['height'],'boxes':frame['boxes'],'cache_hit':result['cache_hit']})
            rows=charge_rows(frame)
            for j,row in enumerate(rows):
                self.add(name,f'{location}/charge-row:{j+1}',row['quote'],method,
                    parent_id=parent.id,source_digest=result.get('pdf_sha256',result['sha256']),bbox=row['bbox'],
                    structured={'kind':'image_charge_row',**row})
            for j,total in enumerate(document_totals(frame,rows)):
                self.add(name,f'{location}/total:{j+1}',total['quote'],method,
                    parent_id=parent.id,source_digest=result.get('pdf_sha256',result['sha256']),bbox=total['bbox'],
                    structured={'kind':'image_document_total',**total})
            self.issues.append({'source':name,'page':frame['page'],'warning':'Local image OCR only; designated OCR not verified'})
