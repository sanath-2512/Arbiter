`get({a: {}}, 'a.b.c')` throws a TypeError. Missing intermediate keys should return the default value (undefined when none is given).
