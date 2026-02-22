from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import feedparser
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

# ── IMAGE CACHE ───────────────────────────────────────────────────────────────
# Stores up to 300 images, refreshed every 6 hours
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
    'menswear', 'men', 'graphic tee', 't-shirt', 'streetwear', 'activewear',
    'gym', 'athletic', 'print', 'graphic', 'collection', 'drop', 'apparel',
    'clothing', 'track', 'performance', 'sportswear', 'gothic', 'skull',
    'illustration', 'artwork', 'design', 'aesthetic', 'vintage', 'retro',
    'wash', 'dye', 'heavyweight', 'oversized', 'merch', 'band', 'metal',
    'fabric', 'textile', 'season', 'fashion', 'runway', 'lookbook',
    'silhouette', 'colour', 'color', 'palette', 'texture', 'treatment',
    'garment', 'cotton', 'nylon', 'technical', 'functional', 'colourway',
    'collaboration', 'limited', 'release', 'archive', 'typography',
    'slogan', 'badge', 'sport', 'culture', 'editorial', 'style',
]

AVOID_KW = [
    "women's", 'womenswear', 'handbag', 'heel', 'makeup', 'beauty',
    'skincare', 'jewellery', 'jewelry', 'bridal', 'baby', 'maternity',
    'fragrance', 'lingerie', 'nail', 'hair care',
]

MIN_IMG_SIZE = 200  # min width/height in URL hints

def is_relevant(text):
    if not text:
        return False
    tl = text.lower()
    if any(kw in tl for kw in AVOID_KW):
        return False
    return any(kw in tl for kw in RELEVANT_KW)

def clean_img_url(url):
    if not url:
        return None
    url = url.strip()
    if url.startswith('//'):
        url = 'https:' + url
    if not url.startswith('http'):
        return None
    # Filter out tiny images, icons, logos
    skip = ['logo', 'icon', 'avatar', 'favicon', 'sprite', 'placeholder',
            '1x1', 'pixel', 'tracking', 'ad.', '/ads/', 'banner_ad']
    if any(s in url.lower() for s in skip):
        return None
    # Must look like an image
    if not re.search(r'\.(jpg|jpeg|png|webp|gif)(\?|$)', url.lower()):
        if 'image' not in url.lower() and 'photo' not in url.lower() and 'media' not in url.lower():
            return None
    return url

def make_id(url):
    return hashlib.md5(url.encode()).hexdigest()[:10]

def scrape_rss_images(feed_url, source_name, layer, category, max_items=8):
    items = []
    try:
        feed = feedparser.parse(feed_url)
        cutoff = datetime.now() - timedelta(days=14)

        for entry in feed.entries:
            if len(items) >= max_items:
                break

            # Date filter
            try:
                from dateutil import parser as dp
                pub = dp.parse(entry.get('published', ''))
                if pub.replace(tzinfo=None) < cutoff:
                    continue
            except:
                pass

            title = entry.get('title', '')
            summary = BeautifulSoup(entry.get('summary', ''), 'html.parser').get_text()
            link = entry.get('link', '')

            if not is_relevant(title + ' ' + summary):
                continue

            # Find images in entry
            img_url = None

            # Check media content
            for media in entry.get('media_content', []):
                u = clean_img_url(media.get('url', ''))
                if u:
                    img_url = u
                    break

            # Check enclosures
            if not img_url:
                for enc in entry.get('enclosures', []):
                    if 'image' in enc.get('type', ''):
                        u = clean_img_url(enc.get('href', ''))
                        if u:
                            img_url = u
                            break

            # Parse summary HTML for img tags
            if not img_url:
                soup = BeautifulSoup(entry.get('summary', '') + entry.get('content', [{}])[0].get('value', '') if entry.get('content') else entry.get('summary', ''), 'html.parser')
                for img in soup.find_all('img'):
                    src = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
                    u = clean_img_url(src)
                    if u:
                        img_url = u
                        break

            # Check og:image by fetching the article page (limited)
            if not img_url and link and len(items) < 4:
                try:
                    r = requests.get(link, headers=HEADERS, timeout=8)
                    soup = BeautifulSoup(r.text, 'html.parser')
                    og = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'twitter:image'})
                    if og:
                        u = clean_img_url(og.get('content', ''))
                        if u:
                            img_url = u
                except:
                    pass

            if img_url:
                items.append({
                    'id': make_id(img_url),
                    'img': img_url,
                    'title': title[:80],
                    'source': source_name,
                    'layer': layer,
                    'category': category,
                    'url': link,
                    'scraped': datetime.now().isoformat(),
                })

        logger.info(f"{source_name}: {len(items)} images")
    except Exception as e:
        logger.error(f"{source_name} error: {e}")

    return items


