from billing_agent.party_layout import party_blocks


class Page:
    width=600
    height=800
    def __init__(self,words): self.words=words
    def extract_words(self,**kwargs): return self.words


def word(text,x,y,width=30):
    return {'text':text,'x0':x,'x1':x+width,'top':y,'bottom':y+9}


def test_letterhead_beneficiary_and_shipping_roles_remain_distinct():
    words=[word('Alpha',20,30),word('LLC',60,30),
        word('BENEFICIARY:',20,400,95),word('Beta',120,400),word('LIMITED',160,400,60),
        word('SHIPPER',20,200,65),word('NOTIFY',330,200,65),
        word('Gamma',20,220),word('LLC',65,220),word('Delta',330,220),word('LLC',375,220),
        word('Shipper',20,500),word('owned',80,500),word('containers',130,500)]
    roles=party_blocks(Page(words))
    pairs={(p['role'],p['entity']) for p in roles}
    assert ('header_party','Alpha LLC') in pairs
    assert ('beneficiary','Beta LIMITED') in pairs
    assert ('shipper','Gamma LLC') in pairs
    assert ('notify','Delta LLC') in pairs
    assert not any(p['role']=='issuer' for p in roles)
    assert not any('owned' in p['entity'] for p in roles)
