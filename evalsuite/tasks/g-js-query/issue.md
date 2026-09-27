`parseQuery('?q=hello%20world&tag=a+b')` returns raw text: `hello%20world` and `a+b`. Values must be URL-decoded (`+` is a space).
