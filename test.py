import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import openai
import json
from threading import Thread

class ShopifyProductClassifier:
    def __init__(self, root):
        self.root = root
        self.root.title("Shopify Product Classifier")
        self.root.geometry("800x600")
        
        # Configuration Frame
        config_frame = ttk.LabelFrame(root, text="Configuration", padding=10)
        config_frame.pack(fill="x", padx=10, pady=5)
        
        # Shopify credentials
        ttk.Label(config_frame, text="Shop URL:").grid(row=0, column=0, sticky="w", pady=2)
        self.shop_url = ttk.Entry(config_frame, width=40)
        self.shop_url.grid(row=0, column=1, pady=2)
        self.shop_url.insert(0, "your-store.myshopify.com")
        
        ttk.Label(config_frame, text="Access Token:").grid(row=1, column=0, sticky="w", pady=2)
        self.access_token = ttk.Entry(config_frame, width=40, show="*")
        self.access_token.grid(row=1, column=1, pady=2)
        
        # OpenAI API Key
        ttk.Label(config_frame, text="OpenAI API Key:").grid(row=3, column=0, sticky="w", pady=2)
        self.openai_key = ttk.Entry(config_frame, width=40, show="*")
        self.openai_key.grid(row=3, column=1, pady=2)
        
        # Email configuration
        ttk.Label(config_frame, text="Email (From):").grid(row=4, column=0, sticky="w", pady=2)
        self.email_from = ttk.Entry(config_frame, width=40)
        self.email_from.grid(row=4, column=1, pady=2)
        
        ttk.Label(config_frame, text="Email Password:").grid(row=5, column=0, sticky="w", pady=2)
        self.email_password = ttk.Entry(config_frame, width=40, show="*")
        self.email_password.grid(row=5, column=1, pady=2)
        
        ttk.Label(config_frame, text="Email (To):").grid(row=6, column=0, sticky="w", pady=2)
        self.email_to = ttk.Entry(config_frame, width=40)
        self.email_to.grid(row=6, column=1, pady=2)
        
        # Tag input and actions
        tag_frame = ttk.LabelFrame(root, text="Actions", padding=10)
        tag_frame.pack(fill="x", padx=10, pady=5)
        
        # First row - tag input
        row1 = ttk.Frame(tag_frame)
        row1.pack(fill="x", pady=2)
        
        ttk.Label(row1, text="Tag to Filter:").pack(side="left", padx=5)
        self.tag_input = ttk.Entry(row1, width=30)
        self.tag_input.pack(side="left", padx=5)
        
        self.fetch_btn = ttk.Button(row1, text="Fetch Products", command=self.start_fetch)
        self.fetch_btn.pack(side="left", padx=5)
        
        # Second row - classify and update buttons
        row2 = ttk.Frame(tag_frame)
        row2.pack(fill="x", pady=2)
        
        self.classify_only_btn = ttk.Button(row2, text="Classify Only", command=self.start_classify_only, state="disabled")
        self.classify_only_btn.pack(side="left", padx=5)
        
        self.update_shopify_btn = ttk.Button(row2, text="Update Shopify", command=self.start_update_shopify, state="disabled")
        self.update_shopify_btn.pack(side="left", padx=5)
        
        # Email checkbox
        self.send_email_var = tk.BooleanVar(value=True)
        self.email_checkbox = ttk.Checkbutton(row2, text="Send Email Notification", variable=self.send_email_var)
        self.email_checkbox.pack(side="left", padx=15)
        
        # Log area
        log_frame = ttk.LabelFrame(root, text="Log", padding=10)
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        self.log_area = scrolledtext.ScrolledText(log_frame, height=20, wrap=tk.WORD)
        self.log_area.pack(fill="both", expand=True)
        
        self.products = []
        self.classified_collections = {}
        
    def log(self, message):
        self.log_area.insert(tk.END, message + "\n")
        self.log_area.see(tk.END)
        self.root.update()
        
    def start_fetch(self):
        Thread(target=self.fetch_products, daemon=True).start()
        
    def fetch_products(self):
        try:
            self.log("Connecting to Shopify...")
            shop_url = self.shop_url.get().strip()
            access_token = self.access_token.get().strip()
            tag = self.tag_input.get().strip()
            
            self.log(f"Fetching products with tag: {tag}")
            
            # Shopify Admin API endpoint
            url = f"https://{shop_url}/admin/api/2024-01/products.json"
            headers = {
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json"
            }
            
            all_products = []
            params = {"limit": 250}
            
            while True:
                response = requests.get(url, headers=headers, params=params)
                response.raise_for_status()
                data = response.json()
                
                products = data.get("products", [])
                if not products:
                    break
                
                # Filter by tag
                for p in products:
                    product_tags = [t.strip().lower() for t in p.get("tags", "").split(",")]
                    if tag.lower() in product_tags:
                        all_products.append((p["id"], p["title"]))
                
                # Check for pagination
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
            
            self.products = all_products
            
            self.log(f"Found {len(self.products)} products:")
            for pid, title in self.products:
                self.log(f"  - {title}")
            
            if self.products:
                self.classify_only_btn.config(state="normal")
                self.log("\nReady to classify. Click 'Classify Only' to see groupings.")
            else:
                self.log("\nNo products found with that tag.")
            
        except Exception as e:
            self.log(f"ERROR: {str(e)}")
            messagebox.showerror("Error", str(e))
    
    def start_classify_only(self):
        Thread(target=self.classify_only, daemon=True).start()
    
    def start_update_shopify(self):
        Thread(target=self.update_shopify, daemon=True).start()
        
    def classify_only(self):
        try:
            self.log("\n--- Starting Classification ---")
            openai.api_key = self.openai_key.get()
            
            # Prepare titles for LLM
            titles_text = "\n".join([f"{i+1}. {title}" for i, (_, title) in enumerate(self.products)])
            
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

            self.log("Sending to LLM for classification...")
            
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are a product categorization expert. Return only valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3
            )
            
            result = response.choices[0].message.content.strip()
            # Extract JSON if wrapped in markdown
            if "```json" in result:
                result = result.split("```json")[1].split("```")[0].strip()
            elif "```" in result:
                result = result.split("```")[1].split("```")[0].strip()
                
            self.classified_collections = json.loads(result)
            
            self.log(f"\n✓ Classified into {len(self.classified_collections)} collections:\n")
            
            for collection, indices in self.classified_collections.items():
                self.log(f"📁 {collection} ({len(indices)} products):")
                for idx in indices:
                    if 1 <= idx <= len(self.products):
                        _, title = self.products[idx - 1]
                        self.log(f"   {idx}. {title}")
                self.log("")
            
            self.update_shopify_btn.config(state="normal")
            self.log("Ready to update Shopify. Click 'Update Shopify' to apply changes.")
            
        except Exception as e:
            self.log(f"\nERROR: {str(e)}")
            messagebox.showerror("Error", str(e))
    
    def create_or_get_collection(self, collection_name, shop_url, access_token, headers):
        """Create a new collection or get existing one by title"""
        try:
            # Search for existing collection
            search_url = f"https://{shop_url}/admin/api/2024-01/custom_collections.json"
            response = requests.get(search_url, headers=headers)
            response.raise_for_status()
            
            collections = response.json().get("custom_collections", [])
            for col in collections:
                if col["title"].lower() == collection_name.lower():
                    self.log(f"  ℹ Collection '{collection_name}' already exists (ID: {col['id']})")
                    return col["id"]
            
            # Create new collection
            create_url = f"https://{shop_url}/admin/api/2024-01/custom_collections.json"
            payload = {
                "custom_collection": {
                    "title": collection_name,
                    "published": True
                }
            }
            response = requests.post(create_url, headers=headers, json=payload)
            response.raise_for_status()
            
            collection_id = response.json()["custom_collection"]["id"]
            self.log(f"  ✓ Created collection '{collection_name}' (ID: {collection_id})")
            return collection_id
            
        except Exception as e:
            self.log(f"  ✗ Failed to create collection '{collection_name}': {str(e)}")
            return None
    
    def add_product_to_collection(self, product_id, collection_id, shop_url, access_token, headers):
        """Add a product to a collection"""
        try:
            url = f"https://{shop_url}/admin/api/2024-01/collects.json"
            payload = {
                "collect": {
                    "product_id": product_id,
                    "collection_id": collection_id
                }
            }
            response = requests.post(url, headers=headers, json=payload)
            
            # 422 means product is already in collection, which is fine
            if response.status_code == 422:
                return True
            
            response.raise_for_status()
            return True
            
        except Exception as e:
            self.log(f"    ✗ Failed to add product to collection: {str(e)}")
            return False
    
    def update_shopify(self):
        try:
            if not self.classified_collections:
                self.log("ERROR: No classification data. Run 'Classify Only' first.")
                return
            
            self.log("\n--- Creating Collections & Adding Products ---")
            success_count = 0
            
            shop_url = self.shop_url.get().strip()
            access_token = self.access_token.get().strip()
            headers = {
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json"
            }
            
            # Create collections and add products
            for collection_name, indices in self.classified_collections.items():
                self.log(f"\n📁 Processing collection: {collection_name}")
                
                # Create or get collection
                collection_id = self.create_or_get_collection(collection_name, shop_url, access_token, headers)
                
                if not collection_id:
                    self.log(f"  ⚠ Skipping products for '{collection_name}' due to collection error")
                    continue
                
                # Add products to collection
                for idx in indices:
                    if 1 <= idx <= len(self.products):
                        product_id, title = self.products[idx - 1]
                        
                        # Add to collection
                        if self.add_product_to_collection(product_id, collection_id, shop_url, access_token, headers):
                            self.log(f"    ✓ Added: {title[:60]}...")
                            success_count += 1
                        else:
                            self.log(f"    ✗ Failed: {title[:60]}...")
            
            status = "SUCCESS" if success_count == len(self.products) else "PARTIAL SUCCESS"
            self.log(f"\n{status}: Added {success_count}/{len(self.products)} products to collections")
            
            # Send email if checkbox is checked
            if self.send_email_var.get():
                self.send_email(status, success_count, len(self.products), self.classified_collections)
            else:
                self.log("Email notification skipped (checkbox unchecked)")
            
        except Exception as e:
            self.log(f"\nERROR: {str(e)}")
            if self.send_email_var.get():
                self.send_email("FAILURE", 0, len(self.products), {})
            messagebox.showerror("Error", str(e))
    
    def send_email(self, status, success_count, total_count, collections):
        try:
            self.log("\nSending email notification...")
            
            msg = MIMEMultipart()
            msg['From'] = self.email_from.get()
            msg['To'] = self.email_to.get()
            msg['Subject'] = f"Shopify Product Classification - {status}"
            
            body = f"""
Product Classification Report

Status: {status}
Products Updated: {success_count}/{total_count}

Collections Created:
"""
            for collection, indices in collections.items():
                body += f"  - {collection}: {len(indices)} products\n"
            
            msg.attach(MIMEText(body, 'plain'))
            
            # Using Gmail SMTP
            server = smtplib.SMTP('smtp.gmail.com', 587)
            server.starttls()
            server.login(self.email_from.get(), self.email_password.get())
            server.send_message(msg)
            server.quit()
            
            self.log("✓ Email sent successfully!")
            
        except Exception as e:
            self.log(f"✗ Email failed: {str(e)}")

if __name__ == "__main__":
    root = tk.Tk()
    app = ShopifyProductClassifier(root)
    root.mainloop()
