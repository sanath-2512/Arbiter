`__in` / `__range` lookups on annotations break when given an iterator (regression in 6.1)

```python
Article.objects.alias(article_id=F("id")).filter(article_id__in=iter([a1.id, a2.id]))
# Django 6.0: both articles; Django 6.1: empty queryset

Article.objects.alias(article_id=F("id")).filter(article_id__range=iter([a1.id, a2.id]))
# Django 6.1: ValueError: not enough values to unpack (expected 2, got 0)
```

Passing a list instead of an iterator works, and the same lookups on a model field
(`filter(id__in=iter([...]))`, `filter(id__range=iter([...]))`) work. An iterator that mixes plain values
and expressions (`iter([a1.id, Value(a2.id)])`) is affected the same way. Iterators passed to these
lookups on annotations/aliases should behave exactly like the equivalent list.
