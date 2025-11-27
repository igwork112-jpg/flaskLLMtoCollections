# Quick Fix for Client

## The Problem
Getting list of collections instead of creating new ones = **Token lacks write permission**

## The Fix (5 minutes)

### 1. Test First
- Open the app
- Click "🔍 Test Permissions" button
- If "Write Collections" = FAIL → continue below

### 2. Shopify Admin
```
Settings → Apps → Develop apps → [Your App] → Configuration
```

### 3. Check Scopes
Make sure these are ✅ checked:
- `read_products`
- `write_products`

Click **Save**

### 4. Generate NEW Token
```
API credentials tab → Revoke old token → Install app → Copy NEW token
```

### 5. Test Again
- Use NEW token in app
- Click "🔍 Test Permissions"
- Should see: ✓ Write Collections: PASS

### 6. Done!
Now run the normal workflow:
1. Fetch Products
2. Classify Products  
3. Update Shopify

---

## Still Not Working?

### Check Shop URL Format
- ✅ `your-store.myshopify.com`
- ❌ `https://your-store.myshopify.com`
- ❌ `your-store.myshopify.com/`

### Make Sure Token is FRESH
The token must be generated AFTER enabling the scopes. Old tokens don't get updated automatically.

### Verify in Shopify
Try manually creating a collection in Shopify admin. If you can't, it's an account permission issue (contact Shopify support).

---

**Need more details?** See `CLIENT_FIX_INSTRUCTIONS.md`
