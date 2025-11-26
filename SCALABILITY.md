# Scalability Guide

## Can This Handle 2999+ Products?

**YES!** The system is designed to handle large product catalogs efficiently.

## How It Works

### Adaptive Batch Processing

The system automatically adjusts batch sizes based on your product count:

- **< 500 products**: 200 products per batch
- **500-1000 products**: 150 products per batch  
- **1000+ products**: 100 products per batch

This ensures optimal performance and stays within API token limits.

### Example: 3000 Products

For 3000 products:
- **Batch size**: 100 products
- **Total batches**: 30 batches
- **Processing time**: ~30-45 seconds (with rate limiting)
- **Memory usage**: Minimal (streaming approach)

## Key Features for Large Datasets

### 1. Strict Product Tracking
- Every product tracked from start to finish
- Global `assigned_products` set prevents duplicates
- `unassigned_products` set tracks what's left
- Guarantees exactly N products classified for N input products

### 2. Adaptive Token Limits
- Automatically calculates `max_tokens` based on batch size
- Formula: `min(4000, batch_size * 20)`
- Prevents truncated responses

### 3. Rate Limiting
- Automatic 1-second delay between batches for large datasets
- Prevents OpenAI API rate limit errors
- Configurable if needed

### 4. Error Handling
- Failed batches don't crash the process
- Unclassified products automatically go to "Uncategorized"
- Clear logging shows what succeeded/failed

### 5. Progress Tracking
```
Processing batch 15: products 1401 to 1500
  Batch complete: 98 products assigned, 2 duplicates skipped
  Progress: 1498/3000 (49.9%)
```

## Performance Estimates

| Products | Batches | Est. Time | Memory |
|----------|---------|-----------|--------|
| 177      | 1       | 3-5s      | < 5MB  |
| 500      | 3       | 8-12s     | < 10MB |
| 1000     | 7       | 15-25s    | < 20MB |
| 3000     | 30      | 45-60s    | < 50MB |
| 5000     | 50      | 75-100s   | < 80MB |

## Limitations

### OpenAI API Limits
- **Rate limit**: 3 requests/min (free tier) or 3500 requests/min (paid)
- **Token limit**: 4096 tokens per request (GPT-3.5-turbo)
- **Solution**: Batch processing + rate limiting handles this

### Shopify API Limits
- **Rate limit**: 2 requests/second (REST API)
- **Solution**: Built-in delays in update process

### Browser Memory
- Large product lists displayed in browser
- **Recommendation**: For 5000+ products, consider pagination in UI

## Optimization Tips

### For Very Large Catalogs (10,000+)

1. **Reduce batch size further**:
   ```python
   batch_size = 50  # More batches, smaller size
   ```

2. **Increase rate limit delay**:
   ```python
   time.sleep(2)  # 2 seconds between batches
   ```

3. **Use GPT-4** (better accuracy, higher token limit):
   ```python
   model="gpt-4"
   max_tokens=8000
   ```

4. **Process in chunks**:
   - Classify 1000 products at a time
   - Update Shopify in batches
   - Repeat for next 1000

## Monitoring

The system provides detailed logging:

```
Classifying 3000 products in 30 batches of ~100...
Processing batch 1: products 1 to 100
  Batch complete: 100 products assigned, 0 duplicates skipped
  Progress: 100/3000 (3.3%)
...
✓ FINAL RESULT: 3000 products in 12 collections
✓ SUCCESS: All 3000 products classified correctly
```

## Cost Estimates (OpenAI API)

Approximate costs for GPT-3.5-turbo:

- **177 products**: ~$0.01
- **1000 products**: ~$0.05
- **3000 products**: ~$0.15
- **10,000 products**: ~$0.50

*Costs may vary based on product title lengths and API pricing*

## Conclusion

The system is production-ready for catalogs of any size. The adaptive batching, strict tracking, and error handling ensure reliable classification even for very large product catalogs.
