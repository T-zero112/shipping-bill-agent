from billing_agent.pdf_layout import charge_rows


class Page:
    width = 650
    def __init__(self, words): self.words = words
    def extract_words(self, **kwargs): return self.words


def word(text, x, y, width=25):
    return {'text':text,'x0':x,'x1':x+width,'top':y,'bottom':y+8}


def test_charge_code_and_description_separate_using_header_geometry():
    for shift in (0,30):
        words = [word('CHARGES',160+shift,100),word('CUR',350+shift,100),
            word('AMOUNT',500+shift,100),word('TYPE',570+shift,100),
            word('Waybill',100+shift,125),word('ABC12',160+shift,125),
            word('On',215+shift,125),word('Carriage',250+shift,125,40),
            word('USD',350+shift,125),word('245.0000',500+shift,125,45),
            word('C',575+shift,125),word('TOTAL',160+shift,145)]
        result = charge_rows(Page(list(reversed(words))))
        assert len(result)==1
        assert result[0]['description']=='On Carriage'
        assert result[0]['charge_code']=='ABC12'
        assert result[0]['amount']=='245.0000'
        assert result[0]['currency']=='USD'
        assert result[0]['description_bbox'][0]==215+shift


def test_unknown_layout_returns_no_fabricated_rows():
    assert charge_rows(Page([word('Weight',10,20),word('245',100,20)])) == []


def test_alphanumeric_word_within_description_is_not_assumed_separate_code():
    words = [word('CHARGES',160,100),word('CUR',350,100),word('AMOUNT',500,100),
        word('TYPE',570,100),word('ABC12',160,125),word('Service',188,125,40),
        word('USD',350,125),word('245',500,125)]
    row = charge_rows(Page(words))[0]
    assert row['charge_code'] is None
    assert row['description']=='ABC12 Service'
