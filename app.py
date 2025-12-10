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
from threading import Lock, Thread

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

# Background task manager
classification_tasks = {}
tasks_lock = Lock()

def get_task_id():
    """Generate unique task ID"""
    return str(uuid.uuid4())

def update_task_progress(task_id, status, progress=0, message="", data=None):
    """Update task progress"""
    with tasks_lock:
        if task_id not in classification_tasks:
            classification_tasks[task_id] = {}
        classification_tasks[task_id].update({
            'status': status,  # 'running', 'complete', 'error'
            'progress': progress,  # 0-100
            'message': message,
            'updated_at': datetime.now(),
            'data': data
        })

def get_task_status(task_id):
    """Get task status"""
    with tasks_lock:
        return classification_tasks.get(task_id, None)

def run_classification_background(task_id, products, user_collections, session_id):
    """Run classification in background thread"""
    try:
        update_task_progress(task_id, 'running', 0, 'Starting classification...')

        if not OPENAI_API_KEY:
            update_task_progress(task_id, 'error', 0, 'OpenAI API key not configured')
            return

        openai.api_key = OPENAI_API_KEY
        total_products = len(products)

        # Step 1: Handle collections
        if user_collections and len(user_collections) > 0:
            suggested_collections = user_collections
            update_task_progress(task_id, 'running', 5, f'Using {len(user_collections)} custom collections')

            parent_mapping = {}
            for col in suggested_collections:
                if " > " in col:
                    parent = col.split(" > ")[0]
                    parent_mapping[col] = parent

            # Store parent mapping in session
            with store_lock:
                if session_id not in data_store:
                    data_store[session_id] = {'created_at': datetime.now()}
                data_store[session_id]['parent_mapping'] = parent_mapping
        else:
            # Generate collections with AI
            update_task_progress(task_id, 'running', 5, 'Generating collections with AI...')

            # Reduce sample size to avoid token limits (16K for gpt-3.5-turbo-16k)
            sample_count = min(total_products, 200)
            all_titles = "\n".join([f"{i+1}. {products[i]['title']}" for i in range(sample_count)])

            collection_prompt = f"""You are analyzing {sample_count} construction/safety/traffic equipment products (sample from {total_products} total). Create a HIGHLY DETAILED collection structure with MANY specific subcategories.

CRITICAL REQUIREMENTS:
1. Create 8-15 PARENT categories based on main product types
2. For EACH parent, create 10-30 SPECIFIC subcategories
3. Target: 80-200+ total subcategories (the more specific, the better!)

Products to analyze:
{all_titles}

Return a JSON object with parent categories as keys, and arrays of specific subcategories as values."""

            try:
                response = openai.ChatCompletion.create(
                    model="gpt-3.5-turbo-16k",
                    messages=[
                        {"role": "system", "content": "You are an expert categorization specialist. Return ONLY valid JSON."},
                        {"role": "user", "content": collection_prompt}
                    ],
                    temperature=0.3,
                    max_tokens=4000,
                    request_timeout=180
                )

                result = response.choices[0].message.content.strip()
                if "```json" in result:
                    result = result.split("```json")[1].split("```")[0].strip()
                elif "```" in result:
                    result = result.split("```")[1].split("```")[0].strip()

                hierarchy = json.loads(result)
                suggested_collections = []
                parent_mapping = {}

                for parent, subcategories in hierarchy.items():
                    for subcat in subcategories:
                        full_name = f"{parent} > {subcat}"
                        suggested_collections.append(full_name)
                        parent_mapping[full_name] = parent

                # Store in session
                with store_lock:
                    if session_id not in data_store:
                        data_store[session_id] = {'created_at': datetime.now()}
                    data_store[session_id]['parent_mapping'] = parent_mapping

                update_task_progress(task_id, 'running', 10, f'Generated {len(suggested_collections)} collections')

            except Exception as e:
                update_task_progress(task_id, 'error', 0, f'AI generation failed: {str(e)}')
                return

        # Step 2: Classify products in batches
        BATCH_SIZE = 500
        total_batches = (total_products + BATCH_SIZE - 1) // BATCH_SIZE

        collections_dict = {name: [] for name in suggested_collections}
        product_to_collection = {}
        collections_list = json.dumps(list(collections_dict.keys()), indent=2)

        update_task_progress(task_id, 'running', 10, f'Processing {total_products} products in {total_batches} batches')

        for batch_num in range(total_batches):
            batch_start = batch_num * BATCH_SIZE + 1
            batch_end = min((batch_num + 1) * BATCH_SIZE, total_products)

            update_task_progress(task_id, 'running', 10 + int((batch_num / total_batches) * 85),
                               f'Batch {batch_num + 1}/{total_batches} (Products {batch_start}-{batch_end})')

            for idx in range(batch_start, batch_end + 1):
                product_title = products[idx - 1]['title']

                # Update progress every 10 products
                if idx % 10 == 0 or idx == batch_start:
                    percentage = 10 + int((idx / total_products) * 85)
                    update_task_progress(task_id, 'running', percentage,
                                       f'Classifying product {idx}/{total_products}: {product_title[:50]}...')

                prompt = f"""Classify this product into the MOST SPECIFIC matching collection.

Product: {product_title}

Available collections (format "Parent > Subcategory"):
{collections_list}

Return ONLY the exact collection name (with " > " format). No explanation, just the collection name."""

                try:
                    resp = openai.ChatCompletion.create(
                        model="gpt-3.5-turbo",
                        messages=[
                            {"role": "system", "content": "You are a product classification expert. Return ONLY the collection name from the provided list, nothing else."},
                            {"role": "user", "content": prompt}
                        ],
                        temperature=0.1,
                        max_tokens=100,
                        request_timeout=60
                    )

                    collection_name = resp.choices[0].message.content.strip().strip('"\'')

                    if collection_name in collections_dict:
                        product_to_collection[idx] = collection_name
                        collections_dict[collection_name].append(idx)
                    else:
                        fallback = max(collections_dict.items(), key=lambda x: len(x[1]) if x[1] else 0)[0]
                        product_to_collection[idx] = fallback
                        collections_dict[fallback].append(idx)

                    time.sleep(0.05)

                except Exception as e:
                    fallback = max(collections_dict.items(), key=lambda x: len(x[1]) if x[1] else 0)[0]
                    product_to_collection[idx] = fallback
                    collections_dict[fallback].append(idx)

        # Remove empty collections
        all_collections = {name: ids for name, ids in collections_dict.items() if ids}

        # Format for display
        formatted_collections = {}
        for collection_name, indices in all_collections.items():
            formatted_collections[collection_name] = [
                {"index": idx, "title": products[idx-1]["title"]}
                for idx in sorted(indices) if 1 <= idx <= len(products)
            ]

        # Store results in session
        with store_lock:
            if session_id not in data_store:
                data_store[session_id] = {'created_at': datetime.now()}
            data_store[session_id]['classified_collections'] = all_collections

        # Complete task
        update_task_progress(task_id, 'complete', 100, 'Classification complete!', {
            'collections': formatted_collections,
            'total_collections': len(all_collections),
            'total_products': len(products)
        })

    except Exception as e:
        update_task_progress(task_id, 'error', 0, str(e))

