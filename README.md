# Shopify Product Classifier

AI-powered Flask web app to automatically classify and organize Shopify products into collections.

## Features
- 🛍️ Fetch products by tag from your Shopify store
- 🤖 AI-powered classification using OpenAI GPT
- 📁 Automatically create collections and add products
- 🎨 Beautiful, responsive web interface
- ☁️ Ready to deploy on Railway

## Local Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Run the application:
```bash
python app.py
```

3. Open http://localhost:5000 in your browser

## Deploy to Railway

1. Push this code to GitHub
2. Go to [Railway](https://railway.app)
3. Click "New Project" → "Deploy from GitHub repo"
4. Select your repository
5. Railway will auto-detect and deploy your Flask app
6. Set environment variable (optional):
   - `SECRET_KEY`: Your secret key for sessions

## Configuration

1. **Create .env file** (copy from .env.example):
```bash
cp .env.example .env
```

2. **Add your OpenAI API Key** to `.env`:
```
OPENAI_API_KEY=sk-your-key-here
```
Get your key from https://platform.openai.com/api-keys

## Shopify Setup

1. Go to your Shopify admin
2. **Settings** → **Apps and sales channels** → **Develop apps**
3. Click **"Create an app"**
4. Configure Admin API scopes:
   - `read_products`
   - `write_products`
5. Install the app and copy the **Admin API access token**

## Usage

1. Enter your Shopify store URL (e.g., `your-store.myshopify.com`)
2. Paste your Shopify Admin API access token
3. **(Optional)** Click **"🔍 Test Permissions"** to verify your token has correct scopes
4. Enter a product tag to filter (e.g., `featured`, `new`)
5. Click **"Fetch Products"** to retrieve products
6. Click **"Classify Products"** to see AI-generated collections
7. Review the groupings
8. Click **"Update Shopify"** to create collections and add products

## Troubleshooting

### "Unexpected response creating collection" Error
If you see this error, your access token lacks write permissions. See `QUICK_FIX.md` for a 5-minute solution.

**Quick test**: Click the "🔍 Test Permissions" button to diagnose the issue.

### Permission Issues
- Make sure your Shopify app has `read_products` and `write_products` scopes
- Generate a **fresh** access token after enabling scopes
- Old tokens don't automatically get new permissions

For detailed troubleshooting, see:
- `QUICK_FIX.md` - Fast solution
- `CLIENT_FIX_INSTRUCTIONS.md` - Step-by-step guide
- `DEBUGGING_GUIDE.md` - Technical details

## How It Works

1. Fetches products from Shopify filtered by tag
2. Sends product titles to OpenAI GPT for intelligent categorization
3. Creates Custom Collections in Shopify
4. Adds products to their respective collections
5. Reuses existing collections if they already exist

## Tech Stack

- **Backend**: Flask (Python)
- **Frontend**: HTML, CSS, JavaScript
- **AI**: OpenAI GPT-3.5
- **API**: Shopify Admin REST API
- **Deployment**: Railway (or any platform supporting Python)
