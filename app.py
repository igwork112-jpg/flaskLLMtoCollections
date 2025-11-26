from flask import Flask, render_template, request, jsonify, session, Response, stream_with_context
from flask_session import Session
import requests
import openai
import json
import os
from datetime import timedelta
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

# Configure server-side session to handle large data
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = './flask_session'
app.config['SESSION_PERMANENT'] = True
Session(app)

# Get OpenAI key from environment
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')

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
        
        # Store credentials in session
        session['shop_url'] = shop_url
        session['access_token'] = access_token
        
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
        
        # Store products in session
        session['products'] = all_products
        session.permanent = True
        session.modified = True  # Force session save
        
        print(f"Stored {len(all_products)} products in session")
        
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
        products = session.get('products', [])
        
        print(f"DEBUG: Retrieved {len(products)} products from session")
        
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
        
        session['classified_collections'] = all_collections
        
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
            products = session.get('products', [])
            collections = session.get('classified_collections', {})
            shop_url = session.get('shop_url', '')
            access_token = session.get('access_token', '')
            
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
                            yield f"data: {json.dumps({'type': 'product_added', 'collection': collection_name, 'product': product['title'], 'status': 'success'})}\n\n"
                        else:
                            yield f"data: {json.dumps({'type': 'product_added', 'collection': collection_name, 'product': product['title'], 'status': 'failed'})}\n\n"
            
            yield f"data: {json.dumps({'type': 'complete', 'success_count': success_count, 'total': total_products})}\n\n"
            
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    
    response = Response(stream_with_context(generate()), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    return response

def create_or_get_collection(collection_name, shop_url, headers):
    try:
        search_url = f"https://{shop_url}/admin/api/2024-01/custom_collections.json"
        response = requests.get(search_url, headers=headers, timeout=30)
        response.raise_for_status()
        
        collections = response.json().get("custom_collections", [])
        for col in collections:
            if col["title"].lower() == collection_name.lower():
                print(f"Found existing collection: {collection_name} (ID: {col['id']})")
                return col["id"]
        
        create_url = f"https://{shop_url}/admin/api/2024-01/custom_collections.json"
        payload = {
            "custom_collection": {
                "title": collection_name,
                "published": True
            }
        }
        response = requests.post(create_url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        
        collection_id = response.json()["custom_collection"]["id"]
        print(f"Created new collection: {collection_name} (ID: {collection_id})")
        return collection_id
        
    except Exception as e:
        print(f"Error creating/getting collection {collection_name}: {str(e)}")
        return None

def add_product_to_collection(product_id, collection_id, shop_url, headers):
    try:
        url = f"https://{shop_url}/admin/api/2024-01/collects.json"
        payload = {
            "collect": {
                "product_id": product_id,
                "collection_id": collection_id
            }
        }
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        
        if response.status_code == 422:
            return True
        
        response.raise_for_status()
        return True
        
    except Exception as e:
        print(f"Error adding product {product_id} to collection {collection_id}: {str(e)}")
        return False

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
