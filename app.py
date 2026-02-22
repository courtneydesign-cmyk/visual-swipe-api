from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re
import random
import time
import hashlib
from datetime import datetime, timedelta
import threading
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

image_cache = []
cache_lock = threading.Lock()
last_refresh = None
CACHE_MAX = 300
REFRESH_HOURS = 6

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}

RELEVANT_KW = [
    'menswear','men','graphic tee','t-shirt','streetwear','activewear','gym',
    'athletic','print','graphic','collection','drop','apparel','clothing',
    'performance','sportswear','gothic','skull','illustration','artwork',
    'design','aesthetic','vintage','retro','wash','dye','heavyweight',
    'oversized','merch','band','metal','fabric','textile','season','fashion',
    'runway','lookbook','silhouette','colour','color','palette','texture',
    'treatment','garment','cotton','nylon','technical','colourway',
    'collaboration','limited','release','archive','typography','slogan',
    'badge','sport','culture','editorial','style',
]

AVOID_KW = [
    "women's",'womenswear','handbag','heel','makeup','beauty','skincare',
    'jewellery','jewelry','bridal','baby','maternity','fragrance','lingerie','nail',
]

def is_relevant(text):
    if not text: return False
    tl = text.lower()
    if any(kw in tl for kw in AVOID_KW): return False
    return any(kw in tl for kw in RELEVANT_KW)

def clean_img_url(url):
    if not url: return None
    url = url.strip()
    if url.startswith('//'): url = 'https:' + url
    if not url.startswith('http'): return None
    skip = ['logo','icon','avatar','favicon','sprite','placeholder','1x1','pixel','tracking','/ads/']
    if any(s in url.lower() for s in skip): return None
    if not re.search(r'\.(jpg|jpeg|png|webp)(\?|$)', url.lower()):
        if not any(x in url.lower() for x in ['image','photo','media','cdn','upload']): return None
    return url

def make_id(url):
    return hashlib.md5(url.encode()).hexdigest()[:10]

def parse_rss(url, source_name, layer, category, max_items=6):
    items = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.content, 'xml')
        entries = soup.find_all('item') or soup.find_all('entry')
        for entry in entries:
            if len(items) >= max_items: break
            title = entry.find('title')
            title = title.get_text(strip=True) if title else ''
            desc_el = entry.find('description') or entry.find('summary') or entry.find('content')
            summary = BeautifulSoup(desc_el.get_text() if desc_el else '', 'html.parser').get_text()[:300]
            link_el = entry.find('link')
            link = (link_el.get('href') or link_el.get_text(strip=True)) if link_el else ''
            if not is_relevant(title + ' ' + summary): continue
            img_url = None
            # media:content
            for tag in ['media:content','media:thumbnail']:
                m = entry.find(tag)
                if m and m.get('url'):
                    img_url = clean_img_url(m.get('url')); break
            # enclosure
            if not img_url:
                enc = entry.find('enclosure')
                if enc and 'image' in enc.get('type',''):
                    img_url = clean_img_url(enc.get('url',''))
            # img in description HTML
            if not img_url and desc_el:
                dsoup = BeautifulSoup(str(desc_el), 'html.parser')
                for img in dsoup.find_all('img'):
                    u = clean_img_url(img.get('src') or img.get('data-src',''))
                    if u: img_url = u; break
            # og:image fetch from article page
            if not img_url and link and len(items) < 3:
                try:
                    r = requests.get(link, headers=HEADERS, timeout=8)
                    pg = BeautifulSoup(r.text, 'html.parser')
                    og = pg.find('meta', property='og:image') or pg.find('meta', attrs={'name':'twitter:image'})
                    if og: img_url = clean_img_url(og.get('content',''))
                except: pass
            if img_url:
                items.append({'id':make_id(img_url),'img':img_url,'title':title[:80],
                    'source':source_name,'layer':layer,'category':category,
                    'url':link,'scraped':datetime.now().isoformat()})
        logger.info(f"{source_name}: {len(items)}")
    except Exception as e:
        logger.error(f"{source_name}: {e}")
    return items

def scrape_page(url, source_name, layer, category, max_imgs=6):
    items = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(resp.text, 'html.parser')
        og = soup.find('meta', property='og:image')
        if og:
            u = clean_img_url(og.get('content',''))
            if u:
                t = soup.find('title')
                items.append({'id':make_id(u),'img':u,'title':t.get_text()[:80] if t else source_name,
                    'source':source_name,'layer':layer,'category':category,
                    'url':url,'scraped':datetime.now().isoformat()})
        for img in soup.find_all('img'):
            if len(items) >= max_imgs: break
            src = img.get('src') or img.get('data-src') or img.get('data-lazy-src') or img.get('data-original','')
            u = clean_img_url(src)
            if not u: continue
            alt = img.get('alt','')
            if not is_relevant(alt + ' ' + source_name + ' ' + category): continue
            try:
                w = img.get('width','500')
                if int(str(w).replace('px','')) < 150: continue
            except: pass
            img_id = make_id(u)
            if not any(i['id'] == img_id for i in items):
                items.append({'id':img_id,'img':u,'title':alt[:80] or source_name,
                    'source':source_name,'layer':layer,'category':category,
                    'url':url,'scraped':datetime.now().isoformat()})
    except Exception as e:
        logger.error(f"{source_name} page: {e}")
    return items

