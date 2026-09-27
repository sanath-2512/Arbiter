`to_int(' 42 ')` raises ValueError and `to_int('x')` crashes. Surrounding whitespace should be accepted, and invalid input should return the default (None).
