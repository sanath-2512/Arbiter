`no-loss-of-precision` reports exact numbers written like `0.e5`

The rule reports number literals whose mantissa ends with a dot right before the exponent, although they do
not lose any precision:

```js
var a = 0.e5;     // "This number literal will lose precision at runtime."
var b = 1.e5;     // reported
var c = 42.e0;    // reported
var d = 0.e-5;    // reported
```

`var x = 42.` and `var x = 0e5` are correctly not reported. The same false positive happens with an
upper-case `E` and with numeric separators (e.g. `12_3.e3_4` with ecmaVersion 2021). Literals that really
lose precision must still be reported.
