"""CPU-only OCR with bundled RapidOCR models, original-pixel boxes and explicit cache."""
import hashlib
from importlib.metadata import version
from io import BytesIO
import json
import tempfile
from pathlib import Path
from PIL import Image, ImageOps, ImageSequence

ENGINE_VERSION='rapidocr-onnxruntime-'+version('rapidocr-onnxruntime')
CONFIG_VERSION='rgb-upscale2-max2400-exif-v2'


class LocalOCR:
    def __init__(self,cache_dir=None):
        self.cache_dir=Path(cache_dir) if cache_dir else None
        self.engine=None

    def recognize(self,data):
        key=hashlib.sha256(data+ENGINE_VERSION.encode()+CONFIG_VERSION.encode()).hexdigest()
        cache=self.cache_dir/(key+'.json') if self.cache_dir else None
        if cache and cache.exists():
            result=json.loads(cache.read_text(encoding='utf-8'))
            return {**result,'cache_hit':True}
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR
        frames=[]
        with Image.open(BytesIO(data)) as document:
            if getattr(document,'n_frames',1)>50:
                raise ValueError('Image frame limit exceeded')
            for number,frame in enumerate(ImageSequence.Iterator(document),1):
                image=ImageOps.exif_transpose(frame.copy()).convert('RGB')
                width,height=image.size
                if width*height>25_000_000:
                    raise ValueError('Image pixel budget exceeded')
                if self.engine is None:
                    self.engine=RapidOCR(intra_op_num_threads=2,inter_op_num_threads=2,
                        det_use_cuda=False,cls_use_cuda=False,rec_use_cuda=False)
                scale=min(2.0,2400/max(width,height))
                resized=image.resize((round(width*scale),round(height*scale)),Image.Resampling.LANCZOS)
                found,timings=self.engine(np.asarray(resized))
                boxes=[]
                for points,text,score in found or []:
                    boxes.append({'text':text,'score':float(score),
                        'polygon':[[float(x)/scale,float(y)/scale] for x,y in points],
                        'x0':min(float(p[0]) for p in points)/scale,
                        'x1':max(float(p[0]) for p in points)/scale,
                        'top':min(float(p[1]) for p in points)/scale,
                        'bottom':max(float(p[1]) for p in points)/scale})
                frames.append({'page':number,'width':width,'height':height,'boxes':boxes,'timings':timings,
                               'coordinate_space':'exif_corrected_original_pixels'})
        result={'engine':ENGINE_VERSION,'configuration':CONFIG_VERSION,
            'sha256':hashlib.sha256(data).hexdigest(),'frames':frames,'cache_hit':False}
        if cache:
            cache.parent.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=cache.parent,suffix='.tmp',delete=False) as handle:
                json.dump(result,handle,ensure_ascii=False)
                temporary=Path(handle.name)
            temporary.replace(cache)
        return result