def run_shopify_update_background(task_id, products, collections, shop_url, access_token, session_id):
    """Run Shopify update with Smart Collections - faster and automatic!"""
    try:
        update_task_progress(task_id, 'running', 0, 'Starting Smart Collections update...')

        api_version = '2024-10'
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        }

        # Test permissions for Smart Collections
        test_url = f"https://{shop_url}/admin/api/{api_version}/smart_collections.json?limit=1"
        try:
            test_response = requests.get(test_url, headers=headers, timeout=30)
            if test_response.status_code == 403:
                update_task_progress(task_id, 'error', 0, 'Access token lacks permissions for Smart Collections')
                return
            elif test_response.status_code != 200:
                update_task_progress(task_id, 'error', 0, f'Cannot access Shopify API. Status: {test_response.status_code}')
                return
        except Exception as e:
            update_task_progress(task_id, 'error', 0, f'Connection test failed: {str(e)}')
            return

        # Deduplicate - each product goes to ONLY ONE collection
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
            update_task_progress(task_id, 'running', 5, f'Removed {duplicates_removed} duplicate products (one product = one collection)')

        total_products = len(seen_products)
        total_collections = len(collections)

        update_task_progress(task_id, 'running', 10, f'Step 1/2: Creating {total_collections} Smart Collections...')

        # STEP 1: Create Smart Collections (fast - no product assignment needed!)
        processed_collections = 0
        for collection_name in collections.keys():
            processed_collections += 1
            progress = 10 + int((processed_collections / total_collections) * 30)

            update_task_progress(task_id, 'running', progress,
                               f'Creating Smart Collection {processed_collections}/{total_collections}: {collection_name}')

            # Create Smart Collection with rule: product_type = collection_name
            collection_id = create_or_get_smart_collection(collection_name, shop_url, headers)

            if not collection_id:
                update_task_progress(task_id, 'running', progress,
                                   f'⚠️ Failed to create: {collection_name}')

        update_task_progress(task_id, 'running', 40, f'Step 2/2: Updating {total_products} products with metadata...')

        # STEP 2: Update product metadata (product_type + tags)
        # Smart Collections will auto-populate based on product_type!
        success_count = 0
        processed_products = 0

        for collection_name, indices in collections.items():
            for idx in indices:
                if 1 <= idx <= len(products):
                    processed_products += 1
                    product = products[idx - 1]

                    # Progress update every 10 products
                    if processed_products % 10 == 0:
                        progress = 40 + int((processed_products / total_products) * 55)
                        update_task_progress(task_id, 'running', progress,
                                           f'Updating product {processed_products}/{total_products}')

                    # Update product metadata (product_type = collection_name)
                    if update_product_metadata(product["id"], collection_name, product["title"], shop_url, headers):
                        success_count += 1

        # Complete
        update_task_progress(task_id, 'complete', 100, 'Smart Collections created! Products will auto-populate.', {
            'success_count': success_count,
            'total': total_products,
            'collections': total_collections,
            'message': 'Smart Collections automatically populate based on product_type. Check your Shopify store!'
        })

    except Exception as e:
        update_task_progress(task_id, 'error', 0, str(e))

