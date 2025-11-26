from flask import Flask, render_template, request, jsonify, session, Response, stream_with_context
import requests
import openai
import json
import os
from datetime import timedelta
import time

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

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
        session['openai_key'] = data.get('openai_key', '').strip()
        
        url = f"https://{shop_url}/admin/api/2024-01/products.json"
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        }
        
        all_products = []
        params = {"limit": 250}
        page_count = 0
        max_pages = 10  # Safety limit
        
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
            
            if not products:
                break
            
            # Filter by tag
            for p in products:
                if p.get("tags"):
                    product_tags = [t.strip().lower() for t in p.get("tags", "").split(",")]
                    if tag.lower() in product_tags:
                        all_products.append({"id": p["id"], "title": p["title"]})
            
            # Check pagination
            link_header = response.headers.get("Link", "")
            if "rel=\"next\"" not in link_header:
                break
            
            # Extract next page URL
            next_url = None
            for link in link_header.split(","):
                if "rel=\"next\"" in link:
                    next_url = link.split(";")[0].strip("<> ")
                    break
            
            if not next_url:
                break
                
            url = next_url
            params = {}
            page_count += 1
        
        session['products'] = all_products
        session.permanent = True
        
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
        openai_key = session.get('openai_key', '')
        
        if not products:
            return jsonify({"success": False, "error": "No products found. Fetch products first."}), 400
        
        openai.api_key = openai_key
        
        titles_text = "\n".join([f"{i+1}. {p['title']}" for i, p in enumerate(products)])
        
        prompt = f"""Analyze these product titles and group them into collections based on their primary category or product type.
Return a JSON object where keys are collection names and values are arrays of title numbers.

Titles:
{titles_text}

Example format:
{{
  "Bike Storage": [12, 14],
  "Flooring Tools": [2, 3, 4],
  "Storage Solutions": [1, 7]
}}"""

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
            
        collections = json.loads(result)
        
        # Format for display
        formatted_collections = {}
        for collection_name, indices in collections.items():
            formatted_collections[collection_name] = [
                {"index": idx, "title": products[idx-1]["title"]}
                for idx in indices if 1 <= idx <= len(products)
            ]
        
        session['classified_collections'] = collections
        
        return jsonify({
            "success": True,
            "collections": formatted_collections
        })
        
    except Exception as e:
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
