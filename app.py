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
        
        # STEP 1: Get hierarchical collection structure from AI (Parent → Subcategories)
        print("Step 1: Getting hierarchical collection structure...")
        # Use strategic sampling for large datasets (faster while still comprehensive)
        if total_products > 500:
            # Sample evenly distributed products for better coverage
            sample_size = 150
            step = total_products // sample_size
            sample_indices = [i * step for i in range(sample_size)]
            sample_titles = "\n".join([f"{i+1}. {products[idx]['title']}" for i, idx in enumerate(sample_indices)])
        else:
            sample_size = min(100, total_products)
            sample_titles = "\n".join([f"{i+1}. {products[i]['title']}" for i in range(sample_size)])

        collection_prompt = f"""Analyze these products and create a DETAILED HIERARCHICAL collection structure with parent categories and specific subcategories.

REQUIREMENTS:
- Create 5-15 PARENT categories (broad product types)
- For EACH parent, create 5-20 SPECIFIC subcategories (granular product types)
- Aim for 50-200+ total subcategories across all parents
- Subcategories should be HIGHLY SPECIFIC and DETAILED
- Use demographic segmentation: Men's, Women's, Kids', Unisex
- Use size/type variations: Ankle, Crew, Knee-High, Compression
- Use material/style details: Cotton, Wool, Running, Casual, Formal

EXCELLENT EXAMPLES:
Parent: "Footwear"
  Subcategories: ["Men's Running Shoes", "Women's Running Shoes", "Kids' Running Shoes",
                  "Men's Casual Shoes", "Women's Casual Shoes", "Kids' Casual Shoes",
                  "Men's Boots", "Women's Boots", "Kids' Boots",
                  "Men's Dress Shoes", "Women's Dress Shoes",
                  "Men's Athletic Socks", "Women's Athletic Socks", "Kids' Athletic Socks",
                  "Men's Dress Socks", "Women's Dress Socks",
                  "Men's Casual Socks", "Women's Casual Socks", "Kids' Casual Socks",
                  "Compression Socks", "Ankle Socks", "Crew Socks", "Knee-High Socks"]

Parent: "Apparel"
  Subcategories: ["Men's T-Shirts", "Women's T-Shirts", "Kids' T-Shirts",
                  "Men's Hoodies", "Women's Hoodies", "Kids' Hoodies",
                  "Men's Jeans", "Women's Jeans", "Kids' Jeans",
                  "Men's Shorts", "Women's Shorts", "Kids' Shorts",
                  "Men's Jackets", "Women's Jackets", "Kids' Jackets"]

Parent: "Electronics"
  Subcategories: ["Smartphones", "Tablets", "Laptops", "Desktop Computers",
                  "Headphones", "Earbuds", "Speakers", "Smart Watches",
                  "Cameras", "Phone Cases", "Screen Protectors", "Chargers", "Cables"]

Products to analyze (sample of {sample_size} from {total_products} total):
{sample_titles}

Return a JSON object with parent categories as keys, and arrays of specific subcategories as values:
{{
  "Parent Category 1": ["Specific Sub 1", "Specific Sub 2", "Specific Sub 3", ...],
  "Parent Category 2": ["Specific Sub 1", "Specific Sub 2", ...],
  ...
}}

CRITICAL RULES:
1. Be VERY SPECIFIC in subcategories (e.g., "Men's Running Shoes" not just "Shoes")
2. Create MANY subcategories (aim for 50-200+ total)
3. Use demographic splits (Men's/Women's/Kids') whenever applicable
4. Include style/type variations (Running/Casual/Formal, etc.)
5. Each subcategory should be unique and descriptive
6. Subcategories are what products will actually be assigned to (not parents)"""

        try:
            print(f"  Analyzing {sample_size} products to generate collection hierarchy...")
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are an expert e-commerce product categorization specialist. Create DETAILED, SPECIFIC hierarchical collections with many subcategories. Return ONLY valid JSON."},
                    {"role": "user", "content": collection_prompt}
                ],
                temperature=0.3,  # Slightly lower for consistency
                max_tokens=1800,  # Optimized for speed
                request_timeout=30  # Add timeout
            )

            result = response.choices[0].message.content.strip()
            if "```" in result:
                result = result.split("```")[1].replace("json", "").strip()

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

            # Store parent mapping for later use
            store_data('parent_mapping', parent_mapping)

        except Exception as e:
            print(f"⚠️ Error getting hierarchical collections: {e}")
            print(f"⚠️ Using default hierarchical structure")
            suggested_collections = [
                "Apparel > Men's Shirts", "Apparel > Women's Shirts", "Apparel > Kids' Shirts",
                "Footwear > Men's Shoes", "Footwear > Women's Shoes", "Footwear > Kids' Shoes",
                "Accessories > Bags", "Accessories > Hats", "Accessories > Belts"
            ]
            parent_mapping = {col: col.split(" > ")[0] for col in suggested_collections}
            store_data('parent_mapping', parent_mapping)
        
        # STEP 2: Initialize tracking - ONE product = ONE collection
        print(f"\nStep 2: Classifying {total_products} products...")
        
        # Create empty collections
        collections_dict = {name: [] for name in suggested_collections}
        
        # Track assignments: product_idx -> collection_name
        product_to_collection = {}
        
        # Process in batches (adaptive batch size based on dataset and collection count)
        # With more collections, we need smaller batches for better accuracy
        num_collections = len(suggested_collections)

        if num_collections > 150:
            # Very many collections = very small batches
            batch_size = 15
        elif num_collections > 100:
            # Many collections = smaller batches for better accuracy
            batch_size = 20
        elif num_collections > 50:
            batch_size = 30
        elif total_products > 1000:
            batch_size = 40
        else:
            batch_size = 35

        print(f"  Using batch size of {batch_size} for {num_collections} collections")
        
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
            
            prompt = f"""CRITICAL: Assign each product to EXACTLY ONE specific subcategory. Be PRECISE and DETAILED.

Available collections (hierarchical format "Parent > Subcategory"):
{json.dumps(list(collections_dict.keys()), indent=2)}

Products to classify:
{batch_text}

Return JSON mapping each product NUMBER to ONE collection name (use exact format "Parent > Subcategory"):
{{"1": "Parent > Subcategory", "2": "Parent > Subcategory", ...}}

CLASSIFICATION RULES:
1. Each product ({batch_start + 1} to {batch_end}) must appear EXACTLY ONCE
2. Choose the MOST SPECIFIC subcategory that matches the product
3. Consider demographics: Men's vs Women's vs Kids' vs Unisex
4. Consider type/style: Running vs Casual vs Formal vs Athletic
5. Consider material/features when available
6. If unsure, pick the closest match - don't leave products unassigned
7. Use the EXACT collection name from the list above (including " > " separator)

EXAMPLES:
- "Nike Men's Air Max Running Shoes" → "Footwear > Men's Running Shoes"
- "Women's Cotton Ankle Socks White" → "Footwear > Women's Casual Socks" or "Footwear > Ankle Socks"
- "Kids Winter Boots Size 5" → "Footwear > Kids' Boots"
- "Men's Business Dress Socks Black" → "Footwear > Men's Dress Socks"
- "iPhone 13 Leather Case" → "Electronics > Phone Cases"
- "Women's Yoga Leggings" → "Apparel > Women's Athletic Wear" (or similar)

Be SPECIFIC and ACCURATE. Match products to the most appropriate granular subcategory."""

            # Get AI response
            ai_response = {}
            try:
                resp = openai.ChatCompletion.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": "You are an expert product classifier. Analyze each product carefully and assign it to the MOST SPECIFIC matching subcategory. Return ONLY a JSON object mapping product numbers to collection names using exact format \"Parent > Subcategory\". Each product number must appear exactly once."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.2,
                    max_tokens=2000,  # Optimized for speed
                    request_timeout=45  # Longer timeout for batch processing
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

                # Check coverage
                missing_in_response = expected_range - ai_products_set
                if missing_in_response and len(missing_in_response) > 5:
                    print(f"  ⚠️ AI missed {len(missing_in_response)} products in this batch (will re-classify later)")
                
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
                
                # Get collection from AI
                collection = ai_response.get(str(product_idx))
                if not collection or collection not in collections_dict:
                    # Mark for re-classification instead of fallback
                    collection = None
                
                # CRITICAL: Only assign if not already assigned
                if product_idx not in product_to_collection:
                    if collection:
                        product_to_collection[product_idx] = collection
                        collections_dict[collection].append(product_idx)
                    # If no valid collection, leave unassigned for Step 3
                else:
                    print(f"  ⚠️ Product {product_idx} already assigned, skipping")
            
            print(f"  ✓ Assigned {batch_count} products")
            print(f"  Total assigned: {len(product_to_collection)}/{total_products}")

            # Minimal rate limiting for speed (OpenAI has generous limits for gpt-3.5-turbo)
            time.sleep(0.2)  # Small delay to avoid rate limits
        
        # STEP 3: Verification and re-classification
        print(f"\nStep 3: Verification and re-classification...")
        
        # Check for missing products
        missing = []
        for i in range(1, total_products + 1):
            if i not in product_to_collection:
                missing.append(i)
        
        if missing:
            print(f"  ⚠️ Found {len(missing)} unclassified products")

            # If too many missing, use fast fallback instead of individual AI calls
            if len(missing) > 100:
                print(f"  ⚠️ Too many missing products ({len(missing)}), using smart fallback assignment...")
                # Assign to most populated collection (likely a good general category)
                fallback = max(collections_dict.items(), key=lambda x: len(x[1]))[0]
                for product_idx in missing:
                    product_to_collection[product_idx] = fallback
                    collections_dict[fallback].append(product_idx)
                print(f"  ✓ Assigned {len(missing)} products to '{fallback}'")
            else:
                print(f"  Using AI to find best matches for {len(missing)} products...")
                # Re-classify missing products using AI
                for product_idx in missing:
                    product_title = products[product_idx - 1]['title']

                    # Ask AI to find the best collection for this specific product
                    reclassify_prompt = f"""Given this product and available hierarchical collections, choose the MOST SPECIFIC matching collection.

Product: {product_title}

Available collections (format "Parent > Subcategory"):
{json.dumps(list(collections_dict.keys()), indent=2)}

Analyze the product and return ONLY the exact collection name (with " > " format) that best matches.
Consider: demographics (Men's/Women's/Kids'), style (Running/Casual/Formal), type, and material.
Be specific and precise. Return the exact collection name from the list above."""

                    try:
                        resp = openai.ChatCompletion.create(
                            model="gpt-3.5-turbo",
                            messages=[
                                {"role": "system", "content": "You are a product categorization expert. Return ONLY the collection name, nothing else."},
                                {"role": "user", "content": reclassify_prompt}
                            ],
                            temperature=0.2,
                            max_tokens=50,
                            request_timeout=20
                        )

                        best_collection = resp.choices[0].message.content.strip().strip('"\'')

                        # Validate the collection exists
                        if best_collection in collections_dict:
                            product_to_collection[product_idx] = best_collection
                            collections_dict[best_collection].append(product_idx)
                            print(f"    ✓ Product {product_idx} → '{best_collection}'")
                        else:
                            # If AI returns invalid collection, use the first one
                            fallback = list(collections_dict.keys())[0]
                            product_to_collection[product_idx] = fallback
                            collections_dict[fallback].append(product_idx)
                            print(f"    ⚠️ Product {product_idx} → '{fallback}' (AI returned invalid collection)")

                        time.sleep(0.1)  # Minimal rate limiting

                    except Exception as e:
                        print(f"    ⚠️ Error re-classifying product {product_idx}: {e}")
                        # Fallback to first collection only on error
                        fallback = list(collections_dict.keys())[0]
                        product_to_collection[product_idx] = fallback
                        collections_dict[fallback].append(product_idx)
        
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
            
            # Verify token permissions first
            api_version = '2024-10'
            headers = {
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json"
            }
            
            # Test if we can access collections endpoint
            test_url = f"https://{shop_url}/admin/api/{api_version}/custom_collections.json?limit=1"
            try:
                test_response = requests.get(test_url, headers=headers, timeout=10)
                if test_response.status_code == 403:
                    yield f"data: {json.dumps({'type': 'error', 'message': 'Access token lacks permissions to read collections. Please verify your Shopify app has read_products and write_products scopes.'})}\n\n"
                    return
                elif test_response.status_code != 200:
                    yield f"data: {json.dumps({'type': 'error', 'message': f'Cannot access Shopify API. Status: {test_response.status_code}'})}\n\n"
                    return
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'message': f'Connection test failed: {str(e)}'})}\n\n"
                return
            
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

            # Extract parent categories and create them first
            parent_categories = set()
            for collection_name in collections.keys():
                if " > " in collection_name:
                    parent = collection_name.split(" > ")[0]
                    parent_categories.add(parent)

            yield f"data: {json.dumps({'type': 'info', 'message': f'Creating {len(parent_categories)} parent categories...'})}\n\n"

            # Create parent collections first (for organizational purposes)
            parent_ids = {}
            for parent_name in sorted(parent_categories):
                parent_id = create_or_get_collection(parent_name, shop_url, headers)
                if parent_id:
                    parent_ids[parent_name] = parent_id
                    yield f"data: {json.dumps({'type': 'parent_created', 'name': parent_name, 'id': parent_id})}\n\n"
                time.sleep(0.5)  # Rate limiting

            success_count = 0
            total_products = len(seen_products)  # Use unique count

            yield f"data: {json.dumps({'type': 'start', 'total': total_products, 'collections': len(collections), 'parents': len(parent_categories)})}\n\n"

            for collection_name, indices in collections.items():
                # Notify collection processing
                yield f"data: {json.dumps({'type': 'collection_start', 'name': collection_name, 'count': len(indices)})}\n\n"
                
                # Create or get collection
                collection_id = create_or_get_collection(collection_name, shop_url, headers)
                
                if not collection_id:
                    error_msg = f"Failed to create collection '{collection_name}'. Check console for details."
                    yield f"data: {json.dumps({'type': 'collection_error', 'name': collection_name, 'message': error_msg})}\n\n"
                    continue
                
                yield f"data: {json.dumps({'type': 'collection_created', 'name': collection_name, 'id': collection_id})}\n\n"
                
                # Add products
                for idx in indices:
                    if 1 <= idx <= len(products):
                        product = products[idx - 1]

                        if add_product_to_collection(product["id"], collection_id, collection_name, shop_url, headers):
                            success_count += 1
                            result = json.dumps({'type': 'product_added', 'collection': collection_name, 'product': product['title'], 'status': 'success'})
                            yield f"data: {result}\n\n"
                        else:
                            result = json.dumps({'type': 'product_added', 'collection': collection_name, 'product': product['title'], 'status': 'failed'})
                            yield f"data: {result}\n\n"
            
            yield f"data: {json.dumps({'type': 'complete', 'success_count': success_count, 'total': total_products})}\n\n"
            
        except PermissionError as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e), 'is_permission_error': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    
    response = Response(stream_with_context(generate()), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    return response

def create_or_get_collection(collection_name, shop_url, headers):
    """Create or get collection with retry logic"""
    max_retries = 3
    
    # Use stable API version compatible with most stores
    api_version = '2024-10'
    
    for attempt in range(max_retries):
        try:
            # Search for existing collection
            search_url = f"https://{shop_url}/admin/api/{api_version}/custom_collections.json"
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
            create_url = f"https://{shop_url}/admin/api/{api_version}/custom_collections.json"
            payload = {
                "custom_collection": {
                    "title": collection_name,
                    "published": True
                }
            }
            
            # Debug logging
            print(f"\n[CREATE COLLECTION DEBUG]")
            print(f"  URL: {create_url}")
            print(f"  Method: POST")
            print(f"  Payload: {json.dumps(payload)}")
            print(f"  Headers: {list(headers.keys())}")
            
            time.sleep(0.5)  # Rate limiting
            response = requests.post(create_url, headers=headers, json=payload, timeout=30)
            
            print(f"  Response Status: {response.status_code}")
            print(f"  Response Headers: {dict(response.headers)}")
            print(f"  Response Body (first 500 chars): {response.text[:500]}")
            
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
                
                # Check if it's a permissions issue
                if "custom_collections" in response_data:
                    error_msg = (
                        f"PERMISSION ERROR: Cannot create collection '{collection_name}'. "
                        f"Your Shopify API token is missing the 'write_collections' scope. "
                        f"Please go to your Shopify Admin → Apps → Your Custom App → Configuration "
                        f"and add 'write_collections' and 'read_collections' permissions, then generate a new access token."
                    )
                    print(f"ERROR: {error_msg}")
                    raise PermissionError(error_msg)
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

def add_product_to_collection(product_id, collection_id, collection_name, shop_url, headers):
    """Add product to collection and update product tags/type with retry logic and rate limiting"""
    max_retries = 3
    retry_delay = 1  # seconds
    api_version = '2024-10'  # Match the version used in create_or_get_collection

    for attempt in range(max_retries):
        try:
            # STEP 1: Add product to collection
            url = f"https://{shop_url}/admin/api/{api_version}/collects.json"
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
            elif response.status_code == 429:
                # 429 = rate limit hit, retry with longer delay
                retry_after = int(response.headers.get('Retry-After', 2))
                print(f"Rate limit hit, waiting {retry_after} seconds...")
                time.sleep(retry_after)
                continue
            else:
                response.raise_for_status()

            # STEP 2: Update product tags and product type
            # First, get the current product data
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

            # Get existing tags and add collection name if not already present
            existing_tags = product_data.get('tags', '')
            tag_list = [tag.strip() for tag in existing_tags.split(',') if tag.strip()]

            # Add full collection name as tag if not already present
            if collection_name not in tag_list:
                tag_list.append(collection_name)

            # Extract subcategory for product_type (cleaner than full "Parent > Subcategory")
            if " > " in collection_name:
                parent_name, subcategory_name = collection_name.split(" > ", 1)
                product_type_value = subcategory_name  # Use just subcategory for product_type
                # Also add parent as a tag for filtering
                if parent_name not in tag_list:
                    tag_list.append(parent_name)
            else:
                product_type_value = collection_name

            updated_tags = ', '.join(tag_list)

            # Update product with new tags and product type
            update_payload = {
                "product": {
                    "id": product_id,
                    "tags": updated_tags,
                    "product_type": product_type_value
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
            print(f"✓ Product {product_id}: added tags ('{parent_name if ' > ' in collection_name else ''}', '{collection_name}') and set product_type to '{product_type_value}'")

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
