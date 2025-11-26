from flask import Flask, render_template, request, jsonify, session, Response, stream_with_context
import requests
import openai
import json
import os
import time
import uuid
from datetime import timedelta, datetime
from dotenv import load_dotenv
from threading import Lock

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

# Get OpenAI key from environment
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')

# In-memory data store (backup solution for large data)
# This avoids cookie size limits completely
data_store = {}
store_lock = Lock()

def cleanup_old_sessions():
    """Remove sessions older than 24 hours"""
    with store_lock:
        now = datetime.now()
        expired = [sid for sid, data in data_store.items() 
                   if (now - data.get('created_at', now)).total_seconds() > 86400]
        for sid in expired:
            del data_store[sid]
            
def get_session_id():
    """Get or create session ID"""
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())
        session.permanent = True
    return session['session_id']

def store_data(key, value):
    """Store data in memory store"""
    cleanup_old_sessions()
    sid = get_session_id()
    with store_lock:
        if sid not in data_store:
            data_store[sid] = {'created_at': datetime.now()}
        data_store[sid][key] = value
        
def get_data(key, default=None):
    """Retrieve data from memory store"""
    sid = get_session_id()
    with store_lock:
        return data_store.get(sid, {}).get(key, default)

print(f"✓ In-memory data store initialized (no cookie size limits)")
print(f"✓ Session cleanup: automatic (24 hour expiry)")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/fetch-products', methods=['POST'])
def fetch_products():
    try:
        data = request.json
        shop_url = data.get('shop_url', '').strip()
        access_token = data.get('access_token', '').strip()
        tag = data.get('tag', '').strip()
        
        if not shop_url or not access_token or not tag:
            return jsonify({"success": False, "error": "Missing required fields"}), 400
        
        # Clean up shop URL - remove https://, http://, trailing slashes
        shop_url = shop_url.replace('https://', '').replace('http://', '').rstrip('/')
        
        # Store credentials in memory (not in cookie)
        store_data('shop_url', shop_url)
        store_data('access_token', access_token)
        
        url = f"https://{shop_url}/admin/api/2024-01/products.json"
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        }
        
        all_products = []
        params = {"limit": 250}
        page_count = 0
        max_pages = 100  # Support up to 25,000 products (way more than Shopify's limit)
        
        while page_count < max_pages:
            print(f"Fetching page {page_count + 1}...")
            response = requests.get(url, headers=headers, params=params, timeout=30)
            
            if response.status_code != 200:
                return jsonify({
                    "success": False, 
                    "error": f"Shopify API error: {response.status_code} - {response.text}"
                }), 400
            
            response_data = response.json()
            products = response_data.get("products", [])
            
            print(f"  Retrieved {len(products)} products from page {page_count + 1}")
            
            if not products:
                print("  No more products, stopping pagination")
                break
            
            # Filter by tag
            matched_on_page = 0
            for p in products:
                if p.get("tags"):
                    product_tags = [t.strip().lower() for t in p.get("tags", "").split(",")]
                    if tag.lower() in product_tags:
                        all_products.append({"id": p["id"], "title": p["title"]})
                        matched_on_page += 1
            
            print(f"  Matched {matched_on_page} products with tag '{tag}' on this page")
            print(f"  Total matched so far: {len(all_products)}")
            
            # Check pagination
            link_header = response.headers.get("Link", "")
            if not link_header or "rel=\"next\"" not in link_header:
                print("  No next page link, stopping pagination")
                break
            
            # Extract next page URL
            next_url = None
            for link in link_header.split(","):
                if "rel=\"next\"" in link:
                    next_url = link.split(";")[0].strip("<> ")
                    break
            
            if not next_url:
                print("  Could not parse next page URL, stopping pagination")
                break
                
            url = next_url
            params = {}
            page_count += 1
        
        print(f"Pagination complete: {page_count + 1} pages fetched, {len(all_products)} products matched")
        
        # Store products in memory (not in cookie)
        store_data('products', all_products)
        
        print(f"✓ Stored {len(all_products)} products in memory store")
        
        return jsonify({
            "success": True,
            "products": all_products,
            "count": len(all_products)
        })
        
    except requests.exceptions.Timeout:
        return jsonify({"success": False, "error": "Request timeout. Please try again."}), 400
    except requests.exceptions.RequestException as e:
        return jsonify({"success": False, "error": f"Network error: {str(e)}"}), 400
    except Exception as e:
        print(f"Error in fetch_products: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 400

@app.route('/api/classify', methods=['POST'])
def classify_products():
    try:
        products = get_data('products', [])
        
        print(f"DEBUG: Retrieved {len(products)} products from memory store")
        
        if not products:
            return jsonify({"success": False, "error": "No products found. Fetch products first."}), 400
        
        if not OPENAI_API_KEY:
            return jsonify({"success": False, "error": "OpenAI API key not configured. Add OPENAI_API_KEY to .env file"}), 400
        
        openai.api_key = OPENAI_API_KEY
        
        # Handle large product lists by batching
        batch_size = 200  # Process 200 products at a time
        all_collections = {}
        
        print(f"Classifying {len(products)} products in batches of {batch_size}...")
        
        for batch_start in range(0, len(products), batch_size):
            batch_end = min(batch_start + batch_size, len(products))
            batch_products = products[batch_start:batch_end]
            
            print(f"Processing batch {batch_start//batch_size + 1}: products {batch_start+1} to {batch_end}")
            
            # Create titles text for this batch
            titles_text = "\n".join([f"{batch_start + i + 1}. {p['title']}" for i, p in enumerate(batch_products)])
            
            prompt = f"""Analyze these product titles and group them into collections based on their primary category or product type.
Return a JSON object where keys are collection names and values are arrays of title numbers.

Titles:
{titles_text}

Example format:
{{
  "Bike Storage": [12, 14],
  "Flooring Tools": [2, 3, 4],
  "Storage Solutions": [1, 7]
}}

IMPORTANT: Use the exact numbers shown in the list above."""

            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are a product categorization expert. Return only valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3
            )
            
            result = response.choices[0].message.content.strip()
            
            if "```json" in result:
                result = result.split("```json")[1].split("```")[0].strip()
            elif "```" in result:
                result = result.split("```")[1].split("```")[0].strip()
                
            batch_collections = json.loads(result)
            
            # Merge batch results into all_collections
            for collection_name, indices in batch_collections.items():
                if collection_name not in all_collections:
                    all_collections[collection_name] = []
                all_collections[collection_name].extend(indices)
            
            print(f"  Batch complete: {len(batch_collections)} collections found")
        
        print(f"Classification complete: {len(all_collections)} total collections")
        
        # Format for display
        formatted_collections = {}
        for collection_name, indices in all_collections.items():
            formatted_collections[collection_name] = [
                {"index": idx, "title": products[idx-1]["title"]}
                for idx in indices if 1 <= idx <= len(products)
            ]
        
        store_data('classified_collections', all_collections)
        
        return jsonify({
            "success": True,
            "collections": formatted_collections,
            "batches_processed": (len(products) + batch_size - 1) // batch_size
        })
        
    except Exception as e:
        print(f"Classification error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 400

@app.route('/api/update-shopify-stream', methods=['GET'])
def update_shopify_stream():
    def generate():
        try:
            products = get_data('products', [])
            collections = get_data('classified_collections', {})
            shop_url = get_data('shop_url', '')
            access_token = get_data('access_token', '')
            
            if not collections:
                yield f"data: {json.dumps({'type': 'error', 'message': 'No classification data'})}\n\n"
                return
            
            if not shop_url or not access_token:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Missing credentials'})}\n\n"
                return
            
            headers = {
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json"
            }
            
            success_count = 0
            total_products = sum(len(indices) for indices in collections.values())
            
            yield f"data: {json.dumps({'type': 'start', 'total': total_products, 'collections': len(collections)})}\n\n"
            
            for collection_name, indices in collections.items():
                # Notify collection processing
                yield f"data: {json.dumps({'type': 'collection_start', 'name': collection_name, 'count': len(indices)})}\n\n"
                
                # Create or get collection
                collection_id = create_or_get_collection(collection_name, shop_url, headers)
                
                if not collection_id:
                    yield f"data: {json.dumps({'type': 'collection_error', 'name': collection_name})}\n\n"
                    continue
                
                yield f"data: {json.dumps({'type': 'collection_created', 'name': collection_name, 'id': collection_id})}\n\n"
                
                # Add products
                for idx in indices:
                    if 1 <= idx <= len(products):
                        product = products[idx - 1]
                        
                        if add_product_to_collection(product["id"], collection_id, shop_url, headers):
                            success_count += 1
                            result = json.dumps({'type': 'product_added', 'collection': collection_name, 'product': product['title'], 'status': 'success'})
                            yield f"data: {result}\n\n"
                        else:
                            result = json.dumps({'type': 'product_added', 'collection': collection_name, 'product': product['title'], 'status': 'failed'})
                            yield f"data: {result}\n\n"
            
            yield f"data: {json.dumps({'type': 'complete', 'success_count': success_count, 'total': total_products})}\n\n"
            
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    
    response = Response(stream_with_context(generate()), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    return response

def create_or_get_collection(collection_name, shop_url, headers):
    """Create or get collection with retry logic"""
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            # Search for existing collection
            search_url = f"https://{shop_url}/admin/api/2024-01/custom_collections.json"
            response = requests.get(search_url, headers=headers, timeout=30)
            
            # Handle rate limiting
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 2))
                print(f"Rate limit hit, waiting {retry_after} seconds...")
                time.sleep(retry_after)
                continue
            
            response.raise_for_status()
            
            collections = response.json().get("custom_collections", [])
            for col in collections:
                if col["title"].lower() == collection_name.lower():
                    print(f"Found existing collection: {collection_name} (ID: {col['id']})")
                    return col["id"]
            
            # Create new collection
            create_url = f"https://{shop_url}/admin/api/2024-01/custom_collections.json"
            payload = {
                "custom_collection": {
                    "title": collection_name,
                    "published": True
                }
            }
            
            time.sleep(0.5)  # Rate limiting
            response = requests.post(create_url, headers=headers, json=payload, timeout=30)
            
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 2))
                print(f"Rate limit hit, waiting {retry_after} seconds...")
                time.sleep(retry_after)
                continue
            
            response.raise_for_status()
            
            collection_id = response.json()["custom_collection"]["id"]
            print(f"Created new collection: {collection_name} (ID: {collection_id})")
            return collection_id
            
        except requests.exceptions.Timeout:
            print(f"Timeout creating collection {collection_name}, attempt {attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
                
        except Exception as e:
            print(f"Error creating/getting collection {collection_name}: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
    
    return None

def add_product_to_collection(product_id, collection_id, shop_url, headers):
    """Add product to collection with retry logic and rate limiting"""
    max_retries = 3
    retry_delay = 1  # seconds
    
    for attempt in range(max_retries):
        try:
            url = f"https://{shop_url}/admin/api/2024-01/collects.json"
            payload = {
                "collect": {
                    "product_id": product_id,
                    "collection_id": collection_id
                }
            }
            
            # Rate limiting: Shopify allows 2 req/sec
            time.sleep(0.5)  # 500ms delay = max 2 req/sec
            
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            
            # 422 = product already in collection (success)
            if response.status_code == 422:
                print(f"Product {product_id} already in collection {collection_id}")
                return True
            
            # 429 = rate limit hit, retry with longer delay
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 2))
                print(f"Rate limit hit, waiting {retry_after} seconds...")
                time.sleep(retry_after)
                continue
            
            response.raise_for_status()
            return True
            
        except requests.exceptions.Timeout:
            print(f"Timeout adding product {product_id}, attempt {attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))  # Exponential backoff
                continue
            return False
            
        except requests.exceptions.RequestException as e:
            print(f"Error adding product {product_id} to collection {collection_id}: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))
                continue
            return False
            
        except Exception as e:
            print(f"Unexpected error adding product {product_id}: {str(e)}")
            return False
    
    return False

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
