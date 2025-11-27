# Client Checklist - Fix Collection Creation Error

## ✅ Pre-Flight Check

Before starting, have ready:
- [ ] Your Shopify admin login
- [ ] Access to your custom app settings
- [ ] 5 minutes of time

## 🔍 Step 1: Diagnose the Problem

1. [ ] Open the Shopify Product Classifier app
2. [ ] Enter your Shop URL: `your-store.myshopify.com`
3. [ ] Enter your current Access Token
4. [ ] Click the **"🔍 Test Permissions"** button
5. [ ] Wait for test results

### What You'll See:
```
✓ Read Products: PASS
✓ Read Collections: PASS
✗ Write Collections: FAIL  ← This is the problem!
```

If "Write Collections" shows **FAIL**, continue to Step 2.

## 🔧 Step 2: Fix Shopify App Permissions

1. [ ] Open a new tab and go to your Shopify Admin
2. [ ] Navigate to: **Settings** → **Apps and sales channels**
3. [ ] Click **"Develop apps"** (or "App development")
4. [ ] Find and click on your custom app (the one you created for this bot)
5. [ ] Click the **"Configuration"** tab
6. [ ] Scroll to **"Admin API access scopes"**
7. [ ] Make sure these are checked:
   - [ ] `read_products` ✅
   - [ ] `write_products` ✅
8. [ ] Click **"Save"** at the top

## 🔑 Step 3: Generate Fresh Access Token

**IMPORTANT**: You MUST generate a new token. Old tokens don't get updated!

1. [ ] Click the **"API credentials"** tab
2. [ ] If you see an existing token:
   - [ ] Click **"Revoke"** or **"Delete"** to remove it
3. [ ] Click **"Install app"** (or "Reinstall app")
4. [ ] Confirm the installation
5. [ ] You'll see **"Admin API access token"**
6. [ ] Click **"Reveal token once"**
7. [ ] **Copy the entire token** (starts with `shpat_`)
8. [ ] Save it somewhere safe (you can't see it again!)

## ✅ Step 4: Test the New Token

1. [ ] Go back to the Shopify Product Classifier app
2. [ ] **Clear the old token** from the Access Token field
3. [ ] **Paste the NEW token** you just copied
4. [ ] Click **"🔍 Test Permissions"** again
5. [ ] Verify all tests pass:
   ```
   ✓ Read Products: PASS
   ✓ Read Collections: PASS
   ✓ Write Collections: PASS  ← Should be PASS now!
   ```

## 🎉 Step 5: Run the Bot

Now you can use the bot normally:

1. [ ] Enter a product tag (e.g., `featured`)
2. [ ] Click **"Fetch Products"**
3. [ ] Wait for products to load
4. [ ] Click **"Classify Products"**
5. [ ] Review the AI-generated collections
6. [ ] Click **"Update Shopify"**
7. [ ] Watch as collections are created! ✨

## ❌ Still Not Working?

### Check Your Shop URL Format
Make sure it's in this format:
- ✅ Correct: `your-store.myshopify.com`
- ❌ Wrong: `https://your-store.myshopify.com`
- ❌ Wrong: `your-store.myshopify.com/`
- ❌ Wrong: `www.your-custom-domain.com`

### Verify Token is Fresh
- The token must be generated AFTER you enabled the scopes
- If you enabled scopes yesterday but generated the token last week, it won't work
- Always generate a NEW token after changing scopes

### Try Creating a Collection Manually
1. Go to Shopify Admin → Products → Collections
2. Try to create a new collection manually
3. If you can't, your Shopify account might have restrictions
4. Contact Shopify support

## 📞 Need Help?

If you've followed all steps and it's still not working:
1. Take a screenshot of the permission test results
2. Take a screenshot of your Shopify app scopes
3. Share both screenshots with your developer
4. Check the browser console (F12) for detailed error logs

---

**Estimated Time**: 5 minutes
**Difficulty**: Easy
**Success Rate**: 99% (if you generate a fresh token!)
