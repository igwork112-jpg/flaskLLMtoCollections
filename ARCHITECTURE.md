# How the App Handles Large Product Lists (e.g., 2999 products)

## Step-by-Step Flow

### 1. Fetch Products (✅ Handles 2999+ products)
**What happens:**
- User enters a tag (e.g., "featured")
- App fetches products from Shopify in pages of 250
- For 2999 products: 12 API calls (12 pages)
- Filters products by tag on each page
- Stores all matched products in session

**Time:** ~30-60 seconds for 2999 products

**Technical Details:**
- Uses Shopify's pagination with `Link` headers
- Max pages: 100 (supports up to 25,000 products)
- Each page request has 30-second timeout
- Real-time logging shows progress per page

---

### 2. Classify Products (✅ Handles 2999+ products with batching)
**What happens:**
- Products are split into batches of 200
- For 2999 products: 15 batches
- Each batch is sent to OpenAI GPT-3.5 separately
- Results are merged into final collection list

**Why batching?**
- OpenAI has token limits (~4000 tokens for GPT-3.5)
- 2999 product titles would exceed this limit
- Batching keeps each request under the limit

**Time:** ~2-5 minutes for 2999 products (15 batches × 8-20 seconds each)

**Technical Details:**
- Batch size: 200 products
- Model: GPT-3.5-turbo
- Temperature: 0.3 (consistent results)
- Collections are merged across batches
- Products with similar names go to same collection

**Example:**
```
Batch 1: Products 1-200   → 15 collections
Batch 2: Products 201-400 → 12 collections (some merge with batch 1)
...
Batch 15: Products 2801-2999 → 8 collections

Final: 45 unique collections total
```

---

### 3. Update Shopify (✅ Handles 2999+ products with streaming)
**What happens:**
- For each collection:
  1. Create collection (or find existing)
  2. Add products one by one
- Real-time updates stream to UI
- Progress bar shows completion percentage

**Time:** ~10-30 minutes for 2999 products
- Collection creation: ~1 second each
- Product addition: ~0.5 seconds each
- Total: 45 collections + 2999 products = ~25 minutes

**Technical Details:**
- Uses Server-Sent Events (SSE) for real-time updates
- Each API call has 30-second timeout
- Shopify rate limits: 2 requests/second (handled automatically)
- If product already in collection, skips (no error)

**UI Shows:**
```
📁 Collection: Bike Storage
   ✓ Collection ready (ID: 123456)
   ✓ Added: Heavy-Duty Cambridge Cycle Shelter...
   ✓ Added: Type A Galvanised Steel Cycle Rack...

Progress: 45% (1350/2999 products)
```

---

## Performance Summary

| Products | Fetch Time | Classify Time | Update Time | Total Time |
|----------|------------|---------------|-------------|------------|
| 50       | 5 sec      | 10 sec        | 30 sec      | ~1 min     |
| 500      | 20 sec     | 30 sec        | 5 min       | ~6 min     |
| 2999     | 60 sec     | 3 min         | 25 min      | ~30 min    |

---

## Limitations & Solutions

### OpenAI Token Limit
**Problem:** Can't send 2999 titles in one request
**Solution:** Batch processing (200 products per batch)

### Shopify Rate Limits
**Problem:** Max 2 requests/second
**Solution:** Sequential processing with automatic delays

### Session Storage
**Problem:** Large product lists in session
**Solution:** Session expires after 24 hours, stores only IDs and titles

### Browser Timeout
**Problem:** Long-running requests timeout
**Solution:** Server-Sent Events (SSE) for real-time streaming

---

## Scalability

The app can handle:
- ✅ Up to 25,000 products (Shopify's max is ~10,000)
- ✅ Unlimited collections
- ✅ Multiple concurrent users (separate sessions)
- ✅ Large product titles (up to 255 characters)

---

## Error Handling

- Network errors: Retry with exponential backoff
- OpenAI errors: Show error message, allow retry
- Shopify errors: Log and continue with next product
- Timeout errors: Clear message, suggest retry
- Session expiry: Prompt to fetch products again
