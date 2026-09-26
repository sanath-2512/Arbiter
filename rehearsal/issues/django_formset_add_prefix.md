Model formsets ignore a form's custom add_prefix() for existing objects

We override `add_prefix()` on a ModelForm to get dotted field names (`form-0.name` instead of
`form-0-name`) for our front-end. The form fields render and validate fine with that naming, but a bound
model formset built with `modelformset_factory(Author, form=AuthorForm)` does not recognise the existing
objects in the submitted data:

```python
data = {
    "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "1", "form-MAX_NUM_FORMS": "0",
    "form-0.id": str(author.pk), "form-0.name": "Charles P. Baudelaire",
}
formset = AuthorFormSet(data, queryset=Author.objects.all())
formset.is_valid()          # the existing author is not matched to form 0
```

The formset looks for the primary key under the default naming, so updates are not applied to the right
object. It should use the form's own naming for the primary key. Forms that keep the default
`add_prefix()` should behave exactly as today (in particular, they should not be instantiated more often
than before).
