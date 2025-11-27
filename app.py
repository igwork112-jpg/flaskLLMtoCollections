from flask import Flask, render_template, request, jsonify, session, Response, stream_with_context
import requests
import openai
import json
import os
import time
import uuid
import re
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
        
        # STEP 1: Get collection names from AI
        print("Step 1: Getting collection categories...")
        sample_size = min(50, total_products)
        sample_titles = "\n".join([f"{i+1}. {products[i]['title']}" for i in range(sample_size)])
        
        collection_prompt = f"""Analyze these products and suggest 5-12 collection categories.

{sample_titles}

Return ONLY a JSON array: ["Category 1", "Category 2", ...]"""

        try:
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "Return ONLY a JSON array."},
                    {"role": "user", "content": collection_prompt}
                ],
                temperature=0.3,
                max_tokens=300
            )
            
            result = response.choices[0].message.content.strip()
            if "```" in result:
                result = result.split("```")[1].replace("json", "").strip()
            
            suggested_collections = json.loads(result)
            print(f"✓ Got {len(suggested_collections)} collections")
        except Exception as e:
            print(f"⚠️ Using default collections")
            suggested_collections = ["General Products"]
        
        # STEP 2: Initialize tracking - ONE product = ONE collection
        print(f"\nStep 2: Classifying {total_products} products...")
        
        # Create empty collections
        collections_dict = {name: [] for name in suggested_collections}
        collections_dict["Other"] = []
        
        # Track assignments: product_idx -> collection_name
        product_to_collection = {}
        
        # Process in batches (adaptive batch size for very large datasets)
        if total_products > 1000:
            batch_size = 100  # Larger batches for big datasets (faster)
        else:
            batch_size = 50
        
        total_batches = (total_products + batch_size - 1) // batch_size
        
        for batch_num in range(total_batches):
            batch_start = batch_num * batch_size
            batch_end = min(batch_start + batch_size, total_products)
            batch_count = batch_end - batch_start
            
            print(f"\nBatch {batch_num + 1}/{total_batches}: Products {batch_start + 1} to {batch_end}")
            
            # Build prompt with product numbers
            batch_lines = []
            for i in range(batch_count):
                idx = batch_start + i + 1
                batch_lines.append(f"{idx}. {products[batch_start + i]['title']}")
            batch_text = "\n".join(batch_lines)
            
            prompt = f"""CRITICAL: Assign each product to EXACTLY ONE collection. Each product number must appear ONLY ONCE.

Available collections: {json.dumps(list(collections_dict.keys()))}

Products to classify:
{batch_text}

Return JSON mapping each product NUMBER to ONE collection name:
{{"1": "Collection Name", "2": "Collection Name", ...}}

RULES:
- Each product number ({batch_start + 1} to {batch_end}) must appear EXACTLY ONCE
- Choose the BEST MATCHING collection for each product
- Do NOT put the same product in multiple collections"""

            # Get AI response
            ai_response = {}
            try:
                resp = openai.ChatCompletion.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": "You are a product classifier. Return ONLY a JSON object mapping product numbers to collection names. Format: {\"1\": \"Collection Name\", \"2\": \"Collection Name\"}. Each product number must appear exactly once."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.1,
                    max_tokens=1500
                )
                
                text = resp.choices[0].message.content.strip()
                
                # Extract JSON from markdown
                if "```json" in text:
                    text = text.split("```json")[1].split("```")[0].strip()
                elif "```" in text:
                    text = text.split("```")[1].split("```")[0].strip()
                
                # Clean up common JSON issues
                text = text.replace(",\n}", "\n}")  # Trailing comma in object
                text = text.replace(",\n]", "\n]")  # Trailing comma in array
                text = text.replace(",}", "}")      # Trailing comma before }
                text = text.replace(",]", "]")      # Trailing comma before ]
                
                # Remove any trailing commas before closing braces (more aggressive)
                text = re.sub(r',(\s*[}\]])', r'\1', text)
                
                ai_response = json.loads(text)
                
                # Validate AI response
                expected_range = set(range(batch_start + 1, batch_end + 1))
                ai_products = [int(k) for k in ai_response.keys() if k.isdigit()]
                ai_products_set = set(ai_products)
                
                # Check for products outside batch range
                unexpected = ai_products_set - expected_range
                if unexpected:
                    print(f"  ⚠️ AI returned products outside batch range: {sorted(unexpected)[:10]}")
                    # Remove them
                    for prod in unexpected:
                        ai_response.pop(str(prod), None)
                
                # Check for duplicates in AI response (shouldn't happen but let's verify)
                if len(ai_products) != len(ai_products_set):
                    duplicates_in_response = len(ai_products) - len(ai_products_set)
                    print(f"  ⚠️ AI returned {duplicates_in_response} duplicate product numbers in response")
                
            except json.JSONDecodeError as e:
                print(f"  ⚠️ JSON parsing failed: {e}")
                print(f"  Problematic JSON (first 500 chars): {text[:500] if 'text' in locals() else 'N/A'}")
                ai_response = {}
            except Exception as e:
                print(f"  ⚠️ AI call failed: {e}")
                ai_response = {}
            
            # Process EACH product in this batch
            for i in range(batch_count):
                product_idx = batch_start + i + 1
                
                # Get collection from AI or use fallback
                collection = ai_response.get(str(product_idx), "Other")
                if collection not in collections_dict:
                    collection = "Other"
                
                # CRITICAL: Only assign if not already assigned
                if product_idx not in product_to_collection:
                    product_to_collection[product_idx] = collection
                    collections_dict[collection].append(product_idx)
                else:
                    print(f"  ⚠️ Product {product_idx} already assigned, skipping")
            
            print(f"  ✓ Assigned {batch_count} products")
            print(f"  Total assigned: {len(product_to_collection)}/{total_products}")
            
            # Rate limiting (adaptive based on dataset size)
            if total_batches > 20:
                time.sleep(1)  # Longer delay for very large datasets
            elif total_batches > 5:
                time.sleep(0.5)
        
        # STEP 3: Verification
        print(f"\nStep 3: Verification...")
        
        # Check for missing products
        missing = []
        for i in range(1, total_products + 1):
            if i not in product_to_collection:
                missing.append(i)
                product_to_collection[i] = "Other"
                collections_dict["Other"].append(i)
        
        if missing:
            print(f"  ⚠️ Added {len(missing)} missing products to 'Other'")
        
        # Remove empty collections
        all_collections = {name: ids for name, ids in collections_dict.items() if ids}
        
        # CRITICAL: Final deduplication check
        seen_products = set()
        deduplicated_collections = {}
        
        for collection_name, indices in all_collections.items():
            unique_indices = []
            for idx in indices:
                if idx not in seen_products:
                    unique_indices.append(idx)
                    seen_products.add(idx)
                else:
                    print(f"  ⚠️ Removing duplicate: Product {idx} from '{collection_name}'")
            
            if unique_indices:
                deduplicated_collections[collection_name] = unique_indices
        
        all_collections = deduplicated_collections
        
        # Final count and verification
        total_assigned = sum(len(ids) for ids in all_collections.values())
        
        # Double-check for duplicates
        all_product_ids = []
        for ids in all_collections.values():
            all_product_ids.extend(ids)
        
        unique_count = len(set(all_product_ids))
        if unique_count != total_assigned:
            print(f"❌ CRITICAL: Duplicates detected! {total_assigned} total, {unique_count} unique")
            print(f"   Duplicates: {total_assigned - unique_count}")
        
        print(f"\n{'='*60}")
        print(f"CLASSIFICATION COMPLETE")
        print(f"{'='*60}")
        print(f"Input: {total_products} products")
        print(f"Output: {total_assigned} products ({unique_count} unique)")
        print(f"Collections: {len(all_collections)}")
        
        if total_assigned == total_products:
            print(f"✓ SUCCESS: All products classified!")
        else:
            print(f"❌ ERROR: Count mismatch!")
        
        print(f"{'='*60}\n")
        
        for name, ids in sorted(all_collections.items(), key=lambda x: len(x[1]), reverse=True):
            print(f"  {name}: {len(ids)} products")
        print()
        
        # Format for display
        formatted_collections = {}
        for collection_name, indices in all_collections.items():
            formatted_collections[collection_name] = [
                {"index": idx, "title": products[idx-1]["title"]}
                for idx in sorted(indices) if 1 <= idx <= len(products)
            ]
        
        # Log what we're storing
        print(f"[STORING] {len(all_collections)} collections with {total_assigned} total products")
        
        store_data('classified_collections', all_collections)
        
        # Verify what was stored
        verify_stored = get_data('classified_collections', {})
        verify_total = sum(len(ids) for ids in verify_stored.values())
        print(f"[VERIFIED] Storage contains {len(verify_stored)} collections with {verify_total} products")
        
        if verify_total != total_assigned:
            print(f"[ERROR] Storage corruption: Expected {total_assigned}, got {verify_total}")
        
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
            
            # Log what we retrieved
            retrieved_total = sum(len(ids) for ids in collections.values())
            print(f"\n[SHOPIFY UPDATE] Retrieved {len(collections)} collections with {retrieved_total} products")
            
            if not collections:
                yield f"data: {json.dumps({'type': 'error', 'message': 'No classification data'})}\n\n"
                return
            
            if not shop_url or not access_token:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Missing credentials'})}\n\n"
                return
            
            # CRITICAL: Deduplicate collections before processing
            seen_products = set()
            clean_collections = {}
            duplicates_removed = 0
            
            for collection_name, indices in collections.items():
                unique_indices = []
                for idx in indices:
                    if idx not in seen_products:
                        unique_indices.append(idx)
                        seen_products.add(idx)
                    else:
                        duplicates_removed += 1
                
                if unique_indices:
                    clean_collections[collection_name] = unique_indices
            
            collections = clean_collections
            
            if duplicates_removed > 0:
                yield f"data: {json.dumps({'type': 'info', 'message': f'Removed {duplicates_removed} duplicate products'})}\n\n"
            
            headers = {
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json"
            }
            
            success_count = 0
            total_products = len(seen_products)  # Use unique count
            
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
            
            search_data = response.json()
            if "custom_collections" not in search_data:
                print(f"Unexpected search response: {search_data}")
                if "errors" in search_data:
                    print(f"API Errors: {search_data['errors']}")
                if attempt < max_retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                return None
            
            collections = search_data.get("custom_collections", [])
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
            
            print(f"POST response status: {response.status_code}")
            
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 2))
                print(f"Rate limit hit, waiting {retry_after} seconds...")
                time.sleep(retry_after)
                continue
            
            response.raise_for_status()
            
            response_data = response.json()
            if "custom_collection" not in response_data:
                print(f"Unexpected response creating collection {collection_name}")
                print(f"Status: {response.status_code}, Response keys: {list(response_data.keys())}")
                if attempt < max_retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                return None
            
            collection_id = response_data["custom_collection"]["id"]
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