def scrape_page_images(page_url, source_name, layer, category, title_hint='', max_imgs=6):
    """Scrape editorial images directly from a page"""
    items = []
    try:
        resp = requests.get(page_url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(resp.text, 'html.parser')

        # Get og:image first
        og = soup.find('meta', property='og:image')
        if og:
            u = clean_img_url(og.get('content', ''))
            if u:
                items.append({
                    'id': make_id(u),
                    'img': u,
                    'title': title_hint or soup.find('title', ).get_text()[:80] if soup.find('title') else source_name,
                    'source': source_name,
                    'layer': layer,
                    'category': category,
                    'url': page_url,
                    'scraped': datetime.now().isoformat(),
                })

        # Get article/editorial images
        for img in soup.find_all('img'):
            if len(items) >= max_imgs:
                break
            src = (img.get('src') or img.get('data-src') or
                   img.get('data-lazy-src') or img.get('data-original', ''))
            u = clean_img_url(src)
            if not u:
                continue
            alt = img.get('alt', '')
            if not is_relevant(alt + ' ' + title_hint + ' ' + source_name):
                continue
            # Skip small images
            w = img.get('width', '500')
            try:
                if int(str(w).replace('px', '')) < 150:
                    continue
            except:
                pass
            img_id = make_id(u)
            if not any(i['id'] == img_id for i in items):
                items.append({
                    'id': img_id,
                    'img': u,
                    'title': alt[:80] or title_hint[:80] or source_name,
                    'source': source_name,
                    'layer': layer,
                    'category': category,
                    'url': page_url,
                    'scraped': datetime.now().isoformat(),
                })

    except Exception as e:
        logger.error(f"Page scrape {source_name}: {e}")

    return items


def build_image_cache():
    """Full scrape across all 20 sources"""
    global image_cache, last_refresh
    logger.info("Starting full image cache build...")
    all_images = []

    sources = [
        # ── LAYER 1: TRADE + RUNWAY ───────────────────────────────────────
        {
            'type': 'rss',
            'url': 'https://www.vogue.com/feed/rss',
            'name': 'Vogue Runway',
            'layer': 'L1',
            'category': 'Runway Signal',
            'max': 8,
        },
        {
            'type': 'rss',
            'url': 'https://www.dezeen.com/design/fashion/feed/',
            'name': 'Dezeen Fashion',
            'layer': 'L1',
            'category': 'Material Innovation',
            'max': 6,
        },
        {
            'type': 'rss',
            'url': 'https://www.sportswear-international.com/rss',
            'name': 'Sportswear International',
            'layer': 'L1',
            'category': 'Activewear Trade',
            'max': 6,
        },

        # ── LAYER 2: SUBCULTURE ───────────────────────────────────────────
        {
            'type': 'rss',
            'url': 'https://www.kerrang.com/rss',
            'name': 'Kerrang',
            'layer': 'L2',
            'category': 'Music + Merch',
            'max': 8,
        },
        {
            'type': 'rss',
            'url': 'https://metalinjection.net/feed',
            'name': 'Metal Injection',
            'layer': 'L2',
            'category': 'Metal Visual Culture',
            'max': 8,
        },
        {
            'type': 'rss',
            'url': 'https://www.revolvermag.com/rss.xml',
            'name': 'Revolver Magazine',
            'layer': 'L2',
            'category': 'Heavy Music Visual',
            'max': 6,
        },
        {
            'type': 'rss',
            'url': 'https://daily.bandcamp.com/feed',
            'name': 'Bandcamp Daily',
            'layer': 'L2',
            'category': 'Underground Signal',
            'max': 6,
        },
        {
            'type': 'rss',
            'url': 'https://www.itsnicethat.com/rss',
            'name': "It's Nice That",
            'layer': 'L2',
            'category': 'Illustration + Graphic Design',
            'max': 8,
        },

        # ── LAYER 3: MARKET + CULTURE ─────────────────────────────────────
        {
            'type': 'rss',
            'url': 'https://hypebeast.com/feed',
            'name': 'Hypebeast',
            'layer': 'L3',
            'category': 'Streetwear Culture',
            'max': 10,
        },
        {
            'type': 'rss',
            'url': 'https://www.highsnobiety.com/feed/',
            'name': 'Highsnobiety',
            'layer': 'L3',
            'category': 'Premium Streetwear',
            'max': 10,
        },
        {
            'type': 'rss',
            'url': 'https://www.complex.com/rss/style',
            'name': 'Complex Style',
            'layer': 'L3',
            'category': 'Urban Culture',
            'max': 8,
        },
        {
            'type': 'rss',
            'url': 'https://www.gq.com/feed/rss',
            'name': 'GQ',
            'layer': 'L3',
            'category': 'Mainstream Menswear',
            'max': 6,
        },
        {
            'type': 'page',
            'url': 'https://www.acclaimmagazine.com/category/style/',
            'name': 'Acclaim Magazine',
            'layer': 'L3',
            'category': 'AU Streetwear',
            'max': 6,
        },
        {
            'type': 'rss',
            'url': 'https://news.nike.com/feed',
            'name': 'Nike News',
            'layer': 'L3',
            'category': 'Performance Direction',
            'max': 6,
        },

        # ── LAYER 4: COMPETITOR + RETAIL ──────────────────────────────────
        {
            'type': 'rss',
            'url': 'https://blog.gymshark.com/rss.xml',
            'name': 'Gymshark Blog',
            'layer': 'L4',
            'category': 'Gym Competitor',
            'max': 8,
        },
        {
            'type': 'rss',
            'url': 'https://www.endclothing.com/au/journal/rss',
            'name': 'END Clothing',
            'layer': 'L4',
            'category': 'Premium AU Retail',
            'max': 8,
        },
        {
            'type': 'rss',
            'url': 'https://blog.culturekings.com.au/feed',
            'name': 'Culture Kings',
            'layer': 'L4',
            'category': 'Direct AU Competitor',
            'max': 8,
        },
        {
            'type': 'page',
            'url': 'https://www.grailed.com/drycleanonly',
            'name': 'Grailed Editorial',
            'layer': 'L4',
            'category': 'Resale Value Signal',
            'max': 6,
        },
        {
            'type': 'page',
            'url': 'https://www.doverstreetmarket.com/magazine',
            'name': 'Dover Street Market',
            'layer': 'L4',
            'category': 'Highest Signal Retail',
            'max': 6,
        },
        {
            'type': 'rss',
            'url': 'https://www.businessoffashion.com/rss/news',
            'name': 'Business of Fashion',
            'layer': 'L4',
            'category': 'Industry Intelligence',
            'max': 6,
        },
    ]

    seen_ids = set()
    for source in sources:
        try:
            if source['type'] == 'rss':
                items = scrape_rss_images(
                    source['url'], source['name'],
                    source['layer'], source['category'], source.get('max', 6)
                )
            else:
                items = scrape_page_images(
                    source['url'], source['name'],
                    source['layer'], source['category'], max_imgs=source.get('max', 6)
                )

            for item in items:
                if item['id'] not in seen_ids:
                    seen_ids.add(item['id'])
                    all_images.append(item)

            time.sleep(random.uniform(0.5, 1.5))
        except Exception as e:
            logger.error(f"Source failed {source['name']}: {e}")

    random.shuffle(all_images)
    all_images = all_images[:CACHE_MAX]

    with cache_lock:
        image_cache = all_images
        last_refresh = datetime.now()

    logger.info(f"Cache built: {len(image_cache)} images from {len(sources)} sources")
    return all_images


def get_or_refresh_cache():
    global last_refresh
    needs_refresh = (
        not image_cache or
        last_refresh is None or
        datetime.now() - last_refresh > timedelta(hours=REFRESH_HOURS)
    )
    if needs_refresh:
        thread = threading.Thread(target=build_image_cache, daemon=True)
        thread.start()
        if not image_cache:
            thread.join(timeout=60)
    return image_cache


# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.route('/')
def health():
    return jsonify({
        'status': 'ok',
        'service': 'Visual Swipe API',
        'version': '1.0',
        'cached_images': len(image_cache),
        'last_refresh': last_refresh.isoformat() if last_refresh else None,
    })

@app.route('/images')
def get_images():
    """Return a batch of images for swiping"""
    count = min(int(request.args.get('count', 50)), 100)
    offset = int(request.args.get('offset', 0))
    layer = request.args.get('layer', None)  # optional filter

    cache = get_or_refresh_cache()

    if layer:
        filtered = [i for i in cache if i.get('layer') == layer]
    else:
        filtered = cache

    # Rotate through cache
    if offset >= len(filtered):
        offset = 0

    batch = filtered[offset:offset + count]
    if len(batch) < count:
        batch += filtered[:count - len(batch)]

    return jsonify({
        'images': batch,
        'total': len(filtered),
        'offset': offset,
        'next_offset': (offset + count) % len(filtered),
        'refreshed': last_refresh.isoformat() if last_refresh else None,
    })

@app.route('/refresh', methods=['POST'])
def force_refresh():
    """Force a cache refresh"""
    thread = threading.Thread(target=build_image_cache, daemon=True)
    thread.start()
    return jsonify({'status': 'refresh started', 'message': 'New images loading in background'})

@app.route('/status')
def status():
    return jsonify({
        'cached': len(image_cache),
        'last_refresh': last_refresh.isoformat() if last_refresh else None,
        'next_refresh': (last_refresh + timedelta(hours=REFRESH_HOURS)).isoformat() if last_refresh else None,
        'sources': 20,
    })


if __name__ == '__main__':
    logger.info("Building initial image cache...")
    build_image_cache()
    app.run(host='0.0.0.0', port=5000, debug=False)
