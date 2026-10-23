"""PDF-point geometry: OCR only image regions and reconcile native text spatially."""
import re
from decimal import Decimal


def overlap(a,b):
    return max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))


def image_regions(page):
    regions=[]
    for image in page.images:
        box=[max(0,image['x0']),max(0,image['top']),min(page.width,image['x1']),min(page.height,image['bottom'])]
        if (box[2]-box[0])*(box[3]-box[1])<page.width*page.height*.05 or box[2]-box[0]<100 or box[3]-box[1]<50:continue
        regions.append(box)
    # Merge overlapping/nested images to avoid recognizing the same painted area twice.
    merged=[]
    for box in regions:
        pending=box
        i=0
        while i<len(merged):
            prior=merged[i]
            if overlap(pending,prior)>0:
                pending=[min(pending[0],prior[0]),min(pending[1],prior[1]),max(pending[2],prior[2]),max(pending[3],prior[3])]
                merged.pop(i);i=0
            else:i+=1
        merged.append(pending)
    return sorted(merged,key=lambda b:(b[1],b[0]))


def compact(text):return re.sub(r'\W+','',text,flags=re.UNICODE).casefold()


def same_text(a,b):
    def numbers(text):
        return [Decimal(t.replace(',','')) for t in re.findall(r'[-+]?\d[\d,]*(?:\.\d+)?',text)]
    return compact(a)==compact(b) and numbers(a)==numbers(b)


def reconcile(frame,words,region,scale=2):
    kept=[];suppressed=[];conflicts=[]
    for original in frame['boxes']:
        box={**original}
        for field,axis in [('x0',0),('x1',0),('top',1),('bottom',1)]:
            box[field]=original[field]/scale+region[axis]
        box['polygon']=[[x/scale+region[0],y/scale+region[1]] for x,y in original['polygon']]
        rect=[box['x0'],box['top'],box['x1'],box['bottom']]
        native=[w for w in words if overlap(rect,[w['x0'],w['top'],w['x1'],w['bottom']])>
                .3*max(1,(w['x1']-w['x0'])*(w['bottom']-w['top']))]
        text=''.join(w['text'] for w in sorted(native,key=lambda w:(round(w['top']/3),w['x0'])))
        if native and same_text(text,box['text']):suppressed.append(box)
        elif native:
            conflicts.append({'ocr':box,'native_text':text})
        else:kept.append(box)
    return {**frame,'boxes':kept,'coordinate_space':'pdf_points_top_left'},suppressed,conflicts
