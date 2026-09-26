`union` returns an empty list for slices of different numeric types

In templates, `union` of an int slice and a float slice returns an empty slice instead of merging the
numbers:

    {{ union (slice 1) (slice 1.0) }}          -> []    (expected [1])
    {{ union (slice 1 2) (slice 2.0 3.0) }}    -> []    (expected [1 2 3])
    {{ union (slice 1.0) (slice 1 2) }}        -> []    (expected [1 2] as floats)

`intersect` already matches numbers across int and float element types, so `union` should unify them the
same way: the result keeps the first list's element type, and values that cannot be converted to it without
loss are skipped. When the first list is empty, the result stays an empty slice of the first list's type.
Mismatched non-numeric element types (for example strings and ints) keep returning an empty result as today.