print(f"✓ Background task manager initialized")

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
        
        api_version = '2024-10'  # Updated to latest stable version
        url = f"https://{shop_url}/admin/api/{api_version}/products.json"
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
            response = requests.get(url, headers=headers, params=params, timeout=60)  # Increased timeout
            
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

        # Check if user provided custom collections
        request_data = request.json or {}
        user_collections = request_data.get('user_collections', None)

        if user_collections and len(user_collections) > 0:
            # User provided collections - use them directly
            print(f"Step 1: Using {len(user_collections)} user-provided collections")
            print(f"  Collections: {user_collections[:5]}{'...' if len(user_collections) > 5 else ''}\n")

            suggested_collections = user_collections

            # Extract parent mapping if hierarchical format used
            parent_mapping = {}
            for col in suggested_collections:
                if " > " in col:
                    parent = col.split(" > ")[0]
                    parent_mapping[col] = parent

            store_data('parent_mapping', parent_mapping)
        else:
            # No user collections - generate automatically with AI
            print("Step 1: No custom collections provided - AI will generate them automatically\n")

        # STEP 1: Analyze ALL products and create MANY detailed collections (only if user didn't provide them)
        if not user_collections:
            print(f"  Analyzing {total_products} products to generate DETAILED collection hierarchy...")
            print(f"  Creating 50-150+ specific collections based on product types, sizes, and features...")

            # Reduce sample size to avoid token limits (16K for gpt-3.5-turbo-16k)
            sample_count = min(total_products, 200)
            all_titles = "\n".join([f"{i+1}. {products[i]['title']}" for i in range(sample_count)])
            print(f"  Analyzing {sample_count} products...")

            collection_prompt = f"""You are analyzing {sample_count} construction/safety/traffic equipment products (sample from {total_products} total). Create a HIGHLY DETAILED collection structure with MANY specific subcategories.

CRITICAL REQUIREMENTS:
1. Create 8-15 PARENT categories based on main product types
2. For EACH parent, create 10-30 SPECIFIC subcategories
3. Target: 80-200+ total subcategories (the more specific, the better!)
4. Use SIZE/CAPACITY as primary differentiator (500mm cones vs 750mm cones vs 1m cones)
5. Use PRODUCT TYPE variations (Traffic Cones, Water Tanks, Safety Signs, Barriers, etc.)
6. Use MATERIAL/CONSTRUCTION (Metal, Plastic, Aluminum, Steel, etc.)
7. Use CAPACITY for tanks (100L, 500L, 1000L, 5000L, etc.)
8. Use SPECIFIC FEATURES (Bunded, Stackable, Heavy Duty, Premium, etc.)

ANALYSIS STRATEGY FOR THIS CATALOG:
- Traffic Cones → Separate by HEIGHT (460mm, 500mm, 750mm, 1m, etc.)
- Water Tanks → Separate by CAPACITY (500L, 1000L, 5000L, Underground, Bunded, etc.)
- Safety Signs → Separate by TYPE (Speed Limit Signs, Warning Signs, Directional, Quick-Fit, Metal, etc.)
- Barriers → Separate by TYPE (Water-Filled, Pedestrian, Crowd Control, Height Restrictors, etc.)
- Construction Equipment → Separate by CATEGORY (Buckets, Tubs, Tools, Flooring, etc.)
- Safety Gear → Separate by TYPE (Coveralls, Gloves, Boots, High-Vis, etc.)

EXAMPLES OF GOOD GRANULARITY:
Parent: "Traffic Cones"
  Subcategories: ["460mm Traffic Cones", "500mm Traffic Cones", "750mm Traffic Cones",
                  "1 Metre Traffic Cones", "Sand Weighted Traffic Cones", "Self-Weighted Traffic Cones",
                  "Chapter 8 Traffic Cones", "No Waiting Cones", "Cone Accessories"]

Parent: "Water Storage Tanks"
  Subcategories: ["100-500 Litre Water Tanks", "500-1000 Litre Water Tanks", "1000-2000 Litre Water Tanks",
                  "2000-5000 Litre Water Tanks", "5000-10000 Litre Water Tanks", "Underground Water Tanks",
                  "Bunded Water Tanks", "Potable Water Tanks", "Baffled Water Tanks", "Cylindrical Water Tanks"]

Parent: "Traffic Safety Signs"
  Subcategories: ["Speed Limit Signs (Metal)", "Speed Limit Signs (Quick-Fit)", "Warning Triangle Signs",
                  "Directional Arrow Signs", "Supplementary Plates", "Custom Signs", "Road Closure Signs",
                  "Pedestrian Signs", "Roadworks Signs", "Cone Mounted Signs"]

Parent: "Safety Barriers"
  Subcategories: ["Water-Filled Barriers 1m", "Water-Filled Barriers 2m", "Pedestrian Barriers",
                  "Crowd Control Barriers", "Height Restriction Barriers", "Temporary Fencing",
                  "Mesh Barriers", "Chapter 8 Barriers", "Barrier Accessories"]

Parent: "Construction Tools & Equipment"
  Subcategories: ["Mortar Tubs 100-300L", "Mortar Tubs 300-500L", "Builders Buckets",
                  "Flooring Tools", "Screeding Equipment", "Trowels & Hand Tools",
                  "Measuring Tools", "Earth Compaction Tools", "Asphalt Tools"]

Parent: "Safety & PPE"
  Subcategories: ["High-Visibility Jackets", "High-Visibility Bodywarmers", "Disposable Coveralls",
                  "Work Trousers", "Safety Boots", "Work Gloves", "Safety Helmets",
                  "First Aid Kits", "Eye Protection", "Respiratory Protection"]

Parent: "Site Equipment"
  Subcategories: ["Cable Ramps & Protectors", "Anti-Slip Matting", "Ground Protection Mats",
                  "Access Mats", "Flooring Sheets", "Scaffold Equipment", "Ladders & Steps",
                  "Storage Solutions", "Site Furniture", "Waste Management"]

Products to analyze:
{all_titles}

Return a JSON object with parent categories as keys, and arrays of specific subcategories as values:
{{
  "Traffic Cones": ["460mm Traffic Cones", "500mm Traffic Cones", "750mm Traffic Cones", ...],
  "Water Storage Tanks": ["100-500 Litre Tanks", "500-1000 Litre Tanks", "Underground Tanks", ...],
  "Traffic Safety Signs": ["Speed Limit Signs Metal", "Warning Signs", "Quick-Fit Signs", ...],
  ...
}}

CRITICAL SUCCESS CRITERIA:
1. Minimum 80 total subcategories (more is better - aim for 100-150+)
2. Be EXTREMELY SPECIFIC with sizes, capacities, and types
3. Separate by SIZE/CAPACITY first (e.g., "500mm Cones" vs "750mm Cones")
4. Then by TYPE/FEATURE (e.g., "Bunded Tanks" vs "Underground Tanks")
5. Each subcategory must be unique and highly descriptive
6. Look at product titles and identify all size/capacity variations
7. Create separate subcategories for different materials (Metal, Plastic, Aluminum)
8. Products will be assigned to subcategories, so make them VERY SPECIFIC!

REMEMBER: This is construction/safety equipment - use technical specifications!"""

            try:
                print(f"  Generating collection hierarchy from {sample_count} products...")
                print(f"  This may take 30-60 seconds...")
                response = openai.ChatCompletion.create(
                    model="gpt-3.5-turbo-16k",  # Use 16k model for larger responses
                    messages=[
                        {"role": "system", "content": "You are an expert construction/safety equipment categorization specialist. Create MANY highly specific subcategories (minimum 80, ideally 100-150+). Separate products by size, capacity, material, and type. Return ONLY valid JSON - no markdown, no explanations."},
                        {"role": "user", "content": collection_prompt}
                    ],
                    temperature=0.3,
                    max_tokens=4000,  # Large limit for many collections
                    request_timeout=180  # Increased to 3 minutes for large responses
                )

                result = response.choices[0].message.content.strip()

                # More robust JSON extraction
                if "```json" in result:
                    result = result.split("```json")[1].split("```")[0].strip()
                elif "```" in result:
                    result = result.split("```")[1].split("```")[0].strip()

                # Remove any leading/trailing whitespace and quotes
                result = result.strip().strip('`').strip()

                # Try to find JSON object bounds
                if not result.startswith('{'):
                    start_idx = result.find('{')
                    if start_idx != -1:
                        result = result[start_idx:]

                if not result.endswith('}'):
                    end_idx = result.rfind('}')
                    if end_idx != -1:
                        result = result[:end_idx+1]

                print(f"  Parsing AI response (length: {len(result)} chars)...")
                hierarchy = json.loads(result)

                # Flatten hierarchy into subcategories with parent prefix
                suggested_collections = []
                parent_mapping = {}  # Track which parent each subcategory belongs to

                for parent, subcategories in hierarchy.items():
                    for subcat in subcategories:
                        # Format: "Parent > Subcategory" for Shopify
                        full_name = f"{parent} > {subcat}"
                        suggested_collections.append(full_name)
                        parent_mapping[full_name] = parent

                print(f"✓ Got {len(hierarchy)} parent categories")
                print(f"✓ Got {len(suggested_collections)} total subcategories")

                # Validate we have enough collections
                if len(suggested_collections) < 50:
                    print(f"  ⚠️ WARNING: Only {len(suggested_collections)} collections created!")
                    print(f"  ⚠️ Expected 80-150+ for detailed categorization")
                    print(f"  ⚠️ Products may be grouped too broadly")
                elif len(suggested_collections) < 80:
                    print(f"  ⚠️ Got {len(suggested_collections)} collections (recommended: 80+)")
                else:
                    print(f"  ✓ Excellent! {len(suggested_collections)} collections will provide detailed organization")

                # Store parent mapping for later use
                store_data('parent_mapping', parent_mapping)

            except Exception as e:
                print(f"⚠️ Error getting hierarchical collections: {e}")
                print(f"⚠️ Using default construction/safety equipment structure")
                suggested_collections = [
                    "Traffic Cones > 460mm-500mm Cones", "Traffic Cones > 750mm Cones", "Traffic Cones > 1 Metre Cones",
                    "Water Tanks > Small Tanks (100-1000L)", "Water Tanks > Medium Tanks (1000-5000L)", "Water Tanks > Large Tanks (5000L+)",
                    "Water Tanks > Bunded Tanks", "Water Tanks > Underground Tanks",
                    "Traffic Signs > Speed Limit Signs", "Traffic Signs > Warning Signs", "Traffic Signs > Quick-Fit Signs",
                    "Traffic Signs > Metal Signs", "Traffic Signs > Supplementary Plates",
                    "Safety Barriers > Water-Filled Barriers", "Safety Barriers > Pedestrian Barriers", "Safety Barriers > Temporary Fencing",
                    "Safety Barriers > Height Restrictors", "Safety Barriers > Crowd Control",
                    "Construction Equipment > Mortar Tubs", "Construction Equipment > Builders Buckets", "Construction Equipment > Hand Tools",
                    "Safety PPE > High-Vis Clothing", "Safety PPE > Work Gloves", "Safety PPE > Safety Boots",
                    "Site Equipment > Cable Ramps", "Site Equipment > Anti-Slip Matting", "Site Equipment > Ground Protection"
                ]
                parent_mapping = {col: col.split(" > ")[0] for col in suggested_collections}
                store_data('parent_mapping', parent_mapping)
        
        # STEP 2: Classify products ONE BY ONE
        print(f"\nStep 2: Classifying {total_products} products one by one...")
        print(f"  Available collections: {len(suggested_collections)}")
        print(f"  This will take a few minutes...\n")

        # Create empty collections
        collections_dict = {name: [] for name in suggested_collections}

        # Track assignments
        product_to_collection = {}

        # Collections list for prompt (formatted nicely)
        collections_list = json.dumps(list(collections_dict.keys()), indent=2)

        # Process each product individually
        for idx in range(1, total_products + 1):
            product_title = products[idx - 1]['title']

            # Progress indicator every 50 products
            if idx % 50 == 0:
                print(f"  Progress: {idx}/{total_products} products classified ({int(idx/total_products*100)}%)")

            prompt = f"""Classify this product into the MOST SPECIFIC matching collection.

Product: {product_title}

Available collections (format "Parent > Subcategory"):
{collections_list}

Return ONLY the exact collection name (with " > " format). No explanation, just the collection name.

Consider:
- Demographics: Men's/Women's/Kids'/Unisex
- Style: Running/Casual/Formal/Athletic/Industrial/Professional
- Type and specific features

Be PRECISE and choose the most granular match."""

            try:
                resp = openai.ChatCompletion.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": "You are a product classification expert. Return ONLY the collection name from the provided list, nothing else."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.1,
                    max_tokens=100,
                    request_timeout=60  # Increased to 60 seconds for reliability
                )

                collection_name = resp.choices[0].message.content.strip().strip('"\'')

                # Validate collection exists
                if collection_name in collections_dict:
                    product_to_collection[idx] = collection_name
                    collections_dict[collection_name].append(idx)
                else:
                    # Fallback to most populated collection
                    fallback = max(collections_dict.items(), key=lambda x: len(x[1]) if x[1] else 0)[0]
                    product_to_collection[idx] = fallback
                    collections_dict[fallback].append(idx)
                    if idx % 50 == 0:  # Only log occasionally to reduce noise
                        print(f"    ⚠️ Product {idx} got invalid collection, using fallback")

                # Minimal rate limiting
                time.sleep(0.05)  # 50ms delay = ~20 requests/sec

            except Exception as e:
                # On error, use fallback
                fallback = max(collections_dict.items(), key=lambda x: len(x[1]) if x[1] else 0)[0]
                product_to_collection[idx] = fallback
                collections_dict[fallback].append(idx)
                if idx % 100 == 0:  # Only log occasionally
                    print(f"    ⚠️ Error on product {idx}, using fallback: {str(e)[:50]}")

        print(f"\n  ✓ Classified all {total_products} products!")

        # STEP 3: Verification
        print(f"\nStep 3: Verification...")

        # Check for missing products (shouldn't have any with one-by-one approach)
        missing = []
        for i in range(1, total_products + 1):
            if i not in product_to_collection:
                missing.append(i)

        if missing:
            print(f"  ⚠️ Found {len(missing)} unclassified products (unexpected!)")
            # Assign to most populated collection
            fallback = max(collections_dict.items(), key=lambda x: len(x[1]))[0]
            for product_idx in missing:
                product_to_collection[product_idx] = fallback
                collections_dict[fallback].append(product_idx)
            print(f"  ✓ Assigned {len(missing)} products to '{fallback}'")
        
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

        # Group by parent category for better display
        parent_breakdown = {}
        for name, ids in all_collections.items():
            if " > " in name:
                parent = name.split(" > ")[0]
                subcat = name.split(" > ")[1]
            else:
                parent = "Other"
                subcat = name

            if parent not in parent_breakdown:
                parent_breakdown[parent] = []
            parent_breakdown[parent].append((subcat, len(ids)))

        print("COLLECTION BREAKDOWN BY PARENT CATEGORY:\n")
        for parent in sorted(parent_breakdown.keys()):
            subcats = parent_breakdown[parent]
            total_in_parent = sum(count for _, count in subcats)
            print(f"📁 {parent} ({len(subcats)} subcategories, {total_in_parent} products)")
            for subcat, count in sorted(subcats, key=lambda x: x[1], reverse=True)[:10]:  # Show top 10
                print(f"   ├─ {subcat}: {count} products")
            if len(subcats) > 10:
                print(f"   └─ ... and {len(subcats) - 10} more subcategories")
            print()
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
            "total_collections": len(all_collections),
            "total_products": total_assigned
        })
        
    except Exception as e:
        print(f"Classification error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 400

@app.route('/api/classify-start', methods=['POST'])
def classify_start():
    """Start classification in background thread"""
    try:
        data = request.json or {}
        user_collections = data.get('user_collections', None)

        # Get products from session
        products = get_data('products', [])

        if not products:
            return jsonify({"success": False, "error": "No products found. Fetch products first."}), 400

        # Parse user collections
        if user_collections and len(user_collections) > 0:
            user_collections_list = user_collections
        else:
            user_collections_list = None

        # Create task
        task_id = get_task_id()
        session_id = get_session_id()

        # Store task ID in session
        store_data('current_task_id', task_id)

        # Start background thread
        thread = Thread(target=run_classification_background,
                       args=(task_id, products, user_collections_list, session_id))
        thread.daemon = True
        thread.start()

        return jsonify({
            "success": True,
            "task_id": task_id,
            "message": "Classification started in background"
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

@app.route('/api/classification-status', methods=['GET'])
def classification_status():
    """Get classification task status"""
    try:
        # Get task_id from query params or from session
        task_id = request.args.get('task_id') or get_data('current_task_id')

        if not task_id:
            return jsonify({"success": False, "error": "No task ID provided"}), 400

        task = get_task_status(task_id)

        if not task:
            return jsonify({"success": False, "error": "Task not found"}), 404

        return jsonify({
            "success": True,
            "task_id": task_id,
            "status": task['status'],
            "progress": task['progress'],
            "message": task['message'],
            "data": task.get('data'),
            "updated_at": task['updated_at'].isoformat()
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

@app.route('/api/classify-stream', methods=['GET'])
def classify_products_stream():
    def generate():
        try:
            print("[STREAM] Starting classification stream...")
            products = get_data('products', [])
            user_collections = get_data('user_collections_input', None)
            print(f"[STREAM] Products: {len(products)}, User collections: {len(user_collections) if user_collections else 0}")

            if not products:
                print("[STREAM] ERROR: No products found")
                yield f"data: {json.dumps({'type': 'error', 'message': 'No products found. Fetch products first.'})}\n\n"
                return

            if not OPENAI_API_KEY:
                print("[STREAM] ERROR: OpenAI API key not configured")
                yield f"data: {json.dumps({'type': 'error', 'message': 'OpenAI API key not configured. Please add OPENAI_API_KEY to .env file'})}\n\n"
                return

            openai.api_key = OPENAI_API_KEY
            total_products = len(products)

            yield f"data: {json.dumps({'type': 'start', 'total': total_products})}\n\n"

            # Step 1: Get or generate collections
            if user_collections and len(user_collections) > 0:
                suggested_collections = user_collections
                yield f"data: {json.dumps({'type': 'info', 'message': f'Using {len(user_collections)} custom collections'})}\n\n"

                parent_mapping = {}
                for col in suggested_collections:
                    if " > " in col:
                        parent = col.split(" > ")[0]
                        parent_mapping[col] = parent
                store_data('parent_mapping', parent_mapping)
            else:
                # AI generation (same as before, but with progress updates)
                yield f"data: {json.dumps({'type': 'info', 'message': 'Generating collections with AI...'})}\n\n"

                # Reduce sample size to avoid token limits (16K for gpt-3.5-turbo-16k)
                sample_count = min(total_products, 200)
                all_titles = "\n".join([f"{i+1}. {products[i]['title']}" for i in range(sample_count)])

                collection_prompt = f"""You are analyzing {sample_count} construction/safety/traffic equipment products (sample from {total_products} total). Create a HIGHLY DETAILED collection structure with MANY specific subcategories.

CRITICAL REQUIREMENTS:
1. Create 8-15 PARENT categories based on main product types
2. For EACH parent, create 10-30 SPECIFIC subcategories
3. Target: 80-200+ total subcategories (the more specific, the better!)

Products to analyze:
{all_titles}

Return a JSON object with parent categories as keys, and arrays of specific subcategories as values."""

                try:
                    response = openai.ChatCompletion.create(
                        model="gpt-3.5-turbo-16k",
                        messages=[
                            {"role": "system", "content": "You are an expert categorization specialist. Return ONLY valid JSON."},
                            {"role": "user", "content": collection_prompt}
                        ],
                        temperature=0.3,
                        max_tokens=4000,
                        request_timeout=180
                    )

                    result = response.choices[0].message.content.strip()
                    if "```json" in result:
                        result = result.split("```json")[1].split("```")[0].strip()
                    elif "```" in result:
                        result = result.split("```")[1].split("```")[0].strip()

                    hierarchy = json.loads(result)
                    suggested_collections = []
                    parent_mapping = {}

                    for parent, subcategories in hierarchy.items():
                        for subcat in subcategories:
                            full_name = f"{parent} > {subcat}"
                            suggested_collections.append(full_name)
                            parent_mapping[full_name] = parent

                    store_data('parent_mapping', parent_mapping)
                    yield f"data: {json.dumps({'type': 'info', 'message': f'Generated {len(suggested_collections)} collections'})}\n\n"

                except Exception as e:
                    yield f"data: {json.dumps({'type': 'error', 'message': f'AI generation failed: {str(e)}'})}\n\n"
                    return

            # Step 2: Classify products in batches with progress updates
            BATCH_SIZE = 500
            total_batches = (total_products + BATCH_SIZE - 1) // BATCH_SIZE  # Ceiling division

            collections_dict = {name: [] for name in suggested_collections}
            product_to_collection = {}
            collections_list = json.dumps(list(collections_dict.keys()), indent=2)

            yield f"data: {json.dumps({'type': 'info', 'message': f'Processing {total_products} products in {total_batches} batches of {BATCH_SIZE}'})}\n\n"

            for batch_num in range(total_batches):
                batch_start = batch_num * BATCH_SIZE + 1
                batch_end = min((batch_num + 1) * BATCH_SIZE, total_products)

                yield f"data: {json.dumps({'type': 'batch_start', 'batch': batch_num + 1, 'total_batches': total_batches, 'start': batch_start, 'end': batch_end})}\n\n"

                for idx in range(batch_start, batch_end + 1):
                    product_title = products[idx - 1]['title']

                    # Send progress update every 10 products
                    if idx % 10 == 0 or idx == batch_start:
                        percentage = int((idx / total_products) * 100)
                        batch_progress = int(((idx - batch_start + 1) / (batch_end - batch_start + 1)) * 100)
                        yield f"data: {json.dumps({'type': 'progress', 'current': idx, 'total': total_products, 'percentage': percentage, 'batch': batch_num + 1, 'batch_progress': batch_progress, 'product': product_title})}\n\n"

                    prompt = f"""Classify this product into the MOST SPECIFIC matching collection.

Product: {product_title}

Available collections (format "Parent > Subcategory"):
{collections_list}

Return ONLY the exact collection name (with " > " format). No explanation, just the collection name."""

                    try:
                        resp = openai.ChatCompletion.create(
                            model="gpt-3.5-turbo",
                            messages=[
                                {"role": "system", "content": "You are a product classification expert. Return ONLY the collection name from the provided list, nothing else."},
                                {"role": "user", "content": prompt}
                            ],
                            temperature=0.1,
                            max_tokens=100,
                            request_timeout=60
                        )

                        collection_name = resp.choices[0].message.content.strip().strip('"\'')

                        if collection_name in collections_dict:
                            product_to_collection[idx] = collection_name
                            collections_dict[collection_name].append(idx)
                        else:
                            fallback = max(collections_dict.items(), key=lambda x: len(x[1]) if x[1] else 0)[0]
                            product_to_collection[idx] = fallback
                            collections_dict[fallback].append(idx)

                        time.sleep(0.05)

                    except Exception as e:
                        fallback = max(collections_dict.items(), key=lambda x: len(x[1]) if x[1] else 0)[0]
                        product_to_collection[idx] = fallback
                        collections_dict[fallback].append(idx)

                # Batch complete
                yield f"data: {json.dumps({'type': 'batch_complete', 'batch': batch_num + 1, 'total_batches': total_batches, 'products_classified': batch_end})}\n\n"

            # Remove empty collections
            all_collections = {name: ids for name, ids in collections_dict.items() if ids}

            # Format for display
            formatted_collections = {}
            for collection_name, indices in all_collections.items():
                formatted_collections[collection_name] = [
                    {"index": idx, "title": products[idx-1]["title"]}
                    for idx in sorted(indices) if 1 <= idx <= len(products)
                ]

            # Store results
            store_data('classified_collections', all_collections)

            # Send completion
            yield f"data: {json.dumps({'type': 'complete', 'collections': formatted_collections, 'total_collections': len(all_collections), 'total_products': len(products)})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    response = Response(stream_with_context(generate()), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    return response

@app.route('/api/update-shopify-start', methods=['POST'])
def update_shopify_start():
    """Start Shopify update in background thread"""
    try:
        # Get data from session
        products = get_data('products', [])
        collections = get_data('classified_collections', {})
        shop_url = get_data('shop_url', '')
        access_token = get_data('access_token', '')

        if not products or not collections:
            return jsonify({"success": False, "error": "No classification data found"}), 400

        if not shop_url or not access_token:
            return jsonify({"success": False, "error": "Missing Shopify credentials"}), 400

        # Create task
        task_id = get_task_id()
        session_id = get_session_id()

        # Store task ID in session
        store_data('current_update_task_id', task_id)

        # Start background thread
        thread = Thread(target=run_shopify_update_background,
                       args=(task_id, products, collections, shop_url, access_token, session_id))
        thread.daemon = True
        thread.start()

        return jsonify({
            "success": True,
            "task_id": task_id,
            "message": "Shopify update started in background"
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

@app.route('/api/update-shopify-status', methods=['GET'])
def update_shopify_status():
    """Get Shopify update task status"""
    try:
        # Get task_id from query params or from session
        task_id = request.args.get('task_id') or get_data('current_update_task_id')

        if not task_id:
            return jsonify({"success": False, "error": "No task ID provided"}), 400

        task = get_task_status(task_id)

        if not task:
            return jsonify({"success": False, "error": "Task not found"}), 404

        return jsonify({
            "success": True,
            "task_id": task_id,
            "status": task['status'],
            "progress": task['progress'],
            "message": task['message'],
            "data": task.get('data'),
            "updated_at": task['updated_at'].isoformat()
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

@app.route('/api/update-shopify-stream', methods=['GET'])
def update_shopify_stream():
    def generate():
        try:
            products = get_data('products', [])
            collections = get_data('classified_collections', {})
            shop_url = get_data('shop_url', '')
            access_token = get_data('access_token', '')

            # Verify token permissions first
            api_version = '2024-10'
            headers = {
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json"
            }

            # Test Smart Collections endpoint
            test_url = f"https://{shop_url}/admin/api/{api_version}/smart_collections.json?limit=1"
            try:
                test_response = requests.get(test_url, headers=headers, timeout=30)
                if test_response.status_code == 403:
                    yield f"data: {json.dumps({'type': 'error', 'message': 'Access token lacks permissions for Smart Collections. Please verify read_products and write_products scopes.'})}\n\n"
                    return
                elif test_response.status_code != 200:
                    yield f"data: {json.dumps({'type': 'error', 'message': f'Cannot access Shopify API. Status: {test_response.status_code}'})}\n\n"
                    return
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'message': f'Connection test failed: {str(e)}'})}\n\n"
                return

            # Log what we retrieved
            retrieved_total = sum(len(ids) for ids in collections.values())
            print(f"\n[SMART COLLECTIONS UPDATE] Retrieved {len(collections)} collections with {retrieved_total} products")

            if not collections:
                yield f"data: {json.dumps({'type': 'error', 'message': 'No classification data'})}\n\n"
                return

            if not shop_url or not access_token:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Missing credentials'})}\n\n"
                return

            # Deduplicate - each product in ONLY ONE collection
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
                yield f"data: {json.dumps({'type': 'info', 'message': f'Removed {duplicates_removed} duplicates (one product = one collection)'})}\n\n"

            total_products = len(seen_products)
            total_collections = len(collections)

            yield f"data: {json.dumps({'type': 'start', 'total': total_products, 'collections': total_collections})}\n\n"
            yield f"data: {json.dumps({'type': 'info', 'message': f'Step 1/2: Creating {total_collections} Smart Collections...'})}\n\n"

            # STEP 1: Create Smart Collections (fast!)
            collections_created = 0
            for collection_name in collections.keys():
                collections_created += 1

                yield f"data: {json.dumps({'type': 'collection_start', 'name': collection_name, 'progress': f'{collections_created}/{total_collections}'})}\n\n"

                # Create Smart Collection with rule
                collection_id = create_or_get_smart_collection(collection_name, shop_url, headers)

                if collection_id:
                    yield f"data: {json.dumps({'type': 'collection_created', 'name': collection_name, 'id': collection_id})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'collection_error', 'name': collection_name, 'message': 'Failed to create'})}\n\n"

            yield f"data: {json.dumps({'type': 'info', 'message': f'Step 2/2: Updating {total_products} products...'})}\n\n"

            # STEP 2: Update product metadata
            success_count = 0
            processed_count = 0

            for collection_name, indices in collections.items():
                for idx in indices:
                    if 1 <= idx <= len(products):
                        processed_count += 1
                        product = products[idx - 1]

                        # Stream updates every product
                        if update_product_metadata(product["id"], collection_name, product["title"], shop_url, headers):
                            success_count += 1
                            yield f"data: {json.dumps({'type': 'product_updated', 'collection': collection_name, 'product': product['title'], 'progress': f'{processed_count}/{total_products}'})}\n\n"
                        else:
                            yield f"data: {json.dumps({'type': 'product_error', 'collection': collection_name, 'product': product['title']})}\n\n"

            yield f"data: {json.dumps({'type': 'complete', 'success_count': success_count, 'total': total_products, 'collections': total_collections, 'message': 'Smart Collections will auto-populate! Check your Shopify store.'})}\n\n"

        except PermissionError as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e), 'is_permission_error': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    response = Response(stream_with_context(generate()), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    return response

def create_or_get_smart_collection(collection_name, shop_url, headers):
    """Create or get SMART collection with rules based on product_type"""
    max_retries = 3

    # Use stable API version compatible with most stores
    api_version = '2024-10'

    for attempt in range(max_retries):
        try:
            # Search for existing smart collection
            search_url = f"https://{shop_url}/admin/api/{api_version}/smart_collections.json"
            response = requests.get(search_url, headers=headers, timeout=30)

            # Handle rate limiting
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 2))
                print(f"Rate limit hit, waiting {retry_after} seconds...")
                time.sleep(retry_after)
                continue

            response.raise_for_status()

            search_data = response.json()
            if "smart_collections" not in search_data:
                print(f"Unexpected search response: {search_data}")
                if "errors" in search_data:
                    print(f"API Errors: {search_data['errors']}")
                if attempt < max_retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                return None

            collections = search_data.get("smart_collections", [])
            for col in collections:
                if col["title"].lower() == collection_name.lower():
                    print(f"✓ Found existing Smart Collection: {collection_name} (ID: {col['id']})")
                    return col["id"]

            # Create new SMART collection with rule: product_type equals collection_name
            create_url = f"https://{shop_url}/admin/api/{api_version}/smart_collections.json"
            payload = {
                "smart_collection": {
                    "title": collection_name,
                    "published": True,
                    "rules": [
                        {
                            "column": "type",  # product_type field
                            "relation": "equals",
                            "condition": collection_name  # Exact match
                        }
                    ],
                    "disjunctive": False  # Must match all rules (only one rule, so strict match)
                }
            }

            # Debug logging
            print(f"\n[CREATE SMART COLLECTION]")
            print(f"  Collection: {collection_name}")
            print(f"  Rule: product_type EQUALS '{collection_name}'")

            time.sleep(0.5)  # Rate limiting
            response = requests.post(create_url, headers=headers, json=payload, timeout=30)

            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 2))
                print(f"Rate limit hit, waiting {retry_after} seconds...")
                time.sleep(retry_after)
                continue

            response.raise_for_status()

            response_data = response.json()
            if "smart_collection" not in response_data:
                print(f"Unexpected response creating Smart Collection {collection_name}")
                print(f"Status: {response.status_code}, Response: {response.text[:500]}")

                # Check if it's a permissions issue
                if "errors" in response_data:
                    error_msg = (
                        f"PERMISSION ERROR: Cannot create Smart Collection '{collection_name}'. "
                        f"Your Shopify API token is missing the 'write_collections' scope. "
                        f"Please verify your app has read_products and write_products scopes."
                    )
                    print(f"ERROR: {error_msg}")
                    raise PermissionError(error_msg)
                if attempt < max_retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                return None

            collection_id = response_data["smart_collection"]["id"]
            print(f"✓ Created Smart Collection: {collection_name} (ID: {collection_id})")
            print(f"  → Auto-populates products with product_type = '{collection_name}'")
            return collection_id

        except requests.exceptions.Timeout:
            print(f"Timeout creating Smart Collection {collection_name}, attempt {attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))
                continue

        except Exception as e:
            print(f"Error creating/getting Smart Collection {collection_name}: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))
                continue

    return None

def update_product_metadata(product_id, collection_name, product_title, shop_url, headers):
    """Update product with ONLY the collection name as tag (replaces all existing tags).
    Also sets product_type to the exact collection name.
    Smart Collections will auto-populate based on product_type match.

    IMPORTANT: This replaces ALL existing tags with ONLY the collection name."""
    max_retries = 3
    retry_delay = 1  # seconds
    api_version = '2024-10'

    for attempt in range(max_retries):
        try:
            # Get current product data
            product_url = f"https://{shop_url}/admin/api/{api_version}/products/{product_id}.json"
            time.sleep(0.5)  # Rate limiting

            get_response = requests.get(product_url, headers=headers, timeout=30)

            if get_response.status_code == 429:
                retry_after = int(get_response.headers.get('Retry-After', 2))
                print(f"Rate limit hit while fetching product, waiting {retry_after} seconds...")
                time.sleep(retry_after)
                continue

            get_response.raise_for_status()
            product_data = get_response.json().get('product', {})

            # CRITICAL: Set ONLY the collection name as the tag (replace all existing tags)
            # Each product gets ONLY ONE tag = the exact collection name
            updated_tags = collection_name

            # CRITICAL: Set product_type to EXACT collection name
            # This is what Smart Collections will match against!
            product_type_value = collection_name

            # Update product with exact collection name as both tag and type
            update_payload = {
                "product": {
                    "id": product_id,
                    "tags": updated_tags,
                    "product_type": product_type_value  # EXACT match for Smart Collection rule
                }
            }

            time.sleep(0.5)  # Rate limiting
            update_response = requests.put(product_url, headers=headers, json=update_payload, timeout=30)

            if update_response.status_code == 429:
                retry_after = int(update_response.headers.get('Retry-After', 2))
                print(f"Rate limit hit while updating product, waiting {retry_after} seconds...")
                time.sleep(retry_after)
                continue

            update_response.raise_for_status()
            print(f"✓ '{product_title[:50]}...' → tag = '{collection_name}' | product_type = '{product_type_value}'")

            return True

        except requests.exceptions.Timeout:
            print(f"Timeout updating product {product_id}, attempt {attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))
                continue
            return False

        except requests.exceptions.RequestException as e:
            print(f"Error updating product {product_id}: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))
                continue
            return False

        except Exception as e:
            print(f"Unexpected error updating product {product_id}: {str(e)}")
            return False

    return False

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
