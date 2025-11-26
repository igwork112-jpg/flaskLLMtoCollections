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
        
        total_products = len(products)
        print(f"\n{'='*60}")
        print(f"STARTING CLASSIFICATION: {total_products} products")
        print(f"{'='*60}\n")
        
        # STEP 1: Get collection names from AI by analyzing a sample
        print("Step 1: Analyzing products to determine collection categories...")
        sample_size = min(50, total_products)
        sample_titles = "\n".join([f"{i+1}. {products[i]['title']}" for i in range(sample_size)])
        
        collection_prompt = f"""Analyze these product titles and suggest 5-15 logical collection categories.

Sample products:
{sample_titles}

Return ONLY a JSON array of collection names (no explanations):
["Collection Name 1", "Collection Name 2", "Collection Name 3"]

Make categories specific and relevant to the products shown."""

        try:
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are a product categorization expert. Return ONLY a JSON array."},
                    {"role": "user", "content": collection_prompt}
                ],
                temperature=0.3,
                max_tokens=500
            )
            
            result = response.choices[0].message.content.strip()
            if "```json" in result:
                result = result.split("```json")[1].split("```")[0].strip()
            elif "```" in result:
                result = result.split("```")[1].split("```")[0].strip()
            
            suggested_collections = json.loads(result)
            print(f"✓ Suggested {len(suggested_collections)} collections: {', '.join(suggested_collections[:5])}...")
        except Exception as e:
            print(f"⚠️ Could not get collection suggestions: {e}")
            suggested_collections = ["General Products"]
        
        # STEP 2: Process EVERY product - WE control the loop, not AI
        print(f"\nStep 2: Classifying all {total_products} products (manual tracking)...")
        
        # Initialize tracking
        all_collections = {name: [] for name in suggested_collections}
        all_collections["Other"] = []  # Fallback collection
        
        # CRITICAL: Create a list to track every single product
        product_assignments = {}  # {product_idx: collection_name}
        unprocessed_products = list(range(1, total_products + 1))  # [1, 2, 3, ..., N]
        
        # Process in batches for API efficiency
        batch_size = 50
        total_batches = (total_products + batch_size - 1) // batch_size
        
        for batch_num in range(total_batches):
            batch_start = batch_num * batch_size
            batch_end = min(batch_start + batch_size, total_products)
            
            print(f"\nBatch {batch_num + 1}/{total_batches}: Processing products {batch_start + 1}-{batch_end}")
            
            # Get AI suggestions for this batch
            batch_titles = "\n".join([f"{batch_start + i + 1}. {products[batch_start + i]['title']}" for i in range(batch_end - batch_start)])
            
            classify_prompt = f"""Assign each product to the BEST MATCHING collection from this list:
{json.dumps(list(all_collections.keys()))}

Products to classify:
{batch_titles}

Return a JSON object mapping each product NUMBER to its collection name:
{{
  "{batch_start + 1}": "Collection Name",
  "{batch_start + 2}": "Collection Name"
}}"""

            ai_assignments = {}
            try:
                response = openai.ChatCompletion.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": "Return ONLY valid JSON. Map every product number to a collection name."},
                        {"role": "user", "content": classify_prompt}
                    ],
                    temperature=0.2,
                    max_tokens=2000
                )
                
                result = response.choices[0].message.content.strip()
                if "```json" in result:
                    result = result.split("```json")[1].split("```")[0].strip()
                elif "```" in result:
                    result = result.split("```")[1].split("```")[0].strip()
                
                result = result.replace(",\n}", "\n}").replace(",\n]", "\n]")
                ai_assignments = json.loads(result)
            except Exception as e:
                print(f"  ⚠️ AI call failed: {e}")
                ai_assignments = {}
            
            # NOW: WE iterate through OUR list, not AI's response
            batch_processed = 0
            for i in range(batch_end - batch_start):
                product_idx = batch_start + i + 1
                product_key = str(product_idx)
                
                # Try to get AI's suggestion
                if product_key in ai_assignments:
                    collection_name = ai_assignments[product_key]
                    # Validate collection exists
                    if collection_name not in all_collections:
                        collection_name = "Other"
                else:
                    # AI didn't provide - use fallback
                    collection_name = "Other"
                
                # ASSIGN THIS PRODUCT (guaranteed)
                product_assignments[product_idx] = collection_name
                all_collections[collection_name].append(product_idx)
                unprocessed_products.remove(product_idx)
                batch_processed += 1
            
            progress_pct = (batch_end / total_products) * 100
            print(f"  ✓ Processed {batch_processed}/{batch_end - batch_start} products")
            print(f"  Remaining: {len(unprocessed_products)} products")
            print(f"  Progress: {batch_end}/{total_products} ({progress_pct:.1f}%)")
            
            # Rate limiting
            if total_batches > 5 and batch_num < total_batches - 1:
                time.sleep(0.5)
        
        # STEP 3: Final verification
        print(f"\nStep 3: Final verification...")
        
        # Check if any products were missed (should be impossible now)
        if unprocessed_products:
            print(f"❌ CRITICAL: {len(unprocessed_products)} products were not processed!")
            print(f"   Adding them to 'Other' collection...")
            for product_idx in unprocessed_products:
                all_collections["Other"].append(product_idx)
                product_assignments[product_idx] = "Other"
        
        # Remove empty collections
        all_collections = {name: indices for name, indices in all_collections.items() if indices}
        
        # Count assignments
        total_assigned = len(product_assignments)
        total_in_collections = sum(len(indices) for indices in all_collections.values())
        
        # Check for duplicates (shouldn't happen with our approach)
        all_indices_flat = []
        for indices in all_collections.values():
            all_indices_flat.extend(indices)
        
        if len(all_indices_flat) != len(set(all_indices_flat)):
            print(f"⚠️ WARNING: Duplicates detected in collections!")
            # Remove duplicates
            seen = set()
            for collection_name in list(all_collections.keys()):
                unique = []
                for idx in all_collections[collection_name]:
                    if idx not in seen:
                        unique.append(idx)
                        seen.add(idx)
                all_collections[collection_name] = unique
            total_in_collections = sum(len(indices) for indices in all_collections.values())
        
        print(f"\n{'='*60}")
        print(f"CLASSIFICATION COMPLETE")
        print(f"{'='*60}")
        print(f"Input products: {total_products}")
        print(f"Products tracked: {total_assigned}")
        print(f"Products in collections: {total_in_collections}")
        print(f"Unprocessed remaining: {len(unprocessed_products)}")
        print(f"Collections created: {len(all_collections)}")
        
        if total_in_collections == total_products:
            print(f"\n✓✓✓ SUCCESS: All {total_products} products classified! ✓✓✓")
        else:
            print(f"\n❌ ERROR: Mismatch detected!")
            print(f"   Expected: {total_products}")
            print(f"   Got: {total_in_collections}")
            print(f"   Difference: {abs(total_products - total_in_collections)}")
        
        print(f"{'='*60}\n")
        
        # Show collection summary
        print("Collection Summary:")
        for collection_name, indices in sorted(all_collections.items(), key=lambda x: len(x[1]), reverse=True):
            print(f"  {collection_name}: {len(indices)} products")
        print()
        
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