def build_image_cache():
    global image_cache, last_refresh
    logger.info("Building cache...")
    all_images = []
    sources = [
        ('rss','https://www.vogue.com/feed/rss','Vogue Runway','L1','Runway Signal',8),
        ('rss','https://www.dezeen.com/design/fashion/feed/','Dezeen Fashion','L1','Material Innovation',6),
        ('rss','https://www.sportswear-international.com/rss','Sportswear International','L1','Activewear Trade',6),
        ('rss','https://www.kerrang.com/rss','Kerrang','L2','Music + Merch',8),
        ('rss','https://metalinjection.net/feed','Metal Injection','L2','Metal Visual Culture',8),
        ('rss','https://daily.bandcamp.com/feed','Bandcamp Daily','L2','Underground Signal',6),
        ('rss','https://www.itsnicethat.com/rss',"It's Nice That",'L2','Illustration + Graphic Design',8),
        ('rss','https://consequenceofsound.net/feed','Consequence of Sound','L2','Music Culture',6),
        ('rss','https://hypebeast.com/feed','Hypebeast','L3','Streetwear Culture',10),
        ('rss','https://www.highsnobiety.com/feed/','Highsnobiety','L3','Premium Streetwear',10),
        ('rss','https://www.complex.com/rss/style','Complex Style','L3','Urban Culture',8),
        ('rss','https://www.gq.com/feed/rss','GQ','L3','Mainstream Menswear',6),
        ('rss','https://www.businessoffashion.com/rss/news','Business of Fashion','L3','Industry Intelligence',6),
        ('page','https://www.acclaimmagazine.com/category/style/','Acclaim Magazine','L3','AU Streetwear',6),
        ('rss','https://news.nike.com/feed','Nike News','L3','Performance Direction',6),
        ('rss','https://blog.gymshark.com/rss.xml','Gymshark Blog','L4','Gym Competitor',8),
        ('rss','https://www.endclothing.com/au/journal/rss','END Clothing','L4','Premium AU Retail',8),
        ('rss','https://blog.culturekings.com.au/feed','Culture Kings','L4','Direct AU Competitor',8),
        ('page','https://www.grailed.com/drycleanonly','Grailed Editorial','L4','Resale Value Signal',6),
        ('page','https://www.doverstreetmarket.com/magazine','Dover Street Market','L4','Highest Signal Retail',6),
    ]
    seen_ids = set()
    for s in sources:
        try:
            stype, url, name, layer, cat, mx = s
            items = parse_rss(url, name, layer, cat, mx) if stype == 'rss' else scrape_page(url, name, layer, cat, mx)
            for item in items:
                if item['id'] not in seen_ids:
                    seen_ids.add(item['id'])
                    all_images.append(item)
            time.sleep(random.uniform(0.5, 1.5))
        except Exception as e:
            logger.error(f"Source failed {s[2]}: {e}")
    random.shuffle(all_images)
    all_images = all_images[:CACHE_MAX]
    with cache_lock:
        image_cache = all_images
        last_refresh = datetime.now()
    logger.info(f"Cache done: {len(image_cache)} images")
    return all_images

def get_or_refresh_cache():
    global last_refresh
    if not image_cache or last_refresh is None or datetime.now() - last_refresh > timedelta(hours=REFRESH_HOURS):
        t = threading.Thread(target=build_image_cache, daemon=True)
        t.start()
        if not image_cache: t.join(timeout=60)
    return image_cache

@app.route('/')
def health():
    return jsonify({'status':'ok','service':'Visual Swipe API','version':'2.0',
        'cached_images':len(image_cache),'last_refresh':last_refresh.isoformat() if last_refresh else None})

@app.route('/images')
def get_images():
    count = min(int(request.args.get('count',50)), 100)
    offset = int(request.args.get('offset',0))
    layer = request.args.get('layer', None)
    cache = get_or_refresh_cache()
    filtered = [i for i in cache if i.get('layer') == layer] if layer else cache
    if offset >= len(filtered): offset = 0
    batch = filtered[offset:offset+count]
    if len(batch) < count: batch += filtered[:count-len(batch)]
    return jsonify({'images':batch,'total':len(filtered),'offset':offset,
        'next_offset':(offset+count) % max(len(filtered),1),
        'refreshed':last_refresh.isoformat() if last_refresh else None})

@app.route('/refresh', methods=['POST'])
def force_refresh():
    threading.Thread(target=build_image_cache, daemon=True).start()
    return jsonify({'status':'refresh started'})

@app.route('/status')
def status():
    return jsonify({'cached':len(image_cache),
        'last_refresh':last_refresh.isoformat() if last_refresh else None,'sources':20})

if __name__ == '__main__':
    build_image_cache()
    app.run(host='0.0.0.0', port=5000, debug=False)
