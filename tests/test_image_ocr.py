from email.message import EmailMessage
from io import BytesIO
from PIL import Image
from billing_agent.parsers import Parser,attachment_payloads
from billing_agent.image_layout import charge_rows,document_totals
from billing_agent.local_ocr import LocalOCR
from billing_agent.models import Evidence
from billing_agent.extraction import extract_local


def box(text,x,y,width=20,score=.99):
    return {'text':text,'score':score,'x0':x,'x1':x+width,'top':y,'bottom':y+8,
            'polygon':[[x,y],[x+width,y],[x+width,y+8],[x,y+8]]}


def frame():
    return {'page':1,'width':600,'height':800,'boxes':[
        box('Base',170,100),box('Rate',220,100),box('Curr',260,100),box('Amount',300,100,45),
        box('R.O.E.',360,100,30),box('Curr',400,100),box('Amount',450,100,45),
        box('TEST SERVICE FEE',15,120,130),box('C',170,120,10,.60),box('2.00',220,120),
        box('USD',260,120),box('5.00',310,120),box('1.2000',360,120,30),box('EUR',400,120),box('6.00',460,120),
        box('USD',400,140),box('5.00',460,140)]}


def test_original_amount_not_rate_or_duplicate_conversion_column():
    f=frame();rows=charge_rows(f)
    assert len(rows)==1
    assert rows[0]['description']=='TEST SERVICE FEE'
    assert rows[0]['amount']=='5.00'
    assert rows[0]['currency']=='USD'
    assert rows[0]['min_critical_score']==.99  # Low base-code confidence does not taint money/name.
    totals=document_totals(f,rows)
    assert len(totals)==1 and totals[0]['amount']=='5.00'


def test_unrecognized_image_layout_does_not_guess_a_table():
    assert charge_rows({'boxes':[box('USD 500',10,10)],'width':600})==[]


def test_nested_related_inline_jpg_is_discovered():
    message=EmailMessage();message.set_content('Hello')
    message.add_alternative('<html><img src="cid:banner"></html>',subtype='html')
    message.get_payload()[1].add_related(b'image-bytes',maintype='image',subtype='jpeg',cid='<banner>',filename='banner.jpg',disposition='inline')
    assert list(attachment_payloads(message))==[('banner.jpg',b'image-bytes')]


def test_parser_routes_related_image_to_local_ocr_and_keeps_mail_source():
    message=EmailMessage();message.set_content('Hello')
    message.add_alternative('<html><img src="cid:banner"></html>',subtype='html')
    message.get_payload()[1].add_related(b'image-bytes',maintype='image',subtype='jpeg',cid='<banner>',filename='banner.jpg',disposition='inline')
    class FakeOCR:
        def recognize(self,data):
            return {'engine':'test-local','sha256':'fake','cache_hit':False,
                    'frames':[{'page':1,'width':600,'height':100,'boxes':[box('Promotional banner',10,10,140)]}]}
    parser=Parser(image_mode='local');parser.local_ocr_engine=FakeOCR()
    parser.parse(message.as_bytes(),'sample.eml')
    assert len(parser.ocr_results)==1
    page=next(e for e in parser.evidence if e.source=='sample.eml::banner.jpg')
    assert page.method=='local_image_ocr_unverified'
    assert page.structured['document_kind']=='non_bill_candidate'


def test_local_ocr_coordinates_cache_and_multiframe(tmp_path):
    images=[Image.new('RGB',(100,100),'white'),Image.new('RGB',(100,100),'black')]
    stream=BytesIO();images[0].save(stream,format='TIFF',save_all=True,append_images=images[1:])
    class FakeEngine:
        calls=0
        def __call__(self,image):
            self.calls+=1
            return [[[[20,20],[60,20],[60,40],[20,40]],'USD 5',.99]],[.01,.01,.01]
    engine=LocalOCR(tmp_path);engine.engine=FakeEngine()
    result=engine.recognize(stream.getvalue())
    assert len(result['frames'])==2
    assert result['frames'][0]['boxes'][0]['x0']==10
    assert result['frames'][0]['boxes'][0]['x1']==30
    assert result['frames'][0]['coordinate_space']=='exif_corrected_original_pixels'
    cached=engine.recognize(stream.getvalue())
    assert cached['cache_hit'] is True
    assert engine.engine.calls==2


def test_empty_ocr_is_error_and_not_non_bill_success():
    class FakeOCR:
        def recognize(self,data):
            return {'engine':'test','sha256':'fake','cache_hit':False,
                    'frames':[{'page':1,'width':100,'height':100,'boxes':[]}]}
    parser=Parser(image_mode='local');parser.local_ocr_engine=FakeOCR()
    parser.parse(b'fake','empty.png')
    assert not parser.evidence
    assert any('error' in issue for issue in parser.issues)


def test_bad_image_records_error_instead_of_crashing_batch():
    parser=Parser(image_mode='local')
    parser.parse(b'not image','invalid.png');parser.parse(b'Valid other document','next.txt')
    assert any('error' in issue for issue in parser.issues)
    assert any(e.source=='next.txt' for e in parser.evidence)


def test_local_amendment_routing_still_compiles_before_model(monkeypatch):
    import urllib.request
    class NoNetwork:
        def open(self,*args,**kwargs):raise AssertionError('Supported amendment must not call model')
    monkeypatch.setattr(urllib.request,'build_opener',lambda *args:NoNetwork())
    e=Evidence(id='e',source='test.eml',location='body:line:1',
        text='Please change bill DN-1001 Handling Fee payment term to CC.',
        structured={'kind':'email_statement'})
    result=extract_local([e])
    assert not result.errors
    assert len(result.charges)==1 and result.charges[0].intent=='MODIFY'
